"""Background work survives absent clients, worker replacement and explicit pause."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

import pytest
from sqlalchemy.orm import Session

from test_game_analysis import fake_engine, imported_game, start
from test_imports import fake_analysis, source_game, create_import


@pytest.fixture
def controlled(logged):
    # Most cases drive checkpoints deterministically. The lifespan case below
    # separately exercises the actual thread without any polling HTTP requests.
    from app.main import app
    app.state.analysis_worker.stop()
    return logged


@pytest.fixture(params=["imports", "game-analysis"])
def task(request, controlled, fake_engine, monkeypatch):
    from app import generation
    from app.models import GameAnalysis, ImportJob
    monkeypatch.setattr(generation, "analyse_position", lambda *args, **kwargs: None)
    game_id, job_id, _ = imported_game()
    if request.param == "imports":
        return f"/api/imports/{job_id}", ImportJob, job_id
    report = start(controlled, game_id)
    return f"/api/game-analysis/{report['id']}", GameAnalysis, report["id"]


def test_only_explicitly_started_work_runs_and_replacement_resumes(controlled, task):
    from app.background import AnalysisWorker
    path, _, _ = task
    assert not controlled.get(path).json()["background_requested"]
    assert not AnalysisWorker().run_once()
    for _ in range(2):
        assert controlled.post(path + "/resume", json={}).json()["background_requested"]
    assert AnalysisWorker().run_once()
    # New instances use the persisted checkpoint, never the browser's state.
    for _ in range(8):
        if not AnalysisWorker().run_once():
            break
    result = controlled.get(path).json()
    assert result["status"] == "completed" and not result["background_requested"]
    assert not AnalysisWorker().run_once()
    assert controlled.post(path + "/resume", json={}).json() == result


def test_pause_during_engine_search_is_persisted_and_prevents_next_step(controlled, task, monkeypatch):
    from app import game_analysis_engine, generation
    from app.background import AnalysisWorker
    path, _, _ = task
    module, name = (generation, "analyse_position") if path.startswith("/api/imports") else (game_analysis_engine, "analyse_game_move")
    original = getattr(module, name)
    entered, release = Event(), Event()

    def blocking(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, blocking)
    controlled.post(path + "/resume", json={})
    with ThreadPoolExecutor(max_workers=2) as pool:
        checkpoint = pool.submit(AnalysisWorker().run_once)
        try:
            assert entered.wait(3)
            # A second worker cannot duplicate the ongoing search.
            assert not AnalysisWorker().run_once()
            paused = pool.submit(controlled.post, path + "/pause", json={}).result(timeout=2)
            assert paused.status_code == 200 and not paused.json()["background_requested"]
        finally:
            release.set()
        assert checkpoint.result(timeout=5)
    assert not controlled.get(path).json()["background_requested"]
    assert not AnalysisWorker().run_once()
    controlled.post(path + "/resume", json={})
    assert AnalysisWorker().run_once()


def test_busy_engine_is_retried_without_cancelling_the_job(controlled, task):
    from app.background import AnalysisWorker
    from app.engine_resources import engine_slots
    path, _, _ = task
    controlled.post(path + "/resume", json={})
    assert engine_slots.acquire(blocking=False)
    try:
        assert not AnalysisWorker().run_once()
    finally:
        engine_slots.release()
    assert controlled.get(path).json()["background_requested"]
    assert AnalysisWorker().run_once()


def test_failure_stops_retries_and_explicit_resume_keeps_checkpoint(controlled, task, monkeypatch):
    import chess.engine
    from app.background import AnalysisWorker
    path, _, _ = task
    controlled.post(path + "/resume", json={})
    assert AnalysisWorker().run_once()
    before = controlled.get(path).json()
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("engine unavailable")
        patch.setattr(chess.engine.SimpleEngine, "popen_uci", fail)
        assert not AnalysisWorker().run_once()
    failed = controlled.get(path).json()
    assert failed["status"] == "failed" and not failed["background_requested"]
    assert failed.get("cursor", failed.get("games")) == before.get("cursor", before.get("games"))
    assert not AnalysisWorker().run_once()
    assert controlled.post(path + "/resume", json={}).json()["status"] == "ready"
    assert AnalysisWorker().run_once()


def test_background_controls_are_owned_and_old_versions_require_restart(controlled, task):
    from app.background import AnalysisWorker
    from app.db import engine
    path, model, record_id = task
    with Session(engine()) as db:
        record = db.get(model, record_id)
        record.analysis_version = 0
        record.background_requested = True
        db.commit()
    assert not AnalysisWorker().run_once()
    assert controlled.post(path + "/resume", json={}).status_code == 409
    assert not controlled.post(path + "/pause", json={}).json()["background_requested"]
    for action in ("pause", "resume"):
        assert controlled.post(path + "/" + action, json={}, headers={"Origin": "https://evil.invalid"}).status_code == 403
    _, _, other_id = imported_game(username="background-other")
    with Session(engine()) as db:
        record = db.get(model, record_id)
        record.user_id = other_id
        db.commit()
    for action in ("pause", "resume"):
        assert controlled.post(path + "/" + action, json={}).status_code == 404
    controlled.post("/api/auth/logout", json={})
    assert controlled.post(path + "/resume", json={}).status_code == 401


def test_import_and_reanalysis_can_enqueue_in_the_initial_request(controlled, fake_analysis):
    from app.background import AnalysisWorker
    job = create_import(controlled, background=True)
    assert job["background_requested"]
    path = f"/api/imports/{job['id']}"
    assert AnalysisWorker().run_once()
    restarted = controlled.post(path + "/reanalyze", json={"background": True}).json()
    assert restarted["background_requested"] and restarted["games"][0]["cursor"] == 0
    for _ in range(3):
        assert AnalysisWorker().run_once()
    completed = controlled.get(path).json()
    assert completed["status"] == "completed" and completed["puzzles_count"] == 1


def test_lifespan_finishes_a_report_without_browser_requests(controlled, fake_engine):
    from fastapi.testclient import TestClient
    from app.db import engine
    from app.main import app
    from app.models import GameAnalysis
    game_id, _, _ = imported_game()
    result = controlled.post("/api/game-analysis", json={"game_id": game_id, "background": True}).json()
    assert result["background_requested"]
    with TestClient(app):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with Session(engine()) as db:
                report = db.get(GameAnalysis, result["id"])
                if report.status == "completed":
                    assert report.cursor == 4 and not report.background_requested
                    break
            time.sleep(0.03)
        else:
            pytest.fail("Lifespan worker did not finish the persisted report")
    assert [ply for ply, _ in fake_engine[0]] == [0, 1, 2, 3]
    restarted = controlled.post(f"/api/game-analysis/{result['id']}/restart", json={"background": True}).json()
    assert restarted["background_requested"] and restarted["cursor"] == 0


def test_batches_and_reports_share_the_queue_without_starvation(controlled, fake_engine, monkeypatch):
    from app import generation
    from app.background import AnalysisWorker
    monkeypatch.setattr(generation, "analyse_position", lambda *args, **kwargs: None)
    game_id, job_id, _ = imported_game()
    report = start(controlled, game_id)
    controlled.post(f"/api/imports/{job_id}/resume", json={})
    controlled.post(f"/api/game-analysis/{report['id']}/resume", json={})
    worker = AnalysisWorker()
    assert worker.run_once() and worker.run_once()
    assert controlled.get(f"/api/imports/{job_id}").json()["games"][0]["cursor"] == 2
    assert controlled.get(f"/api/game-analysis/{report['id']}").json()["cursor"] == 1


def test_broken_tasks_do_not_starve_later_reports_and_roll_back_only_their_work(
        controlled, fake_engine, monkeypatch, caplog):
    from app import game_analysis_api
    from app.background import AnalysisWorker
    from app.db import engine
    from app.models import GameAnalysis

    reports = []
    for index in range(4):
        game_id, _, _ = imported_game(fingerprint=f"queue-isolation-{index}")
        report = start(controlled, game_id)
        controlled.post(f"/api/game-analysis/{report['id']}/resume", json={})
        reports.append(report)
    broken_ids = {report["id"] for report in reports[:-1]}
    original = game_analysis_api.process_step
    visited = []

    def broken_step(record_id, db, user, *, background=False):
        visited.append(record_id)
        if record_id in broken_ids:
            # A handler can fail after a flushed write. The next task must get
            # its own usable transaction, and this write must not be committed.
            db.get(GameAnalysis, record_id).cursor = 999
            db.flush()
            raise RuntimeError("Unexpected task failure")
        return original(record_id, db, user, background=background)

    monkeypatch.setattr(game_analysis_api, "process_step", broken_step)
    worker = AnalysisWorker()
    assert worker.run_once()
    assert visited == [report["id"] for report in reports]
    errors = [record for record in caplog.records if "Background task" in record.message]
    assert len(errors) == len(broken_ids)
    assert all(record.exc_info for record in errors)
    with Session(engine()) as db:
        for record_id in broken_ids:
            saved = db.get(GameAnalysis, record_id)
            assert saved.cursor == 0 and saved.moves == []
            assert saved.status == "ready" and saved.background_requested
        assert db.get(GameAnalysis, reports[-1]["id"]).cursor == 1

    # A transient failure keeps its place and can continue from its checkpoint.
    monkeypatch.setattr(game_analysis_api, "process_step", original)
    assert worker.run_once()
    with Session(engine()) as db:
        assert db.get(GameAnalysis, reports[0]["id"]).cursor == 1
