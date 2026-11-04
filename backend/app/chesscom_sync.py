"""Durable recent-game synchronization, independent of reviews and coach data."""
from datetime import timedelta
import hashlib

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .coach_sources import archive_months
from .game_sources import chesscom_game_url, chesscom_games
from .models import ChessComSync, ImportedGame, ImportJob, User, now

LOCK_NAMESPACE = 782110
MAX_GAMES = 200
MAX_MONTHS = 2
MIN_INTERVAL = timedelta(seconds=30)
AUTO_INTERVAL = timedelta(minutes=5)


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    automatic: bool = False

    @field_validator("username", mode="before")
    @classmethod
    def normalize(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


def sync_view(record: ChessComSync | None) -> dict:
    return {
        "username": record.username if record else None,
        "status": record.status if record else "idle",
        "revision": record.revision if record else 0,
        "background_requested": record.background_requested if record else False,
        "requested_at": record.requested_at if record else None,
        "last_synced_at": record.last_synced_at if record else None,
        "next_sync_at": record.requested_at + MIN_INTERVAL if record and record.requested_at else None,
        "added_games": record.added_games if record else 0,
        "checked_games": record.checked_games if record else 0,
        "error": record.error if record else None,
    }


def get_sync(db: Session, user_id: int) -> dict:
    return sync_view(db.scalar(select(ChessComSync).where(ChessComSync.user_id == user_id)))


def _locked_record(db: Session, user_id: int) -> ChessComSync:
    db.execute(insert(ChessComSync).values(user_id=user_id, updated_at=now())
               .on_conflict_do_nothing(index_elements=[ChessComSync.user_id]))
    return db.scalar(select(ChessComSync).where(ChessComSync.user_id == user_id)
                     .with_for_update().execution_options(populate_existing=True))


def remember_username(db: Session, user_id: int, username: str):
    record = _locked_record(db, user_id)
    if record.username != username:
        record.username, record.revision = username, record.revision + 1
        record.status, record.background_requested, record.error = "idle", False, None
        record.requested_at = record.last_synced_at = None
        record.added_games = record.checked_games = 0
        record.updated_at = now()
    db.commit()


def request_sync(db: Session, user: User, body: SyncRequest) -> dict:
    record = _locked_record(db, user.id)
    same_user = record.username == body.username
    interval = AUTO_INTERVAL if body.automatic else MIN_INTERVAL
    if same_user and (record.background_requested or
                      (record.requested_at and record.requested_at + interval > now())):
        db.commit()
        return sync_view(record)
    if not same_user:
        record.last_synced_at = None
    record.username, record.revision = body.username, record.revision + 1
    record.status, record.background_requested, record.error = "ready", True, None
    record.requested_at = record.updated_at = now()
    record.added_games = record.checked_games = 0
    db.commit()
    return sync_view(record)


def recent_games(username: str) -> list[tuple[str, list[dict]]]:
    """At most three bounded HTTP reads, including the previous active month."""
    batches, remaining = [], MAX_GAMES
    for month in archive_months(username)[:MAX_MONTHS]:
        _, games = chesscom_games(username, month, remaining, allow_empty=True)
        batches.append((month, games))
        remaining -= len(games)
        if remaining <= 0:
            break
    return batches


def _game_url(value: str | None) -> str | None:
    return chesscom_game_url(value)


def _save_games(db: Session, user: User, username: str, batches: list[tuple[str, list[dict]]]) -> int:
    from .imports_api import ANALYSIS_VERSION, fingerprint, refresh_chesscom_accuracies
    references = refresh_chesscom_accuracies(db, user.id, [game for _, games in batches for game in games])
    existing = db.execute(select(ImportedGame.fingerprint, ImportedGame.data["game_url"].astext)
                          .join(ImportJob).where(ImportJob.user_id == user.id,
                                                 ImportJob.source == "chesscom")).all()
    known = {row[0] for row in existing}
    urls = {url for row in existing if (url := _game_url(row[1]))}
    added = 0
    for month, games in batches:
        fresh = []
        for game in games:
            key, url = fingerprint(game), _game_url(game.get("game_url"))
            if key in known or (url and url in urls):
                continue
            known.add(key)
            if url:
                urls.add(url)
            fresh.append((key, {**game, "chesscom_accuracies": references.get(key)}))
        if not fresh:
            continue
        # Append to a dedicated monthly batch; periodic sync must not exhaust
        # the existing 100-batch limit by creating one batch for every new game.
        identity = hashlib.sha256(f"analysis-sync:{ANALYSIS_VERSION}:{username}:{month}".encode()).hexdigest()
        job = db.scalar(select(ImportJob).where(ImportJob.user_id == user.id, ImportJob.identity_key == identity))
        if job is None:
            if db.scalar(select(func.count()).select_from(ImportJob).where(ImportJob.user_id == user.id)) >= 100:
                raise HTTPException(422, "Сохранено 100 подборок. Новую подборку для синхронизации создать не удалось.")
            job = ImportJob(user_id=user.id, source="chesscom", username=username, month=month,
                            identity_key=identity, analysis_version=ANALYSIS_VERSION)
            db.add(job)
            db.flush()
        job.status, job.error, job.updated_at = "ready", None, now()
        for key, game in fresh:
            db.add(ImportedGame(job_id=job.id, fingerprint=key, data=game, analysis_version=ANALYSIS_VERSION))
        added += len(fresh)
    return added


def process_step(record_id: int, db: Session, user: User, *, background: bool = False):
    from .imports_api import lock_user
    record = db.scalar(select(ChessComSync).where(ChessComSync.id == record_id, ChessComSync.user_id == user.id))
    if record is None:
        raise HTTPException(404, "Настройки синхронизации не найдены.")
    if not db.scalar(select(func.pg_try_advisory_xact_lock(LOCK_NAMESPACE, record_id))):
        raise HTTPException(409, "Синхронизация уже выполняется.")
    db.refresh(record)
    if not record.background_requested or not record.username:
        return None
    revision, username = record.revision, record.username
    # Publish progress without keeping a row lock during network requests.
    # The main transaction retains its advisory lock through this checkpoint.
    with Session(db.get_bind()) as status_db:
        status_db.execute(update(ChessComSync).where(ChessComSync.id == record_id,
                          ChessComSync.revision == revision).values(status="running"))
        status_db.commit()
    try:
        batches = recent_games(username)
    except HTTPException as exc:
        db.execute(update(ChessComSync).where(ChessComSync.id == record_id, ChessComSync.revision == revision)
                   .values(status="failed", background_requested=False, error=str(exc.detail), updated_at=now()))
        db.commit()
        return True
    # Follow the existing import/restore lock order. A busy user is retried by
    # the worker with saved intent, without marking an upstream failure.
    lock_user(db, user.id)
    db.refresh(record, with_for_update=True)
    if record.revision != revision or not record.background_requested:
        db.rollback()
        return None
    try:
        record.added_games = _save_games(db, user, username, batches)
        record.checked_games = sum(len(games) for _, games in batches)
        record.status, record.background_requested, record.error = "completed", False, None
        record.last_synced_at = record.updated_at = now()
        db.commit()
    except HTTPException as exc:
        db.rollback()
        db.execute(update(ChessComSync).where(ChessComSync.id == record_id, ChessComSync.revision == revision)
                   .values(status="failed", background_requested=False, error=str(exc.detail), updated_at=now()))
        db.commit()
    return True
