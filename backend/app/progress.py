"""Portable progress snapshots. They contain no account or session secrets."""
from typing import Literal
from urllib.parse import urlparse
from uuid import uuid4

import chess
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_serializer, model_validator
from sqlalchemy import and_, exists, or_, select
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


class GeneratedPuzzleRecord(SnapshotRecord):
    id: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    collection: Literal["public", "personal"]
    initial_fen: str = Field(min_length=10, max_length=160)
    moves: list[str] = Field(min_length=2, max_length=500)
    rating: int = Field(strict=True, ge=0, le=4000)
    themes: list[str] = Field(max_length=32)
    game_url: str = Field(default="", max_length=2000)
    opening_tags: str = Field(default="", max_length=2000)
    rating_estimated: bool = True

    @model_validator(mode="after")
    def legal_definition(self):
        if len(self.moves) % 2 or any(len(theme) > 64 for theme in self.themes):
            raise ValueError("Некорректное определение задачи.")
        if self.game_url:
            parsed = urlparse(self.game_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Некорректная ссылка на исходную партию.")
        try:
            board = chess.Board(self.initial_fen)
            if not board.is_valid():
                raise ValueError("Недопустимая начальная позиция.")
            for uci in self.moves:
                if len(uci) not in {4, 5}:
                    raise ValueError("Некорректный ход.")
                move = chess.Move.from_uci(uci)
                if move not in board.legal_moves:
                    raise ValueError("Недопустимый ход решения.")
                board.push(move)
        except (ValueError, TypeError):
            raise ValueError("Задача содержит некорректную позицию или недопустимые ходы.") from None
        return self


class ProgressSnapshot(SnapshotRecord):
    format_version: Literal[1, 2] = 1
    exported_at: AwareDatetime
    username: str = Field(min_length=1, max_length=80)
    attempts: list[AttemptRecord] = Field(max_length=MAX_RECORDS)
    reviews: list[ReviewRecord] = Field(max_length=MAX_RECORDS)
    generated_puzzles: list[GeneratedPuzzleRecord] = Field(default_factory=list, max_length=MAX_RECORDS)

    @model_validator(mode="after")
    def unique_records(self):
        ids = [item.puzzle_id for item in self.reviews]
        if len(ids) != len(set(ids)):
            raise ValueError("Повторяющаяся запись повторения.")
        puzzle_ids = [item.id for item in self.generated_puzzles]
        if len(puzzle_ids) != len(set(puzzle_ids)):
            raise ValueError("Повторяющееся определение задачи.")
        if self.format_version == 1 and self.generated_puzzles:
            raise ValueError("Определения собственных задач требуют снимок версии 2.")
        return self

    @model_serializer(mode="wrap")
    def serialize_snapshot(self, handler):
        result = handler(self)
        # A plain Lichess backup keeps the original v1 wire format exactly.
        if self.format_version == 1:
            result.pop("generated_puzzles", None)
        return result


ATTEMPT_FIELDS = tuple(AttemptRecord.model_fields)
REVIEW_FIELDS = tuple(ReviewRecord.model_fields)


def export_snapshot(db: Session, user: User) -> ProgressSnapshot:
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    attempts = list(db.scalars(select(Attempt).where(Attempt.user_id == user.id).order_by(Attempt.id).limit(MAX_RECORDS + 1)))
    reviews = list(db.scalars(select(Review).where(Review.user_id == user.id).order_by(Review.id).limit(MAX_RECORDS + 1)))
    referenced_ids = {row.puzzle_id for row in [*attempts, *reviews]}
    # Export generated definitions needed for playback, plus unplayed personal
    # tasks. Import-job histories and other users' personal collections stay out.
    puzzles = list(db.scalars(select(Puzzle).where(or_(
        and_(Puzzle.collection == "personal", Puzzle.owner_id == user.id),
        and_(Puzzle.collection == "public", Puzzle.id.in_(referenced_ids)),
    )).order_by(Puzzle.id).limit(MAX_RECORDS + 1)))
    if any(len(records) > MAX_RECORDS for records in (attempts, reviews, puzzles)):
        from fastapi import HTTPException
        raise HTTPException(422, "Более 10 000 записей. Используйте резервную копию PostgreSQL.")
    foreign_reference = db.scalar(select(exists().where(
        Puzzle.id.in_(referenced_ids), Puzzle.collection == "personal", Puzzle.owner_id != user.id)))
    if foreign_reference:
        from fastapi import HTTPException
        raise HTTPException(422, "Прогресс содержит недоступную личную задачу.")
    result = ProgressSnapshot(
        format_version=2 if puzzles else 1,
        exported_at=now(), username=user.username,
        attempts=[{field: getattr(row, field) for field in ATTEMPT_FIELDS} for row in attempts],
        reviews=[{field: getattr(row, field) for field in REVIEW_FIELDS} for row in reviews],
        generated_puzzles=[{field: getattr(row, field) for field in GeneratedPuzzleRecord.model_fields} for row in puzzles],
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


def _same_definition(puzzle: Puzzle, record: GeneratedPuzzleRecord, owner_id: int | None) -> bool:
    return puzzle.owner_id == owner_id and all(
        getattr(puzzle, field) == getattr(record, field)
        for field in GeneratedPuzzleRecord.model_fields if field != "id"
    )


def _unused_generated_id(db: Session, reserved: set[str]) -> str:
    for _ in range(32):
        candidate = "g" + uuid4().hex[:31]
        if candidate not in reserved and db.get(Puzzle, candidate) is None:
            return candidate
    raise ValueError("Не удалось назначить идентификатор задачи. Повторите восстановление.")


def restore_snapshot(db: Session, snapshot: ProgressSnapshot, username: str) -> dict:
    """Restore progress and generated definitions into an empty existing account.

    Caller owns the transaction. Accounts, sessions and import-job histories are
    not imported. Definitions never overwrite an existing bank; personal puzzle
    IDs are re-scoped to the destination user and references follow the new IDs.
    """
    from .collections import generate_puzzle_id

    user = db.scalar(select(User).where(User.username == username).with_for_update())
    if not user:
        raise ValueError("Пользователь назначения не найден. Сначала выполните bootstrap.")
    occupied = (
        db.scalar(select(exists().where(Attempt.user_id == user.id)))
        or db.scalar(select(exists().where(Review.user_id == user.id)))
        or db.scalar(select(exists().where(Puzzle.collection == "personal", Puzzle.owner_id == user.id)))
    )
    if occupied:
        raise ValueError("У пользователя уже есть прогресс или личные задачи. Восстановление отменено без изменений.")
    requested_ids = {record.puzzle_id for record in [*snapshot.attempts, *snapshot.reviews]}
    embedded_ids = {record.id for record in snapshot.generated_puzzles}
    # Without an embedded definition only seeded Lichess tasks can be resolved.
    # A guessed foreign personal ID must never attach another user's content.
    puzzles = {puzzle.id: puzzle for puzzle in db.scalars(select(Puzzle).where(
        Puzzle.id.in_(requested_ids - embedded_ids), Puzzle.collection == "lichess"))}
    if requested_ids - embedded_ids - puzzles.keys():
        raise ValueError("В базе назначения отсутствуют задачи из снимка или их определения. Импортируйте ту же базу задач.")

    mapped_ids = {puzzle_id: puzzle_id for puzzle_id in puzzles}
    reserved = set(mapped_ids.values())
    new_puzzles = []
    for record in snapshot.generated_puzzles:
        owner_id = user.id if record.collection == "personal" else None
        desired_id = generate_puzzle_id(record.initial_fen, record.moves, record.collection, owner_id)
        existing = db.get(Puzzle, desired_id)
        if desired_id in reserved or (existing and not _same_definition(existing, record, owner_id)):
            # Preserve both definitions when a source uses a colliding ID or the
            # same canonical tactic arose in a different recorded game/FEN.
            desired_id = _unused_generated_id(db, reserved)
            existing = None
        reserved.add(desired_id)
        if existing is None:
            values = record.model_dump(exclude={"id"})
            existing = Puzzle(id=desired_id, owner_id=owner_id, **values)
            new_puzzles.append(existing)
        puzzles[record.id] = existing
        mapped_ids[record.id] = desired_id

    for record in snapshot.attempts:
        _validate_attempt(record, puzzles[record.puzzle_id])
    # All validation happens before writes, so even a caller catching ValueError
    # inside a transaction cannot accidentally persist a partial import.
    db.add_all(new_puzzles)
    db.flush()
    for record in snapshot.attempts:
        values = record.model_dump()
        values["puzzle_id"] = mapped_ids[record.puzzle_id]
        db.add(Attempt(user_id=user.id, **values))
    for record in snapshot.reviews:
        values = record.model_dump()
        values["puzzle_id"] = mapped_ids[record.puzzle_id]
        db.add(Review(user_id=user.id, **values))
    db.flush()
    return {"attempts_restored": len(snapshot.attempts), "reviews_restored": len(snapshot.reviews),
            "puzzles_restored": len(new_puzzles)}
