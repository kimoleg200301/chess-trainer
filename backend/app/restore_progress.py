"""Restore a JSON export into an empty account: python -m app.restore_progress FILE --username USER."""
import argparse
import sys
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from .db import engine
from .progress import MAX_SNAPSHOT_BYTES, ProgressSnapshot, restore_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore Tactics Lab progress into an empty existing account.")
    parser.add_argument("file", type=Path)
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    try:
        with args.file.open("rb") as stream:
            payload = stream.read(MAX_SNAPSHOT_BYTES + 1)
        if len(payload) > MAX_SNAPSHOT_BYTES:
            raise ValueError("Файл больше 5 МБ. Используйте полную резервную копию PostgreSQL.")
        snapshot = ProgressSnapshot.model_validate_json(payload)
        with Session(engine()) as db, db.begin():
            result = restore_snapshot(db, snapshot, args.username)
        print(f"Восстановлено попыток: {result['attempts_restored']}; повторений: {result['reviews_restored']}.")
        return 0
    except ValidationError:
        # Do not echo arbitrary uploaded content or credentials from a malformed file.
        print("Некорректный формат снимка прогресса. Изменения не сохранены.", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"Восстановление отменено: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
