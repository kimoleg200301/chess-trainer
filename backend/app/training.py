from datetime import timedelta

import chess
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Attempt, Puzzle, Review, now
from .ratings import apply_rating
from .transactions import lock_user


def attempt_view(attempt: Attempt) -> dict:
    return {
        "id": attempt.id, "puzzle_id": attempt.puzzle_id,
        "fen": attempt.fen, "rating": (attempt.rating_change or {}).get("puzzle_before", attempt.puzzle.rating),
        "themes": attempt.puzzle.themes, "status": attempt.status,
        "turn": "white" if attempt.learner_white else "black",
        "solved_plies": attempt.ply_index - 1,
        "total_plies": len(attempt.puzzle.moves) - 1,
        "mistakes": attempt.mistakes, "hints_used": attempt.hints_used,
        "last_move": attempt.last_move, "elapsed_ms": attempt.elapsed_ms,
        "collection": attempt.puzzle.collection, "rating_estimated": attempt.puzzle.rating_estimated,
        "rated": attempt.rated, "rating_change": attempt.rating_change,
        "training_options": attempt.training_options or {
            "collection": attempt.puzzle.collection, "mode": "practice", "adaptive": True,
            "theme": None, "min_rating": 0, "max_rating": 4000,
        },
    }


def get_attempt(db: Session, attempt_id: int, user_id: int, active: bool = True) -> Attempt:
    lock_user(db, user_id)
    attempt = db.scalar(select(Attempt).where(Attempt.id == attempt_id, Attempt.user_id == user_id)
                        .with_for_update(of=Attempt))
    if not attempt:
        raise HTTPException(404, "Тренировка не найдена.")
    if active and attempt.status != "active":
        raise HTTPException(409, "Эта задача уже завершена. Выберите следующую.")
    return attempt


def record_elapsed(attempt: Attempt, elapsed_ms: int | None):
    # Caller holds the attempt row lock. Older heartbeats cannot roll the clock
    # back, and delayed requests cannot change an already finalized duration.
    if attempt.status == "active" and elapsed_ms is not None:
        attempt.elapsed_ms = max(attempt.elapsed_ms or 0, elapsed_ms)


def schedule_review(db: Session, attempt: Attempt, clean: bool = False):
    # Serialize review updates for a user, including creation of a new review row.
    lock_user(db, attempt.user_id)
    review = db.scalar(select(Review).where(Review.user_id == attempt.user_id,
                                           Review.puzzle_id == attempt.puzzle_id))
    if not review:
        review = Review(user_id=attempt.user_id, puzzle_id=attempt.puzzle_id, successes=0)
        db.add(review)
    if clean:
        intervals = [1, 3, 7, 14, 30, 60]
        review.due_at = now() + timedelta(days=intervals[min(review.successes, len(intervals) - 1)])
        review.successes += 1
    else:
        review.successes = 0
        review.due_at = now() + timedelta(minutes=10)


def finalize(db: Session, attempt: Attempt, status: str):
    if attempt.completed_at is not None:
        return
    apply_rating(db, attempt, clean=status == "completed" and not attempt.mistakes and not attempt.hints_used)
    attempt.status = status
    attempt.completed_at = now()
    attempt.last_opened_at = attempt.completed_at
    schedule_review(db, attempt, clean=status == "completed" and not attempt.mistakes and not attempt.hints_used)


def play_move(db: Session, attempt: Attempt, uci: str) -> dict:
    board = chess.Board(attempt.fen)
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        raise HTTPException(422, "Неверный формат хода.")
    if move not in board.legal_moves:
        raise HTTPException(422, "Этот ход недопустим в данной позиции.")
    expected = attempt.puzzle.moves[attempt.ply_index]
    board.push(move)
    # Multiple mating moves must be accepted in mate-in-one puzzles.
    alternate_mate = board.is_checkmate()
    if move.uci() != expected and not alternate_mate:
        attempt.mistakes += 1
        schedule_review(db, attempt)
        db.commit()
        return {"accepted": False, "attempt": attempt_view(attempt), "reply_move": None,
                "feedback": "Попробуйте другой ход. Найдите продолжение, сохраняющее цель комбинации."}
    attempt.ply_index += 1
    attempt.last_move = move.uci()
    reply = None
    if alternate_mate:
        attempt.ply_index = len(attempt.puzzle.moves)
    elif attempt.ply_index < len(attempt.puzzle.moves):
        reply = attempt.puzzle.moves[attempt.ply_index]
        response_move = chess.Move.from_uci(reply)
        if response_move not in board.legal_moves:
            raise HTTPException(500, "Ошибка в исходной задаче. Выберите следующую.")
        board.push(response_move)
        attempt.ply_index += 1
        attempt.last_move = reply
    attempt.fen = board.fen()
    if attempt.ply_index >= len(attempt.puzzle.moves):
        finalize(db, attempt, "completed")
    db.commit()
    return {"accepted": True, "attempt": attempt_view(attempt), "reply_move": reply,
            "feedback": "Комбинация найдена!" if attempt.status == "completed" else "Верно. Теперь найдите следующий ход."}


def solution(puzzle: Puzzle) -> list[dict]:
    board = chess.Board(puzzle.initial_fen)
    board.push_uci(puzzle.moves[0])
    result = []
    for uci in puzzle.moves[1:]:
        move = chess.Move.from_uci(uci)
        result.append({"uci": uci, "san": board.san(move)})
        board.push(move)
    return result
