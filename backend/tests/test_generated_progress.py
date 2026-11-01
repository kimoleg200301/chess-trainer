"""Generated puzzle banks remain playable and private across progress backups."""
from copy import deepcopy
from uuid import uuid4

import chess
import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session


def _user(db, prefix):
    from app.models import User
    from app.security import hash_password
    user = User(username=prefix + uuid4().hex[:10], password_hash=hash_password("generated-backup-password"))
    db.add(user)
    db.flush()
    return user


def _puzzle(db, collection, owner_id=None, offset=0):
    from app.collections import generate_puzzle_id
    from app.models import Puzzle
    seed = db.scalar(select(Puzzle).where(Puzzle.collection == "lichess").order_by(Puzzle.id).offset(offset).limit(1))
    values = {field: getattr(seed, field) for field in ("initial_fen", "moves", "rating", "themes", "game_url", "opening_tags")}
    puzzle = Puzzle(id=generate_puzzle_id(seed.initial_fen, seed.moves, collection, owner_id),
                    collection=collection, owner_id=owner_id, rating_estimated=True, **values)
    db.add(puzzle)
    db.flush()
    return puzzle


def _attempt(db, user_id, puzzle):
    from app.models import Attempt
    board = chess.Board(puzzle.initial_fen)
    board.push_uci(puzzle.moves[0])
    attempt = Attempt(user_id=user_id, puzzle_id=puzzle.id, fen=board.fen(),
                      learner_white=board.turn, last_move=puzzle.moves[0], elapsed_ms=12500)
    db.add(attempt)
    return attempt


def test_generated_export_includes_unplayed_personal_and_referenced_public_only(logged):
    from app.db import engine
    from app.models import Review, User, now
    with Session(engine()) as db:
        current = db.scalar(select(User).where(User.username == "tester"))
        other = _user(db, "bank-other-")
        own = _puzzle(db, "personal", current.id)
        unplayed = _puzzle(db, "personal", current.id, offset=1)
        foreign = _puzzle(db, "personal", other.id)
        public = _puzzle(db, "public", offset=2)
        unreferenced = _puzzle(db, "public", offset=3)
        _attempt(db, current.id, own)
        _attempt(db, current.id, public)
        db.add(Review(user_id=current.id, puzzle_id=own.id, due_at=now()))
        db.commit()
        expected = {own.id, unplayed.id, public.id}
        excluded = {foreign.id, unreferenced.id}
    response = logged.get("/api/progress/export")
    assert response.status_code == 200
    data = response.json()
    assert data["format_version"] == 2
    assert {row["id"] for row in data["generated_puzzles"]} == expected
    assert not excluded.intersection(row["id"] for row in data["generated_puzzles"])
    assert all("owner_id" not in row and "user_id" not in row for row in data["generated_puzzles"])
    assert "jobs" not in data and "password_hash" not in response.text


def test_generated_restore_remaps_owner_and_progress_ids_without_altering_source(logged):
    from app.db import engine
    from app.models import Attempt, Puzzle, Review, User, now
    from app.progress import ProgressSnapshot, restore_snapshot
    with Session(engine()) as db:
        current = db.scalar(select(User).where(User.username == "tester"))
        source_id = current.id
        own = _puzzle(db, "personal", current.id)
        unplayed = _puzzle(db, "personal", current.id, offset=1)
        public = _puzzle(db, "public", offset=2)
        original_private_ids = {own.id, unplayed.id}
        public_id = public.id
        _attempt(db, current.id, own)
        _attempt(db, current.id, public)
        db.add(Review(user_id=current.id, puzzle_id=own.id, due_at=now()))
        target = _user(db, "bank-restored-")
        target_id, target_name = target.id, target.username
        db.commit()
    data = logged.get("/api/progress/export").json()
    with Session(engine()) as db, db.begin():
        result = restore_snapshot(db, ProgressSnapshot.model_validate(data), target_name)
        assert result == {"attempts_restored": 2, "reviews_restored": 1, "puzzles_restored": 2}
    with Session(engine()) as db:
        private = list(db.scalars(select(Puzzle).where(Puzzle.owner_id == target_id)))
        assert len(private) == 2 and all(row.collection == "personal" for row in private)
        restored_ids = {row.id for row in private}
        assert not restored_ids.intersection(original_private_ids)
        attempts = list(db.scalars(select(Attempt).where(Attempt.user_id == target_id)))
        assert len(attempts) == 2 and {row.elapsed_ms for row in attempts} == {12500}
        assert {row.puzzle_id for row in attempts} - {public_id} <= restored_ids
        assert db.scalar(select(Review.puzzle_id).where(Review.user_id == target_id)) in restored_ids
        assert {row.id for row in db.scalars(select(Puzzle).where(Puzzle.owner_id == source_id))} == original_private_ids
    with Session(engine()) as db, db.begin(), pytest.raises(ValueError, match="уже есть прогресс"):
        restore_snapshot(db, ProgressSnapshot.model_validate(data), target_name)


def test_unplayed_personal_bank_also_prevents_overwriting_target(logged):
    from app.db import engine
    from app.progress import ProgressSnapshot, restore_snapshot
    data = logged.get("/api/progress/export").json()
    assert data["format_version"] == 1 and "generated_puzzles" not in data
    with Session(engine()) as db:
        target = _user(db, "bank-occupied-")
        _puzzle(db, "personal", target.id)
        name = target.username
        db.commit()
    with Session(engine()) as db, db.begin(), pytest.raises(ValueError, match="личные задачи"):
        restore_snapshot(db, ProgressSnapshot.model_validate(data), name)


def test_missing_private_definition_cannot_attach_foreign_puzzle(logged):
    from app.db import engine
    from app.models import Attempt, User
    from app.progress import ProgressSnapshot, restore_snapshot
    with Session(engine()) as db:
        source = db.scalar(select(User).where(User.username == "tester"))
        _attempt(db, source.id, _puzzle(db, "personal", source.id))
        target = _user(db, "bank-forged-")
        target_id, name = target.id, target.username
        db.commit()
    data = logged.get("/api/progress/export").json()
    data.pop("generated_puzzles")
    for version in (1, 2):
        data["format_version"] = version
        with Session(engine()) as db, db.begin(), pytest.raises(ValueError, match="отсутствуют задачи"):
            restore_snapshot(db, ProgressSnapshot.model_validate(data), name)
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Attempt).where(Attempt.user_id == target_id)) == 0


def test_invalid_definitions_and_attempt_leave_no_partial_bank(logged):
    from app.db import engine
    from app.models import Puzzle, User
    from app.progress import ProgressSnapshot, restore_snapshot
    with Session(engine()) as db:
        source = db.scalar(select(User).where(User.username == "tester"))
        _attempt(db, source.id, _puzzle(db, "personal", source.id))
        target = _user(db, "bank-atomic-")
        target_id, name = target.id, target.username
        db.commit()
    data = logged.get("/api/progress/export").json()
    illegal = deepcopy(data)
    illegal["generated_puzzles"][0]["moves"][1] = "a1a1"
    with pytest.raises(ValidationError):
        ProgressSnapshot.model_validate(illegal)
    duplicate = deepcopy(data)
    duplicate["generated_puzzles"].append(duplicate["generated_puzzles"][0])
    with pytest.raises(ValidationError):
        ProgressSnapshot.model_validate(duplicate)
    bad_attempt = deepcopy(data)
    bad_attempt["attempts"][0]["fen"] = chess.STARTING_FEN
    with Session(engine()) as db, db.begin():
        # Catch inside the transaction deliberately: validation must precede writes.
        with pytest.raises(ValueError, match="Позиция попытки"):
            restore_snapshot(db, ProgressSnapshot.model_validate(bad_attempt), name)
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Puzzle).where(Puzzle.owner_id == target_id)) == 0


def test_public_definition_collision_is_preserved_under_new_id(logged):
    from app.db import engine
    from app.models import Attempt, Puzzle, User
    from app.progress import ProgressSnapshot, restore_snapshot
    with Session(engine()) as db:
        source = db.scalar(select(User).where(User.username == "tester"))
        public = _puzzle(db, "public")
        _attempt(db, source.id, public)
        public_id, original_url = public.id, public.game_url
        target = _user(db, "bank-collision-")
        target_id, name = target.id, target.username
        db.commit()
    data = logged.get("/api/progress/export").json()
    data["generated_puzzles"][0]["game_url"] = "https://lichess.org/another-source"
    with Session(engine()) as db, db.begin():
        restore_snapshot(db, ProgressSnapshot.model_validate(data), name)
    with Session(engine()) as db:
        restored_id = db.scalar(select(Attempt.puzzle_id).where(Attempt.user_id == target_id))
        assert restored_id != public_id
        assert db.get(Puzzle, restored_id).game_url == "https://lichess.org/another-source"
        assert db.get(Puzzle, public_id).game_url == original_url


def test_generated_tasks_restore_when_original_bank_is_gone(logged):
    from app.db import engine
    from app.models import Attempt, Puzzle, User
    from app.progress import ProgressSnapshot, restore_snapshot
    with Session(engine()) as db:
        source = db.scalar(select(User).where(User.username == "tester"))
        own = _puzzle(db, "personal", source.id)
        public = _puzzle(db, "public", offset=1)
        _attempt(db, source.id, own)
        _attempt(db, source.id, public)
        source_id, original_ids = source.id, {own.id, public.id}
        target = _user(db, "bank-migrated-")
        target_id, name = target.id, target.username
        db.commit()
    snapshot = ProgressSnapshot.model_validate(logged.get("/api/progress/export").json())
    with Session(engine()) as db, db.begin():
        db.execute(delete(Attempt).where(Attempt.user_id == source_id))
        db.execute(delete(Puzzle).where(Puzzle.id.in_(original_ids)))
    with Session(engine()) as db, db.begin():
        result = restore_snapshot(db, snapshot, name)
        assert result["puzzles_restored"] == 2
    with Session(engine()) as db:
        restored = list(db.scalars(select(Puzzle).join(Attempt).where(Attempt.user_id == target_id)))
        assert {puzzle.collection for puzzle in restored} == {"public", "personal"}
        for puzzle in restored:
            source = next(record for record in snapshot.generated_puzzles if record.collection == puzzle.collection)
            for field in ("initial_fen", "moves", "rating", "themes", "game_url", "opening_tags", "rating_estimated"):
                assert getattr(puzzle, field) == getattr(source, field)
