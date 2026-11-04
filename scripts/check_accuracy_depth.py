#!/usr/bin/env python3
"""Validate a frozen accuracy model at production depth for one player's plies.

Opponent plies are explicit unassessed placeholders, never invented scores. The
record format is compatible with fit_accuracy.py evaluate --allow-subset
--allow-different-engine. This script performs no fitting or network/DB writes.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil
import time

from calibrate_accuracy import (
    ROOT, aggregate, digest, file_digest, prepare_selection, save_json,
)

import chess
import chess.engine

from app.game_analysis_engine import (
    ANALYSIS_ENGINE_OPTIONS, ANALYSIS_METHOD, ANALYSIS_NODES,
    ANALYSIS_POSITION_SECONDS, ANALYSIS_RETRY_OPTIONS, ANALYSIS_SEARCH_SECONDS,
    analyse_game_move, summarize_moves,
)
from app.generation import parse_games, position_at


def skipped_opponent_row(game: dict, ply: int) -> dict:
    board = position_at(game, ply)
    move = chess.Move.from_uci(game["moves"][ply])
    after = board.copy()
    after.push(move)
    return {
        "ply": ply, "move_number": board.fullmove_number,
        "side": "white" if board.turn else "black", "before_fen": board.fen(), "after_fen": after.fen(),
        "played_uci": move.uci(), "played_san": board.san(move),
        "best_uci": None, "best_san": None, "category": "unassessed", "accuracy": None,
        "expected_loss": None, "best_expected": None, "played_expected": None,
        "score_before": None, "score_after": None, "mate_before": None, "mate_after": None,
        "best_line_uci": [], "best_line_san": [], "played_line_uci": [], "played_line_san": [],
        "depth": 0, "assessed": False, "reason": "opponent_not_requested",
        "analysis_skipped": True, "brilliant_evidence": None,
    }


def analyse_player(entry: dict, config: dict, output_dir: str) -> dict:
    path = Path(output_dir) / "records" / (entry["id"] + ".json")
    raw = entry["source_game"]
    game = parse_games(raw["pgn"], max_games=1, max_plies=1000,
                       time_control_override=raw.get("time_control"))[0]
    sides = [side for side in ("white", "black") if game[side].lower() == config["requested_player"]]
    if len(sides) != 1:
        raise ValueError(f"Requested player is not uniquely present: {entry['id']}")
    player_side = sides[0]
    start_side = "white" if chess.Board(game["initial_fen"]).turn else "black"
    requested_plies = [ply for ply in range(len(game["moves"]))
                       if (start_side if ply % 2 == 0 else ("black" if start_side == "white" else "white")) == player_side]
    record = {
        "schema_version": 1, "id": entry["id"], "identity": entry["identity"], "split": entry["split"],
        "source_digest": entry["source_digest"], "config_digest": digest(config), "config": config,
        "reference_accuracies": raw["accuracies"],
        "source_metadata": {key: raw.get(key) for key in (
            "url", "uuid", "end_time", "time_control", "time_class", "rated", "white", "black", "eco")},
        "game": game, "total_plies": len(game["moves"]), "rows": [], "retry_plies": [],
        "requested_plies": requested_plies, "requested_side": player_side,
        "elapsed_seconds": 0, "status": "running", "error": None,
    }
    if path.exists():
        cached = json.loads(path.read_text())
        for field in ("id", "source_digest", "config_digest", "split", "requested_plies"):
            if cached.get(field) != record[field]:
                raise ValueError(f"Cache mismatch ({field}); use a new output directory: {path}")
        record = cached
        if record["status"] == "completed":
            return record
        if [row.get("ply") for row in record["rows"]] != list(range(len(record["rows"]))):
            raise ValueError(f"Invalid checkpoint row order: {path}")
        record.update(status="running", error=None)
    started, prior_elapsed = time.monotonic(), record.get("elapsed_seconds", 0)

    def checkpoint():
        record["elapsed_seconds"] = round(prior_elapsed + time.monotonic() - started, 3)
        record["summary"] = summarize_moves(record["rows"], record["total_plies"], start_side,
                                             completed=record["status"] == "completed")
        save_json(path, record)

    def evaluate(ply: int, search: dict):
        with chess.engine.SimpleEngine.popen_uci(config["stockfish_path"], timeout=5) as engine:
            engine.configure(config["engine_options"])
            record["engine_identity"] = engine.id
            row = analyse_game_move(game, ply, engine, **search)
            row["engine_name"] = engine.id.get("name", "Stockfish")
            return row

    try:
        for ply in range(len(record["rows"]), record["total_plies"]):
            row = evaluate(ply, config["search"]) if ply in requested_plies else skipped_opponent_row(game, ply)
            record["rows"].append(row)
            checkpoint()
            if ply in requested_plies:
                print(json.dumps({"id": entry["id"], "processed_player_plies": sum(
                    row["ply"] in requested_plies for row in record["rows"]),
                    "total_player_plies": len(requested_plies), "elapsed_seconds": record["elapsed_seconds"]}), flush=True)
        for ply in requested_plies:
            if not record["rows"][ply]["assessed"] and ply not in record["retry_plies"]:
                row = evaluate(ply, config["retry_search"])
                row["retry_attempts"] = record["rows"][ply].get("retry_attempts", 0) + 1
                record["rows"][ply] = row
                record["retry_plies"].append(ply)
                checkpoint()
        record["status"] = "completed"
    except Exception as exc:
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        checkpoint()
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--stockfish", required=True)
    parser.add_argument("--player", type=str.lower, default="kimskiy1114")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--threads", type=int, default=ANALYSIS_ENGINE_OPTIONS["Threads"])
    parser.add_argument("--hash", type=int, default=ANALYSIS_ENGINE_OPTIONS["Hash"], dest="hash_mb")
    parser.add_argument("--nodes", type=int, default=ANALYSIS_NODES)
    parser.add_argument("--max-time", type=float, default=ANALYSIS_SEARCH_SECONDS)
    parser.add_argument("--max-position-time", type=float, default=ANALYSIS_POSITION_SECONDS)
    parser.add_argument("--include-test-metrics", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if (min(args.workers, args.threads, args.hash_mb, args.nodes) < 1 or not args.player
            or not all(math.isfinite(value) and value > 0 for value in (args.max_time, args.max_position_time))):
        parser.error("Limits must be positive and finite; player must not be empty")
    args.archive_dir, args.limit, args.seed, args.split = None, 0, 20261104, "chronological"
    selection = prepare_selection(args)
    for entry in selection["games"]:
        if sum(str(entry["source_game"].get(side, {}).get("username", "")).lower() == args.player
               for side in ("white", "black")) != 1:
            parser.error(f"Requested player missing or ambiguous in game {entry['id']}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    selection_path = args.output_dir / "selection.json"
    if selection_path.exists():
        existing = json.loads(selection_path.read_text())
        signature = lambda data: sorted((row["id"], row["source_digest"], row["split"]) for row in data["games"])
        if signature(existing) != signature(selection):
            parser.error("Selection differs from output directory")
    save_json(selection_path, selection)
    print(json.dumps({"selected_games": len(selection["games"]), "player": args.player,
                      "scope": "player plies only; opponent unassessed", "test_metrics_withheld": not args.include_test_metrics}), flush=True)
    if args.prepare_only:
        return 0
    executable = shutil.which(args.stockfish)
    if not executable:
        parser.error(f"Stockfish executable not found: {args.stockfish}")
    config = {
        "stockfish_path": str(Path(executable).resolve()), "stockfish_sha256": file_digest(Path(executable)),
        "source_sha256": {str(path.relative_to(ROOT)): file_digest(path) for path in (
            ROOT / "backend/app/game_analysis_engine.py", ROOT / "backend/app/generation.py")},
        "runner_sha256": file_digest(Path(__file__)),
        "helper_sha256": file_digest(ROOT / "scripts/calibrate_accuracy.py"),
        "python_chess_version": chess.__version__, "requested_player": args.player,
        "analysis_scope": "requested_player_only", "opponent_policy": "explicit_unassessed_no_retry",
        "engine_options": {"Threads": args.threads, "Hash": args.hash_mb},
        "search": {"nodes": args.nodes, "max_time": args.max_time, "max_position_time": args.max_position_time},
        "retry_unassessed": True, "retry_search": ANALYSIS_RETRY_OPTIONS,
        "fresh_engine_per_ply": True, "engine_timeout": 5, "method": ANALYSIS_METHOD,
    }
    metadata_path = args.output_dir / "metadata.json"
    if metadata_path.exists() and json.loads(metadata_path.read_text())["config_digest"] != digest(config):
        parser.error("Configuration differs from existing output; use a new directory")
    save_json(metadata_path, {"created_at": datetime.now(timezone.utc).isoformat(),
                              "config_digest": digest(config), "config": config,
                              "workers": args.workers, "selection_digest": digest(selection)})
    (args.output_dir / "records").mkdir(exist_ok=True)
    records = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(analyse_player, entry, config, str(args.output_dir)) for entry in selection["games"]]
        for future in as_completed(futures):
            record = future.result()
            records.append(record)
            print(json.dumps({"finished": len(records), "total": len(futures), "id": record["id"],
                              "split": record["split"], "status": record["status"],
                              "requested_plies": len(record["requested_plies"]),
                              "elapsed_seconds": record["elapsed_seconds"], "error": record["error"]}), flush=True)
            save_json(args.output_dir / "aggregate.json", aggregate(records, digest(config), args.include_test_metrics))
    return int(any(record["status"] != "completed" for record in records))


if __name__ == "__main__":
    raise SystemExit(main())
