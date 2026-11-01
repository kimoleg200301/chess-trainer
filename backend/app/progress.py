"""Portable progress snapshots. They contain no account or session secrets."""
from typing import Literal

import chess
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from .models import Attempt, Puzzle, Review, User, now

MAX_RECORDS = 10_000
MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024


class SnapshotRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AttemptRecord(SnapshotRecord):
    puzzle_id: str = Field(min_length=1, max_length=32)
    fen: str = Field(min_length=10, max_length=160)
    status: Literal["active", "completed", "revealed"]
    learner_white: bool
    ply_index: int = Field(ge=1, le=500)
    mistakes: int = Field(ge=0, le=1_000_000)
    hints_used: int = Field(ge=0, le=1)
    last_move: str | None = Field(default=None, pattern=r"^[a-h][1-8][a-h][1-8][qrbn]?$")
    created_at: AwareDatetime
    completed_at: AwareDatetime | None
    elapsed_ms: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)

    @model_validator(mode="after")
    def consistent_timestamps(self):
        if (self.status == "active") != (self.completed_at is None):
            raise ValueError("Несогласованные статус и дата завершения.")
        if self.completed_at and self.completed_at < self.created_at:
            raise ValueError("Дата завершения раньше начала.")
        return self


class ReviewRecord(SnapshotRecord):
    puzzle_id: str = Field(min_length=1, max_length=32)
    successes: int = Field(ge=0, le=10_000)
    due_at: AwareDatetime


class ProgressSnapshot(SnapshotRecord):
    format_version: Literal[1] = 1
    exported_at: AwareDatetime
    username: str = Field(min_length=1, max_length=80)
    attempts: list[AttemptRecord] = Field(max_length=MAX_RECORDS)
    reviews: list[ReviewRecord] = Field(max_length=MAX_RECORDS)

    @model_validator(mode="after")
    def unique_reviews(self):
        ids = [item.puzzle_id for item in self.reviews]
        if len(ids) != len(set(ids)):
            raise ValueError("Повторяющаяся запись повторения.")
        return self


ATTEMPT_FIELDS = tuple(AttemptRecord.model_fields)
REVIEW_FIELDS = tuple(ReviewRecord.model_fields)


def export_snapshot(db: Session, user: User) -> ProgressSnapshot:
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    attempts = list(db.scalars(select(Attempt).where(Attempt.user_id == user.id).order_by(Attempt.id)))
    reviews = list(db.scalars(select(Review).where(Review.user_id == user.id).order_by(Review.id)))
    if len(attempts) > MAX_RECORDS or len(reviews) > MAX_RECORDS:
        from fastapi import HTTPException
        raise HTTPException(422, "Более 10 000 записей. Используйте резервную копию PostgreSQL.")
    result = ProgressSnapshot(
        exported_at=now(), username=user.username,
        attempts=[{field: getattr(row, field) for field in ATTEMPT_FIELDS} for row in attempts],
        reviews=[{field: getattr(row, field) for field in REVIEW_FIELDS} for row in reviews],
    )
    if len(result.model_dump_json().encode()) > MAX_SNAPSHOT_BYTES:
        from fastapi import HTTPException
        raise HTTPException(422, "Снимок больше 5 МБ. Используйте резервную копию PostgreSQL.")
    return result


def _validate_attempt(record: AttemptRecord, puzzle: Puzzle):
    board = chess.Board(puzzle.initial_fen)
    board.push_uci(puzzle.moves[0])
    if record.learner_white != board.turn or record.ply_index > len(puzzle.moves):
        raise ValueError("Некорректная сторона или число ходов в попытке.")
    completed = record.status == "completed"
    if completed != (record.ply_index == len(puzzle.moves)):
        raise ValueError("Несогласованные статус и число ходов в попытке.")
    if not completed and record.ply_index % 2 != 1:
        raise ValueError("Позиция должна ожидать хода ученика.")
    for uci in puzzle.moves[1:record.ply_index]:
        board.push_uci(uci)
    if board.fen() == record.fen:
        if record.last_move and record.last_move != puzzle.moves[record.ply_index - 1]:
            raise ValueError("Последний ход не соответствует позиции.")
        return
    # A different legal mating move is an accepted answer and must survive export.
    if completed and record.last_move:
        prefix = chess.Board(puzzle.initial_fen)
        prefix.push_uci(puzzle.moves[0])
        alternate = chess.Move.from_uci(record.last_move)
        for offset in range(1, len(puzzle.moves), 2):
            if alternate in prefix.legal_moves:
                probe = prefix.copy()
                probe.push(alternate)
                if probe.is_checkmate() and probe.fen() == record.fen:
                    return
            for uci in puzzle.moves[offset:offset + 2]:
                prefix.push_uci(uci)
    raise ValueError("Позиция попытки не соответствует исходной задаче.")


def restore_snapshot(db: Session, snapshot: ProgressSnapshot, username: str) -> dict:
    """Restore atomically into an existing account with no progress.

    Caller owns the transaction. No accounts or sessions are imported, and existing
    progress is never replaced. Repeating a nonempty import fails rather than duplicates.
    """
    user = db.scalar(select(User).where(User.username == username).with_for_update())
    if not user:
        raise ValueError("Пользователь назначения не найден. Сначала выполните bootstrap.")
    if db.scalar(select(exists().where(Attempt.user_id == user.id))) or db.scalar(select(exists().where(Review.user_id == user.id))):
        raise ValueError("У пользователя уже есть прогресс. Восстановление отменено без изменений.")
    requested_ids = {record.puzzle_id for record in [*snapshot.attempts, *snapshot.reviews]}
    puzzles = {puzzle.id: puzzle for puzzle in db.scalars(select(Puzzle).where(Puzzle.id.in_(requested_ids)))}
    if requested_ids - puzzles.keys():
        raise ValueError("В базе назначения отсутствуют задачи из снимка. Импортируйте ту же базу задач.")
    for record in snapshot.attempts:
        _validate_attempt(record, puzzles[record.puzzle_id])
    for record in snapshot.attempts:
        db.add(Attempt(user_id=user.id, **record.model_dump()))
    for record in snapshot.reviews:
        db.add(Review(user_id=user.id, **record.model_dump()))
    db.flush()
    return {"attempts_restored": len(snapshot.attempts), "reviews_restored": len(snapshot.reviews)}
