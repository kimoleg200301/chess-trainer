"""Persistence, visibility and bounded upstream requests for generated collections."""
from copy import deepcopy
from datetime import datetime, timezone
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
                 "phase": "opening", "puzzle_reason_code": None, "puzzle_reason": None, "analysis_depth": 18, "puzzle": {"initial_fen": puzzle_fen, "moves": ["e7e5", "f1c4"],
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


def finish(client, job):
    for _ in range(25):
        if job["status"] == "completed":
            return job
        job = step(client, job)
    raise AssertionError("Fixture analysis did not finish")


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
    assert first["status"] == "running" and first["games"][0]["cursor"] == 2
    assert len(calls) == 1 and first["candidates_count"] == 0
    first = step(logged, first)
    candidate = first["games"][0]["candidates"][0]
    assert candidate["puzzle_available"] and candidate["puzzle_id"] is not None
    assert not candidate["analysis_pending"] and candidate["analysis_depth"] == 18
    assert first["available_puzzles_count"] == first["puzzles_count"] == 1
    assert candidate["phase"] == "opening" and "puzzle" not in candidate
    saved = logged.get(f"/api/imports/{job['id']}").json()
    assert saved == first
    completed = step(logged, saved)
    assert completed["status"] == "completed" and completed["completed_games"] == 1
    assert [ply for ply, _ in calls] == [0, 2, 4]
    assert all(options["require_mistake"] and options["max_time"] == 1.2 and options["max_position_time"] == 14 for _, options in calls)
    assert step(logged, completed) == completed and len(calls) == 3
    assert logged.get("/api/imports").json()["jobs"][0]["candidates_count"] == 1
    result = logged.post(f"/api/candidates/{candidate['id']}/puzzle", json={})
    assert result.status_code == 200, result.text
    created = result.json()
    assert created["collection"] == "personal" and not created["created"]
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
    job = step(logged, step(logged, create_import(logged)))
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
    job = finish(logged, job)
    assert [ply for ply, _ in fake_analysis[0]] == [1, 3, 5]
    assert job["status"] == "completed" and job["candidates_count"] == 0
    assert len(job["games"]) == 1 and job["games"][0]["black"] == "Opponent"
    assert job["games"][0]["status"] == "completed"


def test_failed_step_rolls_back_cursor_and_candidates_then_can_retry(logged, fake_analysis, monkeypatch):
    from app import imports_api
    job = step(logged, create_import(logged))
    original = imports_api.create_puzzle

    def broken(*args):
        raise ValueError("Interrupted while saving")

    monkeypatch.setattr(imports_api, "create_puzzle", broken)
    assert logged.post(f"/api/imports/{job['id']}/step", json={}).status_code == 503
    failed = logged.get(f"/api/imports/{job['id']}").json()
    assert failed["status"] == "failed" and failed["error"]
    assert failed["games"][0]["cursor"] == 2 and failed["candidates_count"] == 0
    assert logged.get("/api/meta?collection=personal").json()["total_puzzles"] == 0
    monkeypatch.setattr(imports_api, "create_puzzle", original)
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
    assert step(logged, step(logged, job))["candidates_count"] == 1


def test_new_games_in_same_month_reuse_completed_analysis(logged, fake_analysis, source_game, monkeypatch):
    from app import imports_api
    original = finish(logged, create_import(logged))
    candidate_id = original["games"][0]["candidates"][0]["id"]
    puzzle_id = logged.post(f"/api/candidates/{candidate_id}/puzzle", json={}).json()["puzzle_id"]
    new_game = {**deepcopy(source_game), "game_url": "https://www.chess.com/game/live/98765"}
    monkeypatch.setattr(imports_api, "chesscom_games", lambda *args: ("2025-01", [source_game, new_game, new_game]))
    updated = create_import(logged)
    assert updated["id"] != original["id"] and updated["total_games"] == 2
    assert updated["completed_games"] == 1 and updated["games"][0]["candidates"][0]["puzzle_id"] == puzzle_id
    updated = finish(logged, updated)
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
    job = finish(logged, job)
    assert job["status"] == "completed" and job["puzzles_count"] == 1
    assert [ply for ply, _ in fake_analysis[0]] == list(range(6))
    assert not any(options["require_mistake"] for _, options in fake_analysis[0])
    assert logged.get("/api/meta?collection=public").json()["total_puzzles"] == before + 1
    assert logged.post("/api/imports/public", json={}).json()["id"] == job["id"]


def test_improvement_without_verified_tactic_cannot_be_created(logged, fake_analysis):
    fake_analysis[1].update(puzzle=None, puzzle_reason_code="defensive_position",
                            puzzle_reason="Позиция требует защитного хода.", analysis_depth=20)
    job = finish(logged, create_import(logged))
    candidate = job["games"][0]["candidates"][0]
    assert candidate["puzzle_reason_code"] == "defensive_position"
    assert candidate["puzzle_reason"] == "Позиция требует защитного хода."
    assert candidate["analysis_depth"] == 20
    assert job["available_puzzles_count"] == job["puzzles_count"] == 0
    assert logged.post(f"/api/candidates/{candidate['id']}/puzzle", json={}).status_code == 422
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
    assert games[0]["source_game_at"] == "1970-01-01T00:05:00+00:00"
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


def test_reanalysis_keeps_training_history_and_replaces_unconfirmed_moments(logged, fake_analysis, monkeypatch):
    from app import generation
    from app.db import engine
    from app.models import ImportJob, ImportedGame
    job = finish(logged, create_import(logged))
    candidate = job["games"][0]["candidates"][0]
    attempt = logged.post("/api/training/start", json={"puzzle_id": candidate["puzzle_id"]}).json()
    logged.post(f"/api/training/{attempt['id']}/move", json={"move": "f1c4", "elapsed_ms": 7000})
    with Session(engine()) as db:
        db.get(ImportJob, job["id"]).analysis_version = 1
        db.get(ImportedGame, job["games"][0]["id"]).analysis_version = 1
        db.commit()
    old = logged.get(f"/api/imports/{job['id']}").json()
    assert old["analysis_outdated"] and old["available_puzzles_count"] == 1
    assert create_import(logged)["id"] == job["id"]
    assert logged.post(f"/api/imports/{job['id']}/step", json={}).status_code == 409
    reset = logged.post(f"/api/imports/{job['id']}/reanalyze", json={}).json()
    assert not reset["analysis_outdated"] and reset["analysis_version"] == 2
    assert reset["status"] == "ready" and reset["games"][0]["cursor"] == 0
    assert reset["games"][0]["candidates"][0]["analysis_pending"]
    assert reset["available_puzzles_count"] == 0 and reset["puzzles_count"] == 1
    assert logged.get("/api/meta?collection=personal").json()["total_puzzles"] == 1
    assert logged.get("/api/stats").json()["solved"] == 1
    monkeypatch.setattr(generation, "analyse_position", lambda *args, **kwargs: None)
    checked = finish(logged, reset)
    assert checked["candidates_count"] == checked["available_puzzles_count"] == 0
    assert checked["puzzles_count"] == 1
    assert logged.get("/api/meta?collection=personal").json()["total_puzzles"] == 1
    assert logged.get("/api/stats").json()["average_solution_ms"] == 7000
    assert logged.post("/api/training/start", json={"puzzle_id": candidate["puzzle_id"]}).status_code == 200


def test_reanalysis_retains_candidate_id_and_deduplicates_saved_puzzles(logged, fake_analysis):
    job = finish(logged, create_import(logged))
    candidate = job["games"][0]["candidates"][0]
    reset = logged.post(f"/api/imports/{job['id']}/reanalyze", json={}).json()
    checked = finish(logged, reset)
    updated = checked["games"][0]["candidates"][0]
    assert updated["id"] == candidate["id"] and updated["puzzle_id"] == candidate["puzzle_id"]
    assert not updated["analysis_pending"]
    assert checked["available_puzzles_count"] == checked["puzzles_count"] == 1
    assert logged.get("/api/meta?collection=personal").json()["total_puzzles"] == 1


def test_old_completed_analysis_is_not_reused_in_an_overlapping_batch(logged, fake_analysis, source_game, monkeypatch):
    from app import imports_api
    from app.db import engine
    from app.models import ImportedGame
    job = finish(logged, create_import(logged))
    with Session(engine()) as db:
        db.get(ImportedGame, job["games"][0]["id"]).analysis_version = 1
        db.commit()
    new_game = {**deepcopy(source_game), "game_url": "https://www.chess.com/game/live/98765"}
    monkeypatch.setattr(imports_api, "chesscom_games", lambda *args: ("2025-01", [source_game, new_game]))
    batch = create_import(logged)
    assert batch["completed_games"] == 0 and batch["candidates_count"] == 0
    assert all(g["cursor"] == 0 and g["analysis_version"] == 2 for g in batch["games"])


def test_reanalysis_is_owned_and_serialized_with_analysis(logged, fake_analysis):
    from app.db import engine
    from app.models import ImportJob, User
    from app.security import hash_password
    job = finish(logged, create_import(logged))
    with Session(engine()) as db, db.begin():
        db.scalar(select(func.pg_advisory_xact_lock(782104, job["id"])))
        assert logged.post(f"/api/imports/{job['id']}/reanalyze", json={}).status_code == 409
    assert logged.get(f"/api/imports/{job['id']}").json()["status"] == "completed"
    with Session(engine()) as db:
        other = User(username="rerun-other-" + uuid4().hex[:12], password_hash=hash_password("other-rerun-password"))
        db.add(other)
        db.flush()
        db.get(ImportJob, job["id"]).user_id = other.id
        db.commit()
    assert logged.post(f"/api/imports/{job['id']}/reanalyze", json={}).status_code == 404


def test_bootstrap_autosaves_legacy_eligible_moments_and_restores_real_source_date(logged, fake_analysis):
    from app.bootstrap import bootstrap
    from app.db import engine
    from app.models import Improvement, ImportedGame, Puzzle
    job = finish(logged, create_import(logged))
    candidate = job["games"][0]["candidates"][0]
    with Session(engine()) as db:
        record = db.get(Improvement, candidate["id"])
        record.puzzle_id = None
        game = db.get(ImportedGame, job["games"][0]["id"])
        game.data = {**game.data, "source_game_at": None,
                     "pgn": game.data["pgn"].replace('[Date "2025.01.02"]', '[Date "2025.01.02"]\n[EndDate "2025.01.02"]\n[EndTime "18:42:10"]')}
        db.flush()
        db.delete(db.get(Puzzle, candidate["puzzle_id"]))
        db.commit()
    assert logged.get("/api/meta?collection=personal").json()["total_puzzles"] == 0
    bootstrap()
    bootstrap()
    saved = logged.get(f"/api/imports/{job['id']}").json()
    assert saved["puzzles_count"] == 1 and saved["available_puzzles_count"] == 1
    with Session(engine()) as db:
        puzzle = db.get(Puzzle, saved["games"][0]["candidates"][0]["puzzle_id"])
        assert puzzle.source_game_at == datetime(2025, 1, 2, 18, 42, 10, tzinfo=timezone.utc)
        assert puzzle.source_ply == 2
        assert db.scalar(select(func.count()).select_from(Puzzle).where(Puzzle.collection == "personal")) == 1


def test_repeated_position_updates_provenance_to_newer_game_without_duplicate(logged, fake_analysis, source_game, monkeypatch):
    from app import imports_api
    from app.db import engine
    from app.models import Puzzle
    original = finish(logged, create_import(logged))
    puzzle_id = original["games"][0]["candidates"][0]["puzzle_id"]
    new_game = {**deepcopy(source_game), "game_url": "https://www.chess.com/game/live/98765",
                "source_game_at": "2025-01-09T16:00:00+00:00"}
    monkeypatch.setattr(imports_api, "chesscom_games", lambda *args: ("2025-01", [new_game]))
    updated = finish(logged, create_import(logged))
    assert updated["games"][0]["candidates"][0]["puzzle_id"] == puzzle_id
    with Session(engine()) as db:
        puzzle = db.get(Puzzle, puzzle_id)
        assert puzzle.source_game_at == datetime(2025, 1, 9, 16, tzinfo=timezone.utc)
        assert puzzle.game_url == new_game["game_url"] and puzzle.source_ply == 2
    assert logged.get("/api/meta?collection=personal").json()["total_puzzles"] == 1


def test_game_timestamp_uses_api_completion_time_and_pgn_fallback():
    from app.game_sources import source_game_time
    assert source_game_time({"end_time": 1735833600, "date": "2025.01.01"}) == datetime(2025, 1, 2, 16, tzinfo=timezone.utc)
    assert source_game_time({"pgn": PGN.replace('[Date "2025.01.02"]', '[Date "2025.01.02"]\n[EndDate "2025.01.03"]\n[EndTime "01:02:03"]')}) == datetime(2025, 1, 3, 1, 2, 3, tzinfo=timezone.utc)
    assert source_game_time({"date": "2025.01.02"}) == datetime(2025, 1, 2, tzinfo=timezone.utc)
    assert source_game_time({"date": "????.??.??", "end_time": float("nan")}) is None


def test_step_and_reanalysis_do_not_queue_behind_an_account_mutation(logged, fake_analysis):
    from app.db import engine
    from app.models import User
    job = create_import(logged)
    with Session(engine()) as db, db.begin():
        user = db.scalar(select(User).where(User.username == "tester").with_for_update())
        assert user is not None
        assert logged.post(f"/api/imports/{job['id']}/step", json={}).status_code == 409
        assert logged.post(f"/api/imports/{job['id']}/reanalyze", json={}).status_code == 409
    assert logged.get(f"/api/imports/{job['id']}").json()["games"][0]["cursor"] == 0


@pytest.mark.parametrize("empty_source_url", [False, True])
def test_saved_batch_count_excludes_other_owners_collections_and_blank_sources(logged, fake_analysis, source_game, monkeypatch, empty_source_url):
    from app import imports_api
    from app.db import engine
    from app.models import Puzzle, User
    from app.security import hash_password
    if empty_source_url:
        source_game["game_url"] = ""
        monkeypatch.setattr(imports_api, "chesscom_games", lambda *args: ("2025-01", [deepcopy(source_game)]))
    job = finish(logged, create_import(logged))
    candidate = job["games"][0]["candidates"][0]
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == "tester"))
        other = User(username="counter-other-" + uuid4().hex[:12], password_hash=hash_password("other-counter-password"))
        db.add(other)
        db.flush()
        fields = deepcopy(fake_analysis[1]["puzzle"])
        for puzzle_id, collection, owner, url in [
            ("foreign-counter", "personal", other.id, source_game["game_url"]),
            ("public-counter", "public", None, source_game["game_url"]),
            ("unknown-counter", "personal", user.id, ""),
        ]:
            db.add(Puzzle(id=puzzle_id, **fields, game_url=url, collection=collection,
                          owner_id=owner, rating_estimated=True, opening_tags=""))
        db.commit()
    result = logged.get(f"/api/imports/{job['id']}").json()
    assert result["puzzles_count"] == 1
    assert result["games"][0]["candidates"][0]["puzzle_id"] == candidate["puzzle_id"]
