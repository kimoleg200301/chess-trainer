"""Personal practice preserves chronology, resumes unfinished work and never cycles finished tasks."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

UTC = timezone.utc


def _date(day, hour=12):
    return datetime(2025, 3, day, hour, tzinfo=UTC)


def _add_puzzle(db, user, label, *, played_at, ply=10, game=None,
                created_at=None, rating=1400, themes=None, long_line=False):
    from app.models import Puzzle
    sources = select(Puzzle).where(Puzzle.collection == "lichess")
    if long_line:
        sources = sources.where(func.cardinality(Puzzle.moves) >= 4)
    source = db.scalar(sources.limit(1))
    puzzle = Puzzle(id="order-" + label, initial_fen=source.initial_fen,
                    moves=list(source.moves), rating=rating, themes=themes or ["fork"],
                    game_url="https://www.chess.com/game/live/" + (game or label),
                    collection="personal", owner_id=user.id, rating_estimated=True,
                    source_game_at=played_at, source_ply=ply,
                    created_at=created_at or _date(25))
    db.add(puzzle)
    return puzzle


def _start(logged, **overrides):
    response = logged.post("/api/training/start", json={
        "collection": "personal", "min_rating": 0, "max_rating": 4000, **overrides,
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_unseen_tasks_follow_game_time_then_ply_and_stop_when_finished(logged):
    from app.db import engine
    from app.models import User
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == "tester"))
        # Saving/reanalysing an older game later must not promote it above a
        # newer game. Moments from one game retain their original move order.
        _add_puzzle(db, user, "new-late", played_at=_date(20, 22), ply=40,
                    game="new", created_at=_date(21))
        _add_puzzle(db, user, "old", played_at=_date(10), created_at=_date(29))
        _add_puzzle(db, user, "new-early", played_at=_date(20, 22), ply=12,
                    game="new", created_at=_date(20, 23))
        _add_puzzle(db, user, "same-day-older", played_at=_date(20, 10))
        _add_puzzle(db, user, "undated-old", played_at=None, created_at=_date(22))
        _add_puzzle(db, user, "undated-new", played_at=None, created_at=_date(28))
        db.commit()
    expected = ["new-early", "new-late", "same-day-older", "old", "undated-new", "undated-old"]
    attempts = [_start(logged) for _ in expected]
    assert [a["puzzle_id"] for a in attempts] == ["order-" + item for item in expected]
    for attempt in attempts:
        assert logged.post(f"/api/training/{attempt['id']}/reveal", json={}).status_code == 200
    exhausted = logged.post("/api/training/start", json={"collection": "personal", "min_rating": 0, "max_rating": 4000})
    assert exhausted.status_code == 404
    assert exhausted.json()["code"] == "personal_collection_complete"
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == "tester"))
        _add_puzzle(db, user, "newly-found-old", played_at=_date(1))
        db.commit()
    assert _start(logged)["puzzle_id"] == "order-newly-found-old"


def test_personal_order_keeps_rating_theme_ownership_and_direct_launch_rules(logged):
    from app.db import engine
    from app.models import User
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == "tester"))
        other = User(username="order-other-" + uuid4().hex[:12], password_hash="unused")
        db.add(other)
        db.flush()
        _add_puzzle(db, other, "foreign", played_at=_date(30))
        _add_puzzle(db, user, "high-rating", played_at=_date(29), rating=2600)
        _add_puzzle(db, user, "other-theme", played_at=_date(28), themes=["pin"])
        _add_puzzle(db, user, "matching", played_at=_date(10))
        db.commit()
    assert _start(logged, theme="fork", min_rating=800, max_rating=1800)["puzzle_id"] == "order-matching"
    denied = logged.post("/api/training/start", json={"puzzle_id": "order-foreign"})
    assert denied.status_code == 404
    direct = _start(logged, puzzle_id="order-other-theme", theme="unknown", min_rating=3000, max_rating=4000)
    assert direct["puzzle_id"] == "order-other-theme"
    # The default source remains Lichess; personal ordering is source-specific.
    assert logged.post("/api/training/start", json={}).json()["collection"] == "lichess"


def test_review_due_order_still_precedes_game_chronology(logged):
    from app.db import engine
    from app.models import Review, User, now
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == "tester"))
        _add_puzzle(db, user, "new-due", played_at=_date(29))
        _add_puzzle(db, user, "old-overdue", played_at=_date(2))
        _add_puzzle(db, user, "future-review", played_at=_date(30))
        db.flush()
        for label, days in [("new-due", -1), ("old-overdue", -3), ("future-review", 1)]:
            db.add(Review(user_id=user.id, puzzle_id="order-" + label, due_at=now() + timedelta(days=days)))
        db.commit()
    assert _start(logged, mode="review")["puzzle_id"] == "order-old-overdue"


def test_equal_game_timestamps_have_stable_order(logged):
    from app.db import engine
    from app.models import User
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == "tester"))
        for label, game, ply in [("b-early", "b", 2), ("a-late", "a", 30), ("a-early", "a", 10)]:
            _add_puzzle(db, user, label, played_at=_date(15), game=game, ply=ply)
        db.commit()
    assert [_start(logged)["puzzle_id"] for _ in range(3)] == ["order-a-early", "order-a-late", "order-b-early"]


def test_partial_personal_attempt_is_resumed_without_duplicate_or_broken_replay(logged):
    import chess
    from app.db import engine
    from app.models import Attempt, User
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == "tester"))
        puzzle = _add_puzzle(db, user, "partial", played_at=_date(20), long_line=True)
        expected_initial = chess.Board(puzzle.initial_fen)
        expected_initial.push_uci(puzzle.moves[0])
        first_move = puzzle.moves[1]
        db.commit()
    first = _start(logged)
    assert first["resumed"] is False
    assert first["starting_fen"] == expected_initial.fen() == first["fen"]
    moved = logged.post(f"/api/training/{first['id']}/move", json={"move": first_move, "elapsed_ms": 4321})
    assert moved.status_code == 200 and moved.json()["accepted"]
    current = moved.json()["attempt"]
    assert current["status"] == "active"
    resumed = _start(logged)
    assert resumed["id"] == first["id"] and resumed["resumed"] is True
    assert resumed["fen"] == current["fen"] != resumed["starting_fen"]
    assert resumed["starting_fen"] == expected_initial.fen()
    assert resumed["elapsed_ms"] == 4321 and resumed["solved_plies"] == current["solved_plies"]
    no_next = logged.post("/api/training/start", json={"collection": "personal", "exclude_attempt_id": first["id"]})
    assert no_next.status_code == 404 and no_next.json()["code"] == "personal_current_remaining"
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Attempt).where(Attempt.puzzle_id == "order-partial")) == 1
    # Source FEN still anchors the complete reveal line after resuming midway.
    revealed = logged.post(f"/api/training/{resumed['id']}/reveal", json={}).json()
    board = chess.Board(resumed["starting_fen"])
    for entry in revealed["solution"]:
        board.push_uci(entry["uci"])
    assert revealed["attempt"]["status"] == "revealed"


def test_completed_and_revealed_personal_tasks_do_not_repeat_even_in_all(logged):
    from app.db import engine
    from app.models import Puzzle, User
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == "tester"))
        # Isolate this pair even when searching all collections.
        assert not db.scalar(select(Puzzle.id).where(Puzzle.rating == 3999).limit(1))
        puzzle = _add_puzzle(db, user, "finished", played_at=_date(20), rating=3999)
        moves = list(puzzle.moves)
        _add_puzzle(db, user, "revealed", played_at=_date(10), rating=3999)
        db.commit()
    solved = _start(logged)
    for uci in moves[1::2]:
        moved = logged.post(f"/api/training/{solved['id']}/move", json={"move": uci})
        assert moved.status_code == 200 and moved.json()["accepted"]
    assert moved.json()["attempt"]["status"] == "completed"
    other = _start(logged)
    assert other["puzzle_id"] == "order-revealed"
    assert logged.post(f"/api/training/{other['id']}/reveal", json={}).status_code == 200
    for collection in ["personal", "all"]:
        response = logged.post("/api/training/start", json={"collection": collection, "min_rating": 3999, "max_rating": 3999})
        assert response.status_code == 404 and response.json()["code"] == "personal_collection_complete"
    # Explicit links are deliberate replay; review remains governed by due_at.
    direct = _start(logged, puzzle_id=solved["puzzle_id"])
    assert direct["id"] != solved["id"] and not direct["resumed"]
    assert logged.post("/api/training/start", json={"collection": "personal", "min_rating": 3999, "max_rating": 3999}).json()["code"] == "personal_collection_complete"
    empty_filters = logged.post("/api/training/start", json={"collection": "personal", "min_rating": 3900, "max_rating": 3900})
    assert empty_filters.status_code == 404 and "code" not in empty_filters.json()


def test_foreign_attempt_id_cannot_exclude_owned_task_and_invalid_ids_are_rejected(logged):
    import chess
    from app.db import engine
    from app.models import Attempt, User
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == "tester"))
        other = User(username="exclude-other-" + uuid4().hex[:12], password_hash="unused")
        db.add(other)
        db.flush()
        puzzle = _add_puzzle(db, user, "owned-current", played_at=_date(20))
        board = chess.Board(puzzle.initial_fen)
        board.push_uci(puzzle.moves[0])
        # Even a legacy foreign attempt pointing to this puzzle cannot control
        # the owner's selection. Only user-scoped attempts affect exclusion.
        foreign = Attempt(user_id=other.id, puzzle=puzzle, fen=board.fen(), learner_white=board.turn)
        db.add(foreign)
        db.commit()
        foreign_id = foreign.id
    assert _start(logged, exclude_attempt_id=foreign_id)["puzzle_id"] == "order-owned-current"
    for invalid in [True, 0, -1, "1", 2**31]:
        response = logged.post("/api/training/start", json={"collection": "personal", "exclude_attempt_id": invalid})
        assert response.status_code == 422


def test_reanalysed_solution_id_does_not_repeat_finished_source_moment(logged):
    import chess
    from app.db import engine
    from app.models import Attempt, User, now
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == "tester"))
        other = User(username="moment-other-" + uuid4().hex[:12], password_hash="unused")
        db.add(other)
        db.flush()
        original = _add_puzzle(db, user, "original-line", played_at=_date(20), game="same-game", ply=12)
        foreign = _add_puzzle(db, other, "foreign-line", played_at=_date(30), game="other-game", ply=22)
        for owner, puzzle in [(user, original), (other, foreign)]:
            board = chess.Board(puzzle.initial_fen)
            board.push_uci(puzzle.moves[0])
            db.add(Attempt(user_id=owner.id, puzzle=puzzle, fen=board.fen(), learner_white=board.turn,
                           status="revealed", completed_at=now()))
        db.commit()
        # A different calculated line has a new puzzle ID; its source moment is
        # nevertheless already finished. Another ply remains a genuinely new task.
        _add_puzzle(db, user, "new-solution-id", played_at=_date(20), game="same-game", ply=12)
        _add_puzzle(db, user, "new-moment", played_at=_date(20), game="same-game", ply=14)
        # A different account solving this source moment cannot hide it for us.
        _add_puzzle(db, user, "owned-foreign-moment", played_at=_date(30), game="other-game", ply=22)
        db.commit()
    first = _start(logged)
    assert first["puzzle_id"] == "order-owned-foreign-moment"
    assert logged.post(f"/api/training/{first['id']}/reveal", json={}).status_code == 200
    second = _start(logged)
    assert second["puzzle_id"] == "order-new-moment"
    assert logged.post(f"/api/training/{second['id']}/reveal", json={}).status_code == 200
    exhausted = logged.post("/api/training/start", json={"collection": "personal", "min_rating": 0, "max_rating": 4000})
    assert exhausted.status_code == 404 and exhausted.json()["code"] == "personal_collection_complete"
    direct = _start(logged, puzzle_id="order-new-solution-id")
    assert direct["puzzle_id"] == "order-new-solution-id" and not direct["resumed"]


def test_unknown_source_moments_do_not_collapse_together(logged):
    from app.db import engine
    from app.models import User
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == "tester"))
        first = _add_puzzle(db, user, "unknown-one", played_at=_date(20), ply=None)
        second = _add_puzzle(db, user, "unknown-two", played_at=_date(10), ply=None)
        first.game_url = second.game_url = ""
        db.commit()
    first = _start(logged)
    assert logged.post(f"/api/training/{first['id']}/reveal", json={}).status_code == 200
    second = _start(logged)
    assert first["puzzle_id"] != second["puzzle_id"]
