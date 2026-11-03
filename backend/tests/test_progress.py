import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_api import solve, start


def test_static_mount_never_replaces_api(client):
    page = client.get("/")
    assert page.status_code == 200 and "Tactics Lab test frontend" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/api/meta").status_code == 401
    unknown = client.get("/api/does-not-exist")
    assert unknown.status_code == 404 and unknown.headers["content-type"].startswith("application/json")
    assert client.get("/missing-file").status_code == 404
    assert client.get("/api/progress/export").status_code == 401


def test_export_owns_only_current_user_data_and_no_solutions(logged):
    from app.db import engine
    from app.models import Attempt, Review, User, now
    from app.security import hash_password
    attempt = start(logged)
    logged.post(f"/api/training/{attempt['id']}/hint")
    with Session(engine()) as db:
        other = User(username="export-other", password_hash=hash_password("a-different-private-password"))
        db.add(other)
        db.flush()
        db.add(Attempt(user_id=other.id, puzzle_id=attempt["puzzle_id"], fen=attempt["fen"], learner_white=True))
        db.add(Review(user_id=other.id, puzzle_id=attempt["puzzle_id"], due_at=now()))
        db.commit()
    result = logged.get("/api/progress/export")
    assert result.status_code == 200
    assert result.headers["content-disposition"].startswith("attachment;")
    data = result.json()
    assert data["format_version"] == 3 and data["username"] == "tester"
    assert len(data["attempts"]) == 1 and len(data["reviews"]) == 1
    assert data["attempts"][0]["status"] == "active"
    forbidden = {"password", "password_hash", "token", "token_hash", "session", "solution", "moves", "game_url", "user_id"}
    assert not forbidden.intersection(data)
    assert not forbidden.intersection(data["attempts"][0])
    assert not forbidden.intersection(data["reviews"][0])


def test_restore_roundtrip_empty_account_and_repeated_restore_refused(logged, tmp_path):
    from app.db import engine
    from app.models import Attempt, Review, User
    from app.progress import ATTEMPT_FIELDS, REVIEW_FIELDS
    from app.security import hash_password
    solve(logged, start(logged, theme="fork"))
    another = start(logged)
    logged.post(f"/api/training/{another['id']}/hint")
    snapshot = logged.get("/api/progress/export").json()
    backup = tmp_path / "progress.json"
    backup.write_text(json.dumps(snapshot), encoding="utf-8")
    with Session(engine()) as db:
        restored = User(username="restored", password_hash=hash_password("restore-account-password"))
        db.add(restored)
        db.commit()
        restored_id = restored.id
    command = [sys.executable, "-m", "app.restore_progress", str(backup), "--username", "restored"]
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    with Session(engine()) as db:
        attempts = list(db.scalars(select(Attempt).where(Attempt.user_id == restored_id).order_by(Attempt.id)))
        reviews = list(db.scalars(select(Review).where(Review.user_id == restored_id).order_by(Review.id)))
        from app.progress import AttemptRecord, ReviewRecord
        actual_attempts = [AttemptRecord(**{field: getattr(row, field) for field in ATTEMPT_FIELDS}).model_dump(mode="json") for row in attempts]
        actual_reviews = [ReviewRecord(**{field: getattr(row, field) for field in REVIEW_FIELDS}).model_dump(mode="json") for row in reviews]
        assert actual_attempts == snapshot["attempts"]
        assert actual_reviews == snapshot["reviews"]
    repeated = subprocess.run(command, capture_output=True, text=True)
    assert repeated.returncode == 1 and "уже есть прогресс" in repeated.stderr
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Attempt).where(Attempt.user_id == restored_id)) == 2


def test_restore_rejects_invalid_puzzle_and_is_atomic(logged):
    from app.db import engine
    from app.models import Attempt, User
    from app.progress import ProgressSnapshot, restore_snapshot
    from app.security import hash_password
    start(logged)
    data = logged.get("/api/progress/export").json()
    data["attempts"].append({**data["attempts"][0], "puzzle_id": "missing-puzzle"})
    with Session(engine()) as db:
        target = User(username="atomic-restore", password_hash=hash_password("atomic-restore-password"))
        db.add(target)
        db.commit()
        target_id = target.id
    with Session(engine()) as db, pytest.raises(ValueError, match="отсутствуют задачи"):
        with db.begin():
            restore_snapshot(db, ProgressSnapshot.model_validate(data), "atomic-restore")
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Attempt).where(Attempt.user_id == target_id)) == 0
