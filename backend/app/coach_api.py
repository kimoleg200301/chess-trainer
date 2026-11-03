"""Authenticated personal coach, with an immutable baseline and live followup."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from .db import get_db
from .models import CoachAttempt, CoachGame, CoachProfile, CoachReview, User, now
from .security import current_user

router = APIRouter(prefix="/api/coach")


class StartCoach(BaseModel):
    username: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    time_control: str = Field(default="300+5", max_length=32, pattern=r"^[1-9][0-9]{0,4}(\+[0-9]{1,3})?$")

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value):
        return value.lower()

    @field_validator("time_control")
    @classmethod
    def validate_control(cls, value):
        base, _, increment = value.partition("+")
        if not 30 <= int(base) <= 10800 or (increment and not 0 <= int(increment) <= 180):
            raise ValueError("Нужен контроль времени от 30 секунд до 3 часов.")
        # Canonical strings match the public API, e.g. 300 instead of 300+0.
        return base if not increment or int(increment) == 0 else f"{int(base)}+{int(increment)}"


def owned_profile(db: Session, user_id: int) -> CoachProfile:
    profile = db.scalar(select(CoachProfile).where(CoachProfile.user_id == user_id))
    if profile is None:
        raise HTTPException(404, "Сначала запустите личного тренера.")
    return profile


def dashboard(db: Session, profile: CoachProfile | None) -> dict:
    if profile is None:
        return {"profile": None, "skills": [], "priorities": [], "summary": {},
                "methodology": "", "games": [], "active_attempt_id": None}
    from .coach_insights import build_insights
    recent_ids = select(CoachGame.id).where(CoachGame.coach_id == profile.id,
        CoachGame.cohort == "followup", CoachGame.status == "completed").order_by(
        CoachGame.source_game_at.desc(), CoachGame.id.desc()).limit(40)
    # Inspect at most 80 reports. Long-running coaches must not transfer every
    # historical PGN and Stockfish line from PostgreSQL on each browser refresh.
    reports = list(db.scalars(select(CoachGame).where(CoachGame.coach_id == profile.id,
        CoachGame.status == "completed", or_(CoachGame.cohort == "baseline", CoachGame.id.in_(recent_ids)))))
    counters = db.execute(select(CoachGame.cohort, CoachGame.status,
        func.jsonb_array_length(CoachGame.data["moves"]).label("plies"),
        func.jsonb_array_length(CoachGame.moves).label("processed"),
        CoachGame.data["white"].astext.label("white"),
        CoachGame.data["initial_fen"].astext.label("initial_fen"))
        .where(CoachGame.coach_id == profile.id)).all()
    attempt_fields = ("skill_id", "status", "mistakes", "hints_used", "is_new",
                      "created_at", "completed_at", "elapsed_ms")
    attempts = db.execute(select(*(getattr(CoachAttempt, key) for key in attempt_fields))
                          .where(CoachAttempt.coach_id == profile.id)).mappings().all()
    insights = build_insights([
        {"id": game.id, "cohort": game.cohort, "status": game.status, "cursor": game.cursor,
         "data": {**game.data, "username": profile.username}, "moves": game.moves} for game in reports
    ], [dict(attempt) for attempt in attempts])
    insights["summary"].update(tracked_games=len(counters),
        completed_games=sum(item.status == "completed" for item in counters),
        pending_games=sum(item.status != "completed" for item in counters))
    baseline = [item for item in counters if item.cohort == "baseline"]
    followup = [item for item in counters if item.cohort == "followup"]
    total = sum((item.plies + int((item.initial_fen.split()[1] == "w") ==
                                 (item.white.lower() == profile.username))) // 2 for item in counters)
    processed = sum(item.processed for item in counters)
    pending = any(item.status in {"pending", "running"} for item in counters)
    # The game list keeps the fixed baseline accessible even after many new games.
    latest_ids = select(CoachGame.id).where(CoachGame.coach_id == profile.id,
        CoachGame.cohort == "followup").order_by(CoachGame.source_game_at.desc(), CoachGame.id.desc()).limit(200)
    game_rows = db.execute(select(CoachGame.id, CoachGame.cohort, CoachGame.source_game_at,
        CoachGame.status, CoachGame.cursor, CoachGame.error,
        func.jsonb_array_length(CoachGame.data["moves"]).label("total_plies"),
        *(CoachGame.data[key].astext.label(key) for key in ("white", "black", "result", "player_result", "game_url")))
        .where(CoachGame.coach_id == profile.id, or_(CoachGame.cohort == "baseline", CoachGame.id.in_(latest_ids)))
        .order_by(CoachGame.source_game_at.desc(), CoachGame.id.desc())).mappings().all()
    active_id = db.scalar(select(CoachAttempt.id).where(CoachAttempt.coach_id == profile.id,
        CoachAttempt.status == "active").order_by(CoachAttempt.id.desc()).limit(1))
    status = ("paused" if not profile.active else "error" if profile.error else
              "loading" if not profile.baseline_ready else "analysing" if pending else "tracking")
    due = db.scalar(select(func.count()).select_from(CoachReview).where(CoachReview.coach_id == profile.id,
                                                                      CoachReview.due_at <= now()))
    return {**insights, "profile": {
        "id": profile.id, "username": profile.username, "time_control": profile.time_control,
        "started_at": profile.started_at, "active": profile.active, "status": status,
        "baseline_target": 40, "baseline_games": len(baseline),
        "baseline_completed": sum(game.status == "completed" for game in baseline),
        "new_games": len(followup), "new_completed": sum(game.status == "completed" for game in followup),
        "processed_moves": processed, "total_moves": total,
        "progress": round(100 * processed / total, 1) if total else 0,
        "baseline_ready": profile.baseline_ready, "last_sync_at": profile.last_sync_at,
        "next_sync_at": profile.next_sync_at, "error": profile.error or profile.sync_error,
        "syncing": bool(profile.sync_state) or profile.next_sync_at <= now(), "review_due": due,
    }, "games": [dict(game) for game in game_rows], "games_truncated": len(followup) > 200,
        "active_attempt_id": active_id}


@router.get("")
def get_coach(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return dashboard(db, db.scalar(select(CoachProfile).where(CoachProfile.user_id == user.id)))


@router.post("/start")
def start_coach(body: StartCoach, db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    profile = db.scalar(select(CoachProfile).where(CoachProfile.user_id == user.id))
    if profile:
        if (profile.username, profile.time_control) != (body.username, body.time_control):
            from .coach_worker import LOCK_NAMESPACE
            if not db.scalar(select(func.pg_try_advisory_xact_lock(LOCK_NAMESPACE, profile.id))):
                raise HTTPException(409, "Дождитесь завершения текущей загрузки и повторите изменение.")
            db.refresh(profile)
            has_games = db.scalar(select(CoachGame.id).where(CoachGame.coach_id == profile.id).limit(1)) is not None
            has_attempts = db.scalar(select(CoachAttempt.id).where(CoachAttempt.coach_id == profile.id).limit(1)) is not None
            if has_games or has_attempts or not (profile.sync_error or profile.baseline_ready):
                raise HTTPException(409, "Тренер уже ведёт выбранного игрока и контроль. Исходная точка сохранена; продолжите текущий план.")
            # An empty failed setup may be corrected without deleting any history.
            stamp = now()
            profile.username, profile.time_control = body.username, body.time_control
            profile.started_at = profile.next_sync_at = profile.updated_at = stamp
            profile.baseline_ready, profile.active, profile.sync_state = False, True, {}
            profile.last_sync_at = profile.sync_error = profile.error = None
    else:
        stamp = now()
        profile = CoachProfile(user_id=user.id, username=body.username, time_control=body.time_control,
                               started_at=stamp, next_sync_at=stamp, updated_at=stamp)
        db.add(profile)
    db.commit()
    return dashboard(db, profile)


@router.post("/pause")
def pause_coach(db: Session = Depends(get_db), user: User = Depends(current_user)):
    profile = owned_profile(db, user.id)
    # Does not acquire the engine/advisory lock: current bounded step may finish.
    db.execute(update(CoachProfile).where(CoachProfile.id == profile.id).values(active=False))
    db.commit()
    db.refresh(profile)
    return dashboard(db, profile)


@router.post("/resume")
def resume_coach(db: Session = Depends(get_db), user: User = Depends(current_user)):
    profile = owned_profile(db, user.id)
    db.execute(update(CoachProfile).where(CoachProfile.id == profile.id).values(
        active=True, error=None, next_sync_at=now()))
    db.execute(update(CoachGame).where(CoachGame.coach_id == profile.id, CoachGame.status == "failed")
               .values(status="pending", error=None))
    db.commit()
    db.refresh(profile)
    return dashboard(db, profile)


@router.post("/sync")
def sync_coach(db: Session = Depends(get_db), user: User = Depends(current_user)):
    profile = owned_profile(db, user.id)
    if not profile.active:
        raise HTTPException(409, "Тренер на паузе. Нажмите «Продолжить».")
    # Queue only; public API calls run in the durable worker, not this request.
    db.execute(update(CoachProfile).where(CoachProfile.id == profile.id).values(next_sync_at=now()))
    db.commit()
    db.refresh(profile)
    return dashboard(db, profile)


from .coach_training import router as practice_router
router.include_router(practice_router)
