"""Game parsing and conservative tactic validation, independent of PostgreSQL."""
import os
from pathlib import Path
import shutil

import chess
import chess.engine
import pytest

from app.generation import (
    _build_puzzle, _loss, _move_themes, _score, _unique_tactical_choice,
    analyse_position, parse_games, position_at,
)


@pytest.fixture(scope="session", autouse=True)
def database():
    # This module exercises no database models or application lifespan.
    yield


@pytest.fixture(autouse=True)
def clean_training():
    yield


FOOLS_MATE = '''[Event "Parser fixture"]
[Site "https://lichess.org/abcdefgh"]
[White "WhitePlayer"]
[Black "BlackPlayer"]
[Result "0-1"]

1. f3 e5 2. g4 Qh4# 0-1
'''


def info(board, uci, cp=0, mate=None):
    return {"pv": [chess.Move.from_uci(uci)], "depth": 18,
            "score": chess.engine.PovScore(chess.engine.Mate(mate) if mate is not None else chess.engine.Cp(cp), board.turn)}


def test_parse_replays_complete_game_and_preserves_metadata():
    game = parse_games(FOOLS_MATE)[0]
    assert game["white"] == "WhitePlayer"
    assert game["game_url"] == "https://lichess.org/abcdefgh"
    assert game["moves"] == ["f2f3", "e7e5", "g2g4", "d8h4"]
    board = position_at(game, 3)
    assert board.turn == chess.BLACK and board.fullmove_number == 2
    board.push_uci(game["moves"][3])
    assert board.is_checkmate()
    with pytest.raises(ValueError):
        position_at(game, 4)


def test_parser_rejects_invalid_unfinished_and_overlong_games():
    for text in ["", FOOLS_MATE.replace('"0-1"', '"*"').replace('0-1\n', '*\n'),
                 FOOLS_MATE.replace("1. f3", "1. e5")]:
        with pytest.raises(ValueError):
            parse_games(text)
    with pytest.raises(ValueError, match="полуходов"):
        parse_games(FOOLS_MATE, max_plies=2)
    with pytest.raises(ValueError, match="2 МБ"):
        parse_games("x" * (2 * 1024 * 1024 + 1))


def test_mixed_parser_reports_skipped_variant_and_strips_untrusted_site():
    variant = FOOLS_MATE.replace('[Event "Parser fixture"]', '[Variant "Crazyhouse"]')
    notices = []
    games = parse_games(variant + "\n" + FOOLS_MATE.replace("https://lichess.org/abcdefgh", "https://evil.example/game"), warnings=notices)
    assert len(games) == 1
    assert games[0]["game_url"] == ""
    assert notices and "обычные шахматы" in notices[0]


def test_scores_are_from_mover_pov_and_faster_mate_is_not_a_blunder():
    board = chess.Board()
    item = info(board, "e2e4", cp=180)
    assert _score(item, chess.WHITE) == (180, None)
    assert _score(item, chess.BLACK) == (-180, None)
    assert _loss((9998, 2), (9995, 5)) == 0
    assert _loss((-9995, -5), (-9998, -2)) == 0
    assert _loss((180, None), (-100, None)) == 280


def test_two_already_winning_moves_are_not_a_single_solution():
    board = chess.Board()
    assert not _unique_tactical_choice(board, [info(board, "e2e4", cp=500), info(board, "d2d4", cp=250)])
    assert _unique_tactical_choice(board, [info(board, "e2e4", cp=300), info(board, "d2d4", cp=0)])
    shallow = info(board, "d2d4", cp=0)
    shallow["depth"] = 5
    assert not _unique_tactical_choice(board, [info(board, "e2e4", cp=300), shallow], min_depth=10)



def test_mating_queen_is_not_tagged_as_an_irrelevant_material_fork():
    board = chess.Board("rn1k1b1r/pbpp1Q1p/1p2p3/7B/3PP2q/P1N5/1PP2PPP/R3K2R b KQ - 0 12")
    board.push_uci("b8c6")
    move = chess.Move.from_uci("f7e8")
    after = board.copy(stack=False)
    after.push(move)
    assert after.is_checkmate()
    themes = _move_themes(board, move, after)
    assert "fork" not in themes
    assert "backRankMate" in themes


def test_en_passant_checks_defenders_of_captured_pawn():
    board = chess.Board("7k/8/2p5/3pP3/8/8/8/K7 w - d6 0 2")
    move = chess.Move.from_uci("e5d6")
    after = board.copy(stack=False)
    after.push(move)
    assert "hangingPiece" not in _move_themes(board, move, after)


def test_mate_against_bare_king_is_not_a_back_rank_motif():
    board = chess.Board("7k/5Q2/6K1/8/8/8/8/8 w - - 0 1")
    move = chess.Move.from_uci("f7f8")
    after = board.copy(stack=False)
    after.push(move)
    assert after.is_checkmate()
    assert "backRankMate" not in _move_themes(board, move, after)


def test_temporary_capture_with_losing_recapture_is_not_endpoint():
    game = {"initial_fen": "7k/7p/5p2/4r3/4Q3/8/8/K7 b - - 0 1", "moves": ["h7h6", "e4e5"]}
    board = position_at(game, 1)
    root = [info(board, "e4e5", cp=500), info(board, "e4d3", cp=0)]

    class RecaptureEngine:
        def analyse(self, current, *_args, **_kwargs):
            if current.turn == chess.BLACK:
                # Even an optimistic score must not allow a temporary material
                # gain to terminate the task before this obvious recapture.
                return [info(current, "f6e5", cp=-500)]
            return []

    assert _build_puzzle(game, 1, board, root, RecaptureEngine(), 1000, 0.1) is None


@pytest.mark.parametrize("followup_depth,second_score", [(18, 0), (5, 0), (18, 150)])
def test_material_endpoint_requires_no_essential_next_learner_move(followup_depth, second_score):
    game = {"initial_fen": "7k/6p1/8/8/3r4/8/3Q4/K7 b - - 0 1",
            "moves": ["g7g6", "d2d4"]}
    board = position_at(game, 1)
    root = [info(board, "d2d4", cp=500), info(board, "d2c3", cp=0)]

    class QuietContinuationEngine:
        def analyse(self, current, *_args, **_kwargs):
            if current.turn == chess.BLACK:
                return [info(current, "h8g8" if current.king(chess.BLACK) == chess.H8 else "g8h7", cp=-500)]
            if current.piece_at(chess.D4):
                result = [info(current, "d4e5", cp=500), info(current, "d4d5", cp=second_score)]
            else:
                result = [info(current, "e5e7", cp=500), info(current, "e5e3", cp=400)]
            for item in result:
                item["depth"] = followup_depth
            return result

    puzzle = _build_puzzle(game, 1, board, root, QuietContinuationEngine(), 1000, 0.1)
    if followup_depth < 10 or second_score >= 120:
        # A shallow search or a large gap between nominally winning moves
        # establishes neither resolution nor an unambiguous next puzzle move.
        assert puzzle is None
    else:
        assert puzzle["moves"] == ["g7g6", "d2d4", "h8g8", "d4e5"]


def test_improvement_can_be_reported_without_fabricating_a_puzzle():
    game = {"initial_fen": chess.STARTING_FEN, "moves": ["a2a3", "a7a6", "h2h3"]}

    class AmbiguousEngine:
        def analyse(self, board, _limit, *, root_moves=None, **_kwargs):
            if root_moves:
                return [info(board, root_moves[0].uci(), cp=0)]
            return [info(board, "e2e4", cp=500), info(board, "d2d4", cp=250)]

    candidate = analyse_position(game, 2, AmbiguousEngine())
    assert candidate["loss_cp"] == 500
    assert candidate["best_san"] == "e4"
    assert candidate["played_san"] == "h3"
    assert candidate["puzzle"] is None
    assert analyse_position(game, 2, AmbiguousEngine(), require_mistake=False) is None


@pytest.fixture
def stockfish():
    executable = os.environ.get("TEST_STOCKFISH_PATH") or shutil.which("stockfish")
    if not executable and Path("/usr/games/stockfish").is_file():
        executable = "/usr/games/stockfish"
    if not executable:
        pytest.skip("Install Stockfish or set TEST_STOCKFISH_PATH for real-engine tests")
    with chess.engine.SimpleEngine.popen_uci(executable) as engine:
        engine.configure({"Threads": 1, "Hash": 32})
        yield engine


def test_real_engine_public_mate_and_personal_missed_mate(stockfish):
    game = parse_games(FOOLS_MATE)[0]
    assert analyse_position(game, 3, stockfish) is None
    found = analyse_position(game, 3, stockfish, require_mistake=False)
    assert found["mate_before"] == 1 and found["side"] == "black"
    assert found["puzzle"]["themes"] == ["mateIn1"]
    board = chess.Board(found["puzzle"]["initial_fen"])
    for uci in found["puzzle"]["moves"]:
        board.push_uci(uci)
    assert board.is_checkmate()
    missed = parse_games(FOOLS_MATE.replace("Qh4#", "Nc6"))[0]
    improvement = analyse_position(missed, 3, stockfish)
    assert improvement["best_uci"] == "d8h4"
    assert improvement["loss_cp"] >= 120
    assert improvement["puzzle"]["moves"] == ["g2g4", "d8h4"]


def test_real_engine_requires_recapture_and_finishes_forced_mate(stockfish):
    material = {"initial_fen": "rnb2rk1/1p4pp/p3p3/1B1pPp2/1b1N4/2q3Q1/P1PK1PPP/RR6 w - - 0 17",
                "moves": ["g3c3", "b4c3"]}
    found = analyse_position(material, 1, stockfish, require_mistake=False,
                             quick_nodes=40000, verify_nodes=500000, max_time=2)
    assert found is not None
    assert found["puzzle"]["moves"][:4] == ["g3c3", "b4c3", "d2c3", "a6b5"]
    mate = {"initial_fen": "r1b2r1k/3qb2p/p1n1R2Q/1p3N2/3P4/1B6/PPP2PPP/R5K1 b - - 0 21",
            "moves": ["f8f5", "e6g6"]}
    found = analyse_position(mate, 1, stockfish, require_mistake=False,
                             quick_nodes=40000, verify_nodes=500000, max_time=2)
    assert found is not None
    final = chess.Board(found["puzzle"]["initial_fen"])
    for uci in found["puzzle"]["moves"]:
        final.push_uci(uci)
    assert final.is_checkmate()
