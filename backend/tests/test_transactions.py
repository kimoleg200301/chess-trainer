"""Account locks preserve fresh ORM state and fail without poisoning retries."""

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session


@pytest.mark.parametrize("nowait", [False, True])
def test_account_lock_refreshes_previously_loaded_user(nowait):
    from app.db import engine
    from app.models import User
    from app.transactions import lock_user

    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == "tester"))
        with engine().begin() as other:
            other.execute(update(User).where(User.id == user.id)
                          .values(puzzle_rating=1300, rated_attempts=3))
        locked = lock_user(db, user.id, nowait=nowait)
        assert locked is user
        assert locked.puzzle_rating == 1300 and locked.rated_attempts == 3


def test_busy_account_returns_conflict_and_session_can_retry():
    from app.db import engine
    from app.models import User
    from app.transactions import lock_user

    with Session(engine()) as owner, Session(engine()) as contender:
        user = owner.scalar(select(User).where(User.username == "tester"))
        lock_user(owner, user.id)
        with pytest.raises(HTTPException) as error:
            lock_user(contender, user.id, nowait=True)
        assert error.value.status_code == 409
        # NOWAIT errors abort a PostgreSQL transaction; the helper must roll it
        # back so callers can retry with the same Session after the owner exits.
        assert not contender.in_transaction()
        owner.rollback()
        assert lock_user(contender, user.id, nowait=True).id == user.id
