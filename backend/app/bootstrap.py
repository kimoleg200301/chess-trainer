"""Idempotent single-user bootstrap and validated CC0 puzzle import."""
import csv
from pathlib import Path

import chess
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .config import settings
from .db import engine
from .models import LoginSession, Puzzle, User
from .security import hash_password, verify_password


def bootstrap():
    cfg = settings()
    if (len(cfg.auth_password) < 12
            or cfg.auth_password.lower().startswith("change_me")
            or cfg.auth_password.lower() in {"changeme123456", "password123456"}):
        raise RuntimeError("AUTH_PASSWORD must contain at least 12 characters; generate a unique password")
    if not cfg.auth_username or len(cfg.auth_username) > 80:
        raise RuntimeError("AUTH_USERNAME must contain 1–80 characters")
    source = Path(cfg.data_path)
    if not source.is_file():
        raise RuntimeError("Puzzle seed file not found: set DATA_PATH")
    rows = []
    with source.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            moves = row["Moves"].split()
            board = chess.Board(row["FEN"])
            if not board.is_valid() or len(moves) < 2 or len(moves) % 2 != 0:
                raise RuntimeError(f"Invalid puzzle: {row['PuzzleId']}")
            for uci in moves:
                move = chess.Move.from_uci(uci)
                if move not in board.legal_moves:
                    raise RuntimeError(f"Invalid solution in puzzle: {row['PuzzleId']}")
                board.push(move)
            rows.append({"id": row["PuzzleId"], "initial_fen": row["FEN"], "moves": moves,
                         "rating": int(row["Rating"]), "themes": row["Themes"].split(),
                         "game_url": row["GameUrl"], "opening_tags": row.get("OpeningTags", "")})
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == cfg.auth_username))
        if not user:
            db.add(User(username=cfg.auth_username, password_hash=hash_password(cfg.auth_password)))
        elif not verify_password(cfg.auth_password, user.password_hash):
            # Explicit environment secret rotation also revokes old sessions.
            user.password_hash = hash_password(cfg.auth_password)
            db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
        for offset in range(0, len(rows), 500):
            db.execute(insert(Puzzle).values(rows[offset:offset + 500]).on_conflict_do_nothing(index_elements=["id"]))
        db.commit()
    print(f"Bootstrap complete: {len(rows)} puzzle records checked; user configured.")


if __name__ == "__main__":
    bootstrap()
