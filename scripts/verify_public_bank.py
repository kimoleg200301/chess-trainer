#!/usr/bin/env python3
"""Audit public-bank provenance, legal moves, and independent Stockfish choices.

Run with backend/.venv/bin/python. This replays the original full games and
checks every learner move with a fresh engine search and a larger node budget.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import chess
import chess.engine

from app.collections import generate_puzzle_id
from app.generation import parse_games, position_at


def verify(engine_path: str, nodes: int, seconds: float) -> None:
    data_path = ROOT / "data/generated-puzzles.json"
    rows = json.loads(data_path.read_text(encoding="utf-8"))
    corpus = (ROOT / "data/public-games.pgn").read_text(encoding="utf-8")
    parsed = parse_games(corpus, max_games=50, max_plies=400)
    games = {game["game_url"]: game for game in parsed}
    metadata_path = ROOT / "data/public-games.metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert hashlib.sha256(corpus.encode()).hexdigest() == metadata["corpus_sha256"]
    assert len(games) == len(parsed) == len(metadata["games"])
    for source in metadata["games"]:
        game = games[source["url"]]
        assert source["id"] == source["url"].rsplit("/", 1)[-1]
        assert source["plies"] == len(game["moves"])
        assert all(source[field] == game[field] for field in ("white", "black", "date", "result"))
    assert len(rows) == len({row["id"] for row in rows}) > 0
    checked_moves, failures = 0, []
    material = lambda board, color: sum(
        value * (len(board.pieces(piece, color)) - len(board.pieces(piece, not color)))
        for piece, value in [(chess.PAWN, 100), (chess.KNIGHT, 320), (chess.BISHOP, 330), (chess.ROOK, 500), (chess.QUEEN, 900)]
    )
    with chess.engine.SimpleEngine.popen_uci(engine_path, timeout=30) as engine:
        engine.configure({"Threads": 1, "Hash": 64})
        for row in rows:
            try:
                game = games[row["game_url"]]
                ply = row["source_ply"]
                assert row["collection"] == "public" and row["rating_estimated"] is True
                assert row["initial_fen"] == position_at(game, ply - 1).fen()
                assert row["moves"][0] == game["moves"][ply - 1]
                assert len(row["moves"]) >= 2 and len(row["moves"]) % 2 == 0
                assert row["id"] == generate_puzzle_id(row["initial_fen"], row["moves"], "public")
                board = chess.Board(row["initial_fen"])
                board.push_uci(row["moves"][0])
                learner = board.turn
                initial_material = material(board, learner)
                for index, uci in enumerate(row["moves"][1:]):
                    move = chess.Move.from_uci(uci)
                    assert move in board.legal_moves, f"illegal continuation {uci}"
                    if index % 2 == 0:
                        after = board.copy(stack=False)
                        after.push(move)
                        engine.configure({"Clear Hash": None})
                        infos = engine.analyse(board, chess.engine.Limit(nodes=nodes, time=seconds), multipv=2)
                        best = infos[0]["pv"][0]
                        if not after.is_checkmate():
                            assert best == move, f"stronger search prefers {best.uci()} to {uci}"
                            assert infos[0].get("depth", 0) >= 10, "insufficient verification depth"
                            best_score = infos[0]["score"].pov(learner)
                            first = best_score.score(mate_score=10000)
                            assert first >= 120, f"not clearly winning: {first} cp"
                            if len(infos) > 1:
                                assert infos[1].get("depth", 0) >= 10, "insufficient alternative-move verification depth"
                                second_score = infos[1]["score"].pov(learner)
                                second = second_score.score(mate_score=10000)
                                if best_score.mate() is not None and best_score.mate() > 0:
                                    assert second_score.mate() is None or second_score.mate() <= 0, "another forced mate exists"
                                else:
                                    assert first - second >= 160 and second < 120, f"ambiguous winning choice: {first}/{second} cp"
                        checked_moves += 1
                    board.push(move)
                assert board.turn != learner
                if not board.is_checkmate():
                    engine.configure({"Clear Hash": None})
                    reply = engine.analyse(board, chess.engine.Limit(nodes=nodes, time=seconds))
                    endpoint_score = reply["score"].pov(learner)
                    assert endpoint_score.mate() is None, "forced mate requires a complete mate continuation"
                    assert endpoint_score.score(mate_score=10000) >= 120, "endpoint loses winning advantage"
                    board.push(reply["pv"][0])
                    assert material(board, learner) - initial_material >= 100, "endpoint is only a temporary capture"
                    engine.configure({"Clear Hash": None})
                    continuation = engine.analyse(board, chess.engine.Limit(nodes=nodes, time=seconds), multipv=2)
                    assert all(info.get("depth", 0) >= 10 for info in continuation), "insufficient endpoint verification depth"
                    next_score = continuation[0]["score"].pov(learner)
                    assert next_score.mate() is None, "endpoint hides a forced mate"
                    assert len(continuation) > 1, "endpoint still requires a forced response"
                    first = next_score.score(mate_score=10000)
                    second = continuation[1]["score"].pov(learner).score(mate_score=10000)
                    assert second >= 120 and first - second < 120, "endpoint still requires a precise winning continuation"
                print(f"PASS {row['id']} {row['game_url']} ply {ply}", flush=True)
            except (AssertionError, KeyError, ValueError) as error:
                failures.append({"id": row["id"], "reason": str(error) or "invalid provenance/schema"})
                print(f"FAIL {row['id']}: {error}", flush=True)
        engine_name = engine.id.get("name", "Stockfish")
    if failures:
        print(json.dumps({"failed": failures, "checked_learner_moves": checked_moves}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    audit = {"engine": engine_name, "nodes_per_search": nodes, "seconds_per_search": seconds,
             "puzzles": len(rows), "learner_moves": checked_moves, "provenance_and_legality": "passed",
             "fresh_hash_choice_and_endpoint_checks": "passed",
             "endpoint_checks": "Actual mate, or stable material after best reply with two winning learner moves within 120 cp and no forced mate"}
    metadata["generation"]["puzzles"] = len(rows)
    metadata["generation"]["games_represented"] = len({row["game_url"] for row in rows})
    metadata["generation"]["phases"] = dict(Counter(row["phase"] for row in rows))
    metadata["generation"]["bank_sha256"] = hashlib.sha256(data_path.read_bytes()).hexdigest()
    metadata["verification"] = audit
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="/usr/games/stockfish")
    parser.add_argument("--nodes", type=int, default=1_000_000)
    parser.add_argument("--seconds", type=float, default=3.0)
    args = parser.parse_args()
    if args.nodes < 1 or args.seconds <= 0:
        parser.error("Search budgets must be positive")
    verify(args.engine, args.nodes, args.seconds)
