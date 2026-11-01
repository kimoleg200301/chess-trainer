"""Timing survives API retries, completion, migrations and portable backups."""
from copy import deepcopy

import chess
from sqlalchemy import BigInteger, inspect, select, text
from sqlalchemy.orm import Session

from test_api import puzzle_for, solve, start


def solve_timed(client, attempt, elapsed_ms):
    puzzle = puzzle_for(attempt)
    for uci in puzzle.moves[attempt["solved_plies"] + 1::2]:
        response = client.post(f"/api/training/{attempt['id']}/move",
                               json={"move": uci, "elapsed_ms": elapsed_ms})
        assert response.status_code == 200, response.text
        assert response.json()["accepted"]
        attempt = response.json()["attempt"]
    assert attempt["status"] == "completed"
    return attempt


def test_time_requires_authentication_and_allowed_origin(client):
    assert client.post("/api/training/1/time", json={"elapsed_ms": 1000}).status_code == 401
    assert client.post("/api/training/1/time", json={"elapsed_ms": 1000},
                       headers={"Origin": "https://attacker.invalid"}).status_code == 403


def test_time_cannot_modify_another_users_attempt(logged):
    from app.db import engine
    from app.models import Attempt, User
    from app.security import hash_password
    attempt = start(logged)
    with Session(engine()) as db:
        other = User(username="timing-other", password_hash=hash_password("other-timing-password"))
        db.add(other)
        db.flush()
        db.get(Attempt, attempt["id"]).user_id = other.id
        db.commit()
    assert logged.post(f"/api/training/{attempt['id']}/time", json={"elapsed_ms": 1000}).status_code == 404
    with Session(engine()) as db:
        assert db.get(Attempt, attempt["id"]).elapsed_ms is None


def test_time_validation_rejects_bad_values_without_mutation(logged):
    from app.db import engine
    from app.models import Attempt
    attempt = start(logged)
    base = f"/api/training/{attempt['id']}"
    correct = puzzle_for(attempt).moves[1]
    for invalid in [-1, 1.5, "1000", True, [], {}, 2**53]:
        for endpoint in ["time", "move", "hint", "reveal"]:
            body = {"elapsed_ms": invalid}
            if endpoint == "move":
                body["move"] = correct
            response = logged.post(f"{base}/{endpoint}", json=body)
            assert response.status_code == 422, (endpoint, invalid, response.text)
    for body in [{}, {"elapsed_ms": None}]:
        assert logged.post(base + "/time", json=body).status_code == 422
    with Session(engine()) as db:
        stored = db.get(Attempt, attempt["id"])
        assert stored.elapsed_ms is None and stored.status == "active"
        assert stored.fen == attempt["fen"] and stored.hints_used == 0


def test_time_is_monotonic_across_moves_hints_and_late_heartbeats(logged):
    attempt = start(logged, theme="mateIn2")
    assert attempt["elapsed_ms"] is None
    base = f"/api/training/{attempt['id']}"
    assert logged.post(base + "/time", json={"elapsed_ms": 2500}).json()["elapsed_ms"] == 2500
    assert logged.post(base + "/time", json={"elapsed_ms": 1000}).json()["elapsed_ms"] == 2500
    invalid = logged.post(base + "/move", json={"move": "a1a1", "elapsed_ms": 9000})
    assert invalid.status_code == 422
    # Validation failure must not commit the larger timer value.
    assert logged.post(base + "/time", json={"elapsed_ms": 2500}).json()["elapsed_ms"] == 2500
    first = puzzle_for(attempt).moves[1]
    response = logged.post(base + "/move", json={"move": first, "elapsed_ms": 2000})
    assert response.status_code == 200 and response.json()["accepted"]
    attempt = response.json()["attempt"]
    assert attempt["elapsed_ms"] == 2500 and attempt["status"] == "active"
    hinted = logged.post(base + "/hint", json={"elapsed_ms": 3100}).json()["attempt"]
    assert hinted["elapsed_ms"] == 3100
    completed = solve_timed(logged, attempt, 4500)
    assert completed["elapsed_ms"] == 4500
    # A response from an earlier browser checkpoint must not rewrite the result.
    assert logged.post(base + "/time", json={"elapsed_ms": 1000}).json()["elapsed_ms"] == 4500
    assert logged.post(base + "/time", json={"elapsed_ms": 12000}).json()["elapsed_ms"] == 4500
    assert logged.post(base + "/reveal", json={"elapsed_ms": 15000}).json()["attempt"]["elapsed_ms"] == 4500
    stats = logged.get("/api/stats").json()
    assert stats["average_solution_ms"] == 4500 and stats["timed_solutions"] == 1


def test_wrong_legal_move_records_time_and_reveal_freezes_it(logged):
    attempt = start(logged, theme="fork")
    board = chess.Board(attempt["fen"])
    expected = puzzle_for(attempt).moves[1]
    wrong = None
    for move in board.legal_moves:
        probe = board.copy()
        probe.push(move)
        if move.uci() != expected and not probe.is_checkmate():
            wrong = move.uci()
            break
    assert wrong is not None
    base = f"/api/training/{attempt['id']}"
    result = logged.post(base + "/move", json={"move": wrong, "elapsed_ms": 1234}).json()
    assert not result["accepted"] and result["attempt"]["elapsed_ms"] == 1234
    assert result["attempt"]["fen"] == attempt["fen"]
    revealed = logged.post(base + "/reveal", json={"elapsed_ms": 4567}).json()["attempt"]
    assert revealed["status"] == "revealed" and revealed["elapsed_ms"] == 4567
    assert logged.post(base + "/time", json={"elapsed_ms": 9000}).json()["elapsed_ms"] == 4567
    assert logged.post(base + "/reveal", json={"elapsed_ms": 10000}).json()["attempt"]["elapsed_ms"] == 4567
    stats = logged.get("/api/stats").json()
    assert stats["timed_solutions"] == 0 and stats["average_solution_ms"] is None
    assert stats["recent"][0]["elapsed_ms"] == 4567


def test_statistics_exclude_untimed_revealed_and_active_attempts(logged):
    empty = logged.get("/api/stats").json()
    assert empty["average_solution_ms"] is None and empty["timed_solutions"] == 0
    legacy = solve(logged, start(logged, theme="mateIn1"))
    assert legacy["elapsed_ms"] is None
    zero = solve_timed(logged, start(logged, theme="mateIn1"), 0)
    assert zero["elapsed_ms"] == 0
    solve_timed(logged, start(logged, theme="mateIn1"), 2000)
    revealed = start(logged)
    logged.post(f"/api/training/{revealed['id']}/reveal", json={"elapsed_ms": 90000})
    active = start(logged)
    logged.post(f"/api/training/{active['id']}/time", json={"elapsed_ms": 100000})
    stats = logged.get("/api/stats").json()
    assert stats["solved"] == 3
    assert stats["timed_solutions"] == 2 and stats["average_solution_ms"] == 1000
    assert {(row["status"], row["elapsed_ms"]) for row in stats["recent"]} == {
        ("completed", None), ("completed", 0), ("completed", 2000),
        ("revealed", 90000), ("active", 100000),
    }


def test_timed_progress_roundtrip_and_older_v1_backup(logged):
    from app.db import engine
    from app.models import Attempt, User
    from app.progress import ProgressSnapshot, restore_snapshot
    from app.security import hash_password
    solve_timed(logged, start(logged, theme="mateIn1"), 12345)
    active = start(logged)
    logged.post(f"/api/training/{active['id']}/time", json={"elapsed_ms": 23456})
    exported = logged.get("/api/progress/export").json()
    assert exported["format_version"] == 1
    assert [item["elapsed_ms"] for item in exported["attempts"]] == [12345, 23456]
    older = deepcopy(exported)
    for item in older["attempts"]:
        del item["elapsed_ms"]
    for name, snapshot, expected in [("timed-restored", exported, [12345, 23456]),
                                     ("legacy-timed-restored", older, [None, None])]:
        with Session(engine()) as db:
            user = User(username=name, password_hash=hash_password("restore-timing-password"))
            db.add(user)
            db.commit()
            user_id = user.id
        with Session(engine()) as db, db.begin():
            restore_snapshot(db, ProgressSnapshot.model_validate(snapshot), name)
        with Session(engine()) as db:
            actual = list(db.scalars(select(Attempt.elapsed_ms).where(Attempt.user_id == user_id).order_by(Attempt.id)))
            assert actual == expected


def test_timing_migration_preserves_legacy_rows_as_unknown(logged):
    from alembic import command
    from alembic.config import Config
    from app.db import engine
    attempt = solve(logged, start(logged, theme="mateIn1"))
    config = Config("alembic.ini")
    # The test database is an isolated disposable schema, never the live database.
    command.downgrade(config, "0001_initial")
    try:
        with engine().connect() as db:
            before = db.execute(text("SELECT id, fen, status, completed_at FROM attempts WHERE id = :id"),
                                {"id": attempt["id"]}).one()
        assert "elapsed_ms" not in {column["name"] for column in inspect(engine()).get_columns("attempts")}
    finally:
        command.upgrade(config, "head")
    columns = {column["name"]: column for column in inspect(engine()).get_columns("attempts")}
    assert columns["elapsed_ms"]["nullable"] and isinstance(columns["elapsed_ms"]["type"], BigInteger)
    with engine().connect() as db:
        after = db.execute(text("SELECT id, fen, status, completed_at, elapsed_ms FROM attempts WHERE id = :id"),
                           {"id": attempt["id"]}).one()
    assert tuple(after[:4]) == tuple(before) and after.elapsed_ms is None
