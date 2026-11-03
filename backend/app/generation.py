"""Bounded, resumable analysis of real games into improvements and tactics.

The caller owns the engine process and concurrency limit. A job can checkpoint
one zero-based game ply after each analyse_position call. Engine scores are from
the moving player's point of view; they are never a puzzle difficulty rating.
"""
from __future__ import annotations

import io
import math
import re
import time
from urllib.parse import urlparse

import chess
import chess.engine
import chess.pgn

MAX_PGN_BYTES = 2 * 1024 * 1024
VALUES = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330, chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}


class _QuietBuilder(chess.pgn.GameBuilder):
    def handle_error(self, error):
        self.game.errors.append(error)


def _clock_metadata(game: chess.pgn.Game) -> dict:
    """Keep observations separate from durations; a missing clock breaks inference.

    Chess.com clocks record the remaining seconds *after* increment is added.
    The initial allotment is usable only for a game from the normal start.
    Multi-stage and correspondence controls need a separate timing model.
    """
    control = game.headers.get("TimeControl", "")
    match = re.fullmatch(r"([1-9]\d{0,5})(?:\+(\d{1,4}))?", control)
    base = int(match[1]) if match else None
    increment = int(match[2] or 0) if match else None
    initial = game.board()
    previous = {chess.WHITE: base, chess.BLACK: base} if initial.fen() == chess.STARTING_FEN else {
        chess.WHITE: None, chess.BLACK: None,
    }
    clocks, before, spent = [], [], []
    side = initial.turn
    for node in game.mainline():
        try:
            clock = node.clock()
        except (ValueError, OverflowError):
            clock = None
        if clock is not None and (not math.isfinite(clock) or clock < 0):
            clock = None
        prior = previous[side]
        elapsed = prior + increment - clock if prior is not None and increment is not None and clock is not None else None
        # Invalid jumps are unknown, never "instant moves" or negative time.
        if elapsed is not None:
            elapsed = round(max(0.0, elapsed), 3) if elapsed >= -0.001 else None
        clocks.append(clock)
        before.append(prior)
        spent.append(elapsed)
        previous[side] = clock
        side = not side
    return {"time_control": control, "base_seconds": base, "increment_seconds": increment,
            "clocks": clocks, "clock_before": before, "move_seconds": spent, "spent_seconds": spent}


def parse_games(pgn_text: str, max_games: int = 50, max_plies: int = 400,
                warnings: list[str] | None = None, *, time_control_override: str | None = None) -> list[dict]:
    """Read complete standard games. Invalid games are skipped with explanations.

    A warning list is optional. If no playable games remain, raise ValueError;
    callers must not silently publish a partial or malformed game as a task.
    """
    if not isinstance(pgn_text, str) or len(pgn_text.encode("utf-8")) > MAX_PGN_BYTES:
        raise ValueError("Файл PGN слишком большой: допустимо до 2 МБ.")
    if not 1 <= max_games <= 1000 or not 1 <= max_plies <= 1000:
        raise ValueError("Некорректный предел числа партий или ходов.")
    notices = warnings if warnings is not None else []
    stream = io.StringIO(pgn_text.lstrip("\ufeff"))
    result = []
    read_count = 0
    while read_count < max_games:
        try:
            game = chess.pgn.read_game(stream, Visitor=_QuietBuilder)
        except (ValueError, IndexError) as exc:
            notices.append(f"Не удалось прочитать партию: {str(exc)[:120]}")
            break
        if game is None:
            break
        read_count += 1
        try:
            if game.errors:
                raise ValueError("запись содержит недопустимые ходы")
            if time_control_override is not None:
                game.headers["TimeControl"] = time_control_override
            board = game.board()
            if type(board) is not chess.Board or board.chess960:
                raise ValueError("поддерживаются только обычные шахматы")
            if not board.is_valid():
                raise ValueError("некорректная начальная позиция")
            if game.headers.get("Result") not in {"1-0", "0-1", "1/2-1/2"}:
                raise ValueError("нужна завершённая партия")
            moves = list(game.mainline_moves())
            if not moves:
                raise ValueError("в партии нет ходов")
            if len(moves) > max_plies:
                raise ValueError(f"более {max_plies} полуходов")
            for move in moves:
                if move not in board.legal_moves:
                    raise ValueError("в партии есть недопустимый ход")
                board.push(move)
            site = game.headers.get("Link") or game.headers.get("Site", "")
            parsed = urlparse(site)
            if parsed.scheme != "https" or parsed.hostname not in {"lichess.org", "www.lichess.org", "chess.com", "www.chess.com"}:
                site = ""
            result.append({
                "white": game.headers.get("White", "Неизвестный"),
                "black": game.headers.get("Black", "Неизвестный"),
                "result": game.headers.get("Result"),
                "date": game.headers.get("UTCDate") or game.headers.get("Date", ""),
                "event": game.headers.get("Event", ""),
                "game_url": site,
                "initial_fen": game.board().fen(),
                "moves": [move.uci() for move in moves],
                "eco": game.headers.get("ECO", "")[:12],
                "opening": game.headers.get("Opening", "")[:250],
                "opening_url": game.headers.get("ECOUrl", "")[:1000],
                **_clock_metadata(game),
                "pgn": game.accept(chess.pgn.StringExporter(headers=True, variations=False, comments=True)),
            })
        except (ValueError, TypeError) as exc:
            notices.append(f"Партия {read_count} пропущена: {exc}.")
    if stream.read().strip():
        notices.append(f"Обработаны первые {max_games} записей PGN.")
    if not result:
        raise ValueError(notices[0] if notices else "В PGN не найдены завершённые партии.")
    return result


def position_at(game: dict, ply: int) -> chess.Board:
    """Rebuild position before a zero-based ply without losing game state."""
    moves = game.get("moves", [])
    if not isinstance(ply, int) or isinstance(ply, bool) or not 0 <= ply < len(moves):
        raise ValueError("Номер хода вне партии.")
    board = chess.Board(game["initial_fen"])
    if not board.is_valid():
        raise ValueError("Некорректная начальная позиция.")
    for uci in moves[:ply]:
        board.push_uci(uci)
    return board


def _probe(engine, board, nodes, max_time, *, multipv=1, root_moves=None):
    info = engine.analyse(board, chess.engine.Limit(nodes=max(1, int(nodes)), time=max_time),
                          multipv=multipv, root_moves=root_moves,
                          info=chess.engine.INFO_SCORE | chess.engine.INFO_PV | chess.engine.INFO_BASIC)
    return info if isinstance(info, list) else [info]


def _score(info, color):
    score = info["score"].pov(color)
    value = score.score(mate_score=10_000)
    return int(value if value is not None else 0), score.mate()


def _loss(best, played):
    # Faster mate is not a missed winning opportunity; surviving a little longer
    # in an unavoidable mate is not a tactical improvement worth presenting.
    if best[1] is not None and played[1] is not None and (best[1] > 0) == (played[1] > 0):
        return 0
    return max(0, best[0] - played[0])


def _pv_move(info, board):
    pv = info.get("pv", [])
    return pv[0] if pv and pv[0] in board.legal_moves else None


def _mates(board, move):
    probe = board.copy(stack=False)
    probe.push(move)
    return probe.is_checkmate()


REJECTION_REASONS = {
    "insufficient_depth": "Движок не успел достаточно глубоко проверить лучшие продолжения. Это не означает, что задача невозможна.",
    "multiple_good_moves": "Найдено несколько близких по силе продолжений. Для этой позиции нужна поддержка нескольких правильных ответов.",
    "defensive_position": "Улучшение помогает защищаться или удерживать позицию. Текущий режим создаёт задачи на выигрыш материала или мат.",
    "line_too_long": "За семь полуходов не удалось подтвердить завершение комбинации. Такой момент пока оставлен для разбора.",
    "unstable_gain": "Устойчивый выигрыш материала или завершённый мат не подтверждён после сильнейшего ответа соперника.",
    "motif_not_supported": "Продолжение проверено, но тактический мотив пока не распознан.",
    "missing_predecessor": "Не хватает корректного предыдущего хода для начальной позиции задачи.",
    "analysis_budget": "Достигнут предел времени проверки комбинации. Это не означает, что из позиции нельзя сделать задачу.",
    "invalid_engine_line": "Движок не вернул корректное продолжение для полной проверки задачи.",
    "search_unstable": "Повторный поиск не подтвердил устойчивость единственного лучшего хода. Позиция сохранена для разбора.",
}


class _AnalysisBudgetExceeded(Exception):
    pass


def _bounded_probe(engine, board, nodes, max_time, deadline=None, **kwargs):
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0.02:
            raise _AnalysisBudgetExceeded
        max_time = min(max_time, remaining)
    return _probe(engine, board, nodes, max_time, **kwargs)


def _choice_rejection(board, infos, min_depth=0):
    if not infos or (move := _pv_move(infos[0], board)) is None:
        return "invalid_engine_line"
    if _mates(board, move):
        # The trainer already accepts any legal immediate checkmate.
        return None
    if infos[0].get("depth", 0) < min_depth:
        return "insufficient_depth"
    first, mate = _score(infos[0], board.turn)
    if first < 120:
        return "defensive_position"
    if len(infos) < 2:
        return None if board.legal_moves.count() == 1 else "insufficient_depth"
    if infos[1].get("depth", 0) < min_depth:
        return "insufficient_depth"
    if _pv_move(infos[1], board) is None:
        return "invalid_engine_line"
    second, second_mate = _score(infos[1], board.turn)
    if mate is not None and mate > 0:
        return "multiple_good_moves" if second_mate is not None and second_mate > 0 else None
    # A concrete tactical gain may be clearly better even if the alternative
    # also preserves a winning position. Require a meaningful gap, then verify
    # the complete material/mate outcome in _build_puzzle.
    return None if first - second >= 120 else "multiple_good_moves"


def _unique_tactical_choice(board, infos, min_depth=0):
    return _choice_rejection(board, infos, min_depth) is None


def _material(board, color):
    return sum(value * (len(board.pieces(piece, color)) - len(board.pieces(piece, not color)))
               for piece, value in VALUES.items())


def _move_themes(before, move, after):
    color = before.turn
    enemy = not color
    themes = set()
    if move.promotion:
        themes.add("promotion")
        if move.promotion != chess.QUEEN:
            themes.add("underPromotion")
    captured_square = move.to_square
    if before.is_en_passant(move):
        captured_square += -8 if color == chess.WHITE else 8
    if before.is_capture(move) and not before.attackers(enemy, captured_square):
        themes.add("hangingPiece")
    targets = [square for square in after.attacks(move.to_square)
               if (piece := after.piece_at(square)) and piece.color == enemy
               and piece.piece_type in {chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN, chess.KING}]
    if len(targets) >= 2 and not after.is_checkmate():
        themes.add("fork")
    if after.is_check() and len(after.checkers()) > 1:
        themes.add("doubleCheck")
    for square, piece in after.piece_map().items():
        if piece.color == enemy and piece.piece_type != chess.KING and after.is_pinned(enemy, square) and not before.is_pinned(enemy, square):
            themes.add("pin")
        if piece.color == color and square != move.to_square and piece.piece_type in {chess.BISHOP, chess.ROOK, chess.QUEEN}:
            new_attacks = after.attacks(square) & ~before.attacks(square)
            if any((target := after.piece_at(target_square)) and target.color == enemy
                   and target.piece_type in {chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN, chess.KING}
                   for target_square in new_attacks):
                themes.add("discoveredAttack")
    if after.is_checkmate():
        king = after.king(enemy)
        checking = after.piece_at(move.to_square)
        if king is not None and checking and checking.piece_type in {chess.ROOK, chess.QUEEN} and chess.square_rank(king) in {0, 7} and chess.square_rank(king) == chess.square_rank(move.to_square):
            escape_rank = 1 if chess.square_rank(king) == 0 else 6
            if any((blocker := after.piece_at(chess.square(file, escape_rank))) and blocker.color == enemy
                   for file in range(max(0, chess.square_file(king) - 1), min(7, chess.square_file(king) + 1) + 1)):
                themes.add("backRankMate")
    return themes


def _build_puzzle(game, ply, start, root_infos, engine, verify_nodes, max_time,
                  *, diagnostics=None, deadline=None):
    def reject(code):
        if diagnostics is not None:
            diagnostics["code"] = code
        return None

    if ply < 1:
        return reject("missing_predecessor")
    if reason := _choice_rejection(start, root_infos, min_depth=10):
        return reject(reason)
    learner = start.turn
    board = start.copy(stack=False)
    initial_material = _material(board, learner)
    line = []
    themes = set()
    cached = {start.fen(): root_infos}
    must_mate = (_score(root_infos[0], learner)[1] or 0) > 0

    def probe(position):
        # Endpoint checks look one reply ahead. Reuse those same searches when
        # the lesson needs to continue instead of paying for them twice.
        key = position.fen()
        if key not in cached:
            cached[key] = _bounded_probe(engine, position, verify_nodes, max_time, deadline,
                                         multipv=2 if position.turn == learner else 1)
        return cached[key]

    complete = False
    for offset in range(7):
        learner_turn = board.turn == learner
        infos = probe(board)
        if learner_turn and (reason := _choice_rejection(board, infos, min_depth=10)):
            return reject(reason)
        if not infos or (move := _pv_move(infos[0], board)) is None:
            return reject("invalid_engine_line")
        must_mate |= (_score(infos[0], learner)[1] or 0) > 0
        before = board.copy(stack=False)
        board.push(move)
        line.append(move.uci())
        if learner_turn:
            themes.update(_move_themes(before, move, board))
        if board.is_game_over():
            complete = board.is_checkmate() and learner_turn
            break
        if learner_turn and not must_mate and _material(board, learner) - initial_material >= 100:
            # A temporary capture followed by an immediate recapture is not the
            # endpoint of a lesson. Check the engine's strongest reply first.
            reply_infos = probe(board)
            reply = _pv_move(reply_infos[0], board) if reply_infos else None
            after_reply = board.copy(stack=False)
            if reply:
                after_reply.push(reply)
                score, mate = _score(reply_infos[0], learner)
                must_mate |= (mate or 0) > 0
                if (_material(after_reply, learner) - initial_material >= 100
                        and score >= 120 and mate is None
                        and reply_infos[0].get("depth", 0) >= 10
                        and not after_reply.is_game_over()):
                    followup = probe(after_reply)
                    # A nominal material gain can still require the next exact
                    # move (e.g. ...Bxc3+ Kxc3 ...axb5). End only after the gain
                    # is stable with multiple similarly good continuations.
                    expected = min(2, after_reply.legal_moves.count())
                    sufficiently_searched = (len(followup) >= expected and
                        all(item.get("depth", 0) >= 10 and _pv_move(item, after_reply)
                            for item in followup[:expected]))
                    if sufficiently_searched:
                        next_score, next_mate = _score(followup[0], learner)
                        must_mate |= (next_mate or 0) > 0
                        alternative_score, alternative_mate = (_score(followup[1], learner)
                            if len(followup) >= 2 else (0, None))
                        # Require several similarly good next moves: a
                        # nominal +1.3 alternative must not hide an essential
                        # +4 move that still belongs to this combination.
                        if (next_score >= 120 and next_mate is None
                                and alternative_score >= 120 and alternative_mate is None
                                and next_score - alternative_score < 120):
                            complete = True
                            break
    if not complete or not line or len(line) % 2 != 1:
        return reject("line_too_long" if len(line) >= 7 else "unstable_gain")
    first = chess.Move.from_uci(line[0])
    if not start.is_capture(first) and not start.gives_check(first):
        themes.add("quietMove")
    if start.is_check():
        themes.add("defensiveMove")
    if board.is_checkmate():
        count = (len(line) + 1) // 2
        if count <= 3:
            themes.add(f"mateIn{count}")
    if not themes:
        return reject("motif_not_supported")
    previous = position_at(game, ply - 1)
    predecessor = game["moves"][ply - 1]
    test = previous.copy(stack=False)
    test.push_uci(predecessor)
    if test.fen() != start.fen():
        return reject("missing_predecessor")
    # This is a heuristic estimate until enough users have solved the puzzle.
    rating = min(2300, 700 + 200 * ((len(line) + 1) // 2) + (150 if "quietMove" in themes else 0))
    return {"initial_fen": previous.fen(), "moves": [predecessor, *line], "themes": sorted(themes), "rating": rating}


def analyse_position(game: dict, ply: int, engine, *, min_loss_cp: int = 120,
                     quick_nodes: int = 12_000, verify_nodes: int = 250_000,
                     max_time: float = 1.2, require_mistake: bool = True,
                     max_position_time: float = 14.0) -> dict | None:
    """Screen ordinary moves cheaply and confirm candidate improvements deeply.

    The quick pass is only a gate. A deeper search may correct its preferred
    move. Interesting moments receive a second, larger comparison before they
    are reported as errors, then a bounded check of the whole tactical line.
    """
    if min_loss_cp < 0 or max_time <= 0 or max_position_time <= 0:
        raise ValueError("Некорректные параметры анализа.")
    board = position_at(game, ply)
    if board.is_game_over():
        return None
    played = chess.Move.from_uci(game["moves"][ply])
    if played not in board.legal_moves:
        raise ValueError("Недопустимый ход в партии.")
    color = board.turn
    deadline = time.monotonic() + max_position_time

    def probe(nodes, duration, **kwargs):
        return _bounded_probe(engine, board, nodes, duration, deadline, **kwargs)

    def compare(infos, nodes, duration):
        played_info = next((item for item in infos if _pv_move(item, board) == played), None)
        if played_info is None:
            checked = probe(nodes, duration, root_moves=[played])
            if not checked or _pv_move(checked[0], board) != played:
                return None
            played_info = checked[0]
        return played_info

    try:
        quick = probe(quick_nodes, min(max_time, 0.2), multipv=2)
        if not quick or (quick_best := _pv_move(quick[0], board)) is None:
            return None
        if require_mistake and quick_best == played:
            return None
        if not require_mistake and not _unique_tactical_choice(board, quick):
            return None
        quick_played = compare(quick, quick_nodes, min(max_time, 0.2))
        if quick_played is None:
            return None
        if require_mistake and _loss(_score(quick[0], color), _score(quick_played, color)) < min_loss_cp:
            return None

        screened = probe(verify_nodes, min(max_time, 0.8), multipv=2)
        if not screened or (screened_best := _pv_move(screened[0], board)) is None:
            return None
        screened_played = compare(screened, verify_nodes, min(max_time, 0.8))
        if screened_played is None:
            return None
        if require_mistake and (screened_best == played or
                _loss(_score(screened[0], color), _score(screened_played, color)) < min_loss_cp):
            return None

        # Re-evaluate both choices with a larger budget. In particular, a
        # shallow "error" can disappear when the played move is searched well.
        confirmation_nodes = max(600_000, verify_nodes * 2)
        # Stockfish confirmation starts independently of the quick/screening
        # tree, so its hash does not merely repeat a shallow preferred move.
        if "Clear Hash" in getattr(engine, "options", {}):
            engine.configure({"Clear Hash": None})
        verified = probe(confirmation_nodes, max_time, multipv=2)
        if not verified or (best := _pv_move(verified[0], board)) is None:
            return None
        played_info = compare(verified, confirmation_nodes, max_time)
        if played_info is None:
            return None
    except _AnalysisBudgetExceeded:
        # An unconfirmed improvement must not be presented as an actual error.
        return None

    best_score, played_score = _score(verified[0], color), _score(played_info, color)
    loss = _loss(best_score, played_score)
    if require_mistake and (best == played or loss < min_loss_cp):
        return None
    diagnostics = {}
    # A large enough gap must be present in both independent root searches.
    # Continuation verification still proves an actual material/mate outcome;
    # a relative score alone never produces a task.
    confirmed_reason = _choice_rejection(board, verified, min_depth=10)
    screen_reason = _choice_rejection(board, screened, min_depth=10)
    if confirmed_reason is None and (screened_best != best or screen_reason is not None):
        diagnostics["code"] = "insufficient_depth" if screen_reason == "insufficient_depth" else "search_unstable"
        puzzle = None
    else:
        try:
            puzzle = _build_puzzle(game, ply, board, verified, engine,
                                   confirmation_nodes, max_time,
                                   diagnostics=diagnostics, deadline=deadline)
        except _AnalysisBudgetExceeded:
            puzzle = None
            diagnostics["code"] = "analysis_budget"
    if not require_mistake and puzzle is None:
        return None
    reason_code = diagnostics.get("code") if puzzle is None else None
    reason = REJECTION_REASONS.get(reason_code) if reason_code else None
    best_line_san = []
    probe_board = board.copy(stack=False)
    for move in verified[0].get("pv", [])[:7]:
        if move not in probe_board.legal_moves:
            break
        best_line_san.append(probe_board.san(move))
        probe_board.push(move)
    non_pawns = sum(1 for piece in board.piece_map().values() if piece.piece_type not in {chess.PAWN, chess.KING})
    phase = "endgame" if len(board.piece_map()) <= 12 or (not board.queens and non_pawns <= 6) else "opening" if board.fullmove_number <= 10 else "middlegame"
    return {
        "phase": phase,
        "ply": ply, "move_number": board.fullmove_number,
        "side": "white" if color else "black", "before_fen": board.fen(),
        "played_uci": played.uci(), "played_san": board.san(played),
        "best_uci": best.uci(), "best_san": board.san(best), "best_line_san": best_line_san,
        "loss_cp": loss, "score_before": best_score[0], "score_after": played_score[0],
        "mate_before": best_score[1], "mate_after": played_score[1], "puzzle": puzzle,
        "puzzle_reason_code": reason_code, "puzzle_reason": reason,
        "analysis_depth": min(item.get("depth", 0) for item in verified[:2]),
        "explanation": "Найдена проверенная тактическая возможность." if puzzle else reason,
    }
