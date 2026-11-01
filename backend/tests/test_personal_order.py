"""Personal practice follows real game chronology without starving old tasks."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

UTC = timezone.utc


def _date(day, hour=12):
    return datetime(2025, 3, day, hour, tzinfo=UTC)


def _add_puzzle(db, user, label, *, played_at, ply=10, game=None,
                created_at=None, rating=1400, themes=None):
    from app.models import Puzzle
    source = db.scalar(select(Puzzle).where(Puzzle.collection == "lichess").limit(1))
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


def test_unseen_tasks_follow_game_time_then_ply_and_cycle_when_exhausted(logged):
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
    assert [_start(logged)["puzzle_id"] for _ in expected] == ["order-" + item for item in expected]
    # Every familiar task gets another turn before a third showing of the first.
    assert [_start(logged)["puzzle_id"] for _ in expected] == ["order-" + item for item in expected]
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
