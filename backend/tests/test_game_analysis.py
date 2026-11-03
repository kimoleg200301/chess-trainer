"""Independent whole-game reports: ownership, resumability and no puzzle side effects."""
from copy import deepcopy
from uuid import uuid4

import chess
import chess.engine
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

PGN = '''[Event "Review integration"]
[Site "https://www.chess.com/game/live/987654321"]
[Date "2025.04.05"]
[UTCDate "2025.04.05"]
[UTCTime "12:30:00"]
[White "Reviewer"]
[Black "Opponent"]
[Result "0-1"]

1. f3 e5 2. g4 Qh4# 0-1
'''


def imported_game(*, username="tester", source="chesscom", game=None, fingerprint=None):
    from app.db import engine
    from app.generation import parse_games
    from app.imports_api import fingerprint as game_fingerprint
    from app.models import ImportedGame, ImportJob, User
    from app.security import hash_password
    game = deepcopy(game) if game is not None else parse_games(PGN)[0]
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            user = User(username=username, password_hash=hash_password("another-test-only-password"))
            db.add(user)
            db.flush()
        job = ImportJob(user_id=user.id, source=source, username="reviewer", month="2025-04",
                        identity_key=uuid4().hex, analysis_version=2)
        db.add(job)
        db.flush()
        imported = ImportedGame(job_id=job.id, fingerprint=fingerprint or game_fingerprint(game),
                                data=game, analysis_version=2)
        db.add(imported)
        db.commit()
        return imported.id, job.id, user.id


@pytest.fixture
def fake_engine(monkeypatch):
    from app import game_analysis_engine
    from app.generation import position_at
    calls = []
    failures = set()
    unassessed = set()

    class Engine:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def configure(self, options):
            # The background queue fixture also uses this fake for imports.
            assert options in ({"Threads": 1, "Hash": 32}, {"Threads": 2, "Hash": 256})
            self.options = options

    def analyse(game, ply, engine, **kwargs):
        assert engine.options == {"Threads": 2, "Hash": 256}
        calls.append((ply, kwargs))
        if ply in failures:
            raise chess.engine.EngineTerminatedError("disposable integration failure")
        board = position_at(game, ply)
        uci = game["moves"][ply]
        move = chess.Move.from_uci(uci)
        before = board.fen()
        side = "white" if board.turn else "black"
        san = board.san(move)
        fullmove = board.fullmove_number
        board.push(move)
        assessed = ply not in unassessed
        category = "unassessed" if not assessed else "blunder" if ply == 2 else "best"
        return {"ply": ply, "move_number": fullmove, "side": side, "before_fen": before,
                "after_fen": board.fen(), "played_uci": uci, "played_san": san,
                "best_uci": uci if category == "best" else None,
                "best_san": san if category == "best" else None, "category": category,
                "accuracy": (25 if category == "blunder" else 100) if assessed else None,
                "expected_loss": (0.25 if category == "blunder" else 0) if assessed else None,
                "best_expected": 0.5 if assessed else None, "played_expected": 0.25 if assessed else None,
                "score_before": 0 if assessed else None, "score_after": -300 if category == "blunder" else 0,
                "mate_before": None, "mate_after": None, "best_line_uci": [], "best_line_san": [],
                "depth": 12 if assessed else 3, "assessed": assessed, "reason": "Integration result",
                "brilliant_evidence": None}

    monkeypatch.setattr(chess.engine.SimpleEngine, "popen_uci", lambda *args, **kwargs: Engine())
    monkeypatch.setattr(game_analysis_engine, "analyse_game_move", analyse)
    return calls, failures, unassessed


def start(client, game_id):
    response = client.post("/api/game-analysis", json={"game_id": game_id})
    assert response.status_code == 200, response.text
    return response.json()


def step(client, analysis_id):
    response = client.post(f"/api/game-analysis/{analysis_id}/step", json={})
    assert response.status_code == 200, response.text
    return response.json()


def counts():
    from app.db import engine
    from app.models import Attempt, Improvement, Puzzle, Review
    with Session(engine()) as db:
        return [db.scalar(select(func.count()).select_from(model)) for model in (Attempt, Improvement, Puzzle, Review)]


def test_analysis_authentication_validation_and_source_scope(client, logged):
    game_id, _, _ = imported_game()
    client.post("/api/auth/logout", json={})
    assert client.get("/api/game-analysis/games").status_code == 401
    assert client.post("/api/game-analysis", json={"game_id": game_id}).status_code == 401
    client.post("/api/auth/login", json={"username": "tester", "password": "integration-test-only-password"})
    for invalid in (True, -1, 0, "1"):
        assert client.post("/api/game-analysis", json={"game_id": invalid}).status_code == 422
    assert client.post("/api/game-analysis", json={"game_id": game_id},
                       headers={"Origin": "https://evil.invalid"}).status_code == 403
    public_id, _, _ = imported_game(source="public")
    assert client.post("/api/game-analysis", json={"game_id": public_id}).status_code == 404
    assert [g["id"] for g in client.get("/api/game-analysis/games").json()["games"]] == [game_id]


def test_analysis_checks_both_sides_and_persists_each_step_without_training_mutations(logged, fake_engine):
    from app.db import engine
    from app.models import ImportedGame, ImportJob
    game_id, job_id, _ = imported_game()
    before = counts()
    analysis = start(logged, game_id)
    assert analysis["cursor"] == 0 and analysis["status"] == "ready" and analysis["moves"] == []
    assert analysis["summary"]["white"]["accuracy"] is None
    for ply in range(4):
        analysis = step(logged, analysis["id"])
        assert analysis["cursor"] == ply + 1
        assert [m["ply"] for m in analysis["moves"]] == list(range(ply + 1))
        assert logged.get(f"/api/game-analysis/{analysis['id']}").json() == analysis
        if ply < 3:
            assert analysis["summary"]["white"]["accuracy"] is None
            assert analysis["summary"]["black"]["accuracy"] is None
    assert analysis["status"] == "completed" and analysis["summary"]["complete"]
    assert [m["side"] for m in analysis["moves"]] == ["white", "black", "white", "black"]
    assert analysis["summary"]["white"]["counts"]["blunder"] == 1
    assert analysis["summary"]["black"]["counts"]["best"] == 2
    assert analysis["summary"]["white"]["accuracy"] < analysis["summary"]["black"]["accuracy"]
    assert analysis["summary"]["white"]["coverage"] == 100
    assert len(fake_engine[0]) == 4
    assert all(kwargs == {} for _, kwargs in fake_engine[0])  # Production uses the default profile.
    assert step(logged, analysis["id"]) == analysis and len(fake_engine[0]) == 4
    assert start(logged, game_id) == analysis
    assert counts() == before
    with Session(engine()) as db:
        assert db.get(ImportedGame, game_id).cursor == 0
        assert db.get(ImportedGame, game_id).status == "pending"
        assert db.get(ImportJob, job_id).status == "ready"


def test_analysis_deduplicates_overlapping_imports_but_not_other_users(logged, fake_engine):
    from app.db import engine
    from app.models import GameAnalysis
    first, _, _ = imported_game()
    analysis = step(logged, start(logged, first)["id"])
    second, _, _ = imported_game()
    third, _, other_id = imported_game(username="review-other-" + uuid4().hex)
    listed = logged.get("/api/game-analysis/games").json()["games"]
    assert len(listed) == 1 and listed[0]["id"] == second
    assert listed[0]["analysis_id"] == analysis["id"]
    assert start(logged, second)["id"] == analysis["id"]
    assert start(logged, second)["cursor"] == 1
    assert logged.post("/api/game-analysis", json={"game_id": third}).status_code == 404
    with Session(engine()) as db:
        own = db.get(GameAnalysis, analysis["id"])
        foreign = GameAnalysis(user_id=other_id, game_id=third, fingerprint=own.fingerprint,
                               analysis_version=1, cursor=0, moves=[])
        db.add(foreign)
        db.commit()
        foreign_id = foreign.id
    for path, method in [(f"/{foreign_id}", "get"), (f"/{foreign_id}/step", "post"),
                         (f"/{foreign_id}/restart", "post")]:
        response = getattr(logged, method)("/api/game-analysis" + path)
        assert response.status_code == 404


def test_engine_failure_keeps_checkpoint_and_allows_resume(logged, fake_engine):
    game_id, _, _ = imported_game()
    analysis = step(logged, start(logged, game_id)["id"])
    fake_engine[1].add(1)
    response = logged.post(f"/api/game-analysis/{analysis['id']}/step", json={})
    assert response.status_code == 503
    saved = logged.get(f"/api/game-analysis/{analysis['id']}").json()
    assert saved["status"] == "failed" and saved["cursor"] == 1 and saved["moves"] == analysis["moves"]
    fake_engine[1].clear()
    resumed = step(logged, analysis["id"])
    assert resumed["cursor"] == 2 and resumed["error"] is None
    assert [ply for ply, _ in fake_engine[0]] == [0, 1, 1]


def test_restart_only_clears_report_and_upgrades_old_version(logged, fake_engine):
    from app.db import engine
    from app import game_analysis_api
    from app.models import GameAnalysis
    game_id, _, _ = imported_game()
    analysis = step(logged, start(logged, game_id)["id"])
    before = counts()
    with Session(engine()) as db:
        db.get(GameAnalysis, analysis["id"]).analysis_version = 1
        db.commit()
    stale = logged.get(f"/api/game-analysis/{analysis['id']}").json()
    assert stale["analysis_outdated"]
    assert logged.post(f"/api/game-analysis/{analysis['id']}/step", json={}).status_code == 409
    result = logged.post(f"/api/game-analysis/{analysis['id']}/restart", json={})
    assert result.status_code == 200
    reset = result.json()
    assert reset["status"] == "ready" and reset["cursor"] == 0 and reset["moves"] == []
    assert reset["analysis_version"] == game_analysis_api.ANALYSIS_VERSION and not reset["analysis_outdated"]
    assert counts() == before
    assert step(logged, analysis["id"])["cursor"] == 1


def test_unassessed_moves_do_not_produce_a_false_complete_accuracy(logged, fake_engine):
    game_id, _, _ = imported_game()
    fake_engine[2].add(0)
    analysis = start(logged, game_id)
    for _ in range(4):
        analysis = step(logged, analysis["id"])
    assert analysis["status"] == "running" and analysis["retry_pending"] == 1
    analysis = step(logged, analysis["id"])
    assert analysis["status"] == "completed"
    assert analysis["retry_pending"] == 0 and analysis["moves"][0]["retry_attempts"] == 1
    assert step(logged, analysis["id"]) == analysis
    assert len(fake_engine[0]) == 5
    white = analysis["summary"]["white"]
    assert white["accuracy"] is None and not white["accuracy_complete"]
    assert white["provisional_accuracy"] is not None and white["counts"]["unassessed"] == 1
    assert white["coverage"] < 100
    assert analysis["summary"]["black"]["accuracy_complete"]


def test_analysis_nonblocking_engine_user_and_game_contention(logged, fake_engine):
    from app.db import engine
    from app.engine_resources import engine_slots
    from app.game_analysis_api import LOCK_NAMESPACE
    from app.models import User
    game_id, _, user_id = imported_game()
    analysis = start(logged, game_id)
    assert engine_slots.acquire(blocking=False)
    try:
        assert logged.post(f"/api/game-analysis/{analysis['id']}/step", json={}).status_code == 503
    finally:
        engine_slots.release()
    with engine().connect() as connection:
        transaction = connection.begin()
        connection.execute(select(func.pg_advisory_xact_lock(LOCK_NAMESPACE, analysis["id"])))
        try:
            for action in ("step", "restart"):
                assert logged.post(f"/api/game-analysis/{analysis['id']}/{action}", json={}).status_code == 409
        finally:
            transaction.rollback()
    with Session(engine()) as db:
        db.scalar(select(User).where(User.id == user_id).with_for_update())
        for action in ("step", "restart"):
            assert logged.post(f"/api/game-analysis/{analysis['id']}/{action}", json={}).status_code == 409
        assert logged.post("/api/game-analysis", json={"game_id": game_id}).status_code == 409
        db.rollback()
    assert fake_engine[0] == []
    assert step(logged, analysis["id"])["cursor"] == 1


def test_game_list_is_sorted_by_real_date_and_hides_unsafe_links(logged):
    from app.generation import parse_games
    game = parse_games(PGN)[0]
    newer = {**game, "source_game_at": "2025-07-01T12:00:00+00:00", "game_url": "https://www.chess.com/game/live/newer"}
    older = {**game, "source_game_at": "2025-03-01T12:00:00+00:00", "game_url": "javascript:alert(1)"}
    newer_id, _, _ = imported_game(game=newer)
    older_id, _, _ = imported_game(game=older)
    listed = logged.get("/api/game-analysis/games").json()["games"]
    assert [game["id"] for game in listed] == [newer_id, older_id]
    assert listed[1]["game_url"] == ""


def test_black_to_move_custom_start_has_correct_side_totals(logged, fake_engine):
    from app.generation import parse_games
    game = parse_games(PGN)[0]
    board = chess.Board()
    board.push_uci("e2e4")
    game.update(initial_fen=board.fen(), moves=["e7e5", "g1f3", "b8c6"])
    game_id, _, _ = imported_game(game=game)
    analysis = start(logged, game_id)
    assert analysis["summary"]["white"]["total_moves"] == 1
    assert analysis["summary"]["black"]["total_moves"] == 2
    for _ in range(3):
        analysis = step(logged, analysis["id"])
    assert [move["side"] for move in analysis["moves"]] == ["black", "white", "black"]
    assert analysis["summary"]["white"]["accuracy_complete"]
    assert analysis["summary"]["black"]["accuracy_complete"]


def test_game_list_limit_counts_unique_games_not_overlapping_copies(logged, monkeypatch):
    from app import game_analysis_api
    from app.generation import parse_games
    monkeypatch.setattr(game_analysis_api, "MAX_LIST_GAMES", 2)
    game = parse_games(PGN)[0]
    first = {**game, "game_url": "https://www.chess.com/game/live/one"}
    second = {**game, "game_url": "https://www.chess.com/game/live/two"}
    imported_game(game=first)
    second_id, _, _ = imported_game(game=second)
    imported_game(game=first)
    first_latest, _, _ = imported_game(game=first)
    listed = logged.get("/api/game-analysis/games").json()["games"]
    assert {row["id"] for row in listed} == {first_latest, second_id}


def test_game_list_limits_by_game_date_before_import_order(logged, monkeypatch):
    from app import game_analysis_api
    from app.generation import parse_games
    monkeypatch.setattr(game_analysis_api, "MAX_LIST_GAMES", 2)
    game = parse_games(PGN)[0]
    newest_id, _, _ = imported_game(game={**game, "source_game_at": "2025-07-01T12:00:00+00:00"},
                                    fingerprint="newest")
    legacy_id, _, _ = imported_game(game={**game, "date": "2025.06.01"}, fingerprint="legacy")
    imported_game(game={**game, "source_game_at": "2025-03-01T12:00:00+00:00"}, fingerprint="old")
    imported_game(game={**game, "source_game_at": "invalid", "date": "????.??.??"}, fingerprint="undated")
    listed = logged.get("/api/game-analysis/games").json()["games"]
    assert [row["id"] for row in listed] == [newest_id, legacy_id]
