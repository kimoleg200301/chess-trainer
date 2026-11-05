"""A read-time estimate enriches persisted reports without rerunning analysis."""
from copy import deepcopy

from sqlalchemy.orm import Session

from test_background_analysis import controlled
from test_game_analysis import PGN, counts, fake_engine, imported_game, start, step


def test_saved_report_gains_scoped_estimate_without_changing_source_or_history(
        controlled, fake_engine, monkeypatch):
    from app import accuracy_calibration
    from app.db import engine
    from app.game_analysis_api import ANALYSIS_VERSION
    from app.generation import parse_games
    from app.models import GameAnalysis, ImportedGame

    monkeypatch.setattr(accuracy_calibration, "load_active_profile", lambda: None, raising=False)
    game = parse_games(PGN)[0]
    game.update(time_control="300+5", chesscom_accuracies={"white": 68.87, "black": 58.96})
    game_id, _, _ = imported_game(game=game)
    report = start(controlled, game_id)
    for _ in game["moves"]:
        report = step(controlled, report["id"])
    assert report["status"] == "completed"
    assert report["summary"]["white"]["accuracy"] == 62.5
    assert report["summary"]["white"]["estimated_chesscom_accuracy"] is None
    original_stats, engine_calls = counts(), len(fake_engine[0])
    with Session(engine()) as db:
        original_game = deepcopy(db.get(ImportedGame, game_id).data)
        original_moves = deepcopy(db.get(GameAnalysis, report["id"]).moves)

    profile = {"id": "integration-test-only", "username": "reviewer", "time_control": "300+5",
               "family": "mean_power", "parameters": {"power": 2}, "apply_to": "both_players"}
    monkeypatch.setattr(accuracy_calibration, "load_active_profile", lambda: profile)
    response = controlled.get(f"/api/game-analysis/{report['id']}")
    assert response.status_code == 200
    enriched = response.json()
    assert enriched["summary"]["white"]["estimated_chesscom_accuracy"] == 39.1
    assert enriched["summary"]["black"]["estimated_chesscom_accuracy"] == 100
    assert enriched["method"]["calibration"]["id"] == profile["id"]
    assert enriched["method"]["calibration"]["scope"]["apply_to"] == "both_players"
    assert enriched["game"]["chesscom_accuracies"] == game["chesscom_accuracies"]
    for side in ("white", "black"):
        assert {key: value for key, value in enriched["summary"][side].items()
                if key != "estimated_chesscom_accuracy"} == {
                    key: value for key, value in report["summary"][side].items()
                    if key != "estimated_chesscom_accuracy"}
    assert enriched["moves"] == report["moves"]
    assert enriched["updated_at"] == report["updated_at"]

    # An otherwise complete report may have an unassessed side. The independently
    # assessed side must still receive its estimate, without filling missing data.
    for bad_side, good_side, good_estimate in (("white", "black", 100), ("black", "white", 39.1)):
        incomplete_rows = deepcopy(original_moves)
        bad_row = next(row for row in incomplete_rows if row["side"] == bad_side)
        bad_row.update(assessed=False, category="unassessed", accuracy=None, expected_loss=None)
        with Session(engine()) as db:
            db.get(GameAnalysis, report["id"]).moves = incomplete_rows
            db.commit()
        response = controlled.get(f"/api/game-analysis/{report['id']}")
        assert response.status_code == 200
        partly_assessed = response.json()
        assert partly_assessed["status"] == "completed" and partly_assessed["summary"]["complete"]
        assert partly_assessed["summary"][bad_side]["accuracy"] is None
        assert partly_assessed["summary"][bad_side]["estimated_chesscom_accuracy"] is None
        assert partly_assessed["summary"][good_side]["accuracy"] == report["summary"][good_side]["accuracy"]
        assert partly_assessed["summary"][good_side]["estimated_chesscom_accuracy"] == good_estimate
        assert partly_assessed["moves"] == incomplete_rows
        assert partly_assessed["game"]["chesscom_accuracies"] == game["chesscom_accuracies"]
        with Session(engine()) as db:
            assert db.get(GameAnalysis, report["id"]).moves == incomplete_rows
    with Session(engine()) as db:
        db.get(GameAnalysis, report["id"]).moves = original_moves
        db.commit()

    # A future engine release must not reuse coefficients from another version.
    profile["analysis_version"] = ANALYSIS_VERSION + 1
    mismatched = controlled.get(f"/api/game-analysis/{report['id']}").json()
    assert all(mismatched["summary"][side]["estimated_chesscom_accuracy"] is None
               for side in ("white", "black"))
    assert "calibration" not in mismatched["method"]
    profile["analysis_version"] = ANALYSIS_VERSION

    # A complete old-engine report and a partial current-engine report both
    # retain their evidence, but neither may display the fitted estimate.
    for version, status, cursor, moves in (
            (ANALYSIS_VERSION - 1, "completed", len(original_moves), original_moves),
            (ANALYSIS_VERSION, "running", 1, original_moves[:1])):
        with Session(engine()) as db:
            stored = db.get(GameAnalysis, report["id"])
            stored.analysis_version, stored.status, stored.cursor, stored.moves = version, status, cursor, moves
            db.commit()
        response = controlled.get(f"/api/game-analysis/{report['id']}")
        assert response.status_code == 200
        withheld = response.json()
        assert all(withheld["summary"][side]["estimated_chesscom_accuracy"] is None
                   for side in ("white", "black"))
        assert "calibration" not in withheld["method"]
        assert withheld["moves"] == moves
        assert withheld["game"]["chesscom_accuracies"] == game["chesscom_accuracies"]

    assert len(fake_engine[0]) == engine_calls
    assert counts() == original_stats
    with Session(engine()) as db:
        assert db.get(ImportedGame, game_id).data == original_game
