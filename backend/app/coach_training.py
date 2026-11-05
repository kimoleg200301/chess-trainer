"""Practice isolated from game evidence: repetition never proves game mastery."""
from datetime import timedelta

import chess
import chess.engine
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .engine_resources import engine_slots
from .models import CoachAttempt, CoachGame, CoachProfile, CoachReview, Puzzle, User, now
from .security import current_user
from .transactions import lock_user

router = APIRouter()
MOVE_CHECK_VERSION = 1
MOVE_CHECK_HISTORY_LIMIT = 32


class PracticeBody(BaseModel):
    skill_id: str | None = Field(default=None, max_length=40)
    game_id: int | None = Field(default=None, strict=True, gt=0)
    ply: int | None = Field(default=None, strict=True, ge=0)
    exclude_attempt_id: int | None = Field(default=None, strict=True, gt=0)

    @model_validator(mode="after")
    def complete_moment(self):
        if (self.game_id is None) != (self.ply is None):
            raise ValueError("Нужна партия и номер хода.")
        return self


class TimingBody(BaseModel):
    elapsed_ms: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)


class MoveBody(TimingBody):
    move: str = Field(min_length=4, max_length=5, pattern=r"^[a-h][1-8][a-h][1-8][qrbn]?$")


def owned_attempt(db: Session, attempt_id: int, user_id: int, *, mutate: bool = False) -> CoachAttempt:
    if mutate:
        lock_user(db, user_id, nowait=True)
    query = select(CoachAttempt).join(CoachProfile).where(CoachAttempt.id == attempt_id,
                                                         CoachProfile.user_id == user_id)
    if mutate:
        query = query.with_for_update(of=CoachAttempt)
    attempt = db.scalar(query)
    if attempt is None:
        raise HTTPException(404, "Занятие не найдено.")
    return attempt


def record_time(attempt: CoachAttempt, elapsed_ms: int | None):
    if attempt.status == "active" and elapsed_ms is not None:
        attempt.elapsed_ms = max(attempt.elapsed_ms or 0, elapsed_ms)


def _solution(attempt: CoachAttempt):
    board = chess.Board(attempt.starting_fen)
    reference = attempt.payload.get("moves", [])
    moves, kind, reference_index = reference, "reference", 0
    legacy_alternative = False
    if attempt.status == "completed":
        if attempt.payload.get("accepted_moves") and attempt.payload.get("accepted_uci"):
            moves, kind = attempt.payload["accepted_moves"], "accepted"
            reference_index = attempt.payload.get("accepted_ply", 0)
        elif attempt.kind == "decision" and reference:
            # Old completed attempts only saved the accepted move and final FEN.
            # They cannot truthfully replay a continuation of a different move.
            try:
                move = chess.Move.from_uci(attempt.payload.get("last_move", ""))
                after = board.copy()
                if move in board.legal_moves and move.uci() != reference[0]:
                    after.push(move)
                    if after.fen() == attempt.fen:
                        moves, kind, legacy_alternative = [move.uci()], "accepted", True
            except (TypeError, ValueError):
                pass
    reference_move = None
    reference_board = board.copy()
    try:
        for uci in reference[:reference_index]:
            reference_board.push_uci(uci)
        if reference_index < len(reference):
            move = chess.Move.from_uci(reference[reference_index])
            if move in reference_board.legal_moves:
                reference_move = {"uci": move.uci(), "san": reference_board.san(move)}
    except (TypeError, ValueError):
        pass
    line = []
    for uci in moves:
        try:
            move = chess.Move.from_uci(uci)
        except (TypeError, ValueError):
            break
        if move not in board.legal_moves:
            break
        san = board.san(move)
        board.push(move)
        line.append({"uci": uci, "san": san, "fen": board.fen()})
    explanation = attempt.payload.get("explanation") or "Разберите продолжение и повторите идею в новой позиции."
    if kind == "accepted":
        accepted_san = line[reference_index]["san"] if reference_index < len(line) else ""
        explanation = f"Принят ваш ход {accepted_san}. "
        if legacy_alternative:
            explanation += "Продолжение этой старой попытки не сохранилось; показан только сыгранный ход."
        else:
            explanation += "В разборе показано проверенное продолжение вашего хода." if len(line) > reference_index + 1 else "В разборе показан ваш ход."
    result = {"initial_fen": attempt.starting_fen, "moves": line, "explanation": explanation, "kind": kind}
    if reference_move is not None:
        result["reference_move"] = reference_move
    return result


def attempt_view(attempt: CoachAttempt) -> dict:
    from .coach_insights import SKILLS
    skill = SKILLS[attempt.skill_id]
    solution = _solution(attempt) if attempt.status != "active" else None
    feedback = attempt.payload.get("feedback", "")
    if solution and solution["kind"] == "accepted" and not attempt.payload.get("accepted_moves"):
        feedback = "Ваш ход принят. В разборе показан ваш сыгранный ход."
    return {
        "id": attempt.id, "skill_id": attempt.skill_id, "skill_title": skill["title"],
        "kind": attempt.kind, "fen": attempt.fen, "starting_fen": attempt.starting_fen,
        "learner_white": attempt.learner_white, "status": attempt.status,
        "mistakes": attempt.mistakes, "hints_used": attempt.hints_used,
        "elapsed_ms": attempt.elapsed_ms, "last_move": attempt.payload.get("last_move"),
        "ply_index": attempt.ply_index,
        "total_plies": 1 if attempt.kind == "decision" else len(attempt.payload["moves"]),
        "prompt": ("Найдите сильное решение в позиции из вашей партии. Допускаются равноценные ходы."
                   if attempt.kind == "decision" else "Найдите и проведите тактическое продолжение."),
        "source": {"game_url": attempt.payload.get("game_url", ""),
                   "move_number": attempt.payload.get("move_number"), "own_game": attempt.kind == "decision"},
        "feedback": feedback,
        "solution": solution,
        "target_seconds": 30 if attempt.skill_id == "time_management" else None,
        "is_new": attempt.is_new,
    }


def _new_attempt(db: Session, profile: CoachProfile, skill_id: str, key: str, kind: str,
                 fen: str, payload: dict, *, game_id=None, puzzle_id=None) -> CoachAttempt:
    # A moment can train more than one skill. Keep one live attempt across all
    # those entry points, including a review selected under a different skill.
    active = db.scalar(select(CoachAttempt).where(CoachAttempt.coach_id == profile.id,
        CoachAttempt.task_key == key, CoachAttempt.status == "active").order_by(CoachAttempt.id.desc()).limit(1))
    if active is not None:
        return active
    seen = db.scalar(select(CoachAttempt.id).where(CoachAttempt.coach_id == profile.id,
                                                  CoachAttempt.task_key == key).limit(1))
    # Repetitions retain the canonical task, never a past learner's alternative
    # or the diagnostic history belonging to that earlier attempt.
    payload = {key: value for key, value in payload.items()
               if not key.startswith("accepted_") and key != "move_checks"}
    attempt = CoachAttempt(coach_id=profile.id, skill_id=skill_id, task_key=key, kind=kind,
                           game_id=game_id, puzzle_id=puzzle_id, payload=payload,
                           fen=fen, starting_fen=fen, learner_white=chess.Board(fen).turn,
                           is_new=seen is None)
    db.add(attempt)
    db.commit()
    return attempt


def _moment_payload(game: CoachGame, row: dict) -> dict | None:
    if (not row.get("assessed") or row.get("category") == "forced"
            or not row.get("best_uci") or not row.get("eligible")
            or not row.get("skill_ids")):
        return None
    board = chess.Board(row["before_fen"])
    line = row.get("best_line_uci") or [row["best_uci"]]
    if not line or line[0] != row["best_uci"] or chess.Move.from_uci(line[0]) not in board.legal_moves:
        return None
    return {"moves": line, "game_url": game.data.get("game_url", ""),
            "move_number": row["move_number"], "explanation": row.get("explanation") or row.get("reason"),
            "last_move": game.data["moves"][row["ply"] - 1] if row["ply"] else None,
            "best_uci": row["best_uci"], "original_move": row.get("played_uci"),
            "original_score": row.get("score_after"), "depth": row.get("depth", 0),
            "game_initial_fen": game.data["initial_fen"], "prefix_moves": game.data["moves"][:row["ply"]]}


def candidate_games(db: Session, profile_id: int, game_id: int | None):
    """Visit saved positions without materializing years of complete PGNs at once."""
    query = select(CoachGame).where(CoachGame.coach_id == profile_id)
    if game_id is not None:
        game = db.scalar(query.where(CoachGame.id == game_id))
        if game is None:
            raise HTTPException(404, "Партия не найдена у вашего тренера.")
        yield game
        return
    cursor = None
    while True:
        batch_query = query
        if cursor:
            stamp, previous_id = cursor
            batch_query = batch_query.where(or_(CoachGame.source_game_at < stamp,
                (CoachGame.source_game_at == stamp) & (CoachGame.id < previous_id)))
        batch = list(db.scalars(batch_query.order_by(CoachGame.source_game_at.desc(), CoachGame.id.desc()).limit(10)))
        if not batch:
            return
        for game in batch:
            yield game
        cursor = batch[-1].source_game_at, batch[-1].id


@router.post("/practice")
def start_practice(body: PracticeBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from .coach_api import dashboard, owned_profile
    from .coach_insights import SKILLS
    lock_user(db, user.id, nowait=True)
    profile = owned_profile(db, user.id)
    if db.scalar(select(CoachGame.id).where(CoachGame.coach_id == profile.id).limit(1)) is None:
        raise HTTPException(409, "Сначала дождитесь загрузки хотя бы одной партии. Если выбранный профиль или контроль неверен, исправьте настройки.")
    skill_id = body.skill_id
    if skill_id is not None and skill_id not in SKILLS:
        raise HTTPException(422, "Неизвестное направление тренировки.")
    if skill_id is None:
        priorities = dashboard(db, profile).get("priorities", [])
        skill_id = priorities[0] if priorities else "calculation"
    active = db.scalar(select(CoachAttempt).where(CoachAttempt.coach_id == profile.id,
        CoachAttempt.skill_id == skill_id, CoachAttempt.status == "active",
        CoachAttempt.id != body.exclude_attempt_id if body.exclude_attempt_id else True)
        .order_by(CoachAttempt.id.desc()).limit(1))
    if active is not None and body.game_id is None:
        return attempt_view(active)
    excluded = db.scalar(select(CoachAttempt.task_key).where(CoachAttempt.coach_id == profile.id,
        CoachAttempt.id == body.exclude_attempt_id)) if body.exclude_attempt_id else None
    seen_keys = set(db.scalars(select(CoachAttempt.task_key).where(CoachAttempt.coach_id == profile.id).distinct()))
    due = list(db.scalars(select(CoachReview).where(CoachReview.coach_id == profile.id,
        CoachReview.skill_id == skill_id, CoachReview.due_at <= now()).order_by(CoachReview.due_at)))
    if body.game_id is None:
        for review in due:
            previous = db.scalar(select(CoachAttempt).where(CoachAttempt.coach_id == profile.id,
                CoachAttempt.task_key == review.task_key).order_by(CoachAttempt.id.desc()).limit(1))
            if previous is None or previous.task_key == excluded:
                continue
            return attempt_view(_new_attempt(db, profile, skill_id, previous.task_key, previous.kind,
                previous.starting_fen, {**previous.payload, "feedback": "", "last_move": None},
                game_id=previous.game_id, puzzle_id=previous.puzzle_id))
    for game in candidate_games(db, profile.id, body.game_id):
        for row in sorted(game.moves, key=lambda item: -float(item.get("expected_loss") or 0)):
            if body.ply is not None and row["ply"] != body.ply:
                continue
            if skill_id not in row.get("skill_ids", []):
                continue
            key = f"game:{game.fingerprint}:{row['ply']}"
            if key == excluded or (body.game_id is None and key in seen_keys):
                continue
            payload = _moment_payload(game, row)
            if payload is None:
                continue
            return attempt_view(_new_attempt(db, profile, skill_id, key, "decision",
                                             row["before_fen"], payload, game_id=game.id))
    if body.game_id is not None:
        raise HTTPException(422, "Эта позиция пока не подтверждена для выбранного упражнения.")
    themes = SKILLS[skill_id]["themes"]
    # Adapt to first encounters only; successful memory recall cannot inflate difficulty.
    fresh = db.execute(select(CoachAttempt.status, CoachAttempt.mistakes, CoachAttempt.hints_used).where(
        CoachAttempt.coach_id == profile.id, CoachAttempt.skill_id == skill_id, CoachAttempt.is_new.is_(True),
        CoachAttempt.completed_at.is_not(None)).order_by(CoachAttempt.id.desc()).limit(12)).all()
    clean = sum(item.status == "completed" and item.mistakes == 0 and item.hints_used == 0 for item in fresh)
    target_rating = min(user.puzzle_rating, 1200)
    if len(fresh) >= 4:
        target_rating = max(400, min(2000, target_rating + (200 if clean / len(fresh) > .8 else -200 if clean / len(fresh) < .5 else 0)))
    puzzle_query = select(Puzzle).where(Puzzle.collection == "lichess")
    if themes:
        puzzle_query = puzzle_query.where(or_(*(Puzzle.themes.contains([theme]) for theme in themes)))
    for puzzle in db.scalars(puzzle_query.order_by(func.abs(Puzzle.rating - target_rating), Puzzle.id)):
        key = f"puzzle:{puzzle.id}"
        if key in seen_keys or key == excluded:
            continue
        board = chess.Board(puzzle.initial_fen)
        try:
            board.push_uci(puzzle.moves[0])
        except ValueError:
            continue
        payload = {"moves": puzzle.moves[1:], "game_url": puzzle.game_url,
                   "move_number": board.fullmove_number, "explanation": SKILLS[skill_id]["practice_tip"],
                   "last_move": puzzle.moves[0]}
        return attempt_view(_new_attempt(db, profile, skill_id, key, "puzzle", board.fen(), payload, puzzle_id=puzzle.id))
    raise HTTPException(404, "Новых упражнений по этой теме пока нет. Вернитесь к повторению позже или выберите другое направление.")


@router.get("/practice/{attempt_id}")
def get_practice(attempt_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return attempt_view(owned_attempt(db, attempt_id, user.id))


def finish(db: Session, attempt: CoachAttempt, status: str):
    attempt.status, attempt.completed_at = status, now()
    clean = status == "completed" and attempt.mistakes == 0 and attempt.hints_used == 0
    review = db.scalar(select(CoachReview).where(CoachReview.coach_id == attempt.coach_id,
                                                CoachReview.task_key == attempt.task_key))
    if review is None:
        review = CoachReview(coach_id=attempt.coach_id, task_key=attempt.task_key,
                             skill_id=attempt.skill_id, successes=0)
        db.add(review)
    review.successes = review.successes + 1 if clean else 0
    days = (1, 3, 7, 14, 30, 60)
    review.due_at = now() + (timedelta(days=days[min(review.successes - 1, len(days) - 1)])
                             if clean else timedelta(minutes=10))


def _alternative(attempt: CoachAttempt, move: str) -> tuple[bool, dict]:
    from .game_analysis_engine import ANALYSIS_ENGINE_OPTIONS, analyse_game_move
    if not engine_slots.acquire(blocking=False):
        raise HTTPException(503, "Движок занят анализом. Повторите ход через несколько секунд.")
    try:
        with chess.engine.SimpleEngine.popen_uci(settings().stockfish_path, timeout=5) as engine:
            engine.configure(ANALYSIS_ENGINE_OPTIONS)
            prefix = attempt.payload.get("prefix_moves", [])
            report = analyse_game_move({"initial_fen": attempt.payload.get("game_initial_fen", attempt.fen),
                                        "moves": [*prefix, move]}, len(prefix), engine)
            report["engine_name"] = getattr(engine, "id", {}).get("name")
        if not report.get("assessed") or report.get("expected_loss") is None:
            raise HTTPException(503, "Не удалось надёжно проверить этот вариант. Попробуйте ещё раз; ошибка не засчитана.")
        played_line = report.get("played_line_uci")
        if not isinstance(played_line, list) or not played_line or played_line[0] != move:
            raise HTTPException(503, "Не удалось сохранить проверенное продолжение этого хода. Повторите проверку; ошибка не засчитана.")
        position = chess.Board(attempt.fen)
        for uci in played_line:
            candidate = chess.Move.from_uci(uci)
            if candidate not in position.legal_moves:
                raise ValueError("Недопустимое продолжение проверенного хода.")
            position.push(candidate)
        # An alternative must retain the result, including a forced mate; a low
        # logistic loss in an already won/lost position alone is insufficient.
        loss = report["expected_loss"]
        before, after = report.get("score_before"), report.get("score_after")
        cp_ok = before is None or after is None or before - after <= 60
        mate_before, mate_after = report.get("mate_before"), report.get("mate_after")
        mate_ok = not (mate_before is not None and mate_before > 0 and (mate_after is None or mate_after < 0))
        mate_ok &= not (mate_after is not None and mate_after < 0 and (mate_before is None or mate_before > 0))
        if mate_before is not None and mate_after is not None and mate_before < 0 and mate_after < 0:
            mate_ok &= mate_after <= mate_before
        return loss <= .03 and cp_ok and mate_ok, report
    except (OSError, chess.engine.EngineError, TimeoutError, TypeError, ValueError):
        raise HTTPException(503, "Движок сейчас недоступен. Позиция сохранена; повторите проверку позже.") from None
    finally:
        engine_slots.release()


@router.post("/practice/{attempt_id}/move")
def play(attempt_id: int, body: MoveBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    attempt = owned_attempt(db, attempt_id, user.id, mutate=True)
    if attempt.status != "active":
        raise HTTPException(409, "Позиция уже завершена. Откройте следующее упражнение.")
    board = chess.Board(attempt.fen)
    move = chess.Move.from_uci(body.move)
    if move not in board.legal_moves:
        raise HTTPException(422, "Этот ход недопустим.")
    record_time(attempt, body.elapsed_ms)
    expected = attempt.payload["moves"][attempt.ply_index]
    accepted = body.move == expected
    report = {}
    check_source = "reference"
    after = board.copy()
    after.push(move)
    if after.is_checkmate():
        accepted = True
        check_source = "checkmate"
        report = {"best_uci": body.move, "mate_before": 1, "mate_after": 1,
                  "expected_loss": 0, "played_line_uci": [body.move]}
    elif not accepted and attempt.kind == "decision":
        accepted, report = _alternative(attempt, body.move)
        check_source = "engine"
    payload = dict(attempt.payload)
    checks = list(payload.get("move_checks", []))
    checks.append({"uci": body.move, "verdict": "accepted" if accepted else "rejected",
                   "source": check_source, "best_uci": report.get("best_uci", expected),
                   "checker_version": MOVE_CHECK_VERSION, "engine_name": report.get("engine_name"),
                   **{key: report.get(key) for key in (
                       "score_before", "score_after", "mate_before", "mate_after", "expected_loss", "depth")},
                   "checked_at": now().isoformat(), "elapsed_ms": attempt.elapsed_ms})
    payload["move_checks"] = checks[-MOVE_CHECK_HISTORY_LIMIT:]
    if not accepted:
        attempt.mistakes += 1
        feedback = "Этот ход не сохраняет нужное продолжение. Проверьте угрозы соперника и попробуйте ещё раз."
    else:
        if body.move != expected:
            payload["accepted_uci"] = body.move
            payload["accepted_ply"] = attempt.ply_index
            payload["accepted_moves"] = [*payload["moves"][:attempt.ply_index], *report["played_line_uci"]]
        board.push(move)
        attempt.ply_index += 1
        payload["last_move"] = body.move
        if attempt.kind == "decision" or board.is_checkmate() or attempt.ply_index >= len(payload["moves"]):
            finish(db, attempt, "completed")
            feedback = "Решение найдено. Разберите идею и проверьте её в следующей позиции."
            if body.move != expected:
                feedback = ("Ваш ход ставит мат. В разборе показан ваш вариант." if board.is_checkmate()
                            else "Ваше продолжение также подходит — проверено движком. В разборе показан ваш вариант.")
        else:
            reply = payload["moves"][attempt.ply_index]
            board.push_uci(reply)
            attempt.ply_index += 1
            payload["last_move"] = reply
            if attempt.ply_index >= len(payload["moves"]):
                finish(db, attempt, "completed")
            feedback = "Верно. Соперник ответил — продолжайте расчёт."
        attempt.fen = board.fen()
    payload["feedback"] = feedback
    attempt.payload = payload
    db.commit()
    return {"accepted": accepted, "attempt": attempt_view(attempt), "feedback": feedback}


@router.post("/practice/{attempt_id}/hint")
def hint(attempt_id: int, body: TimingBody = TimingBody(), db: Session = Depends(get_db), user: User = Depends(current_user)):
    from .coach_insights import SKILLS
    attempt = owned_attempt(db, attempt_id, user.id, mutate=True)
    if attempt.status != "active":
        raise HTTPException(409, "Упражнение уже завершено.")
    record_time(attempt, body.elapsed_ms)
    attempt.hints_used += 1
    square = attempt.payload["moves"][attempt.ply_index][:2] if attempt.hints_used >= 2 else None
    message = f"Присмотритесь к фигуре на поле {square}." if square else SKILLS[attempt.skill_id]["practice_tip"]
    db.commit()
    return {"attempt": attempt_view(attempt), "message": message, "from_square": square}


@router.post("/practice/{attempt_id}/reveal")
def reveal(attempt_id: int, body: TimingBody = TimingBody(), db: Session = Depends(get_db), user: User = Depends(current_user)):
    attempt = owned_attempt(db, attempt_id, user.id, mutate=True)
    if attempt.status == "active":
        record_time(attempt, body.elapsed_ms)
        finish(db, attempt, "revealed")
        attempt.payload = {**attempt.payload, "feedback": "Разберите идею. Эта позиция вернётся в повторение через 10 минут."}
        db.commit()
    return attempt_view(attempt)


@router.post("/practice/{attempt_id}/time")
def save_time(attempt_id: int, body: TimingBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    attempt = owned_attempt(db, attempt_id, user.id, mutate=True)
    record_time(attempt, body.elapsed_ms)
    db.commit()
    return attempt_view(attempt)
