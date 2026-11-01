"""Collection visibility and stable generated-puzzle identities."""
import hashlib
import json
from typing import Literal

import chess
from sqlalchemy import and_, or_

from .models import Puzzle

Collection = Literal["lichess", "public", "personal", "all"]
COLLECTION_NAMES = {"lichess": "Lichess", "public": "Открытые партии",
                    "personal": "Мои партии Chess.com", "all": "Все базы"}


def visible_puzzles(user_id: int):
    return or_(Puzzle.collection.in_(["lichess", "public"]),
               and_(Puzzle.collection == "personal", Puzzle.owner_id == user_id))


def collection_filter(user_id: int, collection: str):
    visible = visible_puzzles(user_id)
    return visible if collection == "all" else and_(visible, Puzzle.collection == collection)


def generate_puzzle_id(initial_fen: str, moves: list[str], collection: str, owner_id: int | None = None) -> str:
    board = chess.Board(initial_fen)
    board.push_uci(moves[0])
    payload = [collection, owner_id if collection == "personal" else None,
               " ".join(board.fen().split()[:4]), moves[1:]]
    return "g" + hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()[:31]
