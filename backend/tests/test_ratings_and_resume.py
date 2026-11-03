"""Real transactions: Elo, adaptive selection, cross-device resume and idempotent grading."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import chess
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def personal(*, rating=1200, long=False, days=0):
    from app.db import engine
    from app.models import Puzzle, User, now
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == 'tester'))
        query = select(Puzzle).where(Puzzle.collection == 'lichess')
        if long:
            query = query.where(func.cardinality(Puzzle.moves) >= 4)
        seed = db.scalar(query.limit(1))
        pid = uuid4().hex
        puzzle = Puzzle(id=pid, initial_fen=seed.initial_fen, moves=list(seed.moves), rating=rating,
                        themes=['fork'], collection='personal', owner_id=user.id, rating_estimated=True,
                        game_url='https://www.chess.com/game/live/'+pid, source_ply=12,
                        source_game_at=now()-timedelta(days=days))
        db.add(puzzle); db.commit()
        return pid, list(puzzle.moves)


def start(logged, **kwargs):
    response = logged.post('/api/training/start', json={'collection':'personal','adaptive':True, **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def solve(logged, attempt):
    from app.db import engine
    from app.models import Puzzle
    with Session(engine()) as db:
        moves = list(db.get(Puzzle, attempt['puzzle_id']).moves)
    for move in moves[1+attempt['solved_plies']::2]:
        response = logged.post(f"/api/training/{attempt['id']}/move", json={'move':move,'elapsed_ms':4321})
        assert response.status_code == 200 and response.json()['accepted'], response.text
    return response.json()['attempt']


def test_elo_updates_both_ratings_once_and_replays_do_not_farm_points(logged):
    from app.db import engine
    from app.models import Puzzle
    pid, _ = personal()
    result = solve(logged, start(logged))
    assert result['rated'] and result['rating_change'] == {
        'before':1200,'after':1216,'delta':16,'puzzle_before':1200,'puzzle_after':1192,'clean':True}
    with Session(engine()) as db:
        puzzle = db.get(Puzzle,pid)
        assert puzzle.rating == 1192 and puzzle.rated_attempts == 1
    for _ in range(2):
        reveal = logged.post(f"/api/training/{result['id']}/reveal",json={}).json()
        assert reveal['attempt']['rating_change'] == result['rating_change']
    replay = start(logged,puzzle_id=pid)
    assert not replay['rated']
    assert solve(logged,replay)['rating_change'] is None
    stats = logged.get('/api/stats').json()
    assert stats['puzzle_rating'] == 1216 and stats['rated_attempts'] == 1


@pytest.mark.parametrize('help_used',['hint','wrong','reveal'])
def test_assisted_solution_lowers_rating_once(logged, help_used):
    pid,moves=personal()
    attempt=start(logged)
    if help_used=='wrong':
        board=chess.Board(attempt['fen'])
        wrong=next(move.uci() for move in board.legal_moves if move.uci()!=moves[1]
                   and not (lambda probe: (probe.push(move),probe.is_checkmate())[1])(board.copy()))
        response=logged.post(f"/api/training/{attempt['id']}/move",json={'move':wrong})
        assert response.status_code==200 and not response.json()['accepted']
    elif help_used=='hint':
        assert logged.post(f"/api/training/{attempt['id']}/hint",json={}).status_code==200
    result=(logged.post(f"/api/training/{attempt['id']}/reveal",json={}).json()['attempt']
            if help_used=='reveal' else solve(logged,attempt))
    assert result['rating_change']=={'before':1200,'after':1184,'delta':-16,'puzzle_before':1200,'puzzle_after':1208,'clean':False}
    assert logged.get('/api/stats').json()['rated_attempts']==1


def test_adaptive_selects_nearest_unseen_task_without_hidden_range_or_empty_band(logged):
    near,_=personal(rating=1150,days=10)
    far,_=personal(rating=2800)
    first=start(logged,min_rating=3500,max_rating=100)
    assert first['puzzle_id']==near
    solve(logged,first)
    assert start(logged)['puzzle_id']==far  # A small personal bank must never be filtered to nothing.


def test_active_position_is_resumed_before_a_new_task_and_contains_no_solution(logged):
    pid,moves=personal(long=True)
    first=start(logged)
    moved=logged.post(f"/api/training/{first['id']}/move",json={'move':moves[1],'elapsed_ms':4321}).json()['attempt']
    assert moved['status']=='active'
    personal(rating=1200)
    restored=logged.get('/api/training/current').json()
    assert restored['attempt']['id']==first['id'] and restored['attempt']['fen']==moved['fen']
    assert restored['attempt']['elapsed_ms']==4321 and restored['attempt']['starting_fen']==first['fen']
    assert restored['solution']==[] and restored['game_url']==''
    resumed=start(logged)
    assert resumed['resumed'] and resumed['id']==first['id']
    result=solve(logged,resumed)
    restored=logged.get('/api/training/current').json()
    assert restored['attempt']['status']=='completed' and restored['attempt']['fen']==result['fen']
    assert restored['attempt']['rating_change']==result['rating_change'] and restored['solution']
    assert logged.post(f"/api/training/{first['id']}/resume",json={}).json()['attempt']['id']==first['id']


def test_current_and_resume_are_private_and_old_timer_cannot_steal_current_task(logged,client):
    from app.db import engine
    from app.models import Attempt,User
    personal()
    old=start(logged)
    newer=start(logged,puzzle_id=old['puzzle_id'])
    logged.post(f"/api/training/{old['id']}/time",json={'elapsed_ms':99999})
    assert logged.get('/api/training/current').json()['attempt']['id']==newer['id']
    with Session(engine()) as db:
        other=User(username='resume-other-'+uuid4().hex[:8],password_hash='unused');db.add(other);db.flush()
        foreign=Attempt(user_id=other.id,puzzle_id=old['puzzle_id'],fen=old['fen'],learner_white=True)
        db.add(foreign);db.commit();foreign_id=foreign.id
    assert logged.post(f'/api/training/{foreign_id}/resume',json={}).status_code==404
    assert logged.get('/api/training/current').json()['attempt']['id']==newer['id']
    logged.post('/api/auth/logout',json={})
    assert client.get('/api/training/current').status_code==401


def test_concurrent_finish_of_duplicate_attempts_grades_only_once(logged):
    from app.db import engine
    from app.models import Attempt
    personal()
    first=start(logged)
    with Session(engine()) as db:
        original=db.get(Attempt,first['id'])
        duplicate=Attempt(user_id=original.user_id,puzzle_id=original.puzzle_id,fen=original.fen,
                          learner_white=original.learner_white,rated=True)
        db.add(duplicate);db.commit();ids=[original.id,duplicate.id]
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies=list(pool.map(lambda aid:logged.post(f'/api/training/{aid}/reveal',json={}),ids))
    assert all(reply.status_code==200 for reply in replies)
    assert sum(reply.json()['attempt']['rating_change'] is not None for reply in replies)==1
    stats=logged.get('/api/stats').json()
    assert stats['puzzle_rating']==1184 and stats['rated_attempts']==1


def test_manual_and_scheduled_review_are_unrated(logged):
    from app.db import engine
    from app.models import Review,User,now
    pid,_=personal()
    manual=start(logged,adaptive=False)
    assert not manual['rated']
    solve(logged,manual)
    with Session(engine()) as db:
        user=db.scalar(select(User).where(User.username=='tester'))
        review=db.scalar(select(Review).where(Review.user_id==user.id,Review.puzzle_id==pid))
        review.due_at=now()-timedelta(days=1);db.commit()
    repeated=start(logged,mode='review')
    assert not repeated['rated']
    solve(logged,repeated)
    assert logged.get('/api/stats').json()['puzzle_rating']==1200


def test_rated_snapshot_roundtrip_keeps_current_task_and_rating(logged):
    from app.db import engine
    from app.models import Attempt,User
    from app.progress import ProgressSnapshot,restore_snapshot
    personal();result=solve(logged,start(logged))
    snapshot=ProgressSnapshot.model_validate(logged.get('/api/progress/export').json())
    assert snapshot.format_version==3 and snapshot.puzzle_rating==1216 and snapshot.rated_attempts==1
    with Session(engine()) as db:
        target=User(username='rating-restored-'+uuid4().hex[:8],password_hash='unused');db.add(target);db.commit()
        name,uid=target.username,target.id
    with Session(engine()) as db,db.begin():
        restore_snapshot(db,snapshot,name)
    with Session(engine()) as db:
        target=db.get(User,uid);attempt=db.scalar(select(Attempt).where(Attempt.user_id==uid))
        assert target.puzzle_rating==1216 and target.rated_attempts==1
        assert attempt.last_opened_at and attempt.rating_change==result['rating_change']
        assert attempt.training_options['adaptive'] is True


def test_rating_migration_keeps_legacy_progress_without_regrading(logged):
    from alembic import command
    from alembic.config import Config
    from app.db import engine
    from sqlalchemy import text
    personal();first=start(logged,adaptive=False)
    logged.post(f"/api/training/{first['id']}/time",json={'elapsed_ms':5432})
    config=Config('alembic.ini')
    command.downgrade(config,'0006_background_analysis')
    try:
        with engine().connect() as db:
            before=db.execute(text('SELECT id,user_id,puzzle_id,fen,status,elapsed_ms FROM attempts')).all()
        command.upgrade(config,'head')
        with engine().connect() as db:
            after=db.execute(text('SELECT id,user_id,puzzle_id,fen,status,elapsed_ms FROM attempts')).all()
            assert before==after
            assert db.execute(text('SELECT rated,rating_change,last_opened_at FROM attempts')).one()==(False,None,None)
            assert db.execute(text("SELECT puzzle_rating,rated_attempts FROM users WHERE username='tester'")).one()==(1200,0)
        restored=logged.get('/api/training/current').json()
        assert restored['attempt']['id']==first['id'] and restored['attempt']['elapsed_ms']==5432
    finally:
        command.upgrade(config,'head')


def test_concurrent_distinct_results_use_latest_account_rating(logged):
    personal();personal()
    first=start(logged)
    second=start(logged,exclude_attempt_id=first['id'])
    assert second['puzzle_id']!=first['puzzle_id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies=list(pool.map(lambda aid:logged.post(f'/api/training/{aid}/reveal',json={}),[first['id'],second['id']]))
    assert all(reply.status_code==200 for reply in replies)
    assert {reply.json()['attempt']['rating_change']['before'] for reply in replies}=={1200,1184}
    stats=logged.get('/api/stats').json()
    assert stats['puzzle_rating']==1169 and stats['rated_attempts']==2
