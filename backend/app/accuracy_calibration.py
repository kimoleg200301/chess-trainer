"""Optional, scoped game-level accuracy estimates from existing engine evidence.

This module neither runs Stockfish nor fits parameters. A caller must explicitly
supply an installed, validated research profile. Original move
scores, summary accuracy, and Chess.com reference values are preserved.
"""
from __future__ import annotations

from copy import deepcopy
import io
import json
import math
from pathlib import Path

import chess
import chess.pgn

from .game_analysis_engine import CATEGORY_NAMES
from .generation import MAX_PGN_BYTES


_PARAMETER_BOUNDS = {
    "baseline": {},
    "mean_power": {"power": (0.1, 5.0)},
    "loss_exponential": {"decay": (0.1, 50.0)},
    "loss_power_exponential": {"decay": (0.1, 50.0), "power": (0.2, 2.0)},
}
_PROFILE_PATH = Path(__file__).with_name("accuracy_calibration_profile.json")
_FORMULAS = {
    "baseline": "mean(move_accuracy)",
    "mean_power": "100 * (mean(move_accuracy) / 100) ** power",
    "loss_exponential": "100 * exp(-decay * mean(expected_loss))",
    "loss_power_exponential": "100 * exp(-decay * mean(expected_loss) ** power)",
}


def _finite_number(value, lower: float, upper: float) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and lower <= value <= upper and math.isfinite(value))


def _username(value) -> str | None:
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _game_time_control(game: dict) -> str | None:
    """Read legacy PGN headers only when the explicit metadata is absent/null."""
    value = game.get("time_control")
    if value is not None:
        return value if isinstance(value, str) else None
    pgn = game.get("pgn")
    if not isinstance(pgn, str) or not pgn or len(pgn) > MAX_PGN_BYTES:
        return None
    try:
        if len(pgn.encode("utf-8")) > MAX_PGN_BYTES:
            return None
        headers = chess.pgn.read_headers(io.StringIO(pgn))
    except (UnicodeError, ValueError, TypeError):
        return None
    return headers.get("TimeControl") if headers is not None else None


def _valid_profile(profile) -> bool:
    if not isinstance(profile, dict) or not _username(profile.get("username")):
        return False
    if not isinstance(profile.get("time_control"), str) or not profile["time_control"]:
        return False
    family, parameters = profile.get("family"), profile.get("parameters")
    if not isinstance(family, str) or family not in _PARAMETER_BOUNDS or not isinstance(parameters, dict):
        return False
    bounds = _PARAMETER_BOUNDS[family]
    return set(parameters) == set(bounds) and all(
        _finite_number(parameters[name], lower, upper) for name, (lower, upper) in bounds.items()
    )


def load_active_profile() -> dict | None:
    """Read an explicitly installed profile; missing/invalid files disable it.

    The small file is read on each call so installing or withdrawing a profile
    never leaves a cached decision active in a running process. This is a local
    configuration read only, with no network access or fallback coefficients.
    """
    try:
        if _PROFILE_PATH.stat().st_size > 512 * 1024:
            return None
        profile = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if (not _valid_profile(profile) or not isinstance(profile.get("id"), str)
            or not 0 < len(profile["id"]) <= 200):
        return None
    return profile


def calibration_metadata(profile: dict | None) -> dict | None:
    """Expose only the profile identity, scope, formula and aggregate test metrics."""
    if (not _valid_profile(profile) or not isinstance(profile.get("id"), str)
            or not 0 < len(profile["id"]) <= 200):
        return None
    formula = profile.get("formula")
    if not isinstance(formula, str) or not 0 < len(formula) <= 500:
        formula = _FORMULAS[profile["family"]]
    metrics = {}
    supplied = profile.get("test_metrics", {})
    if isinstance(supplied, dict):
        for key in ("games", "samples"):
            value = supplied.get(key)
            if isinstance(value, int) and _finite_number(value, 0, 10_000_000):
                metrics[key] = value
        for key in ("baseline_mae_pp", "calibrated_mae_pp"):
            value = supplied.get(key)
            if _finite_number(value, 0, 100):
                metrics[key] = value
        if _finite_number(supplied.get("mae_reduction_pp"), -100, 100):
            metrics["mae_reduction_pp"] = supplied["mae_reduction_pp"]
        interval = supplied.get("bootstrap_95pct_interval_pp")
        if (isinstance(interval, list) and len(interval) == 2
                and all(_finite_number(value, -100, 100) for value in interval)
                and interval[0] <= interval[1]):
            metrics["bootstrap_95pct_interval_pp"] = list(interval)
    return {"id": profile["id"], "scope": {"username": _username(profile["username"]),
                                             "time_control": profile["time_control"]},
            "formula": formula, "test_metrics": metrics}


def _estimate(family: str, parameters: dict, rows: list[dict]) -> float | None:
    if not rows or any(not _finite_number(row.get("accuracy"), 0, 100) for row in rows):
        return None
    mean_accuracy = math.fsum(row["accuracy"] for row in rows) / len(rows)
    if family == "baseline":
        return round(mean_accuracy, 1)
    if family == "mean_power":
        return round(100 * (mean_accuracy / 100) ** parameters["power"], 1)
    if any(not _finite_number(row.get("expected_loss"), 0, 1) for row in rows):
        return None
    mean_loss = math.fsum(row["expected_loss"] for row in rows) / len(rows)
    exponent = mean_loss ** parameters.get("power", 1.0)
    return round(100 * math.exp(-parameters["decay"] * exponent), 1)


def calibrate_summary(rows: list[dict], summary: dict, game: dict, *,
                      username: str | None = None, current_version: bool,
                      profile: dict | None = None) -> dict:
    """Return a copy with an optional estimate for the profile's player side.

    ``profile`` requires ``username``, ``time_control``, ``family`` and a named
    ``parameters`` dict; metadata such as ID and provenance can be retained by
    the caller. Supported bounds match the offline fitting script. ``username``
    is the owning import job's player, not an inferred identity from an opponent.

    The estimate is withheld for incomplete/outdated reports, other players or
    controls, invalid profiles, duplicate/missing plies, inconsistent mover sides,
    unassessed player moves, or missing evidence required by the chosen formula.
    Forced moves are excluded. Original percentages and other fields are copied
    unchanged, including any independently supplied Chess.com reference values.
    Legacy games without time-control metadata may use their stored PGN header;
    an explicit conflicting value is never replaced by that fallback.
    """
    result = deepcopy(summary)
    for side in ("white", "black"):
        if isinstance(result.get(side), dict):
            result[side]["estimated_chesscom_accuracy"] = None
    if current_version is not True or not _valid_profile(profile) or summary.get("complete") is not True:
        return result
    player = _username(profile["username"])
    if (_username(username) != player or not isinstance(game, dict)
            or _game_time_control(game) != profile["time_control"]):
        return result
    player_sides = [side for side in ("white", "black") if _username(game.get(side)) == player]
    if len(player_sides) != 1:
        return result
    player_side = player_sides[0]
    side_summary = summary.get(player_side)
    if (not isinstance(side_summary, dict) or side_summary.get("complete") is not True
            or side_summary.get("accuracy_complete") is not True
            or not _finite_number(side_summary.get("accuracy"), 0, 100)):
        return result
    moves = game.get("moves")
    if not isinstance(moves, list) or not moves or not isinstance(rows, list) or len(rows) != len(moves):
        return result
    try:
        board = chess.Board(game.get("initial_fen", ""))
    except (ValueError, TypeError, AttributeError):
        return result
    if not board.is_valid():
        return result
    start_side = "white" if board.turn else "black"
    other_side = "black" if board.turn else "white"
    seen, player_rows = set(), []
    for row in rows:
        if not isinstance(row, dict):
            return result
        ply = row.get("ply")
        if isinstance(ply, bool) or not isinstance(ply, int) or not 0 <= ply < len(moves) or ply in seen:
            return result
        seen.add(ply)
        expected_side = start_side if ply % 2 == 0 else other_side
        if row.get("side") != expected_side:
            return result
        if expected_side == player_side:
            if (row.get("assessed") is not True or row.get("category") not in CATEGORY_NAMES
                    or row["category"] == "unassessed"):
                return result
            player_rows.append(row)
    scored = [row for row in player_rows if row["category"] != "forced"]
    if (not scored or side_summary.get("total_moves") != len(player_rows)
            or side_summary.get("assessed_moves") != len(player_rows)
            or side_summary.get("accuracy_moves") != len(scored)):
        return result
    estimate = _estimate(profile["family"], profile["parameters"], scored)
    if estimate is not None:
        result[player_side]["estimated_chesscom_accuracy"] = estimate
    return result
