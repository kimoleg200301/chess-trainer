"""The diagnostic uses one bounded engine job without changing training data."""
import threading

import pytest
from fastapi import HTTPException
from sqlalchemy import text


@pytest.fixture
def diagnostic(monkeypatch):
    from app import benchmark_api
    registry = benchmark_api.BenchmarkJobs()
    monkeypatch.setattr(benchmark_api, "jobs", registry)
    started, finish = threading.Event(), threading.Event()
    calls = []

    def run(path, **kwargs):
        calls.append((path, kwargs))
        started.set()
        assert finish.wait(3), "Test did not release diagnostic worker"
        return {"profile": benchmark_api.PROFILE, "runs": [{"nps": 100000}], "summary": {"median_nps": 100000}}

    monkeypatch.setattr(benchmark_api, "run_benchmark", run)
    yield benchmark_api, registry, started, finish, calls
    finish.set()
    if registry.worker:
        registry.worker.join(3)
        assert not registry.worker.is_alive()


def data_counts():
    from app.db import engine
    tables = ("users", "puzzles", "attempts", "reviews", "import_jobs", "imported_games", "improvements", "game_analyses")
    with engine().connect() as db:
        return {table: db.scalar(text(f"SELECT count(*) FROM {table}")) for table in tables}


def test_diagnostic_requires_auth_and_fixed_empty_request(client, logged, diagnostic):
    api, registry, started, finish, calls = diagnostic
    logged.post("/api/auth/logout", json={})
    assert client.get("/api/diagnostics/stockfish-benchmark").status_code == 401
    assert client.post("/api/diagnostics/stockfish-benchmark", json={}).status_code == 401
    client.post("/api/auth/login", json={"username": "tester", "password": "integration-test-only-password"})
    for body in ({"engine": "/bin/sh"}, {"threads": 100}, {"profile": "standard"}, {"repetitions": 999}):
        assert client.post("/api/diagnostics/stockfish-benchmark", json=body).status_code == 422
    assert client.post("/api/diagnostics/stockfish-benchmark", json={}, headers={"Origin": "https://foreign.invalid"}).status_code == 403
    assert calls == []


def test_diagnostic_is_async_idempotent_and_does_not_change_games(logged, diagnostic):
    api, registry, started, finish, calls = diagnostic
    before = data_counts()
    response = logged.post("/api/diagnostics/stockfish-benchmark", json={})
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "running" and started.wait(1)
    assert "owner_id" not in job and "created_monotonic" not in job
    again = logged.post("/api/diagnostics/stockfish-benchmark", json={}).json()
    assert again["id"] == job["id"] and len(calls) == 1
    assert calls[0][1] == {"repetitions": 3, "timeout_per_run": 60}
    assert not api.engine_slots.acquire(blocking=False)
    finish.set()
    registry.worker.join(2)
    result = logged.get(f"/api/diagnostics/stockfish-benchmark/{job['id']}").json()
    assert result["status"] == "completed" and result["finished_at"]
    assert result["result"]["summary"]["median_nps"] == 100000
    assert data_counts() == before
    assert api.engine_slots.acquire(blocking=False)
    api.engine_slots.release()
    cooldown = logged.post("/api/diagnostics/stockfish-benchmark", json={})
    assert cooldown.status_code == 429 and int(cooldown.headers["Retry-After"]) > 0


def test_diagnostic_engine_busy_fails_without_creating_job(logged, diagnostic):
    api, registry, started, finish, calls = diagnostic
    assert api.engine_slots.acquire(blocking=False)
    try:
        response = logged.post("/api/diagnostics/stockfish-benchmark", json={})
        assert response.status_code == 503
        assert not registry.jobs and not calls
    finally:
        api.engine_slots.release()


def test_diagnostic_results_are_owned_and_expire(logged, diagnostic):
    api, registry, started, finish, calls = diagnostic
    job = logged.post("/api/diagnostics/stockfish-benchmark", json={}).json()
    assert started.wait(1)
    with pytest.raises(HTTPException) as hidden:
        registry.get(job["id"], user_id=-1)
    assert hidden.value.status_code == 404
    with pytest.raises(HTTPException) as occupied:
        registry.start(user_id=-1, engine_path="unused")
    assert occupied.value.status_code == 503
    finish.set()
    registry.worker.join(2)
    registry.jobs[job["id"]]["created_monotonic"] -= api.RESULT_TTL_SECONDS + 1
    assert logged.get(f"/api/diagnostics/stockfish-benchmark/{job['id']}").status_code == 404


def test_diagnostic_failure_retains_partial_results_and_releases_engine(logged, diagnostic, monkeypatch):
    api, registry, started, finish, calls = diagnostic

    def fail(*args, **kwargs):
        raise api.BenchmarkError("Превышен лимит времени.", code="timeout", partial_report={"runs": [{"nps": 123}]})

    monkeypatch.setattr(api, "run_benchmark", fail)
    job = logged.post("/api/diagnostics/stockfish-benchmark", json={}).json()
    registry.worker.join(2)
    result = logged.get(f"/api/diagnostics/stockfish-benchmark/{job['id']}").json()
    assert result["status"] == "failed" and result["error"]["code"] == "timeout"
    assert result["result"]["runs"] == [{"nps": 123}]
    assert api.engine_slots.acquire(blocking=False)
    api.engine_slots.release()


def test_diagnostic_unexpected_failure_has_safe_error(logged, diagnostic, monkeypatch):
    api, registry, started, finish, calls = diagnostic

    def fail(*args, **kwargs):
        raise ValueError("internal diagnostic details")

    monkeypatch.setattr(api, "run_benchmark", fail)
    job = logged.post("/api/diagnostics/stockfish-benchmark", json={}).json()
    registry.worker.join(2)
    result = logged.get(f"/api/diagnostics/stockfish-benchmark/{job['id']}").json()
    assert result["status"] == "failed" and "internal diagnostic details" not in str(result)
    assert api.engine_slots.acquire(blocking=False)
    api.engine_slots.release()
