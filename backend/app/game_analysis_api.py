"""Independent, resumable whole-game accuracy reviews for both players."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

import chess
import chess.engine
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, case, cast, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from .config import settings
from .background import BackgroundStart, request_background
from .db import get_db
from .engine_resources import engine_slots
from .game_sources import chesscom_accuracies, safe_game_url, source_game_time
from .models import GameAnalysis, GameFavorite, ImportedGame, ImportJob, User, now
from .security import current_user
from .chesscom_sync import SyncRequest, get_sync, request_sync

router = APIRouter(prefix="/api/game-analysis")
ANALYSIS_VERSION = 2  # Stockfish 19 with the deeper default review budget.
LOCK_NAMESPACE = 782105
MAX_LIST_GAMES = 200
MAX_EXTRA_ATTEMPTS = 1


def pending_retries(moves: list[dict]) -> list[int]:
    return [ply for ply, move in enumerate(moves)
            if (not move.get("assessed") or move.get("category") == "unassessed")
            and move.get("retry_attempts", 0) < MAX_EXTRA_ATTEMPTS]


def resume_report(db: Session, analysis: GameAnalysis):
    # Legacy completed reports may still have a missing score. Reopen only
    # those checkpoints, without discarding any already assessed moves.
    if (analysis.analysis_version == ANALYSIS_VERSION and analysis.status == "completed"
            and pending_retries(analysis.moves)):
        lock_analysis(db, analysis.id, analysis.user_id)
        db.refresh(analysis)
        if analysis.status == "completed" and pending_retries(analysis.moves):
            analysis.status = "ready"
            db.flush()
    request_background(db, analysis, ANALYSIS_VERSION, True)


class StartGameAnalysis(BackgroundStart):
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
        "chesscom_accuracies": chesscom_accuracies(game.data.get("chesscom_accuracies")),
        "username": job.username,
        "total_plies": len(game.data.get("moves", [])),
        "analysis_id": analysis.id if analysis else None,
        "analysis_status": analysis.status if analysis else None,
        "background_requested": analysis.background_requested if analysis else False,
        "analysis_outdated": bool(analysis and analysis.analysis_version != ANALYSIS_VERSION),
    }


def analysis_view(analysis: GameAnalysis, game: ImportedGame, job: ImportJob) -> dict:
    from .game_analysis_engine import ANALYSIS_METHOD, CATEGORY_NAMES, summarize_moves
    from .accuracy_calibration import calibrate_summary, calibration_metadata, load_active_profile
    total = len(game.data["moves"])
    initial_side = "white" if chess.Board(game.data["initial_fen"]).turn else "black"
    summary = summarize_moves(analysis.moves, total, starting_side=initial_side,
                              completed=analysis.status == "completed")
    profile = load_active_profile()
    summary = calibrate_summary(analysis.moves, summary, game.data, username=job.username,
                                current_version=(analysis.analysis_version == ANALYSIS_VERSION
                                                 and (not profile or profile.get("analysis_version", ANALYSIS_VERSION)
                                                      == ANALYSIS_VERSION)),
                                profile=profile)
    method = dict(ANALYSIS_METHOD)
    if profile and any(summary[side].get("estimated_chesscom_accuracy") is not None
                       for side in ("white", "black")):
        method["calibration"] = calibration_metadata(profile)
    return {
        "id": analysis.id, "game_id": game.id,
        "game": {**game_view(game, job, analysis), "initial_fen": game.data["initial_fen"]},
        "status": analysis.status, "background_requested": analysis.background_requested, "cursor": analysis.cursor, "total_plies": total,
        "retry_pending": len(pending_retries(analysis.moves)),
        "analysis_version": analysis.analysis_version,
        "analysis_outdated": analysis.analysis_version != ANALYSIS_VERSION,
        "error": analysis.error, "created_at": analysis.created_at, "updated_at": analysis.updated_at,
        "summary": summary,
        "moves": analysis.moves, "category_names": CATEGORY_NAMES, "method": method,
    }


@router.get("/games")
def list_games(db: Session = Depends(get_db), user: User = Depends(current_user),
               selected_game_id: Annotated[int | None, Query(gt=0)] = None,
               q: Annotated[str, Query(max_length=80)] = ""):
    # One current owned import represents each real game even when monthly
    # batches overlap. Window ranking works with SQLAlchemy 2.0 and newer.
    rank_order = [ImportedGame.id.desc()]
    if selected_game_id is not None:
        rank_order.insert(0, (ImportedGame.id == selected_game_id).desc())
    ranked = (select(ImportedGame.id, func.row_number().over(
                    partition_by=ImportedGame.fingerprint, order_by=rank_order).label("rank"))
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
    nickname = q.strip()
    matches = or_(ImportedGame.data["white"].astext.icontains(nickname, autoescape=True),
                  ImportedGame.data["black"].astext.icontains(nickname, autoescape=True)) if nickname else True
    # Search the owned archive before limiting the results, including older games.
    recent_ids = (select(ImportedGame.id).where(ImportedGame.id.in_(latest_ids), matches)
                  .order_by(game_time.desc().nulls_last(), ImportedGame.id.desc()).limit(MAX_LIST_GAMES))
    favorites = {row.fingerprint: row for row in db.scalars(select(GameFavorite).where(GameFavorite.user_id == user.id))}
    saved_keys = [key for key, value in favorites.items() if value.is_favorite]
    # Saved games remain accessible when they fall outside the recent-game cap.
    # Keep a directly opened game available even after it is unstarred.
    rows = db.execute(select(ImportedGame, ImportJob).join(ImportJob, ImportJob.id == ImportedGame.job_id)
                      .where(ImportedGame.id.in_(latest_ids), or_(matches, ImportedGame.id == selected_game_id),
                             or_(ImportedGame.id.in_(recent_ids),
                             ImportedGame.fingerprint.in_(saved_keys), ImportedGame.id == selected_game_id))
                      .order_by(game_time.desc().nulls_last(), ImportedGame.id.desc())).all()
    fingerprints = [game.fingerprint for game, _ in rows]
    analyses = {analysis.fingerprint: analysis for analysis in db.scalars(select(GameAnalysis).where(
        GameAnalysis.user_id == user.id, GameAnalysis.fingerprint.in_(fingerprints)))}
    games = [{**game_view(game, job, analyses.get(game.fingerprint)),
              **favorite_view(game, favorites.get(game.fingerprint))} for game, job in rows]
    floor = datetime.min.replace(tzinfo=timezone.utc)
    games.sort(key=lambda game: (game["source_game_at"] or floor, game["id"]), reverse=True)
    return {"games": games, "sync": get_sync(db, user.id)}


def favorite_view(game: ImportedGame, record: GameFavorite | None) -> dict:
    return {"favorite_key": game.fingerprint, "is_favorite": bool(record and record.is_favorite),
            "favorite_revision": record.revision if record else 0}


class SetFavorite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_favorite: bool = Field(strict=True)


@router.post("/games/{game_id}/favorite")
def set_favorite(game_id: int, body: SetFavorite, db: Session = Depends(get_db), user: User = Depends(current_user)):
    game, _ = owned_game(db, game_id, user.id)
    statement = insert(GameFavorite).values(user_id=user.id, fingerprint=game.fingerprint,
                                             is_favorite=body.is_favorite, revision=1)
    statement = statement.on_conflict_do_update(
        index_elements=[GameFavorite.user_id, GameFavorite.fingerprint],
        set_={"is_favorite": body.is_favorite,
              "revision": case((GameFavorite.is_favorite != body.is_favorite, GameFavorite.revision + 1),
                               else_=GameFavorite.revision)})
    db.execute(statement)
    db.commit()
    record = db.get(GameFavorite, (user.id, game.fingerprint), populate_existing=True)
    return {"game_id": game.id, **favorite_view(game, record)}


@router.post("/sync")
def sync_games(body: SyncRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return request_sync(db, user, body)


@router.post("")
def start_analysis(body: StartGameAnalysis, db: Session = Depends(get_db), user: User = Depends(current_user)):
    game, job = owned_game(db, body.game_id, user.id)
    lock_user(db, user.id)
    previous = db.scalar(select(GameAnalysis).where(GameAnalysis.user_id == user.id,
                                                    GameAnalysis.fingerprint == game.fingerprint))
    if previous is not None:
        existing, source_game, source_job = owned_analysis(db, previous.id, user.id)
        if body.background and existing.analysis_version == ANALYSIS_VERSION:
            resume_report(db, existing)
        return analysis_view(existing, source_game, source_job)
    if not 1 <= len(game.data.get("moves", [])) <= 400:
        raise HTTPException(422, "Для анализа нужна завершённая стандартная партия до 400 полуходов.")
    analysis = GameAnalysis(user_id=user.id, game_id=game.id, fingerprint=game.fingerprint,
                            analysis_version=ANALYSIS_VERSION, status="ready", cursor=0, moves=[],
                            background_requested=body.background)
    db.add(analysis)
    db.commit()
    return analysis_view(analysis, game, job)


@router.get("/{analysis_id}")
def get_analysis(analysis_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return analysis_view(*owned_analysis(db, analysis_id, user.id))


@router.post("/{analysis_id}/step")
def step_analysis(analysis_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return process_step(analysis_id, db, user)


def process_step(analysis_id: int, db: Session, user: User, *, background: bool = False):
    from .game_analysis_engine import ANALYSIS_ENGINE_OPTIONS, ANALYSIS_RETRY_OPTIONS, analyse_game_move
    analysis, game, job = owned_analysis(db, analysis_id, user.id)
    lock_analysis(db, analysis_id, user.id)
    db.refresh(analysis)
    if background and (not analysis.background_requested or analysis.status not in {"ready", "running"}):
        return None
    if analysis.analysis_version != ANALYSIS_VERSION:
        raise HTTPException(409, "Метод оценки обновлён. Запустите пересчёт этой партии.")
    if analysis.status == "completed":
        return analysis_view(analysis, game, job)
    if not engine_slots.acquire(blocking=False):
        raise HTTPException(503, "Движок занят. Продолжите анализ через несколько секунд.")
    try:
        total = len(game.data["moves"])
        if analysis.cursor != len(analysis.moves) or analysis.cursor > total:
            raise ValueError("Inconsistent analysis cursor")
        retry = analysis.cursor == total
        pending = pending_retries(analysis.moves)
        if retry and not pending:
            analysis.status, analysis.background_requested, analysis.error = "completed", False, None
            db.commit()
            return analysis_view(analysis, game, job)
        ply = pending[0] if retry else analysis.cursor
        analysis.status, analysis.error, analysis.updated_at = "running", None, now()
        with chess.engine.SimpleEngine.popen_uci(settings().stockfish_path, timeout=5) as engine:
            engine.configure(ANALYSIS_ENGINE_OPTIONS)
            result = analyse_game_move(game.data, ply, engine, **(ANALYSIS_RETRY_OPTIONS if retry else {}))
            result["engine_name"] = getattr(engine, "id", {}).get("name", "Stockfish")
        # Assign a new JSON array so SQLAlchemy persists the checkpoint. This
        # route never touches tactic candidates, puzzles, attempts or reviews.
        if result.get("ply") != ply:
            raise ValueError("Unexpected analyzed move")
        if retry:
            result["retry_attempts"] = analysis.moves[ply].get("retry_attempts", 0) + 1
            analysis.moves = [*analysis.moves[:ply], result, *analysis.moves[ply + 1:]]
        else:
            analysis.moves = [*analysis.moves, result]
            analysis.cursor += 1
        analysis.updated_at = now()
        if analysis.cursor == total and not pending_retries(analysis.moves):
            analysis.status = "completed"
            analysis.background_requested = False
        db.commit()
        return analysis_view(analysis, game, job)
    except (OSError, chess.engine.EngineError, TimeoutError, ValueError):
        # Engine errors leave the transaction usable. Keep its locks while
        # recording failure so another successful step cannot be overwritten.
        analysis.status, analysis.error, analysis.updated_at = (
            "failed", "Анализ прерван. Проверенные ходы сохранены; продолжите обработку.", now())
        analysis.background_requested = False
        db.commit()
        raise HTTPException(503, analysis.error) from None
    finally:
        engine_slots.release()


@router.post("/{analysis_id}/restart")
def restart_analysis(analysis_id: int, body: BackgroundStart = BackgroundStart(), db: Session = Depends(get_db), user: User = Depends(current_user)):
    analysis, game, job = owned_analysis(db, analysis_id, user.id)
    lock_analysis(db, analysis_id, user.id)
    db.refresh(analysis)
    analysis.cursor, analysis.moves = 0, []
    analysis.status, analysis.error, analysis.analysis_version = "ready", None, ANALYSIS_VERSION
    analysis.background_requested = body.background
    analysis.updated_at = now()
    db.commit()
    return analysis_view(analysis, game, job)


@router.post("/{analysis_id}/resume")
def resume_analysis(analysis_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    analysis, game, job = owned_analysis(db, analysis_id, user.id)
    resume_report(db, analysis)
    return analysis_view(analysis, game, job)


@router.post("/{analysis_id}/pause")
def pause_analysis(analysis_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    analysis, game, job = owned_analysis(db, analysis_id, user.id)
    request_background(db, analysis, ANALYSIS_VERSION, False)
    return analysis_view(analysis, game, job)
