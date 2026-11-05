"""Account-level serialization shared by training, imports and progress snapshots."""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from .models import User


def lock_user(db: Session, user_id: int, *, nowait: bool = False) -> User:
    """Lock and refresh the account before reading or changing its progress.

    Training takes locks in User → Attempt → Puzzle order. Engine jobs first
    acquire their advisory lock, then use ``nowait=True`` here so a busy account
    can be retried without queuing an expensive search. Callers own the commit.
    """
    try:
        user = db.scalar(select(User).where(User.id == user_id)
                         .with_for_update(nowait=nowait)
                         .execution_options(populate_existing=True))
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        # PostgreSQL aborts this transaction after a failed NOWAIT lock.
        db.rollback()
        raise HTTPException(409, "Другая операция ещё выполняется. Повторите через несколько секунд.") from None
    if user is None:
        raise HTTPException(404, "Аккаунт не найден.")
    return user
