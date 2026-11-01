#!/usr/bin/env python3
"""Rebuild the small public corpus/bank from the real Lichess monthly archive.

Run with backend/.venv/bin/python. A bounded sample of complete rated games is
read from the official CC0 Lichess monthly archive. Stockfish independently
calculates and validates every generated solution. No ready puzzle lines are
copied. Network downloads are cached in ignored artifacts/. The download step
requires the zstd command; analysis requires a Stockfish executable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import chess
import chess.engine
import chess.pgn

SOURCE_URL = "https://database.lichess.org/standard/lichess_db_standard_rated_2013-01.pgn.zst"


def download_games():
    cache = ROOT / "artifacts" / "public-game-downloads"
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / "lichess-2013-01.pgn.zst"
    if not cached.exists():
        request = Request(SOURCE_URL, headers={
            "User-Agent": "tactics-lab-public-bank/1.0 (+https://github.com/kimoleg200301/chess-trainer)",
        })
        temporary = cached.with_suffix(".part")
        try:
            with urlopen(request, timeout=30) as response, temporary.open("wb") as output:
                size = 0
                while chunk := response.read(65536):
                    size += len(chunk)
                    if size > 32 * 1024 * 1024:
                        raise ValueError("Archive exceeded the fixed 32 MiB download budget")
                    output.write(chunk)
            temporary.replace(cached)
        finally:
            temporary.unlink(missing_ok=True)
    records, texts = [], []
    process = subprocess.Popen(["zstd", "-dc", str(cached)], stdout=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        while len(texts) < 40:
            game = chess.pgn.read_game(process.stdout)
            if game is None:
                break
            if game.errors or game.headers.get("Variant", "Standard") not in {"Standard", "Chess"}:
                continue
            source = game.headers.get("Site", "").replace("http://lichess.org/", "https://lichess.org/")
            match = re.fullmatch(r"https://lichess\.org/([A-Za-z0-9]{8})", source)
            if not match:
                continue
            game_id = match.group(1)
            board = game.board()
            moves = list(game.mainline_moves())
            if game.headers.get("Result") == "*" or not 20 <= len(moves) <= 180:
                continue
            for move in moves:
                if move not in board.legal_moves:
                    raise ValueError(f"Illegal move in {game_id}")
                board.push(move)
            game.headers["Site"] = source
            clean = game.accept(chess.pgn.StringExporter(headers=True, variations=False, comments=False)).strip()
            texts.append(clean)
            records.append({"id": game_id, "url": source,
                            "white": game.headers.get("White"), "black": game.headers.get("Black"),
                            "date": game.headers.get("UTCDate") or game.headers.get("Date"), "result": game.headers.get("Result"),
                            "plies": len(moves), "sha256": hashlib.sha256(clean.encode()).hexdigest()})
            print(f"Validated {game_id}: {len(moves)} plies", flush=True)
    finally:
        process.stdout.close()
        process.terminate()
        process.wait()
    if len(texts) < 10:
        raise ValueError("Too few usable games in the public source")
    corpus = "\n\n".join(texts) + "\n"
    (ROOT / "data/public-games.pgn").write_text(corpus, encoding="utf-8")
    metadata = {
        "source": "Lichess rated standard games, January 2013 archive", "source_url": SOURCE_URL,
        "license": "CC0", "license_url": "https://database.lichess.org/",
        "downloaded_on": time.strftime("%Y-%m-%d", time.gmtime()),
        "selection": "First 40 complete standard games with 20–180 plies from the January 2013 archive; legal moves replay-validated. Generated solutions are independently calculated by Stockfish.",
        "archive_sha256": hashlib.sha256(cached.read_bytes()).hexdigest(),
        "corpus_sha256": hashlib.sha256(corpus.encode()).hexdigest(), "games": records,
    }
    (ROOT / "data/public-games.metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def generate_bank(engine_path: str, target: int, quick_nodes: int, verify_nodes: int, max_time: float, refresh_existing: bool = False):
    from app.generation import parse_games, analyse_position
    from app.collections import generate_puzzle_id
    text = (ROOT / "data/public-games.pgn").read_text(encoding="utf-8")
    games = parse_games(text, max_games=50, max_plies=400)
    selected = None
    if refresh_existing:
        bank = json.loads((ROOT / "data/generated-puzzles.json").read_text(encoding="utf-8"))
        selected = {(row["game_url"], row["source_ply"]) for row in bank}
    # Interleave opening, middlegame and endgame moments across real games.
    # Checks/captures only prioritise the search; Stockfish decides eligibility.
    work = []
    for game_index, game in enumerate(games):
        game_id = game["game_url"].rstrip("/").split("/")[-1]
        board = chess.Board(game["initial_fen"])
        phases = [[], [], []]
        for ply, uci in enumerate(game["moves"]):
            if ply >= 10 and (selected is None or (game["game_url"], ply) in selected):
                phase = 2 if len(board.piece_map()) <= 12 else 0 if ply < 24 else 1
                forcing = any(board.is_capture(move) or board.gives_check(move) for move in board.legal_moves)
                phases[phase].append((0 if forcing else 1, ply))
            board.push_uci(uci)
        for phase, positions in enumerate(phases):
            for index, (tier, ply) in enumerate(sorted(positions)):
                work.append((tier, index, phase, game_index, ply, game_id, game))
    work.sort(key=lambda item: item[:4])
    output = []
    ids = set()
    seen = set()
    per_game = {}
    selected_plies = {}
    with chess.engine.SimpleEngine.popen_uci(engine_path, timeout=30) as engine:
        engine.configure({"Threads": 1, "Hash": 64})
        engine_name = engine.id.get("name", "Stockfish")
        for _, _, phase, _, ply, game_id, game in work:
            if per_game.get(game_id, 0) >= 2 or any(abs(ply - other) < 10 for other in selected_plies.get(game_id, [])):
                continue
            if (game_id, ply) in seen:
                continue
            seen.add((game_id, ply))
            candidate = analyse_position(game, ply, engine, require_mistake=False, quick_nodes=quick_nodes, verify_nodes=verify_nodes, max_time=max_time)
            if candidate and candidate.get("puzzle"):
                puzzle = candidate["puzzle"]
                identifier = generate_puzzle_id(puzzle["initial_fen"], puzzle["moves"], "public")
                if identifier in ids:
                    continue
                ids.add(identifier)
                per_game[game_id] = per_game.get(game_id, 0) + 1
                selected_plies.setdefault(game_id, []).append(ply)
                record = {"id": identifier, **puzzle, "game_url": game["game_url"], "opening_tags": "", "collection": "public", "rating_estimated": True, "source_ply": ply, "phase": candidate["phase"]}
                output.append(record)
                print(f"Puzzle {len(output)} {game_id} ply {ply}: {puzzle['themes']} {puzzle['moves']}", flush=True)
                (ROOT / "data/generated-puzzles.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                if len(output) >= target:
                    break
    if not output:
        raise RuntimeError("No verified public puzzles found; previous bank is unchanged")
    metadata_path = ROOT / "data/public-games.metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("verification", None)
    metadata["generation"] = {"engine": engine_name, "quick_nodes": quick_nodes, "verify_nodes": verify_nodes, "max_seconds_per_probe": max_time, "quick_max_seconds_per_probe": min(max_time, 0.2),
                              "puzzles": len(output), "analysed_positions": len(seen), "games_represented": len(per_game), "phases": {phase: sum(row["phase"] == phase for row in output) for phase in ["opening", "middlegame", "endgame"]}, "phase_rule": "Heuristic: endgame if at most 12 pieces, or no queens and at most 6 non-pawn/non-king pieces; otherwise opening through move 10, middlegame thereafter.", "rating": "Heuristic estimate, not a calibrated Lichess rating",
                              "bank_sha256": hashlib.sha256((ROOT / "data/generated-puzzles.json").read_bytes()).hexdigest()}
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata["generation"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--analyse", action="store_true")
    parser.add_argument("--engine", default="/usr/games/stockfish")
    parser.add_argument("--target", type=int, default=24)
    parser.add_argument("--quick-nodes", type=int, default=40_000)
    parser.add_argument("--verify-nodes", type=int, default=250_000)
    parser.add_argument("--max-time", type=float, default=0.6)
    parser.add_argument("--refresh-existing", action="store_true", help="Reanalyse only positions already in generated-puzzles.json")
    args = parser.parse_args()
    if min(args.target, args.quick_nodes, args.verify_nodes) < 1 or args.max_time <= 0:
        parser.error("Target and search budgets must be positive")
    if args.download:
        download_games()
    if args.analyse:
        generate_bank(args.engine, args.target, args.quick_nodes, args.verify_nodes, args.max_time, args.refresh_existing)
    if not args.download and not args.analyse:
        parser.error("Choose --download and/or --analyse")
