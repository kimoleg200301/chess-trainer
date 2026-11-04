"""Pure regression checks for optional, scoped game-accuracy calibration."""
from copy import deepcopy
import json
import math

import chess
import pytest

from app import accuracy_calibration
from app.accuracy_calibration import calibrate_summary, calibration_metadata, load_active_profile
from app.game_analysis_engine import summarize_moves


@pytest.fixture(scope="session", autouse=True)
def database():
    yield


@pytest.fixture(autouse=True)
def clean_training():
    yield


def fixture(*, family="mean_power", parameters=None, player_side="white", start_side="white"):
    game = {"white": "kimskiy1114" if player_side == "white" else "opponent",
            "black": "kimskiy1114" if player_side == "black" else "opponent",
            "time_control": "300+5", "moves": ["e2e4", "e7e5", "g1f3", "b8c6"],
            "initial_fen": chess.STARTING_FEN.replace(" w ", " b ") if start_side == "black" else chess.STARTING_FEN}
    profile = {"id": "test-research-profile", "username": "kimskiy1114", "time_control": "300+5",
               "family": family, "parameters": {"power": 2.0} if parameters is None else parameters,
               "provenance": {"source": "test fixture; not a fitted production profile"}}
    rows = [{"ply": ply, "side": start_side if ply % 2 == 0 else ("black" if start_side == "white" else "white"),
             "category": "best" if ply < 2 else "mistake", "assessed": True,
             "accuracy": 100.0 if ply < 2 else 50.0, "expected_loss": 0.0 if ply < 2 else .2}
            for ply in range(4)]
    summary = summarize_moves(rows, len(rows), starting_side=start_side, completed=True)
    return game, profile, rows, summary


def run(game, profile, rows, summary, **kwargs):
    return calibrate_summary(rows, summary, game, username="kimskiy1114",
                             current_version=True, profile=profile, **kwargs)


def test_default_is_inactive_and_raw_source_values_are_preserved_without_mutation():
    game, profile, rows, summary = fixture()
    summary["white"]["chesscom_accuracy"] = 68.87
    originals = deepcopy((game, profile, rows, summary))
    inactive = calibrate_summary(rows, summary, game, username="kimskiy1114", current_version=True)
    assert inactive["white"]["estimated_chesscom_accuracy"] is None
    assert inactive["black"]["estimated_chesscom_accuracy"] is None
    result = run(game, profile, rows, summary)
    assert result["white"]["estimated_chesscom_accuracy"] == 56.2
    assert result["black"]["estimated_chesscom_accuracy"] is None
    assert result["white"]["accuracy"] == 75
    assert result["white"]["chesscom_accuracy"] == 68.87
    assert (game, profile, rows, summary) == originals
    result["white"]["counts"]["best"] = 999
    assert summary["white"]["counts"]["best"] == 1


@pytest.mark.parametrize("family,parameters,expected", [
    ("baseline", {}, 75.0),
    ("mean_power", {"power": 2}, 56.2),
    ("loss_exponential", {"decay": 5}, round(100 * math.exp(-.5), 1)),
    ("loss_power_exponential", {"decay": 2, "power": .5}, round(100 * math.exp(-2 * math.sqrt(.1)), 1)),
])
def test_supported_families_use_unrounded_move_evidence(family, parameters, expected):
    game, profile, rows, summary = fixture(family=family, parameters=parameters)
    assert run(game, profile, rows, summary)["white"]["estimated_chesscom_accuracy"] == expected


@pytest.mark.parametrize("family,parameters", [
    ("baseline", {}), ("mean_power", {"power": 1.7}),
    ("loss_exponential", {"decay": 7.5}),
    ("loss_power_exponential", {"decay": 3.1, "power": .8}),
])
def test_perfect_is_100_and_worsening_evidence_cannot_improve_estimate(family, parameters):
    game, profile, rows, summary = fixture(family=family, parameters=parameters)
    estimates = []
    for loss, accuracy in [(0., 100.), (.1, 80.), (.5, 20.), (1., 0.)]:
        for row in rows:
            row.update(expected_loss=loss, accuracy=accuracy)
        summary = summarize_moves(rows, 4, completed=True)
        estimates.append(run(game, profile, rows, summary)["white"]["estimated_chesscom_accuracy"])
    assert estimates[0] == 100
    assert all(0 <= value <= 100 for value in estimates)
    assert estimates == sorted(estimates, reverse=True)


@pytest.mark.parametrize("change", ["different_user", "different_control", "old_version", "no_job_user", "ambiguous_players"])
def test_only_explicit_current_player_and_control_scope_can_receive_estimate(change):
    game, profile, rows, summary = fixture()
    username, current = "kimskiy1114", True
    if change == "different_user": username = "opponent"
    elif change == "different_control": game["time_control"] = "180+2"
    elif change == "old_version": current = False
    elif change == "no_job_user": username = None
    else: game["black"] = "kimskiy1114"
    result = calibrate_summary(rows, summary, game, username=username, current_version=current, profile=profile)
    assert result["white"]["estimated_chesscom_accuracy"] is None
    assert result["black"]["estimated_chesscom_accuracy"] is None


def test_username_case_is_normalized_and_black_start_uses_actual_mover_side():
    game, profile, rows, summary = fixture(player_side="black", start_side="black")
    game["black"] = "KiMsKiY1114"
    result = calibrate_summary(list(reversed(rows)), summary, game, username="KIMSKIY1114",
                               current_version=True, profile=profile)
    assert result["black"]["estimated_chesscom_accuracy"] == 56.2
    assert result["white"]["estimated_chesscom_accuracy"] is None


@pytest.mark.parametrize("family,parameters", [
    ("unknown", {}), ("mean_power", {}), ("mean_power", {"power": True}),
    ("mean_power", {"power": float("nan")}), ("mean_power", {"power": float("inf")}),
    ("mean_power", {"power": 10 ** 1000}),
    ("mean_power", {"power": 0}), ("mean_power", {"power": 5.1}),
    ("mean_power", {"power": 1, "decay": 2}), ("loss_exponential", {"decay": -1}),
    ("loss_exponential", {"decay": 50.1}), ("loss_power_exponential", {"decay": 2, "power": .1}),
    ("loss_power_exponential", {"decay": 2, "power": 2.1}),
])
def test_invalid_or_out_of_research_bounds_parameters_fail_closed(family, parameters):
    game, profile, rows, summary = fixture(family=family, parameters=parameters)
    assert run(game, profile, rows, summary)["white"]["estimated_chesscom_accuracy"] is None


@pytest.mark.parametrize("defect", [
    "partial", "duplicate", "missing_ply", "invalid_ply", "boolean_ply", "wrong_side", "invalid_fen",
    "missing_accuracy", "invalid_accuracy", "boolean_accuracy", "unknown_category", "unassessed",
    "wrong_count", "missing_loss", "invalid_loss", "boolean_loss",
])
def test_incomplete_or_inconsistent_evidence_never_produces_estimate(defect):
    game, profile, rows, summary = fixture(family="loss_exponential", parameters={"decay": 5})
    if defect == "partial": summary["complete"] = False
    elif defect == "duplicate": rows[-1] = deepcopy(rows[0])
    elif defect == "missing_ply": rows.pop()
    elif defect == "invalid_ply": rows[0]["ply"] = 9
    elif defect == "boolean_ply": rows[0]["ply"] = False
    elif defect == "wrong_side": rows[0]["side"] = "black"
    elif defect == "invalid_fen": game["initial_fen"] = "invalid"
    elif defect == "missing_accuracy": rows[0].pop("accuracy")
    elif defect == "invalid_accuracy": rows[0]["accuracy"] = float("nan")
    elif defect == "boolean_accuracy": rows[0]["accuracy"] = True
    elif defect == "unknown_category": rows[0]["category"] = "made-up"
    elif defect == "unassessed": rows[0].update(assessed=False, category="unassessed")
    elif defect == "wrong_count": summary["white"]["accuracy_moves"] += 1
    elif defect == "missing_loss": rows[0].pop("expected_loss")
    elif defect == "invalid_loss": rows[0]["expected_loss"] = 1.01
    elif defect == "boolean_loss": rows[0]["expected_loss"] = False
    result = run(game, profile, rows, summary)
    assert result["white"]["estimated_chesscom_accuracy"] is None
    assert result["white"]["accuracy"] == summary["white"]["accuracy"]


def test_forced_moves_are_excluded_and_forced_only_side_stays_unmeasurable():
    game, profile, rows, summary = fixture(family="loss_exponential", parameters={"decay": 5})
    rows[0].update(category="forced", accuracy=None, expected_loss=None)
    summary = summarize_moves(rows, 4, completed=True)
    result = run(game, profile, rows, summary)
    assert result["white"]["estimated_chesscom_accuracy"] == round(100 * math.exp(-1), 1)
    rows[2].update(category="forced", accuracy=None, expected_loss=None)
    summary = summarize_moves(rows, 4, completed=True)
    assert summary["white"]["accuracy_complete"] and summary["white"]["accuracy"] is None
    assert run(game, profile, rows, summary)["white"]["estimated_chesscom_accuracy"] is None


def test_opponent_missing_evaluation_does_not_invent_or_block_player_score():
    game, profile, rows, summary = fixture()
    rows[1].update(assessed=False, category="unassessed", accuracy=None, expected_loss=None)
    summary = summarize_moves(rows, 4, completed=True)
    result = run(game, profile, rows, summary)
    assert result["white"]["estimated_chesscom_accuracy"] == 56.2
    assert result["black"]["estimated_chesscom_accuracy"] is None
    assert result["black"]["accuracy"] is None


def test_mean_power_does_not_require_unused_expected_loss():
    game, profile, rows, summary = fixture()
    for row in rows:
        row.pop("expected_loss")
    assert run(game, profile, rows, summary)["white"]["estimated_chesscom_accuracy"] == 56.2


LEGACY_PGN = '''[White "kimskiy1114"]
[Black "opponent"]
[TimeControl "300+5"]
[Result "1-0"]

1. e4 e5 2. Nf3 Nc6 1-0
'''


@pytest.mark.parametrize("explicit_missing", [True, False])
def test_legacy_missing_or_null_control_uses_stored_pgn_without_modifying_game(explicit_missing):
    game, profile, rows, summary = fixture()
    if explicit_missing:
        game.pop("time_control")
    else:
        game["time_control"] = None
    game["pgn"] = LEGACY_PGN
    original = deepcopy(game)
    assert run(game, profile, rows, summary)["white"]["estimated_chesscom_accuracy"] == 56.2
    assert game == original


@pytest.mark.parametrize("explicit", ["180+2", "", "300", "300+0", 300, False])
def test_explicit_control_is_not_overridden_by_conflicting_legacy_pgn(explicit):
    game, profile, rows, summary = fixture()
    game.update(time_control=explicit, pgn=LEGACY_PGN)
    assert run(game, profile, rows, summary)["white"]["estimated_chesscom_accuracy"] is None


@pytest.mark.parametrize("pgn", [
    None, "", "not a PGN", '[TimeControl 300+5]\n\n1. e4 e5 *',
    LEGACY_PGN.replace('[TimeControl "300+5"]\n', ""),
    LEGACY_PGN.replace('"300+5"', '"?"'),
    LEGACY_PGN.replace('"300+5"', '"-"'),
    LEGACY_PGN.replace('"300+5"', '"40/900:300"'),
    LEGACY_PGN.replace('"300+5"', '"300"'),
    LEGACY_PGN.replace('"300+5"', '"300+05"'),
    LEGACY_PGN + "\ud800",
])
def test_missing_invalid_or_out_of_scope_legacy_headers_do_not_supply_control(pgn):
    game, profile, rows, summary = fixture()
    game.pop("time_control")
    if pgn is not None:
        game["pgn"] = pgn
    assert run(game, profile, rows, summary)["white"]["estimated_chesscom_accuracy"] is None


@pytest.mark.parametrize("character,repetitions", [(" ", 2 * 1024 * 1024), ("é", 1024 * 1024)])
def test_legacy_header_fallback_rejects_pgn_over_two_mib_in_characters_or_bytes(character, repetitions):
    game, profile, rows, summary = fixture()
    game.pop("time_control")
    game["pgn"] = LEGACY_PGN + character * repetitions
    assert run(game, profile, rows, summary)["white"]["estimated_chesscom_accuracy"] is None


def test_profile_loader_requires_explicit_valid_file_and_observes_withdrawal(tmp_path, monkeypatch):
    path = tmp_path / "accuracy_calibration_profile.json"
    monkeypatch.setattr(accuracy_calibration, "_PROFILE_PATH", path)
    assert load_active_profile() is None
    path.write_text("not-json")
    assert load_active_profile() is None
    _, profile, _, _ = fixture()
    path.write_text(json.dumps(profile))
    assert load_active_profile() == profile
    profile["parameters"]["power"] = -1
    path.write_text(json.dumps(profile))
    assert load_active_profile() is None
    profile["parameters"]["power"] = 2
    profile.pop("id")
    path.write_text(json.dumps(profile))
    assert load_active_profile() is None
    path.unlink()
    assert load_active_profile() is None


def test_public_metadata_exposes_only_identity_scope_formula_and_valid_aggregate_metrics():
    _, profile, _, _ = fixture()
    profile.update(formula="100 * (mean(move_accuracy) / 100) ** 2",
                   reference_accuracies={"white": 68.87, "black": 73.9},
                   predictions=[{"target": 68.87, "url": "private-or-unrelated-reference"}],
                   test_metrics={"games": 25, "samples": 25, "baseline_mae_pp": 15.5,
                                 "calibrated_mae_pp": 5.5, "mae_reduction_pp": 10,
                                 "bootstrap_95pct_interval_pp": [5, 15],
                                 "raw_references": [68.87, 73.9]})
    metadata = calibration_metadata(profile)
    assert set(metadata) == {"id", "scope", "formula", "test_metrics"}
    assert metadata["scope"] == {"username": "kimskiy1114", "time_control": "300+5"}
    assert metadata["test_metrics"] == {"games": 25, "samples": 25, "baseline_mae_pp": 15.5,
                                        "calibrated_mae_pp": 5.5, "mae_reduction_pp": 10,
                                        "bootstrap_95pct_interval_pp": [5, 15]}
    metadata["test_metrics"]["bootstrap_95pct_interval_pp"][0] = 0
    assert profile["test_metrics"]["bootstrap_95pct_interval_pp"][0] == 5
    assert calibration_metadata(None) is None
    profile["test_metrics"] = {"games": True, "baseline_mae_pp": float("nan"),
                               "calibrated_mae_pp": 10 ** 1000,
                               "bootstrap_95pct_interval_pp": [15, 5]}
    assert calibration_metadata(profile)["test_metrics"] == {}
