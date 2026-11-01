"""Independent, resumable whole-game accuracy reviews for both players."""
from __future__ import annotations

from datetime import datetime, timezone

import chess
import chess.engine
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, case, cast, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .engine_resources import engine_slots
from .game_sources import safe_game_url, source_game_time
from .models import GameAnalysis, ImportedGame, ImportJob, User, now
from .security import current_user

router = APIRouter(prefix="/api/game-analysis")
ANALYSIS_VERSION = 1
LOCK_NAMESPACE = 782105
MAX_LIST_GAMES = 200


class StartGameAnalysis(BaseModel):
    game_id: int = Field(strict=True, gt=0)


def lock_user(db: Session, user_id: int):
    try:
        db.scalar(select(User).where(User.id == user_id).with_for_update(nowait=True))
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        db.rollback()
        raise HTTPException(409, "Другая операция ещё выполняется. Повторите через несколько секунд.") from None


def owned_game(db: Session, game_id: int, user_id: int) -> tuple[ImportedGame, ImportJob]:
    row = db.execute(select(ImportedGame, ImportJob).join(ImportJob, ImportedGame.job_id == ImportJob.id)
                     .where(ImportedGame.id == game_id, ImportJob.user_id == user_id,
                            ImportJob.source == "chesscom")).first()
    if row is None:
        raise HTTPException(404, "Партия не найдена среди ваших импортов Chess.com.")
    return row


def owned_analysis(db: Session, analysis_id: int, user_id: int) -> tuple[GameAnalysis, ImportedGame, ImportJob]:
    row = db.execute(select(GameAnalysis, ImportedGame, ImportJob)
                     .join(ImportedGame, ImportedGame.id == GameAnalysis.game_id)
                     .join(ImportJob, ImportJob.id == ImportedGame.job_id)
                     .where(GameAnalysis.id == analysis_id, GameAnalysis.user_id == user_id,
                            ImportJob.user_id == user_id, ImportJob.source == "chesscom")).first()
    if row is None:
        raise HTTPException(404, "Анализ партии не найден.")
    return row


def lock_analysis(db: Session, analysis_id: int, user_id: int):
    # The advisory lock is acquired before the nonblocking user lock. Concurrent
    # steps/restarts cannot queue several expensive engine searches for one game.
    if not db.scalar(select(func.pg_try_advisory_xact_lock(LOCK_NAMESPACE, analysis_id))):
        raise HTTPException(409, "Эта партия уже обрабатывается. Дождитесь текущего шага.")
    lock_user(db, user_id)


def game_view(game: ImportedGame, job: ImportJob, analysis: GameAnalysis | None = None) -> dict:
    return {
        "id": game.id,
        **{key: game.data.get(key, "") for key in ("white", "black", "result", "date", "event")},
        "game_url": safe_game_url(game.data.get("game_url", "")),
        "source_game_at": source_game_time(game.data),
        "username": job.username,
        "total_plies": len(game.data.get("moves", [])),
        "analysis_id": analysis.id if analysis else None,
        "analysis_status": analysis.status if analysis else None,
        "analysis_outdated": bool(analysis and analysis.analysis_version != ANALYSIS_VERSION),
    }


def analysis_view(analysis: GameAnalysis, game: ImportedGame, job: ImportJob) -> dict:
    from .game_analysis_engine import ANALYSIS_METHOD, CATEGORY_NAMES, summarize_moves
    total = len(game.data["moves"])
    initial_side = "white" if chess.Board(game.data["initial_fen"]).turn else "black"
    return {
        "id": analysis.id, "game_id": game.id,
        "game": {**game_view(game, job, analysis), "initial_fen": game.data["initial_fen"]},
        "status": analysis.status, "cursor": analysis.cursor, "total_plies": total,
        "analysis_version": analysis.analysis_version,
        "analysis_outdated": analysis.analysis_version != ANALYSIS_VERSION,
        "error": analysis.error, "created_at": analysis.created_at, "updated_at": analysis.updated_at,
        "summary": summarize_moves(analysis.moves, total, starting_side=initial_side,
                                   completed=analysis.status == "completed"),
        "moves": analysis.moves, "category_names": CATEGORY_NAMES, "method": ANALYSIS_METHOD,
    }


@router.get("/games")
def list_games(db: Session = Depends(get_db), user: User = Depends(current_user)):
    # One current owned import represents each real game even when monthly
    # batches overlap. Window ranking works with SQLAlchemy 2.0 and newer.
    ranked = (select(ImportedGame.id, func.row_number().over(
                    partition_by=ImportedGame.fingerprint, order_by=ImportedGame.id.desc()).label("rank"))
              .join(ImportJob, ImportJob.id == ImportedGame.job_id)
              .where(ImportJob.user_id == user.id, ImportJob.source == "chesscom").subquery())
    latest_ids = select(ranked.c.id).where(ranked.c.rank == 1)
    # Modern imports store the real completion timestamp. Legacy imports may
    # have only a PGN date; invalid/unknown dates sort last instead of breaking
    # the query. Apply chronology before the cap so importing old months cannot
    # hide newer games or their saved reports.
    stamp = ImportedGame.data["source_game_at"].astext
    legacy_date = func.replace(ImportedGame.data["date"].astext, ".", "-")
    timestamp_type = DateTime(timezone=True)
    game_time = func.coalesce(
        case((func.pg_input_is_valid(stamp, "timestamp with time zone"), cast(stamp, timestamp_type))),
        case((func.pg_input_is_valid(legacy_date, "timestamp with time zone"), cast(legacy_date, timestamp_type))),
    )
    rows = db.execute(select(ImportedGame, ImportJob).join(ImportJob, ImportJob.id == ImportedGame.job_id)
                      .where(ImportedGame.id.in_(latest_ids))
                      .order_by(game_time.desc().nulls_last(), ImportedGame.id.desc()).limit(MAX_LIST_GAMES)).all()
    fingerprints = [game.fingerprint for game, _ in rows]
    analyses = {analysis.fingerprint: analysis for analysis in db.scalars(select(GameAnalysis).where(
        GameAnalysis.user_id == user.id, GameAnalysis.fingerprint.in_(fingerprints)))}
    games = [game_view(game, job, analyses.get(game.fingerprint)) for game, job in rows]
    floor = datetime.min.replace(tzinfo=timezone.utc)
    games.sort(key=lambda game: (game["source_game_at"] or floor, game["id"]), reverse=True)
    return {"games": games}


@router.post("")
def start_analysis(body: StartGameAnalysis, db: Session = Depends(get_db), user: User = Depends(current_user)):
    game, job = owned_game(db, body.game_id, user.id)
    lock_user(db, user.id)
    previous = db.scalar(select(GameAnalysis).where(GameAnalysis.user_id == user.id,
                                                    GameAnalysis.fingerprint == game.fingerprint))
    if previous is not None:
        existing, source_game, source_job = owned_analysis(db, previous.id, user.id)
        return analysis_view(existing, source_game, source_job)
    if not 1 <= len(game.data.get("moves", [])) <= 400:
        raise HTTPException(422, "Для анализа нужна завершённая стандартная партия до 400 полуходов.")
    analysis = GameAnalysis(user_id=user.id, game_id=game.id, fingerprint=game.fingerprint,
                            analysis_version=ANALYSIS_VERSION, status="ready", cursor=0, moves=[])
    db.add(analysis)
    db.commit()
    return analysis_view(analysis, game, job)


@router.get("/{analysis_id}")
def get_analysis(analysis_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return analysis_view(*owned_analysis(db, analysis_id, user.id))


@router.post("/{analysis_id}/step")
def step_analysis(analysis_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from .game_analysis_engine import analyse_game_move
    analysis, game, job = owned_analysis(db, analysis_id, user.id)
    lock_analysis(db, analysis_id, user.id)
    db.refresh(analysis)
    if analysis.analysis_version != ANALYSIS_VERSION:
        raise HTTPException(409, "Метод оценки обновлён. Запустите пересчёт этой партии.")
    if analysis.status == "completed":
        return analysis_view(analysis, game, job)
    if not engine_slots.acquire(blocking=False):
        raise HTTPException(503, "Движок занят. Продолжите анализ через несколько секунд.")
    try:
        if analysis.cursor != len(analysis.moves) or analysis.cursor >= len(game.data["moves"]):
            raise ValueError("Inconsistent analysis cursor")
        analysis.status, analysis.error, analysis.updated_at = "running", None, now()
        with chess.engine.SimpleEngine.popen_uci(settings().stockfish_path, timeout=5) as engine:
            engine.configure({"Threads": 1, "Hash": 32})
            result = analyse_game_move(game.data, analysis.cursor, engine, max_position_time=8)
        # Assign a new JSON array so SQLAlchemy persists the checkpoint. This
        # route never touches tactic candidates, puzzles, attempts or reviews.
        if result.get("ply") != analysis.cursor:
            raise ValueError("Unexpected analyzed move")
        analysis.moves = [*analysis.moves, result]
        analysis.cursor += 1
        analysis.updated_at = now()
        if analysis.cursor == len(game.data["moves"]):
            analysis.status = "completed"
        db.commit()
        return analysis_view(analysis, game, job)
    except (OSError, chess.engine.EngineError, TimeoutError, ValueError):
        # Engine errors leave the transaction usable. Keep its locks while
        # recording failure so another successful step cannot be overwritten.
        analysis.status, analysis.error, analysis.updated_at = (
            "failed", "Анализ прерван. Проверенные ходы сохранены; продолжите обработку.", now())
        db.commit()
        raise HTTPException(503, analysis.error) from None
    finally:
        engine_slots.release()


@router.post("/{analysis_id}/restart")
def restart_analysis(analysis_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    analysis, game, job = owned_analysis(db, analysis_id, user.id)
    lock_analysis(db, analysis_id, user.id)
    db.refresh(analysis)
    analysis.cursor, analysis.moves = 0, []
    analysis.status, analysis.error, analysis.analysis_version = "ready", None, ANALYSIS_VERSION
    analysis.updated_at = now()
    db.commit()
    return analysis_view(analysis, game, job)
