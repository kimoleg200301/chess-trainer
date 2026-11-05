from collections import Counter
from contextlib import asynccontextmanager
import asyncio
from datetime import timedelta
import os
from typing import Literal

import chess
import chess.engine
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, case, delete, exists, func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from .collections import Collection, COLLECTION_NAMES, collection_filter, visible_puzzles
from .background import AnalysisWorker
from .config import settings
from .db import get_db
from .engine_resources import engine_slots
from .frontend import SPAStaticFiles
from .imports_api import router as imports_router
from .models import Attempt, LoginSession, Puzzle, Review, User, now
from .ratings import previously_finished
from .security import create_session, current_user, hash_password, login_limiter, token_hash, verify_password
from .themes import THEMES
from .training import attempt_view, finalize, get_attempt, play_move, record_elapsed, schedule_review, solution
from .transactions import lock_user

@asynccontextmanager
async def lifespan(app: FastAPI):
    worker = AnalysisWorker()
    app.state.analysis_worker = worker
    worker.start()
    try:
        yield
    finally:
        await asyncio.to_thread(worker.stop)


app = FastAPI(title="Tactics Lab API", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
dummy_password_hash = hash_password("nonexistent-user-dummy-password")


@app.middleware("http")
async def request_policy(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin", "").rstrip("/")
        if origin not in settings().allowed_origins:
            return JSONResponse({"detail": "Источник запроса не разрешён."}, status_code=403)
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > 8192):
            return JSONResponse({"detail": "Слишком большой запрос."}, status_code=413)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return JSONResponse({"detail": "Проверьте параметры запроса."}, status_code=422)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=512)


class StartBody(BaseModel):
    collection: Collection = "lichess"
    puzzle_id: str | None = Field(default=None, min_length=1, max_length=32)
    exclude_attempt_id: int | None = Field(default=None, strict=True, ge=1, le=2**31 - 1)
    theme: str | None = Field(default=None, max_length=64)
    min_rating: int = Field(default=800, ge=0, le=4000)
    max_rating: int = Field(default=1800, ge=0, le=4000)
    mode: Literal["practice", "review"] = "practice"
    # Old clients keep their explicit filters. The current UI opts into Elo selection.
    adaptive: bool = False

    @model_validator(mode="after")
    def check_range(self):
        if not self.adaptive and self.min_rating > self.max_rating:
            raise ValueError("Invalid range")
        return self


class TimingBody(BaseModel):
    elapsed_ms: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)


class TimeBody(BaseModel):
    elapsed_ms: int = Field(strict=True, ge=0, le=2**53 - 1)


class MoveBody(TimingBody):
    move: str = Field(min_length=4, max_length=5, pattern=r"^[a-h][1-8][a-h][1-8][qrbn]?$")


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        raise HTTPException(503, "База данных недоступна.")
    return {"status": "ok", "database": "ok", "engine_available": os.access(settings().stockfish_path, os.X_OK)}


@app.post("/api/auth/login")
def login(body: LoginBody, request: Request, response: Response, db: Session = Depends(get_db)):
    login_limiter.check(request.client.host if request.client else "unknown", body.username)
    user = db.scalar(select(User).where(User.username == body.username))
    valid = verify_password(body.password, user.password_hash if user else dummy_password_hash)
    if not user or not valid:
        raise HTTPException(401, "Неверное имя пользователя или пароль.")
    old = request.cookies.get(settings().cookie_name)
    if old:
        db.execute(delete(LoginSession).where(LoginSession.token_hash == token_hash(old)))
    create_session(db, user, response)
    db.commit()
    return {"username": user.username}


@app.get("/api/auth/me")
def me(user: User = Depends(current_user)):
    return {"username": user.username}


@app.post("/api/auth/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db), user: User = Depends(current_user)):
    token = request.cookies.get(settings().cookie_name, "")
    db.execute(delete(LoginSession).where(LoginSession.token_hash == token_hash(token)))
    db.commit()
    response.delete_cookie(settings().cookie_name, path="/", secure=settings().cookie_secure, httponly=True, samesite="lax")


@app.get("/api/meta")
def meta(collection: Collection = "lichess", db: Session = Depends(get_db), user: User = Depends(current_user)):
    scope = collection_filter(user.id, collection)
    total, low, high = db.execute(select(func.count(), func.min(Puzzle.rating), func.max(Puzzle.rating)).select_from(Puzzle).where(scope)).one()
    counts = Counter(theme for row in db.scalars(select(Puzzle.themes).where(scope)) for theme in row if theme in THEMES)
    collection_counts = dict(db.execute(select(Puzzle.collection, func.count()).where(visible_puzzles(user.id)).group_by(Puzzle.collection)).all())
    return {"total_puzzles": total, "min_rating": low or 0, "max_rating": high or 4000,
            "themes": [{"id": theme, "name": THEMES[theme], "count": count} for theme, count in counts.most_common()],
            "collections": [{"id": key, "name": name, "count": sum(collection_counts.values()) if key == "all" else collection_counts.get(key, 0)} for key, name in COLLECTION_NAMES.items()]}


def started_attempt_view(attempt: Attempt, *, resumed: bool) -> dict:
    # A resumed attempt may be midway through a combination. Replays still
    # begin at the original learner position rather than its current FEN.
    initial = chess.Board(attempt.puzzle.initial_fen)
    initial.push_uci(attempt.puzzle.moves[0])
    return {**attempt_view(attempt), "starting_fen": initial.fen(), "resumed": resumed}


@app.get("/api/training/current")
def current_training(db: Session = Depends(get_db), user: User = Depends(current_user)):
    attempt = db.scalar(select(Attempt).join(Puzzle, Puzzle.id == Attempt.puzzle_id).where(
        Attempt.user_id == user.id, visible_puzzles(user.id)).order_by(
        func.coalesce(Attempt.last_opened_at, Attempt.created_at).desc(), Attempt.id.desc()).limit(1))
    if attempt is None:
        return {"attempt": None, "solution": [], "game_url": ""}
    # Never disclose the solution while the saved task is still active.
    return {"attempt": started_attempt_view(attempt, resumed=True),
            "solution": solution(attempt.puzzle) if attempt.status != "active" else [],
            "game_url": attempt.puzzle.game_url if attempt.status != "active" else ""}


@app.post("/api/training/{attempt_id}/resume")
def resume_training(attempt_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    attempt = get_attempt(db, attempt_id, user.id, active=False)
    attempt.last_opened_at = now()
    db.commit()
    return {"attempt": started_attempt_view(attempt, resumed=True),
            "solution": solution(attempt.puzzle) if attempt.status != "active" else [],
            "game_url": attempt.puzzle.game_url if attempt.status != "active" else ""}


@app.post("/api/training/start")
def start(body: StartBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    # Also serialize against an administrative restore into a previously empty account.
    user = lock_user(db, user.id)
    options = body.model_dump(exclude={"puzzle_id", "exclude_attempt_id"})
    if body.puzzle_id:
        # An explicit puzzle link is an intentional replay, as is review mode.
        puzzle = db.scalar(select(Puzzle).where(Puzzle.id == body.puzzle_id, visible_puzzles(user.id)))
        if not puzzle:
            raise HTTPException(404, "Задача не найдена в доступных базах.")
    else:
        if body.theme and body.theme not in THEMES:
            raise HTTPException(422, "Неизвестный тактический мотив.")
        matching = select(Puzzle).where(collection_filter(user.id, body.collection))
        if not body.adaptive:
            matching = matching.where(Puzzle.rating.between(body.min_rating, body.max_rating))
        if body.theme:
            matching = matching.where(Puzzle.themes.contains([body.theme]))
        query = matching
        if body.mode == "review":
            query = query.join(Review, Review.puzzle_id == Puzzle.id).where(Review.user_id == user.id, Review.due_at <= now())
            query = query.order_by(Review.due_at, func.random())
        else:
            previous_puzzle = aliased(Puzzle)
            finalized = (select(Attempt.id).join(previous_puzzle, Attempt.puzzle_id == previous_puzzle.id)
                         .where(Attempt.user_id == user.id, Attempt.status.in_(["completed", "revealed"]),
                                or_(Attempt.puzzle_id == Puzzle.id,
                                    and_(Puzzle.collection == "personal", Puzzle.game_url != "",
                                         Puzzle.source_ply.is_not(None), previous_puzzle.collection == "personal",
                                         previous_puzzle.owner_id == user.id,
                                         previous_puzzle.game_url == Puzzle.game_url,
                                         previous_puzzle.source_ply == Puzzle.source_ply)))
                         .correlate(Puzzle).exists())
            # Finished personal moments never cycle through ordinary practice,
            # including all-collection mode or a newly calculated solution ID
            # for the same source game and ply.
            query = query.where(or_(Puzzle.collection != "personal", ~finalized))
            if body.exclude_attempt_id is not None:
                current_puzzle = db.scalar(select(Attempt.puzzle_id).where(
                    Attempt.id == body.exclude_attempt_id, Attempt.user_id == user.id,
                    Attempt.status == "active"))
                if current_puzzle is not None:
                    # Only the caller's active personal task is skipped. Never
                    # let another user's attempt ID affect visible selection.
                    query = query.where(or_(Puzzle.collection != "personal", Puzzle.id != current_puzzle))
            seen = exists().where(Attempt.puzzle_id == Puzzle.id, Attempt.user_id == user.id)
            if body.adaptive:
                query = query.order_by(seen, func.abs(Puzzle.rating - user.puzzle_rating))
            if body.collection == "personal":
                undated_created = case((Puzzle.source_game_at.is_(None), Puzzle.created_at))
                query = query.order_by(seen,
                                       Puzzle.source_game_at.desc().nulls_last(),
                                       undated_created.desc().nulls_last(),
                                       Puzzle.game_url, Puzzle.source_ply.asc().nulls_last(), Puzzle.id)
            else:
                query = query.order_by(seen, func.random())
        if body.adaptive and body.mode == "practice":
            active_query = select(Attempt).where(
                Attempt.user_id == user.id, Attempt.status == "active",
                Attempt.puzzle_id.in_(query.order_by(None).with_only_columns(Puzzle.id)))
            if body.exclude_attempt_id is not None:
                active_query = active_query.where(Attempt.id != body.exclude_attempt_id)
            active = db.scalar(active_query.order_by(
                func.coalesce(Attempt.last_opened_at, Attempt.created_at).desc(), Attempt.id.desc()).limit(1))
            if active:
                active.training_options = options
                active.last_opened_at = now()
                db.commit()
                return started_attempt_view(active, resumed=True)
        puzzle = db.scalar(query.limit(1))
        if not puzzle:
            if body.mode == "practice" and body.collection in {"personal", "all"}:
                matching_personal = matching.where(Puzzle.collection == "personal")
                if db.scalar(matching_personal.with_only_columns(Puzzle.id).limit(1)):
                    unfinished = db.scalar(matching_personal.where(~finalized).with_only_columns(Puzzle.id).limit(1))
                    if unfinished:
                        return JSONResponse({"detail": "Новых личных задач с такими параметрами пока нет. Можно завершить текущую позицию или вернуться к незавершённым задачам на главной.",
                                             "code": "personal_current_remaining"}, status_code=404)
                    return JSONResponse({"detail": "Все личные задачи с такими параметрами уже пройдены. Пополнить коллекцию можно в разделе «Мои партии», а повторить пройденные — в режиме «Повторение».",
                                         "code": "personal_collection_complete"}, status_code=404)
            raise HTTPException(404, "Нет задач с такими параметрами." if body.mode == "practice" else "Пока нет задач для повторения с такими параметрами.")
        if body.mode == "practice" and puzzle.collection == "personal":
            active = db.scalar(select(Attempt).where(Attempt.user_id == user.id, Attempt.puzzle_id == puzzle.id,
                                                     Attempt.status == "active").order_by(Attempt.id.desc()).limit(1))
            if active:
                active.last_opened_at = now()
                db.commit()
                return started_attempt_view(active, resumed=True)
    board = chess.Board(puzzle.initial_fen)
    board.push_uci(puzzle.moves[0])
    attempt = Attempt(user_id=user.id, puzzle=puzzle, puzzle_id=puzzle.id, fen=board.fen(),
                      learner_white=board.turn, last_move=puzzle.moves[0], training_options=options,
                      rated=body.adaptive and body.mode == "practice" and not body.puzzle_id
                      and not previously_finished(db, user.id, puzzle))
    db.add(attempt)
    db.commit()
    return started_attempt_view(attempt, resumed=False)


@app.post("/api/training/{attempt_id}/move")
def move(attempt_id: int, body: MoveBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    attempt = get_attempt(db, attempt_id, user.id)
    attempt.last_opened_at = now()
    record_elapsed(attempt, body.elapsed_ms)
    return play_move(db, attempt, body.move)


@app.post("/api/training/{attempt_id}/hint")
def hint(attempt_id: int, body: TimingBody | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    attempt = get_attempt(db, attempt_id, user.id)
    attempt.last_opened_at = now()
    record_elapsed(attempt, body.elapsed_ms if body else None)
    if not attempt.hints_used:
        schedule_review(db, attempt)
    attempt.hints_used = 1
    source = attempt.puzzle.moves[attempt.ply_index][:2]
    db.commit()
    return {"message": f"Присмотритесь к фигуре на поле {source}.", "from_square": source, "attempt": attempt_view(attempt)}


@app.post("/api/training/{attempt_id}/reveal")
def reveal(attempt_id: int, body: TimingBody | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    attempt = get_attempt(db, attempt_id, user.id, active=False)
    if attempt.status == "active":
        record_elapsed(attempt, body.elapsed_ms if body else None)
        finalize(db, attempt, "revealed")
    attempt.last_opened_at = now()
    db.commit()
    return {"attempt": attempt_view(attempt), "solution": solution(attempt.puzzle), "game_url": attempt.puzzle.game_url}


@app.post("/api/training/{attempt_id}/time")
def save_time(attempt_id: int, body: TimeBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    attempt = get_attempt(db, attempt_id, user.id, active=False)
    record_elapsed(attempt, body.elapsed_ms)
    db.commit()
    return attempt_view(attempt)


@app.get("/api/training/{attempt_id}/analysis")
def analysis(attempt_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    attempt = db.scalar(select(Attempt).where(Attempt.id == attempt_id, Attempt.user_id == user.id))
    if not attempt:
        raise HTTPException(404, "Тренировка не найдена.")
    if attempt.status == "active":
        raise HTTPException(403, "Анализ доступен после решения или показа ответа.")
    if not engine_slots.acquire(blocking=False):
        raise HTTPException(503, "Движок занят. Повторите запрос через несколько секунд.")
    try:
        board = chess.Board(attempt.fen)
        with chess.engine.SimpleEngine.popen_uci(settings().stockfish_path, timeout=5) as engine:
            engine.configure({"Threads": 1, "Hash": 32})
            info = engine.analyse(board, chess.engine.Limit(time=0.4), info=chess.engine.INFO_SCORE | chess.engine.INFO_PV)
            score = info["score"].white()
            evaluation = f"Мат: {score.mate()}" if score.is_mate() else f"{score.score() / 100:+.2f}"
            line = []
            for move in info.get("pv", [])[:12]:
                line.append(board.san(move))
                board.push(move)
            return {"evaluation": evaluation, "line": line, "engine": engine.id.get("name", "Stockfish")}
    except (OSError, chess.engine.EngineError, TimeoutError):
        raise HTTPException(503, "Stockfish сейчас недоступен. Решение задачи сохранено.")
    finally:
        engine_slots.release()


@app.get("/api/stats")
def stats(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = list(db.scalars(select(Attempt).where(Attempt.user_id == user.id).order_by(Attempt.created_at.desc())))
    completed = [row for row in rows if row.completed_at is not None]
    is_clean = lambda a: a.status == "completed" and a.mistakes == 0 and a.hints_used == 0
    clean = sum(is_clean(row) for row in completed)
    measured_solutions = [row.elapsed_ms for row in completed if row.status == "completed" and row.elapsed_ms is not None]
    streak = 0
    for row in sorted(completed, key=lambda a: a.completed_at, reverse=True):
        if not is_clean(row):
            break
        streak += 1
    theme_counts = {}
    for row in completed:
        for theme in row.puzzle.themes:
            if theme not in THEMES:
                continue
            item = theme_counts.setdefault(theme, {"id": theme, "name": THEMES[theme], "attempts": 0, "solved": 0})
            item["attempts"] += 1
            item["solved"] += row.status == "completed"
    due = db.scalar(select(func.count()).select_from(Review).where(Review.user_id == user.id, Review.due_at <= now()))
    return {"solved": sum(row.status == "completed" for row in completed), "total_attempts": len(rows),
            "puzzle_rating": user.puzzle_rating, "rated_attempts": user.rated_attempts,
            "accuracy": round(100 * clean / len(completed), 1) if completed else 0,
            "streak": streak, "due_count": due,
            "average_solution_ms": round(sum(measured_solutions) / len(measured_solutions)) if measured_solutions else None,
            "timed_solutions": len(measured_solutions),
            "by_theme": sorted(theme_counts.values(), key=lambda item: -item["attempts"]),
            "recent": [{"puzzle_id": row.puzzle_id, "rating": (row.rating_change or {}).get("puzzle_before", row.puzzle.rating), "status": row.status,
                        "rating_change": row.rating_change,
                        "created_at": row.created_at.isoformat(), "elapsed_ms": row.elapsed_ms,
                        "collection": row.puzzle.collection, "rating_estimated": row.puzzle.rating_estimated} for row in rows[:10]]}


@app.get("/api/progress/export")
def export_progress(db: Session = Depends(get_db), user: User = Depends(current_user)):
    from .progress import export_snapshot
    snapshot = export_snapshot(db, user)
    return JSONResponse(snapshot.model_dump(mode="json"), headers={
        "Content-Disposition": f'attachment; filename="tactics-progress-{now().date()}.json"'
    })


app.include_router(imports_router)

from .game_analysis_api import router as game_analysis_router
app.include_router(game_analysis_router)

from .coach_api import router as coach_router
app.include_router(coach_router)

from .benchmark_api import router as benchmark_router
app.include_router(benchmark_router)


@app.api_route("/api/{unmatched:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def api_not_found(unmatched: str):
    raise HTTPException(404, "Метод API не найден.")


# Optional same-process frontend for the single-service Render trial.
# API routes above always win; standalone backend behavior is unchanged by default.
frontend_dist = os.environ.get("FRONTEND_DIST")
if frontend_dist:
    app.mount("/", SPAStaticFiles(directory=frontend_dist, html=True), name="frontend")
