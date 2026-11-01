"""Resumable, request-driven batches for personal and public game analysis."""
import hashlib
import json
import os
from pathlib import Path
import time

import chess
import chess.engine
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .collections import generate_puzzle_id
from .config import settings
from .db import get_db
from .engine_resources import engine_slots
from .game_sources import chesscom_games, safe_game_url
from .models import ImportJob, ImportedGame, Improvement, Puzzle, User, now
from .security import current_user

router = APIRouter(prefix="/api")
ANALYSIS_VERSION = 1


class ChessComImportBody(BaseModel):
    username: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    month: str | None = Field(default=None, pattern=r"^20\d{2}-(0[1-9]|1[0-2])$")
    limit: int = Field(default=10, ge=1, le=50, strict=True)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value):
        return value.lower()

    @field_validator("month")
    @classmethod
    def validate_month(cls, value):
        if value and (int(value[:4]) < 2007 or value > now().strftime("%Y-%m")):
            raise ValueError("Месяц вне доступного диапазона")
        return value


def fingerprint(game: dict) -> str:
    data = [game.get("game_url", ""), game["white"].lower(), game["black"].lower(),
            game.get("date", ""), game["initial_fen"], game["moves"]]
    return hashlib.sha256(json.dumps(data, separators=(",", ":")).encode()).hexdigest()


def owned_job(db: Session, job_id: int, user_id: int) -> ImportJob:
    job = db.scalar(select(ImportJob).where(ImportJob.id == job_id, ImportJob.user_id == user_id))
    if not job:
        raise HTTPException(404, "Подборка партий не найдена.")
    return job


def candidate_view(candidate: Improvement) -> dict:
    fields = ("ply", "move_number", "side", "before_fen", "played_uci", "played_san", "best_uci", "best_san",
              "loss_cp", "score_before", "score_after", "mate_before", "mate_after", "best_line_san", "explanation", "phase")
    result = {field: candidate.data.get(field) for field in fields}
    result.update(id=candidate.id, puzzle_available=candidate.data.get("puzzle") is not None, puzzle_id=candidate.puzzle_id)
    return result


def job_view(db: Session, job: ImportJob, details: bool = True) -> dict:
    games = list(db.scalars(select(ImportedGame).where(ImportedGame.job_id == job.id).order_by(ImportedGame.id)))
    candidates = list(db.scalars(select(Improvement).join(ImportedGame).where(ImportedGame.job_id == job.id).order_by(Improvement.ply)))
    by_game = {}
    for candidate in candidates:
        by_game.setdefault(candidate.game_id, []).append(candidate)
    result = {"id": job.id, "source": job.source, "username": job.username, "month": job.month,
              "status": job.status, "total_games": len(games),
              "completed_games": sum(game.status in {"completed", "skipped"} for game in games),
              "candidates_count": len(candidates), "puzzles_count": sum(candidate.puzzle_id is not None for candidate in candidates),
              "error": job.error, "created_at": job.created_at.isoformat()}
    if details:
        result["games"] = [{"id": game.id, **{field: game.data.get(field, "") for field in
                            ("white", "black", "result", "date", "event", "game_url")},
                            "status": game.status, "cursor": game.cursor, "total_plies": len(game.data["moves"]),
                            "error": game.error, "candidates": [candidate_view(item) for item in by_game.get(game.id, [])]}
                           for game in games]
    return result


def create_job(db: Session, user: User, source: str, games: list[dict], username=None, month=None) -> ImportJob:
    unique = {fingerprint(game): game for game in games}
    if not unique:
        raise HTTPException(422, "В подборке нет корректных завершённых партий.")
    identity = hashlib.sha256(json.dumps([ANALYSIS_VERSION, source, username, month, sorted(unique)], separators=(",", ":")).encode()).hexdigest()
    # Serializes repeated imports and generated-progress restore for this user.
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    previous = db.scalar(select(ImportJob).where(ImportJob.user_id == user.id, ImportJob.identity_key == identity))
    if previous:
        return previous
    count = db.scalar(select(func.count()).select_from(ImportJob).where(ImportJob.user_id == user.id))
    if count >= 100:
        raise HTTPException(422, "Сохранено 100 подборок. Продолжите анализ существующих подборок.")
    job = ImportJob(user_id=user.id, source=source, username=username, month=month, identity_key=identity)
    db.add(job)
    db.flush()
    for game_hash, data in unique.items():
        data = {**data, "game_url": safe_game_url(data.get("game_url", ""))}
        prior = db.scalar(select(ImportedGame).join(ImportJob).where(
            ImportJob.user_id == user.id, ImportJob.source == source, ImportJob.username == username,
            ImportedGame.fingerprint == game_hash, ImportedGame.status == "completed").order_by(ImportedGame.id.desc()).limit(1))
        game = ImportedGame(job_id=job.id, fingerprint=game_hash, data=data)
        if prior:
            game.cursor, game.status = prior.cursor, "completed"
        db.add(game)
        db.flush()
        if prior:
            for item in db.scalars(select(Improvement).where(Improvement.game_id == prior.id)):
                db.add(Improvement(game_id=game.id, ply=item.ply, data=item.data, puzzle_id=item.puzzle_id))
    db.flush()
    if not db.scalar(select(ImportedGame.id).where(ImportedGame.job_id == job.id, ImportedGame.status != "completed").limit(1)):
        job.status = "completed"
    db.commit()
    return job


def validate_puzzle(payload: dict) -> dict:
    try:
        moves = payload["moves"]
        if not isinstance(moves, list) or not 2 <= len(moves) <= 16 or len(moves) % 2:
            raise ValueError("Invalid line")
        board = chess.Board(payload["initial_fen"])
        if not board.is_valid():
            raise ValueError("Invalid position")
        for uci in moves:
            board.push_uci(uci)
        rating = payload["rating"]
        if isinstance(rating, bool) or not isinstance(rating, int) or not 0 <= rating <= 4000:
            raise ValueError("Invalid rating")
        themes = payload["themes"]
        if not isinstance(themes, list) or len(themes) > 20 or not all(isinstance(tag, str) and 0 < len(tag) <= 64 for tag in themes):
            raise ValueError("Invalid themes")
        return {"initial_fen": payload["initial_fen"], "moves": moves, "rating": rating, "themes": themes}
    except (KeyError, ValueError, TypeError, chess.InvalidMoveError, chess.IllegalMoveError):
        raise HTTPException(422, "Этот момент не прошёл проверку задачи.") from None


def create_puzzle(db: Session, job: ImportJob, game: ImportedGame, candidate: Improvement) -> tuple[str, bool]:
    payload = candidate.data.get("puzzle")
    if payload is None:
        raise HTTPException(422, "Это полезное улучшение, но однозначная тактическая задача не найдена.")
    fields = validate_puzzle(payload)
    collection = "public" if job.source == "public" else "personal"
    owner = job.user_id if collection == "personal" else None
    puzzle_id = generate_puzzle_id(fields["initial_fen"], fields["moves"], collection, owner)
    created_id = db.scalar(insert(Puzzle).values(id=puzzle_id, **fields, collection=collection, owner_id=owner,
                        rating_estimated=True, game_url=safe_game_url(game.data.get("game_url", "")), opening_tags="")
                        .on_conflict_do_nothing(index_elements=["id"]).returning(Puzzle.id))
    candidate.puzzle_id = puzzle_id
    return puzzle_id, created_id is not None


@router.get("/imports")
def list_imports(db: Session = Depends(get_db), user: User = Depends(current_user)):
    jobs = db.scalars(select(ImportJob).where(ImportJob.user_id == user.id).order_by(ImportJob.created_at.desc(), ImportJob.id.desc()).limit(100))
    return {"jobs": [job_view(db, job, details=False) for job in jobs]}


@router.post("/imports/chesscom")
def import_chesscom(body: ChessComImportBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    month, games = chesscom_games(body.username, body.month, body.limit)
    return job_view(db, create_job(db, user, "chesscom", games, body.username, month))


@router.post("/imports/public")
def import_public(db: Session = Depends(get_db), user: User = Depends(current_user)):
    from .generation import parse_games
    path = Path(os.environ.get("PUBLIC_GAMES_PATH", str(Path(settings().data_path).with_name("public-games.pgn"))))
    if not path.is_file():
        raise HTTPException(503, "Подборка открытых партий ещё не установлена.")
    if path.stat().st_size > 2 * 1024 * 1024:
        raise HTTPException(422, "Подборка открытых партий слишком большая.")
    try:
        games = parse_games(path.read_text(encoding="utf-8"), max_games=50, max_plies=400)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return job_view(db, create_job(db, user, "public", games))


@router.get("/imports/{job_id}")
def get_import(job_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return job_view(db, owned_job(db, job_id, user.id))


@router.post("/imports/{job_id}/step")
def analyse_step(job_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from .generation import analyse_position
    job = owned_job(db, job_id, user.id)
    if not db.scalar(select(func.pg_try_advisory_xact_lock(782104, job_id))):
        raise HTTPException(409, "Эта подборка уже обрабатывается. Дождитесь текущего шага.")
    db.refresh(job)
    if job.status == "completed":
        return job_view(db, job)
    if not engine_slots.acquire(blocking=False):
        raise HTTPException(503, "Движок занят. Повторите шаг анализа через несколько секунд.")
    try:
        job.status, job.error, job.updated_at = "running", None, now()
        count, deadline = 0, time.monotonic() + 8
        with chess.engine.SimpleEngine.popen_uci(settings().stockfish_path, timeout=5) as engine:
            engine.configure({"Threads": 1, "Hash": 32})
            for game in db.scalars(select(ImportedGame).where(ImportedGame.job_id == job.id,
                                    ImportedGame.status.in_(["pending", "analysing"])).order_by(ImportedGame.id)):
                game.status = "analysing"
                initial_turn = chess.Board(game.data["initial_fen"]).turn
                learner_white = game.data["white"].lower() == job.username if job.username else None
                while game.cursor < len(game.data["moves"]):
                    ply = game.cursor
                    mover_white = initial_turn if ply % 2 == 0 else not initial_turn
                    if job.source == "chesscom" and mover_white != learner_white:
                        game.cursor += 1
                        continue
                    if count >= 2 or time.monotonic() >= deadline:
                        break
                    candidate = analyse_position(game.data, ply, engine, max_time=0.4, require_mistake=job.source == "chesscom")
                    game.cursor += 1
                    count += 1
                    if candidate is not None:
                        found = Improvement(game_id=game.id, ply=ply, data=candidate)
                        db.add(found)
                        db.flush()
                        if job.source == "public" and candidate.get("puzzle") is not None:
                            create_puzzle(db, job, game, found)
                if game.cursor >= len(game.data["moves"]):
                    game.status = "completed"
                if count >= 2 or time.monotonic() >= deadline:
                    break
        db.flush()
        if not db.scalar(select(ImportedGame.id).where(ImportedGame.job_id == job.id,
                         ImportedGame.status.in_(["pending", "analysing"])).limit(1)):
            job.status = "completed"
        db.commit()
        return job_view(db, job)
    except (OSError, chess.engine.EngineError, TimeoutError, ValueError):
        db.rollback()
        job = owned_job(db, job_id, user.id)
        job.status, job.error, job.updated_at = "failed", "Анализ прерван. Уже сохранённые шаги доступны; повторите обработку.", now()
        db.commit()
        raise HTTPException(503, job.error) from None
    finally:
        engine_slots.release()


@router.post("/candidates/{candidate_id}/puzzle")
def candidate_to_puzzle(candidate_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    row = db.execute(select(Improvement, ImportedGame, ImportJob).join(ImportedGame, Improvement.game_id == ImportedGame.id)
                     .join(ImportJob, ImportedGame.job_id == ImportJob.id)
                     .where(Improvement.id == candidate_id, ImportJob.user_id == user.id)
                     .with_for_update(of=Improvement)).first()
    if not row:
        raise HTTPException(404, "Момент партии не найден.")
    candidate, game, job = row
    puzzle_id, created = create_puzzle(db, job, game, candidate)
    db.commit()
    return {"puzzle_id": puzzle_id, "collection": "public" if job.source == "public" else "personal", "created": created}
