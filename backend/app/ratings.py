"""Local puzzle Elo. Only the first rated encounter counts, once per user/moment."""
import math

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .models import Attempt, Puzzle, User


def previously_finished(db: Session, user_id: int, puzzle: Puzzle, attempt_id: int | None = None) -> bool:
    same_moment = and_(Puzzle.collection == "personal", Puzzle.owner_id == user_id,
                       Puzzle.game_url == puzzle.game_url, Puzzle.source_ply == puzzle.source_ply)
    same_puzzle = Puzzle.id == puzzle.id
    if puzzle.collection == "personal" and puzzle.game_url and puzzle.source_ply is not None:
        same_puzzle = or_(same_puzzle, same_moment)
    query = (select(Attempt.id).join(Puzzle, Puzzle.id == Attempt.puzzle_id)
             .where(Attempt.user_id == user_id, Attempt.completed_at.is_not(None), same_puzzle))
    if attempt_id is not None:
        query = query.where(Attempt.id != attempt_id)
    return db.scalar(query.limit(1)) is not None


def apply_rating(db: Session, attempt: Attempt, *, clean: bool):
    if not attempt.rated or attempt.rating_change is not None:
        return
    # All training mutations lock User, then Attempt, then Puzzle. This also
    # serializes two devices finishing different attempts for the same account.
    user = db.scalar(select(User).where(User.id == attempt.user_id).with_for_update()
                     .execution_options(populate_existing=True))
    if previously_finished(db, user.id, attempt.puzzle, attempt.id):
        attempt.rated = False
        return
    puzzle = db.scalar(select(Puzzle).where(Puzzle.id == attempt.puzzle_id).with_for_update()
                       .execution_options(populate_existing=True))
    before, difficulty = user.puzzle_rating, puzzle.rating
    expected = 1 / (1 + 10 ** ((difficulty - before) / 400))
    # An informative first encounter changes each rating by at least one point.
    def adjusted(rating, change):
        delta = max(1, math.floor(abs(change) + .5)) * (1 if change > 0 else -1)
        return max(0, min(4000, rating + delta))
    user.puzzle_rating = adjusted(before, 32 * (int(clean) - expected))
    puzzle.rating = adjusted(difficulty, 16 * (expected - int(clean)))
    user.rated_attempts += 1
    puzzle.rated_attempts += 1
    attempt.rating_change = {
        "before": before, "after": user.puzzle_rating, "delta": user.puzzle_rating - before,
        "puzzle_before": difficulty, "puzzle_after": puzzle.rating,
        "clean": clean,
    }
