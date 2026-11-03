"""Checkpointed coach ingestion and analysis; independent of browser lifetime."""
from datetime import datetime, timedelta

import chess
import chess.engine
from fastapi import HTTPException
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from . import coach_sources
from .config import settings
from .engine_resources import engine_slots
from .game_sources import source_game_time
from .imports_api import fingerprint
from .models import CoachGame, CoachProfile, User, now

ANALYSIS_VERSION = 1
BASELINE_LIMIT = 40
SYNC_INTERVAL = timedelta(minutes=5)
LOCK_NAMESPACE = 782107


def pending_profiles(db: Session):
    pending = exists().where(CoachGame.coach_id == CoachProfile.id,
                             CoachGame.status.in_(["pending", "running"]))
    return db.execute(select(CoachProfile.id, CoachProfile.user_id, CoachProfile.updated_at).where(
        CoachProfile.active.is_(True), CoachProfile.error.is_(None),
        or_(CoachProfile.next_sync_at <= now(), pending))).all()


def _finish_sync(profile: CoachProfile, state: dict):
    if state["mode"] == "baseline":
        profile.baseline_ready = True
        # Followup starts at activation, including games played while baseline ran.
        profile.last_sync_at = profile.started_at
        profile.next_sync_at = now()
    else:
        profile.last_sync_at = datetime.fromisoformat(state["cutoff"])
        profile.next_sync_at = now() + SYNC_INTERVAL
    profile.sync_state, profile.sync_error = {}, None


def _sync_one_month(db: Session, profile: CoachProfile):
    state = dict(profile.sync_state or {})
    if not state:
        months = coach_sources.archive_months(profile.username)
        mode = "followup" if profile.baseline_ready else "baseline"
        cutoff = now() if profile.baseline_ready else profile.started_at
        if mode == "followup":
            # Catch up all missed months after downtime, and re-read the previous
            # month for archives that are published late around a month boundary.
            anchor = profile.last_sync_at or profile.started_at
            previous_month = (anchor.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
            months = [month for month in months if previous_month <= month <= cutoff.strftime("%Y-%m")]
        else:
            months = [month for month in months if month <= cutoff.strftime("%Y-%m")]
        state = {"mode": mode, "months": months, "index": 0, "cutoff": cutoff.isoformat()}
        profile.sync_state = state
        if not months:
            _finish_sync(profile, state)
        return
    cutoff = datetime.fromisoformat(state["cutoff"])
    index, months = state["index"], state["months"]
    if index >= len(months):
        _finish_sync(profile, state)
        return
    games = coach_sources.month_games(profile.username, months[index], profile.time_control)
    baseline_count = db.scalar(select(func.count()).select_from(CoachGame).where(
        CoachGame.coach_id == profile.id, CoachGame.cohort == "baseline"))
    # Sort again at the boundary to keep baseline selection independent of a source implementation.
    games.sort(key=lambda game: source_game_time(game) or profile.started_at, reverse=True)
    for data in games:
        stamp = source_game_time(data)
        if stamp is None or stamp > cutoff or data.get("time_control") != profile.time_control:
            continue
        if state["mode"] == "baseline":
            if stamp > profile.started_at or baseline_count >= BASELINE_LIMIT:
                continue
        elif stamp <= profile.started_at:
            continue
        identity = fingerprint(data)
        if db.scalar(select(CoachGame.id).where(CoachGame.coach_id == profile.id,
                                               CoachGame.fingerprint == identity)) is not None:
            continue
        db.add(CoachGame(coach_id=profile.id, fingerprint=identity, cohort=state["mode"],
                         data={**data, "username": profile.username}, source_game_at=stamp,
                         analysis_version=ANALYSIS_VERSION))
        db.flush()
        if state["mode"] == "baseline":
            baseline_count += 1
    state["index"] = index + 1
    profile.sync_state = state
    profile.sync_error = None
    if state["index"] >= len(months) or (state["mode"] == "baseline" and baseline_count >= BASELINE_LIMIT):
        _finish_sync(profile, state)


def _analyse_one_move(db: Session, profile: CoachProfile) -> bool:
    from .coach_insights import inspect_move
    from .game_analysis_engine import ANALYSIS_ENGINE_OPTIONS, ANALYSIS_RETRY_OPTIONS, analyse_game_move
    game = db.scalar(select(CoachGame).where(CoachGame.coach_id == profile.id,
        CoachGame.status.in_(["pending", "running"])).order_by(
        (CoachGame.cohort != "baseline"), CoachGame.source_game_at.desc(), CoachGame.id).limit(1))
    if game is None:
        return False
    if not engine_slots.acquire(blocking=False):
        raise HTTPException(503, "Движок занят. Тренер продолжит автоматически.")
    try:
        initial_white = chess.Board(game.data["initial_fen"]).turn
        learner_white = game.data["white"].lower() == profile.username
        own_plies = [ply for ply in range(len(game.data["moves"]))
                     if (initial_white if ply % 2 == 0 else not initial_white) == learner_white]
        pending = [ply for ply in own_plies if ply >= game.cursor]
        retry = False
        if not pending:
            pending = [row["ply"] for row in game.moves if not row.get("assessed") and row.get("retry_attempts", 0) < 1]
            retry = True
        if not pending:
            game.status = "completed"
            game.cursor = len(game.data["moves"])
            return True
        ply = pending[0]
        with chess.engine.SimpleEngine.popen_uci(settings().stockfish_path, timeout=5) as engine:
            engine.configure(ANALYSIS_ENGINE_OPTIONS)
            kwargs = ANALYSIS_RETRY_OPTIONS if retry else {}
            row = analyse_game_move(game.data, ply, engine, **kwargs)
        if row.get("ply") != ply:
            raise ValueError("Unexpected coach move")
        row.update(inspect_move(game.data, row, profile.username))
        row["retry_attempts"] = int(retry)
        game.moves = [item for item in game.moves if item["ply"] != ply] + [row]
        game.moves.sort(key=lambda item: item["ply"])
        if not retry:
            game.cursor = ply + 1
        game.status, game.error = "running", None
        remaining = any(item >= game.cursor for item in own_plies)
        retries = any(not item.get("assessed") and item.get("retry_attempts", 0) < 1 for item in game.moves)
        if not remaining and not retries:
            game.cursor, game.status = len(game.data["moves"]), "completed"
        return True
    except (OSError, chess.engine.EngineError, TimeoutError, ValueError):
        game.status, game.error = "failed", "Не удалось продолжить проверку движком. Проверенные позиции сохранены."
        profile.error = game.error
        return True
    finally:
        engine_slots.release()


def process_step(profile_id: int, db: Session, user: User, *, background: bool = False):
    profile = db.scalar(select(CoachProfile).where(CoachProfile.id == profile_id,
                                                  CoachProfile.user_id == user.id))
    if profile is None:
        raise HTTPException(404, "Личный тренер не найден.")
    if not db.scalar(select(func.pg_try_advisory_xact_lock(LOCK_NAMESPACE, profile.id))):
        raise HTTPException(409, "Тренер уже обрабатывает текущую позицию.")
    db.refresh(profile)
    if not profile.active or profile.error:
        return None
    progressed = False
    if profile.next_sync_at <= now():
        try:
            _sync_one_month(db, profile)
        except HTTPException as exc:
            # Keep cursor and retry automatically. A transient API outage does not
            # erase baseline or stop work on games already downloaded.
            profile.sync_error = str(exc.detail)
            profile.next_sync_at = now() + SYNC_INTERVAL
        progressed = True
    else:
        progressed = _analyse_one_move(db, profile)
    if progressed:
        profile.updated_at = now()
        db.commit()
        return True
    return None
