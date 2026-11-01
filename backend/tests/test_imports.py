"""Persistence, visibility and bounded upstream requests for generated collections."""
from copy import deepcopy
import io
import json
from urllib.error import HTTPError
from uuid import uuid4

import chess
from fastapi import HTTPException
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

PGN = '''[Event "Integration game"]
[Site "https://www.chess.com/game/live/12345"]
[Date "2025.01.02"]
[White "TesterChess"]
[Black "Opponent"]
[Result "1-0"]

1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5 1-0
'''


@pytest.fixture
def source_game():
    from app.generation import parse_games
    return parse_games(PGN)[0]


@pytest.fixture
def fake_analysis(monkeypatch, source_game):
    from app import generation, imports_api
    board = chess.Board()
    board.push_uci("e2e4")
    puzzle_fen = board.fen()
    board.push_uci("e7e5")
    candidate = {"ply": 2, "move_number": 2, "side": "white", "before_fen": board.fen(),
                 "played_uci": "g1f3", "played_san": "Nf3", "best_uci": "f1c4", "best_san": "Bc4",
                 "score_before": 230, "score_after": 50, "loss_cp": 180, "mate_before": None,
                 "mate_after": None, "best_line_san": ["Bc4"], "explanation": "Test improvement",
                 "phase": "opening", "puzzle": {"initial_fen": puzzle_fen, "moves": ["e7e5", "f1c4"],
                                                   "rating": 1300, "themes": ["quietMove"]}}
    calls = []

    class Engine:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def configure(self, options):
            assert options == {"Threads": 1, "Hash": 32}

    def analyse(game, ply, engine, **options):
        calls.append((ply, options))
        return deepcopy(candidate) if ply == 2 else None

    monkeypatch.setattr(chess.engine.SimpleEngine, "popen_uci", lambda *args, **kwargs: Engine())
    monkeypatch.setattr(generation, "analyse_position", analyse)
    monkeypatch.setattr(imports_api, "chesscom_games", lambda username, month, limit: (month or "2025-01", [deepcopy(source_game)]))
    return calls, candidate


def create_import(client, **overrides):
    response = client.post("/api/imports/chesscom", json={"username": "TesterChess", "month": "2025-01", **overrides})
    assert response.status_code == 200, response.text
    return response.json()


def step(client, job):
    response = client.post(f"/api/imports/{job['id']}/step", json={})
    assert response.status_code == 200, response.text
    return response.json()


def test_import_auth_and_validation(client, fake_analysis):
    assert client.get("/api/imports").status_code == 401
    assert client.post("/api/imports/chesscom", json={"username": "TesterChess"}).status_code == 401
    client.post("/api/auth/login", json={"username": "tester", "password": "integration-test-only-password"})
    for payload in ({"username": "../../localhost"}, {"username": "https://example.com"},
                    {"username": "x", "month": "2025-13"}, {"username": "x", "month": "2006-01"},
                    {"username": "x", "limit": 51}, {"username": "x", "limit": True}):
        assert client.post("/api/imports/chesscom", json=payload).status_code == 422
    assert client.post("/api/imports/public", headers={"Origin": "https://evil.invalid"}).status_code == 403


def test_personal_import_resumes_and_creates_a_playable_scoped_puzzle(logged, fake_analysis):
    calls, _ = fake_analysis
    job = create_import(logged)
    assert job["username"] == "testerchess" and job["status"] == "ready"
    assert create_import(logged)["id"] == job["id"]
    first = step(logged, job)
    assert first["status"] == "running" and first["games"][0]["cursor"] == 4
    candidate = first["games"][0]["candidates"][0]
    assert candidate["puzzle_available"] and candidate["puzzle_id"] is None
    assert candidate["phase"] == "opening" and "puzzle" not in candidate
    saved = logged.get(f"/api/imports/{job['id']}").json()
    assert saved == first
    completed = step(logged, saved)
    assert completed["status"] == "completed" and completed["completed_games"] == 1
    assert [ply for ply, _ in calls] == [0, 2, 4]
    assert all(options["require_mistake"] for _, options in calls)
    assert step(logged, completed) == completed and len(calls) == 3
    assert logged.get("/api/imports").json()["jobs"][0]["candidates_count"] == 1
    result = logged.post(f"/api/candidates/{candidate['id']}/puzzle", json={})
    assert result.status_code == 200, result.text
    created = result.json()
    assert created["collection"] == "personal" and created["created"]
    again = logged.post(f"/api/candidates/{candidate['id']}/puzzle", json={}).json()
    assert again == {**created, "created": False}
    meta = logged.get("/api/meta?collection=personal").json()
    assert meta["total_puzzles"] == 1 and meta["themes"] == [{"id": "quietMove", "name": "Тихий ход", "count": 1}]
    attempt = logged.post("/api/training/start", json={"puzzle_id": created["puzzle_id"], "min_rating": 3000,
                                                     "max_rating": 4000, "theme": "unknown"}).json()
    assert attempt["collection"] == "personal" and attempt["rating_estimated"]
    assert attempt["fen"] == candidate["before_fen"]
    solved = logged.post(f"/api/training/{attempt['id']}/move", json={"move": "f1c4", "elapsed_ms": 8500}).json()
    assert solved["attempt"]["status"] == "completed"
    stats = logged.get("/api/stats").json()
    assert stats["average_solution_ms"] == 8500 and stats["recent"][0]["rating_estimated"]
    assert logged.post("/api/training/start", json={"min_rating": 0, "max_rating": 4000}).json()["collection"] == "lichess"


def test_foreign_jobs_candidates_and_personal_puzzles_are_hidden(logged, fake_analysis):
    from app.db import engine
    from app.models import ImportJob, Puzzle, User
    from app.security import hash_password
    job = step(logged, create_import(logged))
    candidate_id = job["games"][0]["candidates"][0]["id"]
    puzzle_id = logged.post(f"/api/candidates/{candidate_id}/puzzle", json={}).json()["puzzle_id"]
    with Session(engine()) as db:
        other = User(username="imports-other-" + uuid4().hex[:12], password_hash=hash_password("other-import-password"))
        db.add(other)
        db.flush()
        db.get(ImportJob, job["id"]).user_id = other.id
        db.get(Puzzle, puzzle_id).owner_id = other.id
        db.commit()
    assert logged.get("/api/imports").json() == {"jobs": []}
    assert logged.get(f"/api/imports/{job['id']}").status_code == 404
    assert logged.post(f"/api/imports/{job['id']}/step", json={}).status_code == 404
    assert logged.post(f"/api/candidates/{candidate_id}/puzzle", json={}).status_code == 404
    assert logged.get("/api/meta?collection=personal").json()["total_puzzles"] == 0
    assert logged.post("/api/training/start", json={"puzzle_id": puzzle_id}).status_code == 404


def test_black_player_only_and_games_without_improvements_remain_visible(logged, fake_analysis):
    job = create_import(logged, username="Opponent")
    job = step(logged, step(logged, job))
    assert [ply for ply, _ in fake_analysis[0]] == [1, 3, 5]
    assert job["status"] == "completed" and job["candidates_count"] == 0
    assert len(job["games"]) == 1 and job["games"][0]["black"] == "Opponent"
    assert job["games"][0]["status"] == "completed"


def test_failed_step_rolls_back_cursor_and_candidates_then_can_retry(logged, fake_analysis, monkeypatch):
    from app import generation
    calls, candidate = fake_analysis
    job = create_import(logged)
    original = generation.analyse_position

    def broken(game, ply, engine, **options):
        if ply == 2:
            raise chess.engine.EngineTerminatedError("Interrupted")
        return {**deepcopy(candidate), "ply": ply}

    monkeypatch.setattr(generation, "analyse_position", broken)
    assert logged.post(f"/api/imports/{job['id']}/step", json={}).status_code == 503
    failed = logged.get(f"/api/imports/{job['id']}").json()
    assert failed["status"] == "failed" and failed["error"]
    assert failed["games"][0]["cursor"] == 0 and failed["candidates_count"] == 0
    monkeypatch.setattr(generation, "analyse_position", original)
    retried = step(logged, failed)
    assert retried["status"] == "running" and retried["error"] is None and retried["candidates_count"] == 1


def test_engine_busy_and_concurrent_step_preserve_progress(logged, fake_analysis):
    from app.db import engine
    from app.engine_resources import engine_slots
    job = create_import(logged)
    assert engine_slots.acquire(blocking=False)
    try:
        assert logged.post(f"/api/imports/{job['id']}/step", json={}).status_code == 503
    finally:
        engine_slots.release()
    with Session(engine()) as db, db.begin():
        db.scalar(select(func.pg_advisory_xact_lock(782104, job["id"])))
        assert logged.post(f"/api/imports/{job['id']}/step", json={}).status_code == 409
    assert logged.get(f"/api/imports/{job['id']}").json()["games"][0]["cursor"] == 0
    assert step(logged, job)["candidates_count"] == 1


def test_new_games_in_same_month_reuse_completed_analysis(logged, fake_analysis, source_game, monkeypatch):
    from app import imports_api
    original = step(logged, step(logged, create_import(logged)))
    candidate_id = original["games"][0]["candidates"][0]["id"]
    puzzle_id = logged.post(f"/api/candidates/{candidate_id}/puzzle", json={}).json()["puzzle_id"]
    new_game = {**deepcopy(source_game), "game_url": "https://www.chess.com/game/live/98765"}
    monkeypatch.setattr(imports_api, "chesscom_games", lambda *args: ("2025-01", [source_game, new_game, new_game]))
    updated = create_import(logged)
    assert updated["id"] != original["id"] and updated["total_games"] == 2
    assert updated["completed_games"] == 1 and updated["games"][0]["candidates"][0]["puzzle_id"] == puzzle_id
    updated = step(logged, step(logged, updated))
    second_id = updated["games"][1]["candidates"][0]["id"]
    assert logged.post(f"/api/candidates/{second_id}/puzzle", json={}).json() == {
        "puzzle_id": puzzle_id, "collection": "personal", "created": False}
    assert logged.get("/api/meta?collection=personal").json()["total_puzzles"] == 1


def test_public_batch_analyses_both_sides_and_publishes_eligible_moments(logged, fake_analysis, tmp_path, monkeypatch):
    path = tmp_path / "public.pgn"
    path.write_text(PGN)
    monkeypatch.setenv("PUBLIC_GAMES_PATH", str(path))
    before = logged.get("/api/meta?collection=public").json()["total_puzzles"]
    job = logged.post("/api/imports/public", json={}).json()
    assert job["source"] == "public" and job["username"] is None
    for _ in range(3):
        job = step(logged, job)
    assert job["status"] == "completed" and job["puzzles_count"] == 1
    assert [ply for ply, _ in fake_analysis[0]] == list(range(6))
    assert not any(options["require_mistake"] for _, options in fake_analysis[0])
    assert logged.get("/api/meta?collection=public").json()["total_puzzles"] == before + 1
    assert logged.post("/api/imports/public", json={}).json()["id"] == job["id"]


def test_improvement_without_verified_tactic_cannot_be_created(logged, fake_analysis):
    from app.db import engine
    from app.models import Improvement
    job = step(logged, create_import(logged))
    candidate_id = job["games"][0]["candidates"][0]["id"]
    with Session(engine()) as db:
        candidate = db.get(Improvement, candidate_id)
        candidate.data = {**candidate.data, "puzzle": None}
        db.commit()
    assert logged.post(f"/api/candidates/{candidate_id}/puzzle", json={}).status_code == 422
    assert logged.get("/api/meta?collection=personal").json()["total_puzzles"] == 0


def test_latest_chesscom_archive_uses_fixed_paths_and_skips_other_games(monkeypatch):
    from app import game_sources
    paths = []

    def fetch(path):
        paths.append(path)
        if path.endswith("archives"):
            return {"archives": ["http://localhost/private", "https://api.chess.com/pub/player/other/games/2025/03",
                                  "https://api.chess.com/pub/player/testerchess/games/2025/01",
                                  "https://api.chess.com/pub/player/testerchess/games/2025/02"]}
        return {"games": [{"pgn": PGN, "rules": "chess960", "end_time": 500},
                           {"pgn": PGN.replace("TesterChess", "SomeoneElse"), "rules": "chess", "end_time": 400},
                           {"pgn": PGN, "rules": "chess", "end_time": 300, "url": None},
                           {"pgn": PGN, "rules": "chess", "end_time": 200}]}

    monkeypatch.setattr(game_sources, "fetch_json", fetch)
    month, games = game_sources.chesscom_games("TesterChess", None, 1)
    assert month == "2025-02" and len(games) == 1 and games[0]["white"] == "TesterChess"
    assert paths == ["testerchess/games/archives", "testerchess/games/2025/02"]
    for invalid in (None, {}, [], 1, "https://lichess.org@localhost/game", "http://lichess.org/abc", "https://evil.invalid/abc"):
        assert game_sources.safe_game_url(invalid) == ""


def test_chesscom_upstream_errors_and_size_limits_are_readable(monkeypatch):
    from app import game_sources

    class Response(io.BytesIO):
        headers = {}

    class Opener:
        def __init__(self, response=None, error=None):
            self.response, self.error = response, error

        def open(self, request, timeout):
            assert request.full_url == "https://api.chess.com/pub/player/tester/games/2025/01"
            assert timeout <= 8
            if self.error:
                raise self.error
            return self.response

    for code, expected in ((429, 429), (404, 404), (302, 502)):
        upstream = HTTPError("https://api.chess.com/", code, "Error", {}, None)
        monkeypatch.setattr(game_sources, "build_opener", lambda *args: Opener(error=upstream))
        with pytest.raises(HTTPException) as error:
            game_sources.fetch_json("tester/games/2025/01")
        assert error.value.status_code == expected
    for body, size, expected in ((b"not-json", 100, 502), (json.dumps({"games": []}).encode(), 4, 413)):
        monkeypatch.setattr(game_sources, "MAX_RESPONSE_BYTES", size)
        monkeypatch.setattr(game_sources, "build_opener", lambda *args: Opener(response=Response(body)))
        with pytest.raises(HTTPException) as error:
            game_sources.fetch_json("tester/games/2025/01")
        assert error.value.status_code == expected
