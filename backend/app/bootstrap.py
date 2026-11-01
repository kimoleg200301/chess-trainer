"""Idempotent single-user bootstrap and validated CC0 puzzle import."""
import csv
import json
import os
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
    generated_path = Path(os.environ.get("GENERATED_PUZZLES_PATH", str(source.with_name("generated-puzzles.json"))))
    generated = []
    if generated_path.is_file():
        from .collections import generate_puzzle_id
        from .imports_api import validate_puzzle
        from .game_sources import safe_game_url
        bank = json.loads(generated_path.read_text(encoding="utf-8"))
        if not isinstance(bank, list) or len(bank) > 5000:
            raise RuntimeError("Invalid generated puzzle seed")
        for record in bank:
            fields = validate_puzzle(record)
            puzzle_id = generate_puzzle_id(fields["initial_fen"], fields["moves"], "public")
            if record.get("id") != puzzle_id:
                raise RuntimeError("Generated puzzle seed identity mismatch")
            generated.append({"id": puzzle_id, **fields, "collection": "public", "owner_id": None,
                              "rating_estimated": True, "game_url": safe_game_url(record.get("game_url", "")),
                              "opening_tags": record.get("opening_tags", ""), "source_ply": record.get("source_ply")})
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
        for offset in range(0, len(generated), 500):
            db.execute(insert(Puzzle).values(generated[offset:offset + 500]).on_conflict_do_nothing(index_elements=["id"]))
        from .imports_api import backfill_generated_puzzles
        auto_saved = backfill_generated_puzzles(db)
        db.commit()
    print(f"Bootstrap complete: {len(rows)} Lichess and {len(generated)} generated puzzle records checked; {auto_saved} legacy moments auto-saved; user configured.")


if __name__ == "__main__":
    bootstrap()
