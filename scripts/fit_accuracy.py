#!/usr/bin/env python3
"""Fit small game-accuracy approximations, then evaluate a frozen held-out set.

Uses offline records from calibrate_accuracy.py. NumPy/SciPy are optional research
requirements, not backend runtime dependencies. `fit` never reads test records;
`evaluate` requires its saved model. This does not change application scoring.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np
from scipy.optimize import differential_evolution, minimize_scalar

FAMILIES = {
    "baseline": {"parameters": [], "bounds": [], "formula": "100 * mean(move_accuracy / 100)"},
    "mean_power": {"parameters": ["power"], "bounds": [(0.1, 5.0)],
                   "formula": "100 * mean(move_accuracy / 100) ** power"},
    "loss_exponential": {"parameters": ["decay"], "bounds": [(0.1, 50.0)],
                         "formula": "100 * exp(-decay * mean(expected_loss))"},
    "loss_power_exponential": {"parameters": ["decay", "power"],
                               "bounds": [(0.1, 50.0), (0.2, 2.0)],
                               "formula": "100 * exp(-decay * mean(expected_loss) ** power)"},
}
GROUPS = ("player", "opponent", "both")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def write_new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def finite_number(value, low=0, high=100) -> bool:
    return (isinstance(value, (float, int)) and not isinstance(value, bool)
            and math.isfinite(value) and low <= value <= high)


def load_selection(directory: Path) -> tuple[dict, str]:
    selection = json.loads((directory / "selection.json").read_text())
    games = selection.get("games", [])
    if not games or len({item["id"] for item in games}) != len(games):
        raise ValueError("Missing games or duplicate game IDs in selection.json")
    if any(item.get("split") not in {"train", "validation", "test"} for item in games):
        raise ValueError("Every game must have a frozen train/validation/test group")
    signatures = sorted((item["id"], item["source_digest"], item["split"]) for item in games)
    return selection, digest(signatures)


def load_samples(directory: Path, selection: dict, splits: set[str], player: str) -> tuple[list, list, list]:
    """Read only requested splits. Incomplete sides stay explicit exclusions."""
    samples, exclusions, configs = [], [], {}
    for entry in selection["games"]:
        if entry["split"] not in splits:
            continue
        key = {"id": entry["id"], "split": entry["split"]}
        path = directory / "records" / (entry["id"] + ".json")
        if not path.exists():
            exclusions.append({**key, "reason": "record_missing"})
            continue
        record = json.loads(path.read_text())
        if any(record.get(field) != entry[field] for field in ("id", "source_digest", "split")):
            raise ValueError(f"Record does not match frozen selection: {path}")
        config = record.get("config", {})
        config_hash = digest(config)
        if config_hash != record.get("config_digest"):
            raise ValueError(f"Invalid engine configuration checksum: {path}")
        configs[config_hash] = config
        if record.get("status") != "completed":
            exclusions.append({**key, "reason": "record_not_completed"})
            continue
        metadata = record["source_metadata"]
        sides = [side for side in ("white", "black")
                 if str(metadata.get(side, {}).get("username", "")).lower() == player]
        if len(sides) != 1:
            exclusions.append({**key, "reason": "requested_player_not_found"})
            continue
        rows = record.get("rows", [])
        total = record.get("total_plies", 0)
        if len(rows) != total or sorted(row.get("ply", -1) for row in rows) != list(range(total)):
            exclusions.append({**key, "reason": "incomplete_or_duplicate_plies"})
            continue
        fen_parts = record.get("game", {}).get("initial_fen", "").split()
        if len(fen_parts) < 2 or fen_parts[1] not in {"w", "b"}:
            exclusions.append({**key, "reason": "invalid_initial_side"})
            continue
        start_side = "white" if fen_parts[1] == "w" else "black"
        other_side = "black" if start_side == "white" else "white"
        if any(row.get("side") != (start_side if row["ply"] % 2 == 0 else other_side) for row in rows):
            exclusions.append({**key, "reason": "invalid_move_sides"})
            continue
        if record.get("reference_accuracies") != entry.get("source_game", {}).get("accuracies"):
            raise ValueError(f"Reference scores differ from frozen source: {path}")
        for side in ("white", "black"):
            side_key = {**key, "side": side}
            side_rows = [row for row in rows if row.get("side") == side]
            target = record.get("reference_accuracies", {}).get(side)
            if not finite_number(target):
                exclusions.append({**side_key, "reason": "invalid_reference_accuracy"})
                continue
            if not side_rows or any(not row.get("assessed") or row.get("category") == "unassessed"
                                    for row in side_rows):
                exclusions.append({**side_key, "reason": "unassessed_moves"})
                continue
            scored = [row for row in side_rows if row.get("category") != "forced"]
            if not scored:
                exclusions.append({**side_key, "reason": "no_nonforced_moves"})
                continue
            if any(not finite_number(row.get("accuracy")) or
                   not finite_number(row.get("expected_loss"), 0, 1) for row in scored):
                exclusions.append({**side_key, "reason": "invalid_move_evidence"})
                continue
            samples.append({**side_key, "url": metadata.get("url"), "target": float(target),
                            "group": "player" if side == sides[0] else "opponent",
                            "mean_move_accuracy": sum(row["accuracy"] for row in scored) / len(scored),
                            "mean_loss": sum(row["expected_loss"] for row in scored) / len(scored),
                            "scored_moves": len(scored), "total_moves": len(side_rows),
                            "player_rating": metadata.get(side, {}).get("rating"),
                            "time_control": metadata.get("time_control"),
                            "record_digest": digest(record)})
    if len(configs) > 1:
        raise ValueError("Mixed engine configurations: evaluate each configuration separately")
    return samples, exclusions, [{"digest": key, "config": value} for key, value in configs.items()]


def predict(family: str, parameters: list[float], samples: list[dict], *, round_output: bool = True) -> np.ndarray:
    mean = np.array([sample["mean_move_accuracy"] / 100 for sample in samples], dtype=float)
    loss = np.array([sample["mean_loss"] for sample in samples], dtype=float)
    if family == "baseline":
        scores = mean
    elif family == "mean_power":
        scores = mean ** parameters[0]
    elif family == "loss_exponential":
        scores = np.exp(-parameters[0] * loss)
    elif family == "loss_power_exponential":
        scores = np.exp(-parameters[0] * loss ** parameters[1])
    else:
        raise ValueError(f"Unknown model family: {family}")
    values = np.clip(100 * scores, 0, 100)
    return np.array([round(float(value), 1) for value in values]) if round_output else values


def metrics(target: np.ndarray, predictions: np.ndarray) -> dict | None:
    if not len(target):
        return None
    error = predictions - target
    absolute = abs(error)
    return {"samples": len(target), "mae_pp": float(absolute.mean()),
            "median_absolute_error_pp": float(np.median(absolute)),
            "p90_absolute_error_pp": float(np.quantile(absolute, .9)),
            "maximum_absolute_error_pp": float(absolute.max()),
            "rmse_pp": float(np.sqrt((error * error).mean())),
            "bias_pp": float(error.mean()), "within_5pp_fraction": float((absolute <= 5).mean())}


def grouped_metrics(family: str, parameters: list, samples: list) -> dict:
    result = {}
    for group in GROUPS:
        subset = [row for row in samples if group == "both" or row["group"] == group]
        result[group] = metrics(np.array([row["target"] for row in subset]),
                                predict(family, parameters, subset))
    return result


def fit_parameters(family: str, samples: list) -> list[float]:
    bounds = FAMILIES[family]["bounds"]
    if not bounds:
        return []
    target = np.array([sample["target"] for sample in samples])

    def objective(parameters):
        return float(np.mean(abs(predict(family, parameters, samples, round_output=False) - target)))

    if len(bounds) == 1:
        # Locate the best interval before a bounded search: MAE is not globally smooth.
        grid = np.linspace(*bounds[0], 500)
        index = min(range(len(grid)), key=lambda i: objective([grid[i]]))
        lower, upper = grid[max(0, index - 1)], grid[min(len(grid) - 1, index + 1)]
        result = minimize_scalar(lambda value: objective([value]), bounds=(lower, upper), method="bounded")
        return [float(result.x)]
    result = differential_evolution(objective, bounds=bounds, seed=20261104,
                                    polish=True, tol=1e-9, maxiter=1000)
    return [float(value) for value in result.x]


def fit(args) -> dict:
    selection, selection_digest = load_selection(args.input_dir)
    samples, exclusions, configs = load_samples(args.input_dir, selection, {"train", "validation"}, args.player)
    train = [row for row in samples if row["split"] == "train"]
    validation = [row for row in samples if row["split"] == "validation"]
    training_player = [row for row in train if row["group"] == "player"]
    if len(training_player) < 15 or sum(row["group"] == "player" for row in validation) < 8:
        raise ValueError("At least 15 complete player training sides and 8 validation sides are required")
    candidates = []
    for family in FAMILIES:
        parameters = fit_parameters(family, training_player)
        candidates.append({"family": family, "parameters": parameters,
                           "named_parameters": dict(zip(FAMILIES[family]["parameters"], parameters)),
                           "formula": FAMILIES[family]["formula"],
                           "training": grouped_metrics(family, parameters, train),
                           "validation": grouped_metrics(family, parameters, validation)})
    best_mae = min(row["validation"]["player"]["mae_pp"] for row in candidates)
    eligible = [row for row in candidates
                if row["validation"]["player"]["mae_pp"] <= best_mae + args.simplicity_margin]
    chosen = min(eligible, key=lambda row: (len(row["parameters"]), row["validation"]["player"]["mae_pp"]))
    model = {"schema_version": 1, "kind": "frozen_accuracy_calibration",
             "created_at": datetime.now(timezone.utc).isoformat(), "player": args.player,
             "selection_digest": selection_digest, "engine_configs": configs,
             "split_game_ids": {split: [row["id"] for row in selection["games"] if row["split"] == split]
                                for split in ("train", "validation", "test")},
             "selection_signatures": {row["id"]: {"source_digest": row["source_digest"], "split": row["split"]}
                                      for row in selection["games"]},
             "prediction_rounding": "Round final game accuracy to one decimal, matching application output; fit objective uses unrounded scores.",
             "training_record_digests": {row["id"]: row["record_digest"] for row in samples},
             "objective": "player-side training MAE; validation selection with simplicity preference",
             "simplicity_margin_pp": args.simplicity_margin,
             "chosen": {key: chosen[key] for key in ("family", "parameters", "named_parameters", "formula")},
             "candidates": candidates, "exclusions": exclusions, "test_records_read": False,
             "scope": "Approximation calibrated only on reviewed games of the named player; not CAPS2 or per-move label ground truth."}
    model["model_digest"] = digest(model)
    return model


def bootstrap_improvement(samples: list, family: str, parameters: list, repetitions: int = 5000) -> dict:
    result = {}
    rng = np.random.default_rng(20261104)
    for group in GROUPS:
        subset = [row for row in samples if group == "both" or row["group"] == group]
        if not subset:
            result[group] = None
            continue
        target = np.array([row["target"] for row in subset])
        reduction = abs(predict("baseline", [], subset) - target) - abs(predict(family, parameters, subset) - target)
        ids = sorted({row["id"] for row in subset})
        grouped = [[float(value) for row, value in zip(subset, reduction) if row["id"] == identity] for identity in ids]
        sums = np.array([sum(values) for values in grouped])
        counts = np.array([len(values) for values in grouped])
        indices = rng.integers(0, len(ids), size=(repetitions, len(ids)))
        estimates = sums[indices].sum(axis=1) / counts[indices].sum(axis=1)
        result[group] = {"games": len(ids), "mae_reduction_pp": float(reduction.mean()),
                         "bootstrap_95pct_interval_pp": np.quantile(estimates, [.025, .975]).tolist(),
                         "bootstrap_repetitions": repetitions, "resampling_unit": "whole game"}
    return result


def evaluate(args) -> dict:
    model = json.loads(args.model.read_text())
    claimed_digest = model.pop("model_digest", None)
    if model.get("kind") != "frozen_accuracy_calibration" or digest(model) != claimed_digest:
        raise ValueError("Invalid frozen model checksum")
    selection, selection_digest = load_selection(args.input_dir)
    is_subset = selection_digest != model["selection_digest"]
    if is_subset:
        signatures = model.get("selection_signatures", {})
        if not args.allow_subset or any(signatures.get(row["id"]) !=
                                       {"source_digest": row["source_digest"], "split": row["split"]}
                                       for row in selection["games"]):
            raise ValueError("Corpus or game split differs; --allow-subset only permits unchanged frozen game groups")
    samples, exclusions, configs = load_samples(args.input_dir, selection, {args.split}, model["player"])
    if not samples:
        raise ValueError("No fully assessed sides for evaluation")
    frozen_configs = [row["digest"] for row in model["engine_configs"]]
    same_config = all(row["digest"] in frozen_configs for row in configs)
    if not same_config and not args.allow_different_engine:
        raise ValueError("Engine profile differs: use --allow-different-engine only for an explicit transfer check")
    family, parameters = model["chosen"]["family"], model["chosen"]["parameters"]
    predictions = predict(family, parameters, samples)
    baseline = predict("baseline", [], samples)
    rows = [{**sample, "baseline": float(old), "prediction": float(new)}
            for sample, old, new in zip(samples, baseline, predictions)]
    return {"schema_version": 1, "kind": "frozen_model_evaluation", "model_digest": claimed_digest,
            "selection_digest": selection_digest, "split": args.split,
            "evaluation_is_subset": is_subset,
            "frozen_split_game_count": len(model["split_game_ids"][args.split]),
            "same_engine_profile_as_fitting": same_config, "engine_configs": configs,
            "selected_games": sum(row["split"] == args.split for row in selection["games"]),
            "scored_games": len({row["id"] for row in samples}),
            "baseline": grouped_metrics("baseline", [], samples),
            "calibrated": grouped_metrics(family, parameters, samples),
            "paired_improvement": bootstrap_improvement(samples, family, parameters),
            "exclusions": exclusions, "predictions": rows,
            "note": "Do not retune against this evaluation; new model selection requires a fresh hold-out."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fit_parser = subparsers.add_parser("fit", help="Fit train data and select on validation; never read test records")
    fit_parser.add_argument("--input-dir", required=True, type=Path)
    fit_parser.add_argument("--player", default="kimskiy1114", type=str.lower)
    fit_parser.add_argument("--output", required=True, type=Path, help="New model JSON file; never overwritten")
    fit_parser.add_argument("--simplicity-margin", type=float, default=.5)
    eval_parser = subparsers.add_parser("evaluate", help="Evaluate an already frozen model")
    eval_parser.add_argument("--input-dir", required=True, type=Path)
    eval_parser.add_argument("--model", required=True, type=Path)
    eval_parser.add_argument("--output", required=True, type=Path, help="New evaluation JSON file")
    eval_parser.add_argument("--split", choices=("train", "validation", "test"), default="test")
    eval_parser.add_argument("--allow-different-engine", action="store_true")
    eval_parser.add_argument("--allow-subset", action="store_true",
                             help="Explicit transfer check on a subset of the same frozen game/source/split identities")
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("Output already exists; preserve the frozen artifact and choose a new path")
        if args.command == "fit" and (not math.isfinite(args.simplicity_margin) or args.simplicity_margin < 0):
            raise ValueError("Simplicity margin must be finite and nonnegative")
        result = fit(args) if args.command == "fit" else evaluate(args)
        write_new(args.output, result)
        if args.command == "fit":
            summary = {"model": str(args.output), "chosen": result["chosen"], "test_records_read": False,
                       "candidates": [{"family": row["family"],
                                       "training_player_mae_pp": row["training"]["player"]["mae_pp"],
                                       "validation_player_mae_pp": row["validation"]["player"]["mae_pp"]}
                                      for row in result["candidates"]]}
        else:
            summary = {key: result[key] for key in ("split", "scored_games", "baseline", "calibrated", "paired_improvement")}
        print(json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Calibration error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
