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
            "time_control": "300+5",
            "moves": ["e7e5", "e2e4", "b8c6", "g1f3"] if start_side == "black" else ["e2e4", "e7e5", "g1f3", "b8c6"],
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


@pytest.mark.parametrize("apply_to", ["player_only", "both_players"])
@pytest.mark.parametrize("change", ["different_user", "different_control", "old_version", "no_job_user", "ambiguous_players"])
def test_only_explicit_current_player_and_control_scope_can_receive_estimate(change, apply_to):
    game, profile, rows, summary = fixture()
    profile["apply_to"] = apply_to
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


@pytest.mark.parametrize("player_side", ["white", "black"])
@pytest.mark.parametrize("start_side", ["white", "black"])
def test_both_players_use_their_own_asymmetric_evidence_for_either_player_color_or_start(player_side, start_side):
    game, profile, rows, _ = fixture(player_side=player_side, start_side=start_side)
    profile["apply_to"] = "both_players"
    for row in rows:
        row["accuracy"] = (100 if row["ply"] < 2 else 80) if row["side"] == "white" else (60 if row["ply"] < 2 else 20)
    summary = summarize_moves(rows, 4, starting_side=start_side, completed=True)
    original = deepcopy((game, profile, rows, summary))
    result = run(game, profile, list(reversed(rows)), summary)
    assert result["white"]["estimated_chesscom_accuracy"] == 81
    assert result["black"]["estimated_chesscom_accuracy"] == 16
    assert result["white"]["accuracy"] == 90
    assert result["black"]["accuracy"] == 40
    assert (game, profile, rows, summary) == original


@pytest.mark.parametrize("player_side", ["white", "black"])
@pytest.mark.parametrize("explicit_policy", [False, True])
def test_legacy_or_explicit_player_only_profile_does_not_calibrate_the_opponent(player_side, explicit_policy):
    game, profile, rows, summary = fixture(player_side=player_side)
    if explicit_policy:
        profile["apply_to"] = "player_only"
    else:
        assert "apply_to" not in profile
    result = run(game, profile, rows, summary)
    opponent = "black" if player_side == "white" else "white"
    assert result[player_side]["estimated_chesscom_accuracy"] == 56.2
    assert result[opponent]["estimated_chesscom_accuracy"] is None


@pytest.mark.parametrize("apply_to", [None, True, False, "", "everyone", [], {}])
def test_unknown_or_malformed_side_policy_disables_both_estimates_and_metadata(apply_to):
    game, profile, rows, summary = fixture()
    profile["apply_to"] = apply_to
    result = run(game, profile, rows, summary)
    assert all(result[side]["estimated_chesscom_accuracy"] is None for side in ("white", "black"))
    assert calibration_metadata(profile) is None


@pytest.mark.parametrize("bad_side", ["white", "black"])
@pytest.mark.parametrize("defect", ["unassessed", "missing_loss", "invalid_loss", "forced_only", "wrong_count"])
def test_incomplete_side_does_not_block_independently_measurable_other_side(bad_side, defect):
    game, profile, rows, _ = fixture(family="loss_exponential", parameters={"decay": 5})
    profile["apply_to"] = "both_players"
    affected = [row for row in rows if row["side"] == bad_side]
    if defect == "unassessed":
        affected[0].update(assessed=False, category="unassessed", accuracy=None, expected_loss=None)
    elif defect == "missing_loss":
        affected[0].pop("expected_loss")
    elif defect == "invalid_loss":
        affected[0]["expected_loss"] = 1.5
    elif defect == "forced_only":
        for row in affected:
            row.update(category="forced", accuracy=None, expected_loss=None)
    summary = summarize_moves(rows, 4, completed=True)
    if defect == "wrong_count":
        summary[bad_side]["accuracy_moves"] += 1
    assert summary["complete"]
    result = run(game, profile, rows, summary)
    other = "black" if bad_side == "white" else "white"
    assert result[bad_side]["estimated_chesscom_accuracy"] is None
    assert result[other]["estimated_chesscom_accuracy"] == 60.7
    assert all(result[side]["accuracy"] == summary[side]["accuracy"] for side in ("white", "black"))


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
    assert metadata["scope"] == {"username": "kimskiy1114", "time_control": "300+5", "apply_to": "player_only"}
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


def test_both_players_metadata_exposes_only_valid_aggregate_opponent_metrics():
    _, profile, _, _ = fixture()
    profile.update(apply_to="both_players", opponent_test_metrics={
        "games": 25, "samples": 25, "baseline_mae_pp": 11.2, "calibrated_mae_pp": 5.8,
        "mae_reduction_pp": 5.4, "bootstrap_95pct_interval_pp": [2.1, 8.7],
        "raw_references": [68.87, 73.9], "usernames": ["unrelated-player"]})
    metadata = calibration_metadata(profile)
    assert metadata["scope"]["apply_to"] == "both_players"
    assert metadata["opponent_test_metrics"] == {
        "games": 25, "samples": 25, "baseline_mae_pp": 11.2, "calibrated_mae_pp": 5.8,
        "mae_reduction_pp": 5.4, "bootstrap_95pct_interval_pp": [2.1, 8.7]}
    metadata["opponent_test_metrics"]["bootstrap_95pct_interval_pp"][0] = -1
    assert profile["opponent_test_metrics"]["bootstrap_95pct_interval_pp"][0] == 2.1
    profile["opponent_test_metrics"] = {"games": True, "samples": -1, "baseline_mae_pp": float("inf"),
                                        "calibrated_mae_pp": 101, "bootstrap_95pct_interval_pp": [8, 2]}
    assert calibration_metadata(profile)["opponent_test_metrics"] == {}
    profile["apply_to"] = "player_only"
    assert "opponent_test_metrics" not in calibration_metadata(profile)
