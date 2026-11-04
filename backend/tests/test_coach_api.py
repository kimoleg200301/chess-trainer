"""Personal coach: frozen baseline, durable catch-up and private practice state.

The public source and Stockfish are deterministic substitutes here. PostgreSQL,
ownership checks, HTTP handlers and persisted worker checkpoints remain real.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from threading import Event
import time
from uuid import uuid4

import chess
import chess.engine
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

START = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
PGN = '''[Event "Coach integration"]
[White "reviewer"]
[Black "opponent"]
[Result "0-1"]
[TimeControl "300+5"]

1. f3 {[%clk 0:04:59]} e5 {[%clk 0:05:01]} 2. g4 {[%clk 0:04:55]} Qh4# {[%clk 0:05:02]} 0-1
'''


@pytest.fixture
def controlled(logged, monkeypatch):
    from app import coach_api, coach_worker
    from app.main import app
    app.state.analysis_worker.stop()
    clock = [START]
    monkeypatch.setattr(coach_api, "now", lambda: clock[0])
    monkeypatch.setattr(coach_worker, "now", lambda: clock[0])
    return logged, clock


def game(number, stamp, *, player="reviewer", control="300+5"):
    from app.generation import parse_games
    item = parse_games(PGN.replace('"reviewer"', f'"{player}"'))[0]
    item.update(game_url=f"https://www.chess.com/game/live/{number}",
                end_time=int(stamp.timestamp()), source_game_at=stamp.isoformat(),
                username=player, player_username=player, player_side="white",
                player_result="checkmated", player_rating=300,
                white_result="checkmated", black_result="win",
                white_rating=300, black_rating=350,
                time_control=control, time_class="blitz", rated=True,
                date=stamp.strftime("%Y.%m.%d"))
    return item


@pytest.fixture
def sources(monkeypatch):
    from app import coach_sources
    archives = {}
    calls = []

    def months(username):
        calls.append(("archives", username))
        return sorted(archives, reverse=True)

    def month_games(username, month, control):
        calls.append(("month", username, month, control))
        return deepcopy(sorted((item for item in archives.get(month, [])
                                if item["time_control"] == control),
                               key=lambda item: item["end_time"], reverse=True))

    monkeypatch.setattr(coach_sources, "archive_months", months)
    monkeypatch.setattr(coach_sources, "month_games", month_games)
    return archives, calls


@pytest.fixture
def fake_engine(monkeypatch):
    from app import game_analysis_engine
    from app.generation import position_at
    calls, failures = [], set()

    class Engine:
        id = {"name": "Deterministic coach test engine"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def configure(self, options):
            assert options == game_analysis_engine.ANALYSIS_ENGINE_OPTIONS

    def analyse(data, ply, engine, **kwargs):
        calls.append((data.get("game_url"), ply))
        if ply in failures:
            raise chess.engine.EngineTerminatedError("disposable coach failure")
        board = position_at(data, ply)
        played = chess.Move.from_uci(data["moves"][ply])
        best = chess.Move.from_uci("e2e4" if ply == 0 else "g2g3")
        if best not in board.legal_moves:
            best = played
        before, side, san = board.fen(), "white" if board.turn else "black", board.san(played)
        best_san = board.san(best)
        number = board.fullmove_number
        board.push(played)
        return {"ply": ply, "move_number": number, "side": side,
                "before_fen": before, "after_fen": board.fen(),
                "played_uci": played.uci(), "played_san": san,
                "best_uci": best.uci(), "best_san": best_san,
                "category": "mistake", "accuracy": 55.0,
                "expected_loss": 0.1, "best_expected": 0.5, "played_expected": 0.4,
                "score_before": 0, "score_after": -150,
                "mate_before": None, "mate_after": None,
                "best_line_uci": [best.uci()], "best_line_san": [best_san],
                "played_line_uci": [played.uci()], "played_line_san": [san],
                "depth": 16, "assessed": True, "reason": "Проверенный тестовый ход",
                "brilliant_evidence": None}

    monkeypatch.setattr(chess.engine.SimpleEngine, "popen_uci", lambda *a, **k: Engine())
    monkeypatch.setattr(game_analysis_engine, "analyse_game_move", analyse)
    return calls, failures


def start(client, **overrides):
    response = client.post("/api/coach/start", json={"username": "reviewer", "time_control": "300+5", **overrides})
    assert response.status_code == 200, response.text
    return response.json()


def checkpoint(profile_id, *, background=True):
    from app.coach_worker import process_step
    from app.db import engine
    from app.models import CoachProfile, User
    with Session(engine(), expire_on_commit=False) as db:
        profile = db.get(CoachProfile, profile_id)
        return process_step(profile_id, db, db.get(User, profile.user_id), background=background)


def loaded(profile_id):
    from app.db import engine
    from app.models import CoachProfile
    with Session(engine()) as db:
        return db.get(CoachProfile, profile_id).baseline_ready


def games(profile_id):
    from app.db import engine
    from app.models import CoachGame
    with Session(engine()) as db:
        return [(row.id, row.cohort, row.data, row.cursor, row.status, row.moves)
                for row in db.scalars(select(CoachGame).where(CoachGame.coach_id == profile_id)
                                      .order_by(CoachGame.source_game_at.desc()))]


def finish_loading(profile_id):
    for _ in range(20):
        if loaded(profile_id):
            return
        checkpoint(profile_id)
    pytest.fail("Baseline loading did not reach a persisted terminal checkpoint")


def finish_sync(profile_id):
    from app.db import engine
    from app.models import CoachProfile
    for _ in range(20):
        checkpoint(profile_id)
        with Session(engine()) as db:
            if not db.get(CoachProfile, profile_id).sync_state:
                return
    pytest.fail("Archive catch-up did not finish")


def reach_analysis(profile_id, *, completed=False):
    for _ in range(12):
        saved = games(profile_id)
        if saved and (saved[0][4] == "completed" if completed else bool(saved[0][5])):
            return saved[0]
        checkpoint(profile_id)
    pytest.fail("Coach did not reach the requested analysis checkpoint")


def test_start_validation_idempotency_and_origin(controlled, sources):
    client, _ = controlled
    assert client.get("/api/coach").json()["profile"] is None
    for body in ({"username": "../other", "time_control": "300+5"},
                 {"username": "reviewer", "time_control": "0+5"},
                 {"username": "reviewer", "time_control": "daily"}):
        assert client.post("/api/coach/start", json=body).status_code == 422
    assert client.post("/api/coach/start", json={"username": "reviewer", "time_control": "300+5"},
                       headers={"Origin": "https://evil.invalid"}).status_code == 403
    first = start(client, username="ReViewer")["profile"]
    second = start(client)["profile"]
    assert first["id"] == second["id"] and first["started_at"] == second["started_at"]
    assert second["username"] == "reviewer" and second["time_control"] == "300+5"
    assert second["baseline_target"] == 40
    assert client.post("/api/coach/start", json={"username": "reviewer", "time_control": "600"}).status_code == 409
    assert sources[1] == []  # The start request never performs a slow external import.


@pytest.mark.parametrize("setup", ["failed", "empty"])
def test_empty_setup_can_be_corrected_without_deleting_history(controlled, sources, monkeypatch, setup):
    from app import coach_sources
    from app.db import engine
    from app.models import CoachProfile
    client, clock = controlled
    first = start(client)["profile"]
    if setup == "failed":
        def unavailable(*args):
            raise HTTPException(404, "Player not found")
        monkeypatch.setattr(coach_sources, "archive_months", unavailable)
        checkpoint(first["id"])
    else:
        finish_loading(first["id"])
    clock[0] += timedelta(minutes=1)
    fixed = start(client, username="corrected-player", time_control="600")["profile"]
    assert fixed["id"] == first["id"] and fixed["username"] == "corrected-player"
    assert fixed["time_control"] == "600" and fixed["started_at"] != first["started_at"]
    assert fixed["active"] and not fixed["baseline_ready"] and fixed["error"] is None
    with Session(engine()) as db:
        profile = db.get(CoachProfile, first["id"])
        assert profile.sync_state == {} and profile.last_sync_at is None


def test_profile_with_imported_games_cannot_be_retargeted_even_after_source_error(controlled, sources):
    from app.db import engine
    from app.models import CoachProfile
    client, _ = controlled
    sources[0]["2026-11"] = [game(1, START - timedelta(hours=1))]
    profile = start(client)["profile"]
    finish_loading(profile["id"])
    before = games(profile["id"])
    with Session(engine()) as db:
        db.get(CoachProfile, profile["id"]).sync_error = "Temporary source outage"
        db.commit()
    response = client.post("/api/coach/start", json={"username": "different-player", "time_control": "600"})
    assert response.status_code == 409
    saved = client.get("/api/coach").json()["profile"]
    assert saved["username"] == "reviewer" and saved["time_control"] == "300+5"
    assert saved["started_at"] == profile["started_at"] and games(profile["id"]) == before


def test_latest_40_cross_month_baseline_has_exact_control_and_no_duplicates(controlled, sources):
    client, _ = controlled
    archives, _ = sources
    november = [game(1000 + n, START - timedelta(hours=n + 1)) for n in range(10)]
    october = [game(2000 + n, datetime(2026, 10, 31, 23, tzinfo=timezone.utc) - timedelta(hours=n))
               for n in range(45)]
    archives.update({"2026-11": [*november, november[0], game(9999, START + timedelta(seconds=1))],
                     "2026-10": [*october, game(8888, START - timedelta(days=3), control="300")]})
    profile_id = start(client)["profile"]["id"]
    finish_loading(profile_id)
    saved = games(profile_id)
    assert len(saved) == 40 and all(row[1] == "baseline" for row in saved)
    assert [row[2]["game_url"] for row in saved] == [item["game_url"] for item in [*november, *october[:30]]]
    assert all(row[2]["time_control"] == "300+5" for row in saved)
    assert client.get("/api/coach").json()["profile"]["baseline_games"] == 40


def test_short_history_is_finalized_honestly_and_new_games_never_rewrite_baseline(controlled, sources):
    client, clock = controlled
    archives, _ = sources
    archives["2026-11"] = [game(1, START - timedelta(hours=1)), game(2, START)]
    profile_id = start(client)["profile"]["id"]
    finish_loading(profile_id)
    baseline_ids = [row[0] for row in games(profile_id)]
    assert len(baseline_ids) == 2
    clock[0] += timedelta(hours=2)
    archives["2026-11"].extend([game(3, START + timedelta(hours=1)),
                                 game(4, START - timedelta(days=1))])
    assert client.post("/api/coach/sync", json={}).status_code == 200
    finish_sync(profile_id)
    saved = games(profile_id)
    assert [row[0] for row in saved if row[1] == "baseline"] == baseline_ids
    assert [row[2]["game_url"] for row in saved if row[1] == "followup"] == ["https://www.chess.com/game/live/3"]
    assert len(saved) == 3  # Late historical imports cannot contaminate the frozen baseline.
    client.post("/api/coach/sync", json={})
    finish_sync(profile_id)
    assert [row[0] for row in games(profile_id)] == [row[0] for row in saved]


def test_monitoring_catches_up_across_months_after_downtime(controlled, sources):
    client, clock = controlled
    archives, calls = sources
    archives["2026-11"] = [game(1, START - timedelta(hours=1))]
    profile_id = start(client)["profile"]["id"]
    finish_loading(profile_id)
    clock[0] = datetime(2027, 1, 5, tzinfo=timezone.utc)
    archives["2026-11"].append(game(2, datetime(2026, 11, 30, tzinfo=timezone.utc)))
    archives["2026-12"] = [game(3, datetime(2026, 12, 12, tzinfo=timezone.utc))]
    archives["2027-01"] = [game(4, datetime(2027, 1, 2, tzinfo=timezone.utc))]
    calls.clear()
    client.post("/api/coach/sync", json={})
    finish_sync(profile_id)
    assert {row[2]["game_url"] for row in games(profile_id) if row[1] == "followup"} == {
        f"https://www.chess.com/game/live/{number}" for number in (2, 3, 4)}
    assert {call[2] for call in calls if call[0] == "month"} >= {"2026-11", "2026-12", "2027-01"}


def test_paused_coach_neither_imports_nor_analyzes_and_worker_replacement_resumes(controlled, sources, fake_engine):
    client, _ = controlled
    archives, calls = sources
    archives["2026-11"] = [game(1, START - timedelta(hours=1))]
    profile_id = start(client)["profile"]["id"]
    assert client.post("/api/coach/pause", json={}).json()["profile"]["active"] is False
    checkpoint(profile_id)
    assert calls == [] and games(profile_id) == []
    client.post("/api/coach/resume", json={})
    finish_loading(profile_id)
    before = reach_analysis(profile_id)
    assert before[3] > 0 and before[5]
    client.post("/api/coach/pause", json={})
    checkpoint(profile_id)
    assert games(profile_id)[0] == before
    client.post("/api/coach/resume", json={})
    after = reach_analysis(profile_id, completed=True)
    assert after[4] == "completed" and after[3] == 4
    assert [row["ply"] for row in after[5]] == [0, 2]
    assert [ply for _, ply in fake_engine[0]] == [0, 2]


def test_failed_network_does_not_advance_month_checkpoint_and_resume_retries(controlled, sources, monkeypatch):
    from app import coach_sources
    from app.db import engine
    from app.models import CoachProfile
    client, _ = controlled
    sources[0]["2026-11"] = [game(1, START - timedelta(hours=1))]
    profile_id = start(client)["profile"]["id"]
    checkpoint(profile_id)  # Archive directory is now checkpointed.
    original = coach_sources.month_games
    monkeypatch.setattr(coach_sources, "month_games", lambda *args: (_ for _ in ()).throw(HTTPException(502, "Temporary source failure")))
    try:
        checkpoint(profile_id)
    except HTTPException as exc:
        assert exc.status_code in (502, 503)
    with Session(engine()) as db:
        profile = db.get(CoachProfile, profile_id)
        assert profile.sync_error and profile.sync_state["index"] == 0
    assert games(profile_id) == []
    monkeypatch.setattr(coach_sources, "month_games", original)
    client.post("/api/coach/resume", json={})
    finish_loading(profile_id)
    assert len(games(profile_id)) == 1


def test_engine_failure_retains_move_checkpoint_and_resume_continues(controlled, sources, fake_engine):
    client, _ = controlled
    sources[0]["2026-11"] = [game(1, START - timedelta(hours=1))]
    profile_id = start(client)["profile"]["id"]
    finish_loading(profile_id)
    before = reach_analysis(profile_id)
    fake_engine[1].add(2)
    try:
        checkpoint(profile_id)
    except HTTPException as exc:
        assert exc.status_code == 503
    failed = games(profile_id)[0]
    assert failed[3] == before[3] and failed[5] == before[5]
    assert failed[4] == "failed"
    fake_engine[1].clear()
    client.post("/api/coach/resume", json={})
    assert reach_analysis(profile_id, completed=True)[4] == "completed"


def test_busy_engine_preserves_intent_and_is_retried(controlled, sources, fake_engine):
    from app.background import AnalysisWorker
    from app.engine_resources import engine_slots
    client, _ = controlled
    sources[0]["2026-11"] = [game(1, START - timedelta(hours=1))]
    profile_id = start(client)["profile"]["id"]
    finish_loading(profile_id)
    finish_sync(profile_id)
    assert engine_slots.acquire(blocking=False)
    try:
        assert not AnalysisWorker().run_once()
    finally:
        engine_slots.release()
    assert client.get("/api/coach").json()["profile"]["active"]
    assert not games(profile_id)[0][5]
    assert AnalysisWorker().run_once()
    assert games(profile_id)[0][5]


def test_pause_is_accepted_during_search_and_a_second_worker_cannot_duplicate_it(controlled, sources, fake_engine, monkeypatch):
    from app import game_analysis_engine
    from app.background import AnalysisWorker
    client, _ = controlled
    sources[0]["2026-11"] = [game(1, START - timedelta(hours=1))]
    profile_id = start(client)["profile"]["id"]
    finish_loading(profile_id)
    finish_sync(profile_id)
    entered, release = Event(), Event()
    original = game_analysis_engine.analyse_game_move

    def blocking(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(game_analysis_engine, "analyse_game_move", blocking)
    with ThreadPoolExecutor(max_workers=2) as pool:
        step = pool.submit(AnalysisWorker().run_once)
        try:
            assert entered.wait(3)
            assert not AnalysisWorker().run_once()
            paused = pool.submit(client.post, "/api/coach/pause", json={}).result(timeout=2)
            assert paused.status_code == 200 and not paused.json()["profile"]["active"]
        finally:
            release.set()
        assert step.result(timeout=5)
    assert len(fake_engine[0]) == 1
    assert not client.get("/api/coach").json()["profile"]["active"]
    assert not AnalysisWorker().run_once()


def test_lifespan_imports_and_analyzes_without_browser_polling(controlled, sources, fake_engine):
    from fastapi.testclient import TestClient
    from app.main import app
    client, _ = controlled
    sources[0]["2026-11"] = [game(1, START - timedelta(hours=1))]
    profile_id = start(client)["profile"]["id"]
    with TestClient(app):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            saved = games(profile_id)
            if saved and saved[0][4] == "completed":
                break
            time.sleep(0.03)
        else:
            pytest.fail("Lifespan worker did not finish coach without client requests")
    assert [ply for _, ply in fake_engine[0]] == [0, 2]
    assert loaded(profile_id)


def test_unassessed_moves_get_one_retry_and_do_not_disappear(controlled, sources, fake_engine, monkeypatch):
    from app import game_analysis_engine
    client, _ = controlled
    sources[0]["2026-11"] = [game(1, START - timedelta(hours=1))]
    original = game_analysis_engine.analyse_game_move
    budgets = []

    def unassessed(*args, **kwargs):
        row = original(*args, **kwargs)
        budgets.append(kwargs)
        return {**row, "assessed": False, "expected_loss": None, "accuracy": None,
                "category": "unassessed", "best_uci": None, "best_san": None,
                "best_line_uci": [], "best_line_san": []}

    monkeypatch.setattr(game_analysis_engine, "analyse_game_move", unassessed)
    profile_id = start(client)["profile"]["id"]
    finish_loading(profile_id)
    saved = reach_analysis(profile_id, completed=True)
    assert [ply for _, ply in fake_engine[0]] == [0, 2, 0, 2]
    assert len(saved[5]) == 2 and all(not row["assessed"] and row["retry_attempts"] == 1 for row in saved[5])
    assert all(budget.get("nodes", 0) > game_analysis_engine.ANALYSIS_NODES for budget in budgets[2:])


def test_real_stockfish_turns_a_personal_blunder_into_a_playable_exercise(controlled, sources):
    executable = os.environ.get("TEST_STOCKFISH_PATH", "")
    if not executable or not Path(executable).is_file():
        pytest.skip("Set TEST_STOCKFISH_PATH for the coach engine integration smoke test")
    client, _ = controlled
    sources[0]["2026-11"] = [game(1, START - timedelta(hours=1))]
    profile_id = start(client)["profile"]["id"]
    finish_loading(profile_id)
    saved = reach_analysis(profile_id, completed=True)
    critical = next(row for row in saved[5] if row["ply"] == 2)
    assert critical["assessed"] and critical["expected_loss"] > .03
    assert critical["mate_after"] < 0 and critical["best_uci"] != "g2g4"
    assert critical["skill_ids"]
    response = client.post("/api/coach/practice", json={"skill_id": critical["skill_ids"][0],
                                                       "game_id": saved[0], "ply": 2})
    assert response.status_code == 200, response.text
    attempt = response.json()
    assert attempt["solution"] is None and attempt["kind"] == "decision"
    solved = client.post(f"/api/coach/practice/{attempt['id']}/move", json={"move": critical["best_uci"]})
    assert solved.status_code == 200 and solved.json()["accepted"]
    board = chess.Board(attempt["starting_fen"])
    for move in solved.json()["attempt"]["solution"]["moves"]:
        board.push_uci(move["uci"])


@pytest.fixture
def personal_position(controlled, sources, fake_engine):
    client, _ = controlled
    sources[0]["2026-11"] = [game(1, START - timedelta(hours=1))]
    profile_id = start(client)["profile"]["id"]
    finish_loading(profile_id)
    source = reach_analysis(profile_id, completed=True)
    response = client.post("/api/coach/practice", json={"skill_id": "calculation", "game_id": source[0], "ply": 0})
    assert response.status_code == 200, response.text
    return client, profile_id, source[0], response.json()


def test_personal_practice_hides_solution_reuses_active_attempt_and_persists_time(personal_position):
    client, _, game_id, attempt = personal_position
    path = f"/api/coach/practice/{attempt['id']}"
    assert attempt["status"] == "active" and attempt["solution"] is None
    assert "payload" not in attempt and "best_uci" not in attempt
    assert attempt["source"]["own_game"] and attempt["kind"] == "decision"
    again = client.post("/api/coach/practice", json={"skill_id": "calculation", "game_id": game_id, "ply": 0}).json()
    assert again["id"] == attempt["id"]
    assert client.get("/api/coach").json()["active_attempt_id"] == attempt["id"]
    assert client.post(path + "/time", json={"elapsed_ms": 12000}).json()["elapsed_ms"] == 12000
    assert client.post(path + "/time", json={"elapsed_ms": 5000}).json()["elapsed_ms"] == 12000
    for value in (-1, True, "1000"):
        assert client.post(path + "/time", json={"elapsed_ms": value}).status_code == 422
    revealed = client.post(path + "/reveal", json={"elapsed_ms": 13000}).json()
    assert revealed["status"] == "revealed" and revealed["solution"]["moves"][0]["uci"] == "e2e4"
    assert client.post(path + "/time", json={"elapsed_ms": 99000}).json()["elapsed_ms"] == 13000
    assert client.post(path + "/reveal", json={}).json()["status"] == "revealed"


def test_correct_personal_decision_finishes_and_schedules_review(personal_position):
    from app.db import engine
    from app.models import CoachReview
    client, profile_id, _, attempt = personal_position
    response = client.post(f"/api/coach/practice/{attempt['id']}/move", json={"move": "e2e4", "elapsed_ms": 9000})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["accepted"] and result["attempt"]["status"] == "completed"
    assert result["attempt"]["solution"] and result["attempt"]["mistakes"] == 0
    with Session(engine()) as db:
        review = db.scalar(select(CoachReview).where(CoachReview.coach_id == profile_id))
        assert review is not None and review.successes == 1
    assert client.get("/api/coach").json()["active_attempt_id"] is None


def test_due_review_resumes_same_active_moment_across_skill_tracks(controlled, sources, fake_engine):
    from app.db import engine
    from app.models import CoachAttempt, CoachReview, now
    client, _ = controlled
    sources[0]["2026-11"] = [game(1, START - timedelta(hours=1))]
    profile_id = start(client)["profile"]["id"]
    finish_loading(profile_id)
    source = reach_analysis(profile_id, completed=True)

    opening = client.post("/api/coach/practice", json={"skill_id": "opening", "game_id": source[0], "ply": 0})
    assert opening.status_code == 200, opening.text
    original_id = opening.json()["id"]
    solved = client.post(f"/api/coach/practice/{original_id}/move", json={"move": "e2e4"})
    assert solved.status_code == 200 and solved.json()["attempt"]["status"] == "completed"
    with Session(engine()) as db:
        review = db.scalar(select(CoachReview).where(CoachReview.coach_id == profile_id))
        assert review.skill_id == "opening"
        review.due_at = now() - timedelta(minutes=1)
        db.commit()

    calculation = client.post("/api/coach/practice", json={"skill_id": "calculation", "game_id": source[0], "ply": 0})
    assert calculation.status_code == 200, calculation.text
    active_id = calculation.json()["id"]
    assert active_id != original_id and calculation.json()["status"] == "active"
    due_review = client.post("/api/coach/practice", json={"skill_id": "opening"})
    assert due_review.status_code == 200, due_review.text
    assert due_review.json()["id"] == active_id
    with Session(engine()) as db:
        active = list(db.scalars(select(CoachAttempt).where(CoachAttempt.coach_id == profile_id,
                                                          CoachAttempt.status == "active")))
        assert [attempt.id for attempt in active] == [active_id]
        assert len(list(db.scalars(select(CoachAttempt.id).where(CoachAttempt.coach_id == profile_id)))) == 2


def test_wrong_legal_move_counts_but_illegal_move_does_not(personal_position, monkeypatch):
    from app import game_analysis_engine
    client, _, _, attempt = personal_position
    path = f"/api/coach/practice/{attempt['id']}"
    assert client.post(path + "/move", json={"move": "e2e5"}).status_code == 422
    assert client.get(path).json()["mistakes"] == 0
    original = game_analysis_engine.analyse_game_move

    def worse(*args, **kwargs):
        row = original(*args, **kwargs)
        return {**row, "expected_loss": 0.3, "assessed": True, "category": "blunder"}

    monkeypatch.setattr(game_analysis_engine, "analyse_game_move", worse)
    result = client.post(path + "/move", json={"move": "a2a3", "elapsed_ms": 2500})
    assert result.status_code == 200, result.text
    body = result.json()
    assert not body["accepted"] and body["attempt"]["mistakes"] == 1
    assert body["attempt"]["fen"] == attempt["fen"] and body["attempt"]["status"] == "active"
    assert body["attempt"]["solution"] is None


def test_accepted_alternative_replays_submitted_line_and_review_retains_reference(personal_position, monkeypatch):
    from app import game_analysis_engine
    from app.db import engine
    from app.models import CoachAttempt, CoachReview, now
    client, profile_id, _, attempt = personal_position
    path = f"/api/coach/practice/{attempt['id']}"
    calls = []
    original = game_analysis_engine.analyse_game_move
    accepted_line = ["d2d4", "d7d5", "c2c4", "e7e6"]

    def checked(data, ply, *args, **kwargs):
        move = data["moves"][ply]
        calls.append(move)
        rejected = move == "a2a3"
        return {**original(data, ply, *args, **kwargs), "assessed": True,
                "expected_loss": 0.3 if rejected else 0.005,
                "category": "blunder" if rejected else "excellent",
                "score_before": 20, "score_after": -500 if rejected else 15,
                "mate_before": None, "mate_after": None,
                # Neither this new root choice nor the saved e4 reference is d4.
                "best_uci": "c2c4", "best_line_uci": ["c2c4", "e7e5"],
                "played_line_uci": [move] if rejected else accepted_line}

    monkeypatch.setattr(game_analysis_engine, "analyse_game_move", checked)
    rejected = client.post(path + "/move", json={"move": "a2a3", "elapsed_ms": 2000})
    assert rejected.status_code == 200 and not rejected.json()["accepted"]
    assert rejected.json()["attempt"]["mistakes"] == 1
    assert rejected.json()["attempt"]["solution"] is None
    response = client.post(path + "/move", json={"move": "d2d4", "elapsed_ms": 4000})
    assert response.status_code == 200, response.text
    result = response.json()["attempt"]
    assert calls == ["a2a3", "d2d4"] and response.json()["accepted"]
    assert result["status"] == "completed" and result["mistakes"] == 1
    assert result["last_move"] == "d2d4" and result["elapsed_ms"] == 4000
    solution = result["solution"]
    assert solution["kind"] == "accepted"
    assert solution["reference_move"] == {"uci": "e2e4", "san": "e4"}
    assert [move["uci"] for move in solution["moves"]] == accepted_line
    board = chess.Board(attempt["starting_fen"])
    for index, move in enumerate(solution["moves"]):
        assert move["san"] == board.san(chess.Move.from_uci(move["uci"]))
        board.push_uci(move["uci"])
        assert move["fen"] == board.fen()
        if index == 0:
            assert result["fen"] == board.fen()
    assert client.get(path).json() == result
    assert "move_checks" not in result and "payload" not in result
    with Session(engine()) as db:
        saved = db.get(CoachAttempt, attempt["id"])
        assert saved.payload["moves"] == ["e2e4"]
        assert saved.payload["accepted_moves"] == accepted_line
        assert saved.payload["accepted_uci"] == "d2d4"
        checks = saved.payload["move_checks"]
        assert [(item["uci"], item["verdict"], item["source"]) for item in checks] == [
            ("a2a3", "rejected", "engine"), ("d2d4", "accepted", "engine")]
        assert checks[-1]["best_uci"] == "c2c4" and checks[-1]["score_after"] == 15
        assert checks[-1]["expected_loss"] == 0.005 and checks[-1]["elapsed_ms"] == 4000
        assert checks[-1]["checker_version"] == 1
        assert checks[-1]["engine_name"] == "Deterministic coach test engine"
        assert datetime.fromisoformat(checks[-1]["checked_at"]).tzinfo is not None
        review = db.scalar(select(CoachReview).where(CoachReview.coach_id == profile_id))
        assert review.successes == 0  # Earlier mistake remains part of this attempt.
        review.due_at = now() - timedelta(minutes=1)
        db.commit()

    repeat = client.post("/api/coach/practice", json={"skill_id": "calculation"})
    assert repeat.status_code == 200, repeat.text
    fresh = repeat.json()
    assert fresh["id"] != attempt["id"] and not fresh["is_new"]
    assert fresh["status"] == "active" and fresh["solution"] is None
    assert fresh["mistakes"] == 0 and fresh["fen"] == attempt["starting_fen"]
    with Session(engine()) as db:
        payload = db.get(CoachAttempt, fresh["id"]).payload
        assert payload["moves"] == ["e2e4"]
        assert not any(key.startswith("accepted_") or key == "move_checks" for key in payload)
    revealed = client.post(f"/api/coach/practice/{fresh['id']}/reveal", json={}).json()
    assert revealed["solution"]["kind"] == "reference"
    assert [move["uci"] for move in revealed["solution"]["moves"]] == ["e2e4"]
    assert client.get(path).json()["solution"] == solution


def test_move_check_history_retains_recent_verdicts_without_resetting_mistakes(personal_position):
    from app.db import engine
    from app.models import CoachAttempt
    client, _, _, attempt = personal_position
    path = f"/api/coach/practice/{attempt['id']}"
    # The fake engine reliably rejects a3. A long practice session still keeps
    # all errors in its counter while bounding the private diagnostic history.
    for index in range(34):
        response = client.post(path + "/move", json={"move": "a2a3", "elapsed_ms": index + 1})
        assert response.status_code == 200 and not response.json()["accepted"]
    response = client.post(path + "/move", json={"move": "e2e4", "elapsed_ms": 35})
    assert response.status_code == 200 and response.json()["accepted"]
    assert response.json()["attempt"]["mistakes"] == 34
    with Session(engine()) as db:
        checks = db.get(CoachAttempt, attempt["id"]).payload["move_checks"]
        assert len(checks) == 32
        assert [row["elapsed_ms"] for row in checks] == list(range(4, 36))
        assert all(row["verdict"] == "rejected" for row in checks[:-1])
        assert checks[-1]["verdict"] == "accepted" and checks[-1]["source"] == "reference"


def test_legacy_completed_alternative_replays_actual_move_without_inventing_continuation(personal_position):
    from app.db import engine
    from app.models import CoachAttempt, now
    client, _, _, attempt = personal_position
    board = chess.Board(attempt["starting_fen"])
    board.push_uci("d2d4")
    with Session(engine()) as db:
        saved = db.get(CoachAttempt, attempt["id"])
        saved.status, saved.completed_at, saved.ply_index = "completed", now(), 1
        saved.fen = board.fen()
        saved.payload = {**saved.payload, "moves": ["e2e4", "e7e5", "g1f3"], "last_move": "d2d4",
                         "feedback": "Ваше продолжение также подходит — проверено движком. В разборе показан исходный вариант."}
        db.commit()
    result = client.get(f"/api/coach/practice/{attempt['id']}").json()
    assert result["solution"]["kind"] == "accepted"
    assert "исходный вариант" not in result["feedback"]
    assert result["solution"]["reference_move"] == {"uci": "e2e4", "san": "e4"}
    assert result["solution"]["moves"] == [{"uci": "d2d4", "san": "d4", "fen": board.fen()}]


def test_immediate_mating_alternative_replays_actual_mate_without_engine(personal_position, monkeypatch):
    from app import game_analysis_engine
    from app.db import engine
    from app.models import CoachAttempt
    client, _, _, attempt = personal_position
    fen = "7k/5K2/6Q1/8/8/8/8/8 w - - 0 1"
    with Session(engine()) as db:
        saved = db.get(CoachAttempt, attempt["id"])
        saved.fen = saved.starting_fen = fen
        saved.payload = {**saved.payload, "moves": ["g6g7"], "best_uci": "g6g7",
                         "game_initial_fen": fen, "prefix_moves": []}
        db.commit()

    def no_search(*args, **kwargs):
        raise AssertionError("An immediate mate is established by the legal board position")

    monkeypatch.setattr(game_analysis_engine, "analyse_game_move", no_search)
    path = f"/api/coach/practice/{attempt['id']}"
    response = client.post(path + "/move", json={"move": "g6h6"})
    assert response.status_code == 200, response.text
    result = response.json()["attempt"]
    assert response.json()["accepted"] and result["mistakes"] == 0
    assert chess.Board(result["fen"]).is_checkmate()
    assert result["solution"]["kind"] == "accepted"
    assert result["solution"]["reference_move"] == {"uci": "g6g7", "san": "Qg7#"}
    assert result["solution"]["moves"] == [{"uci": "g6h6", "san": "Qh6#", "fen": result["fen"]}]
    assert client.get(path).json() == result


@pytest.mark.parametrize("mate_after,accepted", [(-1, False), (-8, True), (-9, True)])
def test_alternative_in_forced_loss_must_preserve_resistance(personal_position, monkeypatch, mate_after, accepted):
    from app import game_analysis_engine
    client, _, _, attempt = personal_position
    original = game_analysis_engine.analyse_game_move

    def forced_loss(*args, **kwargs):
        return {**original(*args, **kwargs), "expected_loss": 0, "assessed": True,
                "score_before": None, "score_after": None,
                "mate_before": -8, "mate_after": mate_after}

    monkeypatch.setattr(game_analysis_engine, "analyse_game_move", forced_loss)
    response = client.post(f"/api/coach/practice/{attempt['id']}/move", json={"move": "d2d4"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["accepted"] is accepted
    assert result["attempt"]["status"] == ("completed" if accepted else "active")
    assert result["attempt"]["mistakes"] == (0 if accepted else 1)


def test_uncertain_alternative_is_not_counted_as_an_error(personal_position, monkeypatch):
    from app import game_analysis_engine
    client, _, _, attempt = personal_position
    original = game_analysis_engine.analyse_game_move

    def uncertain(*args, **kwargs):
        return {**original(*args, **kwargs), "expected_loss": None, "assessed": False,
                "category": "unassessed"}

    monkeypatch.setattr(game_analysis_engine, "analyse_game_move", uncertain)
    path = f"/api/coach/practice/{attempt['id']}"
    assert client.post(path + "/move", json={"move": "d2d4"}).status_code == 503
    saved = client.get(path).json()
    assert saved["status"] == "active" and saved["mistakes"] == 0
    assert saved["fen"] == attempt["fen"] and saved["solution"] is None


@pytest.mark.parametrize("candidate_line", [None, [], ["e2e4"],
    ["d2d4", "d7d5", "d2d4"], ["d2d4", "0000"], ["d2d4", None]])
def test_unusable_candidate_replay_does_not_accept_or_count_a_mistake(personal_position, monkeypatch, candidate_line):
    from app import game_analysis_engine
    from app.db import engine
    from app.models import CoachAttempt
    client, _, _, attempt = personal_position
    original = game_analysis_engine.analyse_game_move

    def incomplete(*args, **kwargs):
        row = {**original(*args, **kwargs), "assessed": True, "expected_loss": 0.005,
               "score_before": 20, "score_after": 15, "played_line_uci": candidate_line}
        if candidate_line is None:
            row.pop("played_line_uci")
        return row

    monkeypatch.setattr(game_analysis_engine, "analyse_game_move", incomplete)
    path = f"/api/coach/practice/{attempt['id']}"
    response = client.post(path + "/move", json={"move": "d2d4", "elapsed_ms": 2000})
    assert response.status_code == 503, response.text
    result = client.get(path).json()
    assert result["status"] == "active" and result["mistakes"] == 0
    assert result["fen"] == attempt["fen"] and result["solution"] is None
    with Session(engine()) as db:
        payload = db.get(CoachAttempt, attempt["id"]).payload
        assert not payload.get("move_checks")
        assert not any(key.startswith("accepted_") for key in payload)


def test_all_skill_tracks_can_start_supplementary_practice(controlled, sources):
    from app.coach_insights import SKILLS
    client, _ = controlled
    sources[0]["2026-11"] = [game(200, START - timedelta(hours=1))]
    profile_id = start(client)["profile"]["id"]
    finish_loading(profile_id)
    for skill_id in SKILLS:
        response = client.post("/api/coach/practice", json={"skill_id": skill_id})
        assert response.status_code == 200, (skill_id, response.text)
        attempt = response.json()
        assert attempt["skill_id"] == skill_id and attempt["kind"] == "puzzle"
        assert not attempt["source"]["own_game"] and attempt["solution"] is None


def test_supplementary_task_checks_entire_line_and_records_only_coach_practice(controlled, sources):
    from app.db import engine
    from app.models import Attempt, CoachAttempt
    client, _ = controlled
    sources[0]["2026-11"] = [game(201, START - timedelta(hours=1))]
    profile_id = start(client)["profile"]["id"]
    finish_loading(profile_id)
    response = client.post("/api/coach/practice", json={"skill_id": "calculation"})
    assert response.status_code == 200
    attempt = response.json()
    for _ in range(12):
        if attempt["status"] == "completed":
            break
        with Session(engine()) as db:
            record = db.get(CoachAttempt, attempt["id"])
            expected = record.payload["moves"][record.ply_index]
        reply = client.post(f"/api/coach/practice/{attempt['id']}/move", json={"move": expected})
        assert reply.status_code == 200 and reply.json()["accepted"]
        attempt = reply.json()["attempt"]
    assert attempt["status"] == "completed" and attempt["ply_index"] == attempt["total_plies"]
    assert attempt["solution"]["moves"]
    with Session(engine()) as db:
        assert not db.scalar(select(Attempt.id).limit(1))


def test_empty_setup_stays_correctable_instead_of_creating_practice_history(controlled, sources):
    from app.db import engine
    from app.models import CoachAttempt
    client, _ = controlled
    profile_id = start(client)["profile"]["id"]
    finish_loading(profile_id)
    assert client.post("/api/coach/practice", json={}).status_code == 409
    with Session(engine()) as db:
        assert db.scalar(select(CoachAttempt.id).limit(1)) is None
    corrected = client.post("/api/coach/start", json={"username": "corrected", "time_control": "600"})
    assert corrected.status_code == 200
    assert corrected.json()["profile"]["username"] == "corrected"


def test_hint_records_assistance_and_owner_cannot_read_or_mutate_another_coach(personal_position):
    from app.db import engine
    from app.models import CoachProfile, User
    from app.security import hash_password
    client, profile_id, game_id, attempt = personal_position
    path = f"/api/coach/practice/{attempt['id']}"
    hinted = client.post(path + "/hint", json={"elapsed_ms": 1000})
    assert hinted.status_code == 200 and hinted.json()["attempt"]["hints_used"] > 0
    with Session(engine()) as db:
        user = User(username="coach-other-" + uuid4().hex, password_hash=hash_password("test-only-password"))
        db.add(user)
        db.flush()
        db.get(CoachProfile, profile_id).user_id = user.id
        db.commit()
    assert client.get("/api/coach").json()["profile"] is None
    assert client.get(path).status_code == 404
    for suffix, body in (("move", {"move": "e2e4"}), ("hint", {}), ("reveal", {}), ("time", {"elapsed_ms": 1})):
        assert client.post(path + "/" + suffix, json=body).status_code == 404
    assert client.post("/api/coach/practice", json={"skill_id": "calculation", "game_id": game_id, "ply": 0}).status_code == 404
    client.post("/api/auth/logout", json={})
    assert client.get("/api/coach").status_code == 401
    assert client.post("/api/coach/start", json={"username": "reviewer", "time_control": "300+5"}).status_code == 401
