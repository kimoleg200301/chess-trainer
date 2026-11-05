"""Resumable batches for personal and public game analysis."""
import hashlib
import json
import os
from pathlib import Path
import time

import chess
import chess.engine
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .collections import generate_puzzle_id
from .background import BackgroundStart, request_background
from .config import settings
from .db import get_db
from .engine_resources import engine_slots
from .game_sources import (chesscom_accuracies, chesscom_game_url, chesscom_games,
                           safe_game_url, source_game_time)
from .models import ImportJob, ImportedGame, Improvement, Puzzle, User, now
from .security import current_user
from .transactions import lock_user

router = APIRouter(prefix="/api")
ANALYSIS_VERSION = 2


class ChessComImportBody(BackgroundStart):
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


def candidate_view(candidate: Improvement, pending: bool = False) -> dict:
    fields = ("ply", "move_number", "side", "before_fen", "played_uci", "played_san", "best_uci", "best_san",
              "loss_cp", "score_before", "score_after", "mate_before", "mate_after", "best_line_san", "explanation", "phase",
              "puzzle_reason_code", "puzzle_reason", "analysis_depth")
    result = {field: candidate.data.get(field) for field in fields}
    result.update(id=candidate.id, puzzle_available=candidate.data.get("puzzle") is not None,
                  puzzle_id=candidate.puzzle_id, analysis_pending=pending)
    return result


def job_view(db: Session, job: ImportJob, details: bool = True) -> dict:
    games = list(db.scalars(select(ImportedGame).where(ImportedGame.job_id == job.id).order_by(ImportedGame.id)))
    candidates = list(db.scalars(select(Improvement).join(ImportedGame).where(ImportedGame.job_id == job.id).order_by(Improvement.ply)))
    by_game = {}
    game_map = {game.id: game for game in games}

    def pending(candidate):
        game = game_map[candidate.game_id]
        return game.status in {"pending", "analysing"} and candidate.ply >= game.cursor

    for candidate in candidates:
        by_game.setdefault(candidate.game_id, []).append(candidate)
    linked_ids = {candidate.puzzle_id for candidate in candidates if candidate.puzzle_id is not None}
    # Rechecking may remove an error card while preserving its playable puzzle.
    # Include saved tasks from these source games, strictly within this bank/owner.
    game_urls = {url for game in games if (url := safe_game_url(game.data.get("game_url", "")))}
    saved_ids = set()
    if linked_ids or game_urls:
        source_collection = "public" if job.source == "public" else "personal"
        source_owner = None if source_collection == "public" else job.user_id
        saved_ids.update(db.scalars(select(Puzzle.id).where(Puzzle.collection == source_collection,
                                Puzzle.owner_id == source_owner,
                                or_(Puzzle.id.in_(linked_ids), Puzzle.game_url.in_(game_urls)))))
    result = {"id": job.id, "source": job.source, "username": job.username, "month": job.month,
              "status": job.status, "background_requested": job.background_requested, "total_games": len(games),
              "completed_games": sum(game.status in {"completed", "skipped"} for game in games),
              "candidates_count": len(candidates),
              "available_puzzles_count": sum(candidate.data.get("puzzle") is not None and not pending(candidate) for candidate in candidates),
              "puzzles_count": len(saved_ids),
              "analysis_version": job.analysis_version,
              "analysis_outdated": job.analysis_version != ANALYSIS_VERSION or any(game.analysis_version != ANALYSIS_VERSION for game in games),
              "error": job.error, "created_at": job.created_at.isoformat()}
    if details:
        result["games"] = [{"id": game.id, **{field: game.data.get(field, "") for field in
                            ("white", "black", "result", "date", "event", "game_url")},
                            "status": game.status, "cursor": game.cursor, "total_plies": len(game.data["moves"]),
                            "error": game.error, "analysis_version": game.analysis_version,
                            "source_game_at": source_game_time(game.data),
                            "candidates": [candidate_view(item, pending(item)) for item in by_game.get(game.id, [])]}
                           for game in games]
    return result


def refresh_chesscom_accuracies(db: Session, user_id: int, games: list[dict]) -> dict[str, dict]:
    """Refresh reference scores in every owned copy, keeping local analysis intact.

    The caller holds the user lock. A public archive can omit a previously
    calculated score, so only valid observed sides update stored values. Return
    remembered values as well, so a new overlapping import cannot hide them.
    """
    incoming_hashes, incoming_urls = {}, {}
    for game in games:
        scores = chesscom_accuracies(game.get("chesscom_accuracies"))
        if not scores:
            continue
        incoming_hashes.setdefault(fingerprint(game), {}).update(scores)
        if url := chesscom_game_url(game.get("game_url")):
            incoming_urls.setdefault(url, {}).update(scores)
    stored_hashes, stored_urls = {}, {}
    existing = db.scalars(select(ImportedGame).join(ImportJob).where(
        ImportJob.user_id == user_id, ImportJob.source == "chesscom").order_by(ImportedGame.id))
    for stored in existing:
        url = chesscom_game_url(stored.data.get("game_url"))
        scores = chesscom_accuracies(stored.data.get("chesscom_accuracies")) or {}
        updates = {**incoming_urls.get(url, {}), **incoming_hashes.get(stored.fingerprint, {})}
        if updates:
            scores = {**scores, **updates}
            if stored.data.get("chesscom_accuracies") != scores:
                stored.data = {**stored.data, "chesscom_accuracies": scores}
        if scores:
            stored_hashes.setdefault(stored.fingerprint, {}).update(scores)
            if url:
                stored_urls.setdefault(url, {}).update(scores)
    result = {}
    for game in games:
        key, url = fingerprint(game), chesscom_game_url(game.get("game_url"))
        scores = {**stored_urls.get(url, {}), **stored_hashes.get(key, {}),
                  **incoming_urls.get(url, {}), **incoming_hashes.get(key, {})}
        if scores:
            result[key] = scores
    return result


def create_job(db: Session, user: User, source: str, games: list[dict], username=None, month=None) -> ImportJob:
    unique = {fingerprint(game): game for game in games}
    if not unique:
        raise HTTPException(422, "В подборке нет корректных завершённых партий.")
    identity = hashlib.sha256(json.dumps([ANALYSIS_VERSION, source, username, month, sorted(unique)], separators=(",", ":")).encode()).hexdigest()
    # Serializes repeated imports and generated-progress restore for this user.
    lock_user(db, user.id, nowait=True)
    if source == "chesscom":
        references = refresh_chesscom_accuracies(db, user.id, games)
        unique = {key: {**game, "chesscom_accuracies": references.get(key)}
                  for key, game in unique.items()}
    previous = db.scalar(select(ImportJob).where(ImportJob.user_id == user.id, ImportJob.identity_key == identity))
    if previous:
        db.commit()
        return previous
    # Older releases included the analysis version in identity. Identical games
    # still reopen their existing batch; the explicit rerun upgrades its analysis.
    possible = db.scalars(select(ImportJob).where(ImportJob.user_id == user.id,
                          ImportJob.source == source, ImportJob.username == username,
                          ImportJob.month == month).order_by(ImportJob.id.desc()))
    for prior_job in possible:
        hashes = set(db.scalars(select(ImportedGame.fingerprint).where(ImportedGame.job_id == prior_job.id)))
        if hashes == set(unique):
            db.commit()
            return prior_job
    count = db.scalar(select(func.count()).select_from(ImportJob).where(ImportJob.user_id == user.id))
    if count >= 100:
        raise HTTPException(422, "Сохранено 100 подборок. Продолжите анализ существующих подборок.")
    job = ImportJob(user_id=user.id, source=source, username=username, month=month, identity_key=identity,
                    analysis_version=ANALYSIS_VERSION)
    db.add(job)
    db.flush()
    for game_hash, data in unique.items():
        stamp = source_game_time(data)
        data = {**data, "game_url": safe_game_url(data.get("game_url", "")),
                "source_game_at": stamp.isoformat() if stamp else None}
        prior = db.scalar(select(ImportedGame).join(ImportJob).where(
            ImportJob.user_id == user.id, ImportJob.source == source, ImportJob.username == username,
            ImportJob.analysis_version == ANALYSIS_VERSION, ImportedGame.analysis_version == ANALYSIS_VERSION,
            ImportedGame.fingerprint == game_hash, ImportedGame.status == "completed").order_by(ImportedGame.id.desc()).limit(1))
        game = ImportedGame(job_id=job.id, fingerprint=game_hash, data=data, analysis_version=ANALYSIS_VERSION)
        if prior:
            game.cursor, game.status = prior.cursor, "completed"
        db.add(game)
        db.flush()
        if prior:
            for item in db.scalars(select(Improvement).where(Improvement.game_id == prior.id)):
                copied = Improvement(game_id=game.id, ply=item.ply, data=item.data, puzzle_id=item.puzzle_id)
                db.add(copied)
                if copied.data.get("puzzle") is not None:
                    create_puzzle(db, job, game, copied)
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
    stamp = source_game_time(game.data)
    game_url = safe_game_url(game.data.get("game_url", ""))
    created_id = db.scalar(insert(Puzzle).values(id=puzzle_id, **fields, collection=collection, owner_id=owner,
                        rating_estimated=True, game_url=game_url, opening_tags="",
                        source_game_at=stamp, source_ply=candidate.ply)
                        .on_conflict_do_nothing(index_elements=["id"]).returning(Puzzle.id))
    if created_id is None:
        existing = db.get(Puzzle, puzzle_id)
        if existing.source_game_at is None or (stamp is not None and stamp > existing.source_game_at):
            existing.source_game_at, existing.source_ply, existing.game_url = stamp, candidate.ply, game_url
        elif existing.source_ply is None:
            existing.source_ply = candidate.ply
    candidate.puzzle_id = puzzle_id
    return puzzle_id, created_id is not None


def backfill_generated_puzzles(db: Session) -> int:
    """Bootstrap legacy eligible moments and real game dates without rerunning Stockfish."""
    saved = 0
    rows = db.execute(select(Improvement, ImportedGame, ImportJob)
                      .join(ImportedGame, Improvement.game_id == ImportedGame.id)
                      .join(ImportJob, ImportedGame.job_id == ImportJob.id)
                      .order_by(ImportJob.user_id, ImportedGame.id, Improvement.ply)).all()
    locked_user = None
    for candidate, game, job in rows:
        if job.user_id != locked_user:
            lock_user(db, job.user_id)
            locked_user = job.user_id
        if candidate.data.get("puzzle") is not None:
            try:
                _, created = create_puzzle(db, job, game, candidate)
                saved += int(created)
            except HTTPException:
                # An invalid old candidate must not prevent the application starting.
                continue
        elif candidate.puzzle_id:
            puzzle = db.get(Puzzle, candidate.puzzle_id)
            if puzzle is not None and puzzle.source_game_at is None:
                puzzle.source_game_at = source_game_time(game.data)
                puzzle.source_ply = candidate.ply
    return saved


@router.get("/imports")
def list_imports(db: Session = Depends(get_db), user: User = Depends(current_user)):
    jobs = db.scalars(select(ImportJob).where(ImportJob.user_id == user.id).order_by(ImportJob.created_at.desc(), ImportJob.id.desc()).limit(100))
    return {"jobs": [job_view(db, job, details=False) for job in jobs]}


@router.post("/imports/chesscom")
def import_chesscom(body: ChessComImportBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    month, games = chesscom_games(body.username, body.month, body.limit)
    job = create_job(db, user, "chesscom", games, body.username, month)
    from .chesscom_sync import remember_username
    remember_username(db, user.id, body.username)
    if body.background and job.analysis_version == ANALYSIS_VERSION:
        request_background(db, job, ANALYSIS_VERSION, True)
    return job_view(db, job)


@router.post("/imports/public")
def import_public(body: BackgroundStart = BackgroundStart(), db: Session = Depends(get_db), user: User = Depends(current_user)):
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
    job = create_job(db, user, "public", games)
    if body.background and job.analysis_version == ANALYSIS_VERSION:
        request_background(db, job, ANALYSIS_VERSION, True)
    return job_view(db, job)


@router.get("/imports/{job_id}")
def get_import(job_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return job_view(db, owned_job(db, job_id, user.id))


@router.post("/imports/{job_id}/step")
def analyse_step(job_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return process_step(job_id, db, user)


def process_step(job_id: int, db: Session, user: User, *, background: bool = False):
    from .generation import analyse_position
    job = owned_job(db, job_id, user.id)
    if not db.scalar(select(func.pg_try_advisory_xact_lock(782104, job_id))):
        raise HTTPException(409, "Эта подборка уже обрабатывается. Дождитесь текущего шага.")
    lock_user(db, user.id, nowait=True)
    db.refresh(job)
    if background and (not job.background_requested or job.status not in {"ready", "running"}):
        return None
    if job.analysis_version != ANALYSIS_VERSION:
        raise HTTPException(409, "Метод проверки обновлён. Запустите повторный анализ подборки.")
    if job.status == "completed":
        return job_view(db, job)
    if not engine_slots.acquire(blocking=False):
        raise HTTPException(503, "Движок занят. Повторите шаг анализа через несколько секунд.")
    try:
        with db.begin_nested():
            count, deadline = 0, time.monotonic() + 16
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
                        if count >= 1 or time.monotonic() >= deadline:
                            break
                        candidate = analyse_position(game.data, ply, engine, max_time=1.2, max_position_time=14,
                                                     require_mistake=job.source == "chesscom")
                        game.cursor += 1
                        count += 1
                        found = db.scalar(select(Improvement).where(Improvement.game_id == game.id, Improvement.ply == ply))
                        if candidate is not None:
                            if found is None:
                                found = Improvement(game_id=game.id, ply=ply, data=candidate)
                                db.add(found)
                            else:
                                found.data = candidate
                            db.flush()
                            if candidate.get("puzzle") is not None:
                                create_puzzle(db, job, game, found)
                        elif found is not None:
                            db.delete(found)
                    if game.cursor >= len(game.data["moves"]):
                        game.status, game.analysis_version = "completed", ANALYSIS_VERSION
                    if count >= 1 or time.monotonic() >= deadline:
                        break
            job.status, job.error, job.updated_at = "running", None, now()
            db.flush()
            if not db.scalar(select(ImportedGame.id).where(ImportedGame.job_id == job.id,
                             ImportedGame.status.in_(["pending", "analysing"])).limit(1)):
                job.status, job.analysis_version = "completed", ANALYSIS_VERSION
                job.background_requested = False
        db.commit()
        return job_view(db, job)
    except (OSError, chess.engine.EngineError, TimeoutError, ValueError):
        # Roll back only this position; retain the advisory/user locks while
        # recording failure so a concurrent successful step cannot be overwritten.
        db.refresh(job)
        job.status, job.error, job.updated_at = "failed", "Анализ прерван. Уже сохранённые шаги доступны; повторите обработку.", now()
        job.background_requested = False
        db.commit()
        raise HTTPException(503, job.error) from None
    finally:
        engine_slots.release()


@router.post("/imports/{job_id}/reanalyze")
def reanalyse_import(job_id: int, body: BackgroundStart = BackgroundStart(), db: Session = Depends(get_db), user: User = Depends(current_user)):
    job = owned_job(db, job_id, user.id)
    if not db.scalar(select(func.pg_try_advisory_xact_lock(782104, job_id))):
        raise HTTPException(409, "Подождите завершения текущего шага, затем повторите анализ.")
    lock_user(db, user.id, nowait=True)
    # Store old cards until their position is rechecked. Their training tasks and
    # attempts remain intact even if the new search no longer finds an error.
    for game in db.scalars(select(ImportedGame).where(ImportedGame.job_id == job.id)):
        game.cursor, game.status, game.error = 0, "pending", None
        game.analysis_version = ANALYSIS_VERSION
    job.status, job.error, job.analysis_version, job.updated_at = "ready", None, ANALYSIS_VERSION, now()
    job.background_requested = body.background
    db.commit()
    return job_view(db, job)


@router.post("/imports/{job_id}/resume")
def resume_import(job_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    job = owned_job(db, job_id, user.id)
    request_background(db, job, ANALYSIS_VERSION, True)
    return job_view(db, job)


@router.post("/imports/{job_id}/pause")
def pause_import(job_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    job = owned_job(db, job_id, user.id)
    request_background(db, job, ANALYSIS_VERSION, False)
    return job_view(db, job)


@router.post("/candidates/{candidate_id}/puzzle")
def candidate_to_puzzle(candidate_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    lock_user(db, user.id, nowait=True)
    row = db.execute(select(Improvement, ImportedGame, ImportJob).join(ImportedGame, Improvement.game_id == ImportedGame.id)
                     .join(ImportJob, ImportedGame.job_id == ImportJob.id)
                     .where(Improvement.id == candidate_id, ImportJob.user_id == user.id)
                     .with_for_update(of=Improvement)).first()
    if not row:
        raise HTTPException(404, "Момент партии не найден.")
    candidate, game, job = row
    if candidate.puzzle_id is None and game.status in {"pending", "analysing"} and candidate.ply >= game.cursor:
        raise HTTPException(409, "Дождитесь повторной проверки этого момента.")
    puzzle_id, created = create_puzzle(db, job, game, candidate)
    db.commit()
    return {"puzzle_id": puzzle_id, "collection": "public" if job.source == "public" else "personal", "created": created}
