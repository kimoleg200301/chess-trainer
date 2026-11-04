"""Published Chess.com percentages remain independent of local engine reviews."""
from copy import deepcopy

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_background_analysis import controlled
from test_chesscom_sync import remote_games, sync
from test_game_analysis import fake_engine, imported_game, start, step
from test_imports import create_import, fake_analysis, source_game


@pytest.mark.parametrize("value,expected", [
    (None, None),
    ({}, None),
    ([], None),
    ({"white": 0, "black": 100}, {"white": 0, "black": 100}),
    ({"white": 68.87, "black": "90.2"}, {"white": 68.87}),
    ({"white": True, "black": False}, None),
    ({"white": float("nan"), "black": float("inf")}, None),
    ({"white": -0.1, "black": 100.1}, None),
    ({"white": 83.75, "black": 79.42, "other": 91}, {"white": 83.75, "black": 79.42}),
])
def test_only_valid_published_percentages_are_accepted(value, expected):
    from app.game_sources import chesscom_accuracies
    assert chesscom_accuracies(value) == expected


def test_source_metadata_preserves_exact_published_percentages(source_game):
    from app.game_sources import chesscom_metadata
    raw = {"accuracies": {"white": 68.87, "black": 58.96}}
    enriched = chesscom_metadata(source_game, raw, "TesterChess")
    assert enriched["chesscom_accuracies"] == raw["accuracies"]
    assert "chesscom_accuracies" not in source_game
    unavailable = chesscom_metadata(source_game, {}, "TesterChess")
    assert unavailable.get("chesscom_accuracies") is None


def completed_report(client, game_id, plies):
    report = start(client, game_id)
    for _ in range(plies):
        report = step(client, report["id"])
    assert report["status"] == "completed"
    return report


def test_repeat_import_refreshes_reference_without_recalculating_report(
        controlled, fake_analysis, fake_engine, source_game):
    from app.db import engine
    from app.models import GameAnalysis, ImportedGame, ImportJob
    job = create_import(controlled)
    game_id = job["games"][0]["id"]
    original = completed_report(controlled, game_id, job["games"][0]["total_plies"])
    assert original["game"]["chesscom_accuracies"] is None
    original_call_count = len(fake_engine[0])

    source_game["chesscom_accuracies"] = {"white": 63.21, "black": 89.34}
    assert create_import(controlled)["id"] == job["id"]
    refreshed = controlled.get(f"/api/game-analysis/{original['id']}").json()
    assert refreshed["game"]["chesscom_accuracies"] == source_game["chesscom_accuracies"]
    assert refreshed["summary"] == original["summary"]
    assert refreshed["moves"] == original["moves"]
    assert refreshed["status"] == original["status"]
    assert refreshed["updated_at"] == original["updated_at"]
    assert len(fake_engine[0]) == original_call_count

    # Absence is unknown, not zero, and one updated side must retain the other.
    source_game.pop("chesscom_accuracies")
    create_import(controlled)
    source_game["chesscom_accuracies"] = {"white": 0, "black": True}
    create_import(controlled)
    listed = controlled.get("/api/game-analysis/games").json()["games"]
    assert listed[0]["chesscom_accuracies"] == {"white": 0, "black": 89.34}
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ImportJob)) == 1
        assert db.scalar(select(func.count()).select_from(ImportedGame)) == 1
        assert db.scalar(select(func.count()).select_from(GameAnalysis)) == 1


def test_sync_refreshes_all_owned_duplicates_by_identity_or_url_only(
        controlled, fake_analysis, fake_engine, remote_games, source_game):
    from app.background import AnalysisWorker
    from app.db import engine
    from app.models import ImportedGame
    job = create_import(controlled)
    first_id = job["games"][0]["id"]
    report = completed_report(controlled, first_id, job["games"][0]["total_plies"])
    duplicate_id, _, _ = imported_game(game=source_game)
    cosmetic = deepcopy(source_game)
    cosmetic["game_url"] = cosmetic["game_url"].replace("www.chess.com", "chess.com") + "/?source=review#board"
    cosmetic["retained_metadata"] = "keep this"
    cosmetic_id, _, _ = imported_game(game=cosmetic)
    foreign = deepcopy(source_game)
    foreign["chesscom_accuracies"] = {"white": 22.2}
    foreign_id, _, _ = imported_game(username="reference-other", game=foreign)
    with Session(engine()) as db:
        before = {game_id: deepcopy(db.get(ImportedGame, game_id).data)
                  for game_id in (first_id, duplicate_id, cosmetic_id, foreign_id)}

    remote_games[0][0][1][0]["chesscom_accuracies"] = {"white": 71.43, "black": 84.92}
    sync(controlled)
    assert AnalysisWorker().run_once()
    state = controlled.get("/api/game-analysis/games").json()
    assert state["sync"]["added_games"] == 0
    after_report = controlled.get(f"/api/game-analysis/{report['id']}").json()
    assert after_report["summary"] == report["summary"]
    assert after_report["moves"] == report["moves"]
    assert after_report["game"]["chesscom_accuracies"] == {"white": 71.43, "black": 84.92}
    with Session(engine()) as db:
        for game_id in (first_id, duplicate_id, cosmetic_id):
            updated = db.get(ImportedGame, game_id).data
            assert updated == {**before[game_id], "chesscom_accuracies": {"white": 71.43, "black": 84.92}}
        assert db.get(ImportedGame, foreign_id).data == before[foreign_id]


def test_api_filters_invalid_legacy_reference_without_fabricating_local_accuracy(
        controlled, source_game):
    source_game["chesscom_accuracies"] = {"white": False, "black": 92.64, "other": 100}
    game_id, _, _ = imported_game(game=source_game)
    report = start(controlled, game_id)
    assert report["game"]["chesscom_accuracies"] == {"black": 92.64}
    assert report["summary"]["white"]["accuracy"] is None
    assert report["summary"]["black"]["accuracy"] is None
    assert report["moves"] == []


def test_new_overlapping_import_retains_previously_published_reference(
        controlled, fake_analysis, source_game, monkeypatch):
    from app import imports_api
    source_game["chesscom_accuracies"] = {"white": 77.31, "black": 64.81}
    first = create_import(controlled)
    source_game.pop("chesscom_accuracies")
    extra = deepcopy(source_game)
    extra["game_url"] += "-extra"
    monkeypatch.setattr(imports_api, "chesscom_games", lambda *args: ("2025-01", [deepcopy(source_game), extra]))
    second = create_import(controlled)
    assert second["id"] != first["id"]
    listed = controlled.get("/api/game-analysis/games").json()["games"]
    original = next(game for game in listed if game["game_url"] == source_game["game_url"])
    assert original["chesscom_accuracies"] == {"white": 77.31, "black": 64.81}
    assert len(listed) == 2


def test_first_sync_keeps_reference_present_only_on_later_duplicate(controlled, remote_games):
    from app.background import AnalysisWorker
    duplicate = deepcopy(remote_games[0][0][1][0])
    duplicate["chesscom_accuracies"] = {"white": 81.25, "black": 72.37}
    remote_games[0][0][1].append(duplicate)
    sync(controlled)
    assert AnalysisWorker().run_once()
    state = controlled.get("/api/game-analysis/games").json()
    assert state["sync"]["added_games"] == 1
    assert state["games"][0]["chesscom_accuracies"] == {"white": 81.25, "black": 72.37}
