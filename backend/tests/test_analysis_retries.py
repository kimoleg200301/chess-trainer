"""Extra-budget retries are bounded, durable and preserve assessed moves."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy.orm import Session

from test_game_analysis import fake_engine, imported_game, start, step
from test_background_analysis import controlled


def pending_report(client, fake_engine):
    fake_engine[2].update({0, 2})
    game_id, _, _ = imported_game()
    report = start(client, game_id)
    for _ in range(4):
        report = step(client, report["id"])
    assert report["status"] == "running" and report["retry_pending"] == 2
    return report


def test_retry_only_missing_moves_with_larger_budget_and_worker_replacement(controlled, fake_engine):
    from app.background import AnalysisWorker
    from app.game_analysis_engine import ANALYSIS_RETRY_OPTIONS, ANALYSIS_POSITION_SECONDS, ANALYSIS_NODES, ANALYSIS_SEARCH_SECONDS
    report = pending_report(controlled, fake_engine)
    path = f"/api/game-analysis/{report['id']}"
    fake_engine[2].clear()
    controlled.post(path + "/resume", json={})
    assert AnalysisWorker().run_once()
    halfway = controlled.get(path).json()
    assert halfway["retry_pending"] == 1 and halfway["cursor"] == 4
    assert halfway["moves"][0]["retry_attempts"] == 1
    assert halfway["summary"]["white"]["accuracy"] is None
    assert AnalysisWorker().run_once()
    final = controlled.get(path).json()
    assert final["status"] == "completed" and not final["background_requested"]
    assert final["retry_pending"] == 0 and final["summary"]["white"]["accuracy_complete"]
    assert final["moves"][1::2] == report["moves"][1::2]
    assert [m["ply"] for m in final["moves"]] == [0, 1, 2, 3]
    assert [ply for ply, _ in fake_engine[0]] == [0, 1, 2, 3, 0, 2]
    assert all(options == ANALYSIS_RETRY_OPTIONS for _, options in fake_engine[0][-2:])
    assert ANALYSIS_RETRY_OPTIONS["nodes"] > ANALYSIS_NODES
    assert ANALYSIS_RETRY_OPTIONS["max_time"] > ANALYSIS_SEARCH_SECONDS
    assert ANALYSIS_POSITION_SECONDS < ANALYSIS_RETRY_OPTIONS["max_position_time"] <= 45
    assert not AnalysisWorker().run_once()
    assert controlled.post(path + "/resume", json={}).json() == final


def test_exhausted_retry_stays_unassessed_and_restart_resets_attempts(controlled, fake_engine):
    from app.background import AnalysisWorker
    report = pending_report(controlled, fake_engine)
    path = f"/api/game-analysis/{report['id']}"
    controlled.post(path + "/resume", json={})
    assert AnalysisWorker().run_once() and AnalysisWorker().run_once()
    final = controlled.get(path).json()
    assert final["status"] == "completed" and final["retry_pending"] == 0
    assert final["summary"]["white"]["accuracy"] is None
    for _ in range(3):
        assert controlled.post(path + "/resume", json={}).json() == final
        assert not AnalysisWorker().run_once()
    assert len(fake_engine[0]) == 6
    reset = controlled.post(path + "/restart", json={}).json()
    assert reset["cursor"] == 0 and reset["moves"] == [] and reset["retry_pending"] == 0


def test_completed_legacy_report_can_resume_only_missing_moves(controlled, fake_engine):
    from app.db import engine
    from app.models import GameAnalysis
    from app.background import AnalysisWorker
    report = pending_report(controlled, fake_engine)
    path = f"/api/game-analysis/{report['id']}"
    with Session(engine()) as db:
        db.get(GameAnalysis, report["id"]).status = "completed"
        db.commit()
    saved = controlled.get(path).json()
    assert saved["status"] == "completed" and not AnalysisWorker().run_once()
    fake_engine[2].clear()
    resumed = controlled.post("/api/game-analysis", json={"game_id": report["game_id"], "background": True}).json()
    assert resumed["status"] == "ready" and resumed["moves"] == saved["moves"]
    assert AnalysisWorker().run_once() and AnalysisWorker().run_once()
    assert controlled.get(path).json()["summary"]["white"]["accuracy_complete"]


def test_pause_and_engine_failure_keep_retry_checkpoint(controlled, fake_engine, monkeypatch):
    from app import game_analysis_engine
    from app.background import AnalysisWorker
    report = pending_report(controlled, fake_engine)
    path = f"/api/game-analysis/{report['id']}"
    controlled.post(path + "/resume", json={})
    fake_engine[1].add(0)
    assert not AnalysisWorker().run_once()
    failed = controlled.get(path).json()
    assert failed["status"] == "failed" and not failed["background_requested"]
    assert failed["moves"] == report["moves"] and failed["retry_pending"] == 2
    fake_engine[1].clear()
    fake_engine[2].clear()
    controlled.post(path + "/resume", json={})
    original = game_analysis_engine.analyse_game_move
    entered, release = Event(), Event()

    def blocking(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(game_analysis_engine, "analyse_game_move", blocking)
    with ThreadPoolExecutor(max_workers=2) as pool:
        checkpoint = pool.submit(AnalysisWorker().run_once)
        try:
            assert entered.wait(3)
            assert not AnalysisWorker().run_once()
            paused = pool.submit(controlled.post, path + "/pause", json={}).result(timeout=2)
            assert paused.status_code == 200 and not paused.json()["background_requested"]
        finally:
            release.set()
        assert checkpoint.result(timeout=3)
    saved = controlled.get(path).json()
    assert saved["cursor"] == 4 and saved["retry_pending"] == 1
    assert saved["moves"][0]["assessed"] and not saved["background_requested"]
    assert not AnalysisWorker().run_once()
    controlled.post(path + "/resume", json={})
    assert AnalysisWorker().run_once()
    assert controlled.get(path).json()["status"] == "completed"
