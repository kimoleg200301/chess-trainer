from collections import Counter
from datetime import timedelta
import os
import threading
from typing import Literal

import chess
import chess.engine
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, delete, exists, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import Attempt, LoginSession, Puzzle, Review, User, now
from .security import create_session, current_user, hash_password, login_limiter, token_hash, verify_password
from .themes import THEMES
from .training import attempt_view, finalize, get_attempt, play_move, schedule_review, solution

app = FastAPI(title="Tactics Lab API", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
engine_slots = threading.BoundedSemaphore(1)
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
    theme: str | None = Field(default=None, max_length=64)
    min_rating: int = Field(default=800, ge=0, le=4000)
    max_rating: int = Field(default=1800, ge=0, le=4000)
    mode: Literal["practice", "review"] = "practice"

    @model_validator(mode="after")
    def check_range(self):
        if self.min_rating > self.max_rating:
            raise ValueError("Invalid range")
        return self


class MoveBody(BaseModel):
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
def meta(db: Session = Depends(get_db), user: User = Depends(current_user)):
    total, low, high = db.execute(select(func.count(), func.min(Puzzle.rating), func.max(Puzzle.rating)).select_from(Puzzle)).one()
    counts = Counter(theme for row in db.scalars(select(Puzzle.themes)) for theme in row if theme in THEMES)
    return {"total_puzzles": total, "min_rating": low or 0, "max_rating": high or 4000,
            "themes": [{"id": theme, "name": THEMES[theme], "count": count} for theme, count in counts.most_common()]}


@app.post("/api/training/start")
def start(body: StartBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    # Also serialize against an administrative restore into a previously empty account.
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    if body.theme and body.theme not in THEMES:
        raise HTTPException(422, "Неизвестный тактический мотив.")
    query = select(Puzzle).where(Puzzle.rating.between(body.min_rating, body.max_rating))
    if body.theme:
        query = query.where(Puzzle.themes.contains([body.theme]))
    if body.mode == "review":
        query = query.join(Review, Review.puzzle_id == Puzzle.id).where(Review.user_id == user.id, Review.due_at <= now())
        query = query.order_by(Review.due_at, func.random())
    else:
        seen = exists().where(Attempt.puzzle_id == Puzzle.id, Attempt.user_id == user.id)
        # Prefer unseen puzzles. Once exhausted, allow practice with familiar material.
        query = query.order_by(seen, func.random())
    puzzle = db.scalar(query.limit(1))
    if not puzzle:
        raise HTTPException(404, "Нет задач с такими параметрами." if body.mode == "practice" else "Пока нет задач для повторения с такими параметрами.")
    board = chess.Board(puzzle.initial_fen)
    board.push_uci(puzzle.moves[0])
    attempt = Attempt(user_id=user.id, puzzle=puzzle, puzzle_id=puzzle.id, fen=board.fen(),
                      learner_white=board.turn, last_move=puzzle.moves[0])
    db.add(attempt)
    db.commit()
    return attempt_view(attempt)


@app.post("/api/training/{attempt_id}/move")
def move(attempt_id: int, body: MoveBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    attempt = get_attempt(db, attempt_id, user.id)
    return play_move(db, attempt, body.move)


@app.post("/api/training/{attempt_id}/hint")
def hint(attempt_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    attempt = get_attempt(db, attempt_id, user.id)
    if not attempt.hints_used:
        schedule_review(db, attempt)
    attempt.hints_used = 1
    source = attempt.puzzle.moves[attempt.ply_index][:2]
    db.commit()
    return {"message": f"Присмотритесь к фигуре на поле {source}.", "from_square": source, "attempt": attempt_view(attempt)}


@app.post("/api/training/{attempt_id}/reveal")
def reveal(attempt_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    attempt = get_attempt(db, attempt_id, user.id, active=False)
    if attempt.status == "active":
        finalize(db, attempt, "revealed")
        db.commit()
    return {"attempt": attempt_view(attempt), "solution": solution(attempt.puzzle), "game_url": attempt.puzzle.game_url}


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
            "accuracy": round(100 * clean / len(completed), 1) if completed else 0,
            "streak": streak, "due_count": due,
            "by_theme": sorted(theme_counts.values(), key=lambda item: -item["attempts"]),
            "recent": [{"puzzle_id": row.puzzle_id, "rating": row.puzzle.rating, "status": row.status,
                        "created_at": row.created_at.isoformat()} for row in rows[:10]]}


@app.get("/api/progress/export")
def export_progress(db: Session = Depends(get_db), user: User = Depends(current_user)):
    from .progress import export_snapshot
    snapshot = export_snapshot(db, user)
    return JSONResponse(snapshot.model_dump(mode="json"), headers={
        "Content-Disposition": f'attachment; filename="tactics-progress-{now().date()}.json"'
    })


@app.api_route("/api/{unmatched:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def api_not_found(unmatched: str):
    raise HTTPException(404, "Метод API не найден.")


# Optional same-process frontend for the single-service Render trial.
# API routes above always win; standalone backend behavior is unchanged by default.
frontend_dist = os.environ.get("FRONTEND_DIST")
if frontend_dist:
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
