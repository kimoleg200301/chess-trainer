#!/usr/bin/env python3
"""Cache offline Stockfish reviews against public Chess.com accuracy references.

Run with the backend Python environment. No database, network requests, or fitted
parameters are used. See --help. Keep generated archives/results outside Git.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import chess
import chess.engine

from app.game_analysis_engine import (
    ANALYSIS_METHOD, ANALYSIS_NODES, ANALYSIS_POSITION_SECONDS,
    ANALYSIS_RETRY_OPTIONS, ANALYSIS_SEARCH_SECONDS, analyse_game_move,
    summarize_moves,
)
from app.generation import parse_games


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def save_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n")
    temporary.replace(path)


def valid_accuracy(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 100)


def prepare_selection(args) -> dict:
    paths = [args.manifest] if args.manifest else sorted(args.archive_dir.glob("*.json"))
    candidates, exclusions = {}, []
    for path in paths:
        data = json.loads(path.read_text())
        entries = data if isinstance(data, list) else data.get("games", [])
        if not isinstance(entries, list):
            raise ValueError(f"Expected a games list in {path}")
        for entry in entries:
            raw = entry.get("source_game", entry)
            if not isinstance(raw, dict) or raw.get("rules") != "chess":
                continue
            references = raw.get("accuracies", {})
            if not isinstance(references, dict) or not all(
                valid_accuracy(references.get(side)) for side in ("white", "black")
            ):
                continue
            identity = str(raw.get("uuid") or raw.get("url") or digest(raw.get("pgn")))
            key = digest(identity)[:24]
            if key in candidates:
                continue
            try:
                game = parse_games(raw.get("pgn"), max_games=1, max_plies=1000,
                                   time_control_override=raw.get("time_control"))[0]
            except (ValueError, TypeError) as exc:
                exclusions.append({"identity": identity, "reason": str(exc)})
                continue
            split = entry.get("split") if "source_game" in entry else None
            if split not in {None, "train", "validation", "test"}:
                raise ValueError(f"Unknown split for {identity}: {split}")
            candidates[key] = {"id": key, "identity": identity, "source_game": raw,
                               "source_digest": digest(raw), "split": split,
                               "total_plies": len(game["moves"])}
    ordered = sorted(candidates.values(), key=lambda row: digest([args.seed, row["identity"]]))
    if args.limit:
        ordered = ordered[:args.limit]
    if not ordered:
        raise ValueError("No valid reviewed standard games with both accuracy references")
    if args.split == "chronological":
        ordered.sort(key=lambda row: (row["source_game"].get("end_time", 0), row["identity"]))
    n = len(ordered)
    train_end, validation_end = int(n * 0.6), int(n * 0.8)
    for index, row in enumerate(ordered):
        if row["split"] is None:
            row["split"] = ("train" if index < train_end else
                            "validation" if index < validation_end else "test")
    return {"schema_version": 1, "seed": args.seed, "split_method": args.split,
            "eligible_games": len(candidates), "selected_games": n,
            "exclusions": exclusions, "games": ordered}


def analyse_one(entry: dict, config: dict, output_dir: str) -> dict:
    path = Path(output_dir) / "records" / (entry["id"] + ".json")
    raw = entry["source_game"]
    game = parse_games(raw["pgn"], max_games=1, max_plies=1000,
                       time_control_override=raw.get("time_control"))[0]
    start_side = "white" if chess.Board(game["initial_fen"]).turn else "black"
    config_digest = digest(config)
    record = {"schema_version": 1, "id": entry["id"], "identity": entry["identity"],
              "split": entry["split"], "source_digest": entry["source_digest"],
              "config_digest": config_digest, "config": config,
              "reference_accuracies": raw["accuracies"],
              "source_metadata": {k: raw.get(k) for k in (
                  "url", "uuid", "end_time", "time_control", "time_class", "rated",
                  "white", "black", "eco")},
              "game": game, "total_plies": len(game["moves"]), "rows": [],
              "retry_plies": [], "elapsed_seconds": 0, "status": "running", "error": None}
    if path.exists():
        cached = json.loads(path.read_text())
        for field in ("id", "source_digest", "config_digest", "split"):
            if cached.get(field) != record[field]:
                raise ValueError(f"Cache mismatch ({field}): use a new output directory: {path}")
        record = cached
        if record["status"] == "completed":
            return record
        if [row.get("ply") for row in record["rows"]] != list(range(len(record["rows"]))):
            raise ValueError(f"Invalid checkpoint row order: {path}")
        record.update(status="running", error=None)
    started = time.monotonic()
    previous_elapsed = record.get("elapsed_seconds", 0)

    def checkpoint():
        record["elapsed_seconds"] = round(previous_elapsed + time.monotonic() - started, 3)
        record["summary"] = summarize_moves(record["rows"], record["total_plies"],
                                            start_side, completed=record["status"] == "completed")
        save_json(path, record)

    try:
        with chess.engine.SimpleEngine.popen_uci(config["stockfish_path"], timeout=60) as engine:
            engine.configure(config["engine_options"])
            record["engine_identity"] = engine.id

            def evaluate(ply, search):
                if config["fresh_engine_per_ply"]:
                    with chess.engine.SimpleEngine.popen_uci(config["stockfish_path"], timeout=60) as fresh:
                        fresh.configure(config["engine_options"])
                        return analyse_game_move(game, ply, fresh, **search)
                if config["clear_hash_per_ply"] and "Clear Hash" in engine.options:
                    engine.configure({"Clear Hash": None})
                return analyse_game_move(game, ply, engine, **search)

            for ply in range(len(record["rows"]), record["total_plies"]):
                row = evaluate(ply, config["search"])
                record["rows"].append(row)
                checkpoint()
            if config["retry_unassessed"]:
                for ply, row in enumerate(record["rows"]):
                    if not row["assessed"] and ply not in record["retry_plies"]:
                        retry = evaluate(ply, config["retry_search"])
                        record["rows"][ply] = retry
                        record["retry_plies"].append(ply)
                        checkpoint()
        record["status"] = "completed"
    except Exception as exc:
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        checkpoint()
    return record


def aggregate(records: list[dict], config_digest: str, include_test_metrics: bool) -> dict:
    result = {"schema_version": 1, "config_digest": config_digest,
              "test_metrics_withheld": not include_test_metrics, "splits": {}, "games": []}
    for split in ("train", "validation", "test"):
        items = [record for record in records if record["split"] == split]
        differences = [record["summary"][side]["accuracy"] - record["reference_accuracies"][side]
                       for record in items for side in ("white", "black")
                       if record.get("summary", {}).get(side, {}).get("accuracy") is not None]
        metrics = (None if split == "test" and not include_test_metrics else
                   {"mae": sum(abs(value) for value in differences) / len(differences),
                    "rmse": math.sqrt(sum(value * value for value in differences) / len(differences)),
                    "bias": sum(differences) / len(differences)} if differences else None)
        result["splits"][split] = {
            "games": len(items), "completed_games": sum(row["status"] == "completed" for row in items),
            "measurable_sides": len(differences),
            "assessed_plies": sum(sum(row["assessed"] for row in record["rows"]) for record in items),
            "processed_plies": sum(len(row["rows"]) for row in items),
            "total_plies": sum(row["total_plies"] for row in items), "baseline_metrics": metrics,
        }
    for record in records:
        result["games"].append({
            "id": record["id"], "split": record["split"], "status": record["status"],
            "url": record["source_metadata"].get("url"), "total_plies": record["total_plies"],
            "processed_plies": len(record["rows"]), "error": record["error"],
            "record_path": "records/" + record["id"] + ".json",
        })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--archive-dir", type=Path, help="Directory of Chess.com monthly JSON responses")
    sources.add_argument("--manifest", type=Path, help="Prior selection.json, or {games: [...]} archive")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--stockfish", default=os.environ.get("STOCKFISH_PATH", "/usr/games/stockfish"))
    parser.add_argument("--limit", type=int, default=0, help="0 selects every eligible game")
    parser.add_argument("--seed", type=int, default=20261104)
    parser.add_argument("--split", choices=("chronological", "shuffled"), default="chronological",
                        help="60/20/20 game groups; manifest groups are preserved")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--hash", type=int, default=64, dest="hash_mb")
    parser.add_argument("--nodes", type=int, default=ANALYSIS_NODES)
    parser.add_argument("--max-time", type=float, default=ANALYSIS_SEARCH_SECONDS)
    parser.add_argument("--max-position-time", type=float, default=ANALYSIS_POSITION_SECONDS)
    parser.add_argument("--retry-unassessed", action="store_true",
                        help="After the first pass, retry unassessed plies once at production retry budgets")
    parser.add_argument("--fresh-engine-per-ply", action="store_true",
                        help="Launch a new engine for each ply, matching the production lifecycle exactly")
    parser.add_argument("--keep-hash", action="store_true",
                        help="Keep hash across plies (default clears hash before every ply)")
    parser.add_argument("--prepare-only", action="store_true", help="Write selection only; do not launch Stockfish")
    parser.add_argument("--include-test-metrics", action="store_true",
                        help="Reveal aggregate test baseline metrics only after the candidate is frozen")
    args = parser.parse_args()
    if (args.limit < 0 or min(args.workers, args.threads, args.hash_mb, args.nodes) < 1
            or not all(math.isfinite(value) and value > 0 for value in (args.max_time, args.max_position_time))):
        parser.error("Limits must be positive and finite; --limit may be 0")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    selection = prepare_selection(args)
    selection_path = args.output_dir / "selection.json"
    if selection_path.exists():
        old = json.loads(selection_path.read_text())
        signatures = lambda data: [(row["id"], row["source_digest"], row["split"]) for row in data["games"]]
        if sorted(signatures(old)) != sorted(signatures(selection)):
            parser.error("Selection differs from existing output; choose a new output directory")
    save_json(selection_path, selection)
    print(json.dumps({"selected_games": len(selection["games"]),
                      "total_plies": sum(row["total_plies"] for row in selection["games"]),
                      "splits": {split: sum(row["split"] == split for row in selection["games"])
                                 for split in ("train", "validation", "test")}}, sort_keys=True), flush=True)
    if args.prepare_only:
        return 0
    executable = shutil.which(args.stockfish)
    if not executable:
        parser.error(f"Stockfish executable not found: {args.stockfish}")
    config = {
        "stockfish_path": str(Path(executable).resolve()),
        "stockfish_sha256": file_digest(Path(executable)),
        "source_sha256": {str(path.relative_to(ROOT)): file_digest(path) for path in (
            ROOT / "backend/app/game_analysis_engine.py", ROOT / "backend/app/generation.py")},
        "runner_sha256": file_digest(Path(__file__)), "python_chess_version": chess.__version__,
        "engine_options": {"Threads": args.threads, "Hash": args.hash_mb},
        "search": {"nodes": args.nodes, "max_time": args.max_time,
                   "max_position_time": args.max_position_time},
        "retry_unassessed": args.retry_unassessed, "retry_search": ANALYSIS_RETRY_OPTIONS,
        "fresh_engine_per_ply": args.fresh_engine_per_ply,
        "clear_hash_per_ply": not args.keep_hash,
        "method": ANALYSIS_METHOD,
    }
    config_digest = digest(config)
    metadata_path = args.output_dir / "metadata.json"
    if metadata_path.exists() and json.loads(metadata_path.read_text())["config_digest"] != config_digest:
        parser.error("Engine/source configuration differs from existing output; choose a new output directory")
    save_json(metadata_path, {"created_at": datetime.now(timezone.utc).isoformat(),
                              "config_digest": config_digest, "config": config,
                              "workers": args.workers, "selection_digest": digest(selection)})
    (args.output_dir / "records").mkdir(exist_ok=True)
    records = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(analyse_one, entry, config, str(args.output_dir)): entry
                   for entry in selection["games"]}
        for future in as_completed(futures):
            record = future.result()
            records.append(record)
            print(json.dumps({"finished": len(records), "total": len(futures),
                              "id": record["id"], "split": record["split"], "status": record["status"],
                              "processed_plies": len(record["rows"]), "total_plies": record["total_plies"],
                              "elapsed_seconds": record["elapsed_seconds"], "error": record["error"]}), flush=True)
            save_json(args.output_dir / "aggregate.json", aggregate(records, config_digest, args.include_test_metrics))
    return int(any(record["status"] != "completed" for record in records))


if __name__ == "__main__":
    raise SystemExit(main())
