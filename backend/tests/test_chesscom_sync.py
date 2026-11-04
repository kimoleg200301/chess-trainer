"""Saved accounts, durable sync, ownership, deduplication and safe cancellation."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Event

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_background_analysis import controlled
from test_imports import source_game, fake_analysis, create_import
from test_game_analysis import fake_engine, start, step


def sync(client, username="TesterChess", **extra):
    response = client.post("/api/game-analysis/sync", json={"username": username, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def age_request():
    from app.db import engine
    from app.models import ChessComSync, now
    with Session(engine()) as db:
        record = db.scalar(select(ChessComSync))
        record.requested_at = now() - timedelta(minutes=10)
        db.commit()


@pytest.fixture
def remote_games(monkeypatch, source_game):
    from app import chesscom_sync
    batches = [("2025-01", [deepcopy(source_game)])]
    calls = []
    def recent(username):
        calls.append(username)
        return deepcopy(batches)
    monkeypatch.setattr(chesscom_sync, "recent_games", recent)
    return batches, calls


def test_request_saves_username_without_network_and_worker_replacement_finishes(controlled, remote_games):
    from app.background import AnalysisWorker
    before = controlled.get("/api/game-analysis/games").json()
    assert before["sync"]["username"] is None
    request = sync(controlled, "  TeStErChEsS  ")
    assert request["username"] == "testerchess" and request["background_requested"]
    assert request["status"] == "ready" and remote_games[1] == []
    assert sync(controlled) == request
    assert AnalysisWorker().run_once()
    listed = controlled.get("/api/game-analysis/games").json()
    assert len(listed["games"]) == 1 and listed["games"][0]["analysis_id"] is None
    result = listed["sync"]
    assert result["status"] == "completed" and result["last_synced_at"]
    assert result["added_games"] == result["checked_games"] == 1
    assert not result["background_requested"] and not AnalysisWorker().run_once()
    assert remote_games[1] == ["testerchess"]
    assert sync(controlled) == result  # Bounded upstream rate, even across tabs.
    age_request()
    sync(controlled, automatic=True)
    assert AnalysisWorker().run_once()
    assert controlled.get("/api/game-analysis/games").json()["sync"]["added_games"] == 0


def test_sync_preserves_reports_and_appends_without_new_batch_every_time(controlled, fake_analysis, fake_engine, remote_games):
    from app.background import AnalysisWorker
    from app.db import engine
    from app.models import CoachProfile, GameAnalysis, ImportedGame, ImportJob, User
    job = create_import(controlled)
    assert controlled.get("/api/game-analysis/games").json()["sync"]["username"] == "testerchess"
    report = start(controlled, job["games"][0]["id"])
    for _ in range(job["games"][0]["total_plies"]):
        report = step(controlled, report["id"])
    assert report["status"] == "completed"
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == "tester"))
        profile = CoachProfile(user_id=user.id, username="different-coach-account", time_control="300+5", active=False)
        db.add(profile); db.commit()
        coach_id, started = profile.id, profile.started_at
    sync(controlled); assert AnalysisWorker().run_once()
    assert controlled.get(f"/api/game-analysis/{report['id']}").json() == report
    for suffix in ("new1", "new2"):
        fresh = deepcopy(remote_games[0][0][1][0]); fresh["game_url"] += suffix
        remote_games[0][0][1].extend([fresh, deepcopy(fresh)])
        age_request(); sync(controlled); assert AnalysisWorker().run_once()
        result = controlled.get("/api/game-analysis/games").json()
        assert result["sync"]["added_games"] == 1
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ImportJob)) == 2
        assert db.scalar(select(func.count()).select_from(ImportedGame)) == 3
        assert db.scalar(select(func.count()).select_from(GameAnalysis)) == 1
        profile = db.get(CoachProfile, coach_id)
        assert profile.username == "different-coach-account" and profile.started_at == started and not profile.active
    assert controlled.get(f"/api/game-analysis/{report['id']}").json() == report


def test_same_real_game_url_does_not_duplicate_on_cosmetic_url_changes(controlled, fake_analysis, remote_games):
    from app.background import AnalysisWorker
    create_import(controlled)
    remote_games[0][0][1][0]["game_url"] = remote_games[0][0][1][0]["game_url"].replace("www.chess.com", "chess.com") + "/?source=review#board"
    sync(controlled); assert AnalysisWorker().run_once()
    data = controlled.get("/api/game-analysis/games").json()
    assert len(data["games"]) == 1 and data["sync"]["added_games"] == 0


@pytest.mark.parametrize("failing", [False, True])
def test_changing_username_during_download_discards_old_response(controlled, remote_games, monkeypatch, failing):
    from app import chesscom_sync
    from app.background import AnalysisWorker
    original = chesscom_sync.recent_games
    entered, release = Event(), Event()
    def blocking(username):
        entered.set(); assert release.wait(5)
        if failing and username == "testerchess":
            raise HTTPException(502, "Ответ для прежнего ника недоступен.")
        return original(username)
    monkeypatch.setattr(chesscom_sync, "recent_games", blocking)
    first = sync(controlled)
    with ThreadPoolExecutor(max_workers=2) as pool:
        work = pool.submit(AnalysisWorker().run_once)
        try:
            assert entered.wait(3)
            assert not AnalysisWorker().run_once()
            second = pool.submit(sync, controlled, "AnotherPlayer").result(timeout=2)
            assert second["revision"] > first["revision"]
        finally:
            release.set()
        work.result(timeout=3)
    data = controlled.get("/api/game-analysis/games").json()
    assert data["games"] == [] and data["sync"]["username"] == "anotherplayer"
    assert data["sync"]["background_requested"] and data["sync"]["status"] == "ready"
    assert AnalysisWorker().run_once()
    assert controlled.get("/api/game-analysis/games").json()["sync"]["status"] == "completed"


def test_upstream_error_keeps_username_and_existing_games(controlled, fake_analysis, monkeypatch):
    from app import chesscom_sync
    from app.background import AnalysisWorker
    create_import(controlled)
    before = controlled.get("/api/game-analysis/games").json()["games"]
    def unavailable(username):
        raise HTTPException(429, "Chess.com ограничил частоту запросов.")
    monkeypatch.setattr(chesscom_sync, "recent_games", unavailable)
    sync(controlled); assert AnalysisWorker().run_once()
    after = controlled.get("/api/game-analysis/games").json()
    assert after["games"] == before
    assert after["sync"]["username"] == "testerchess" and after["sync"]["status"] == "failed"
    assert "Chess.com" in after["sync"]["error"] and not AnalysisWorker().run_once()


def test_empty_history_is_success_and_sync_is_owned(controlled, remote_games):
    from app.background import AnalysisWorker
    from app.db import engine
    from app.models import ChessComSync, User
    from app.chesscom_sync import process_step
    from test_game_analysis import imported_game
    _, _, other_id = imported_game(username="sync-other")
    remote_games[0].clear()
    for invalid in ("", "bad/name", "https://example.com", "name?query", "x" * 41):
        assert controlled.post("/api/game-analysis/sync", json={"username": invalid}).status_code == 422
    assert controlled.post("/api/game-analysis/sync", json={"username": "TesterChess", "user_id": other_id}).status_code == 422
    assert controlled.post("/api/game-analysis/sync", json={"username": "TesterChess"}, headers={"Origin":"https://evil.invalid"}).status_code == 403
    sync(controlled)
    with Session(engine()) as db:
        record = db.scalar(select(ChessComSync))
        with pytest.raises(HTTPException) as exc:
            process_step(record.id, db, db.get(User, other_id), background=True)
        assert exc.value.status_code == 404
    assert AnalysisWorker().run_once()
    saved = controlled.get("/api/game-analysis/games").json()["sync"]
    assert saved["status"] == "completed" and saved["checked_games"] == saved["added_games"] == 0
    controlled.post("/api/auth/logout", json={})
    assert controlled.post("/api/game-analysis/sync", json={"username":"TesterChess"}).status_code == 401
    controlled.post("/api/auth/login", json={"username":"sync-other","password":"another-test-only-password"})
    assert controlled.get("/api/game-analysis/games").json()["sync"]["username"] is None


def test_recent_archive_reads_are_bounded_and_cross_month_boundary(monkeypatch, source_game):
    from app import chesscom_sync
    calls = []
    monkeypatch.setattr(chesscom_sync, "archive_months", lambda username: ["2026-11","2026-10","2026-09"])
    def month_games(username, month, limit, *, allow_empty):
        calls.append((username, month, limit, allow_empty))
        return month, [] if month == "2026-11" else [deepcopy(source_game)]
    monkeypatch.setattr(chesscom_sync, "chesscom_games", month_games)
    assert len(chesscom_sync.recent_games("testerchess")) == 2
    assert calls == [("testerchess","2026-11",200,True),("testerchess","2026-10",200,True)]
    monkeypatch.setattr(chesscom_sync, "archive_months", lambda username: [])
    assert chesscom_sync.recent_games("testerchess") == []


def test_empty_month_retains_legacy_import_error_but_sync_accepts_it(monkeypatch):
    from app import game_sources
    monkeypatch.setattr(game_sources, "fetch_json", lambda path: {"games": []})
    with pytest.raises(HTTPException) as exc:
        game_sources.chesscom_games("testerchess", "2026-11", 10)
    assert exc.value.status_code == 404
    assert game_sources.chesscom_games("testerchess", "2026-11", 200, allow_empty=True) == ("2026-11", [])


def test_import_limit_rolls_back_all_months(controlled, remote_games):
    from app.background import AnalysisWorker
    from app.db import engine
    from app.models import ImportedGame, ImportJob, User
    second = deepcopy(remote_games[0][0][1][0]); second["game_url"] += "new-month"
    remote_games[0].append(("2025-02", [second]))
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == "tester"))
        db.add_all(ImportJob(user_id=user.id, source="chesscom", username="old",identity_key=f"old-{i}",analysis_version=2) for i in range(99))
        db.commit()
    sync(controlled); assert AnalysisWorker().run_once()
    state = controlled.get("/api/game-analysis/games").json()["sync"]
    assert state["status"] == "failed" and state["added_games"] == 0
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ImportJob)) == 99
        assert db.scalar(select(func.count()).select_from(ImportedGame)) == 0


def test_busy_user_keeps_durable_request_for_next_worker(controlled, remote_games):
    from app.background import AnalysisWorker
    from app.db import engine
    from app.models import User
    sync(controlled)
    with Session(engine()) as db:
        db.scalar(select(User).where(User.username == "tester").with_for_update())
        assert not AnalysisWorker().run_once()
        saved = controlled.get("/api/game-analysis/games").json()["sync"]
        assert saved["background_requested"] and saved["error"] is None
    assert AnalysisWorker().run_once()
    assert controlled.get("/api/game-analysis/games").json()["sync"]["added_games"] == 1


def test_migration_backfills_last_import_and_coach_fallback_without_queueing(controlled, source_game):
    from alembic import command
    from alembic.config import Config
    from app.db import engine
    from app.models import ChessComSync, CoachProfile, ImportJob, User, now
    from test_game_analysis import imported_game
    _, _, other_id = imported_game(username="sync-migration-other")
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == "tester"))
        for index, name in enumerate(["EarlierNick", "LatestNick"]):
            db.add(ImportJob(user_id=user.id, source="chesscom", username=name, identity_key="nick-"+str(index),
                             analysis_version=2, created_at=now()+timedelta(seconds=index)))
        db.query(ImportJob).filter(ImportJob.user_id==other_id).delete()
        db.add(CoachProfile(user_id=other_id, username="CoachNick",time_control="300",active=False))
        db.commit(); user_id=user.id
    command.downgrade(Config("alembic.ini"), "0008_personal_coach")
    command.upgrade(Config("alembic.ini"), "head")
    with Session(engine()) as db:
        own=db.scalar(select(ChessComSync).where(ChessComSync.user_id==user_id))
        other=db.scalar(select(ChessComSync).where(ChessComSync.user_id==other_id))
        assert own.username == "latestnick" and other.username == "coachnick"
        assert own.status == other.status == "idle" and not own.background_requested and not other.background_requested
        assert db.get(User,user_id).puzzle_rating==1200
