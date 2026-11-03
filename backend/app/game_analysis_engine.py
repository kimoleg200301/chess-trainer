"""Bounded move-quality analysis, independent from tactical puzzle generation.

This is Tactics Lab's transparent heuristic, not Chess.com's CAPS algorithm or
an empirically calibrated win probability. Centipawns map to an expected-result
proxy with logistic(cp / 300); a move loses at most one point of that proxy.
Its score is 100 * exp(-6 * loss). A side's final accuracy is the arithmetic mean
of assessed, non-forced moves, and is withheld until that side is fully checked.
"""
from __future__ import annotations

import math
import threading
import time

import chess
import chess.engine

from .generation import position_at

CATEGORY_NAMES = {
    "brilliant": "Бриллиантовый", "great": "Сильный", "best": "Лучший",
    "excellent": "Отличный", "good": "Хороший", "inaccuracy": "Неточность",
    "mistake": "Ошибка", "blunder": "Зевок", "forced": "Вынужденный",
    "unassessed": "Не оценён",
}
ANALYSIS_METHOD = {
    "id": "tactics-lab-expected-result-v1",
    "name": "Оценка тренажёра на основе Stockfish",
    "description": "Собственная приближённая оценка, которая может отличаться от Chess.com.",
    "expected_result": "1 / (1 + exp(-centipawns / 300)); мат за игрока — 1, против — 0",
    "move_accuracy": "100 * exp(-6 * max(0, best_expected - played_expected))",
    "game_accuracy": "Среднее по оценённым невынужденным ходам после полной проверки стороны",
    "loss_thresholds": {"excellent": 0.01, "good": 0.03, "inaccuracy": 0.07, "mistake": 0.15},
    "special_moves": "Сильный и бриллиантовый — консервативные эвристические отметки, а не официальный вердикт Chess.com.",
}
VALUES = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
MIN_DEPTH = 8
SPECIAL_DEPTH = 12
# One default review profile. Budget applies to all searches for a played move.
ANALYSIS_ENGINE_OPTIONS = {"Threads": 2, "Hash": 256}
ANALYSIS_NODES = 2_000_000
ANALYSIS_SEARCH_SECONDS = 3.0
ANALYSIS_POSITION_SECONDS = 30.0
# A separate persisted step, with room for engine shutdown inside the API's
# 60-second graceful-stop/proxy limit. Only unassessed moves use this budget.
ANALYSIS_RETRY_OPTIONS = {"nodes": 4_000_000, "max_time": 6.0, "max_position_time": 45.0}


class _BudgetExceeded(Exception):
    pass


def expected_result(cp: int | float | None, mate: int | None = None) -> float:
    """A smooth expected-result proxy; not a player-specific win probability."""
    if mate is not None:
        # MateGiven and Mate(0) both report zero; their signed score separates them.
        return 1.0 if mate > 0 or (mate == 0 and cp is not None and cp >= 0) else 0.0
    if cp is None or not math.isfinite(cp):
        raise ValueError("Нужна конечная оценка позиции.")
    return 1.0 / (1.0 + math.exp(-max(-3000.0, min(3000.0, float(cp))) / 300.0))


def move_accuracy(loss: float) -> float:
    if not math.isfinite(loss):
        raise ValueError("Некорректная потеря оценки.")
    return 100.0 * math.exp(-6.0 * max(0.0, min(1.0, loss)))


def ordinary_category(loss: float, is_best: bool = False) -> str:
    """Best means the checked engine choice, not every approximately equal move."""
    if is_best and loss <= 0.01:
        return "best"
    if loss <= 0.01:
        return "excellent"
    if loss <= 0.03:
        return "good"
    if loss <= 0.07:
        return "inaccuracy"
    if loss <= 0.15:
        return "mistake"
    return "blunder"


def _material(board: chess.Board, color: chess.Color) -> int:
    return sum(value * (len(board.pieces(piece, color)) - len(board.pieces(piece, not color)))
               for piece, value in VALUES.items())


def _values(info: dict, color: chess.Color) -> tuple[int, int | None, float]:
    engine_score = info.get("score")
    if not isinstance(engine_score, chess.engine.PovScore):
        raise ValueError("Движок не вернул оценку с указанной стороной.")
    score = engine_score.pov(color)
    mate = score.mate()
    cp = score.score(mate_score=10_000)
    if cp is None:
        raise ValueError("Движок не вернул числовую оценку.")
    return int(cp), mate, expected_result(cp, mate)


def _first(info: dict, board: chess.Board) -> chess.Move | None:
    pv = info.get("pv", [])
    return pv[0] if pv and pv[0] in board.legal_moves else None


def _immediate_mate(board: chess.Board, move: chess.Move | None) -> bool:
    if move is None:
        return False
    copy = board.copy(stack=False)
    copy.push(move)
    return copy.is_checkmate()


def _usable(info: dict, board: chess.Board, minimum_depth: int = MIN_DEPTH) -> bool:
    move = _first(info, board)
    return (move is not None and isinstance(info.get("score"), chess.engine.PovScore)
            and (info.get("depth", 0) >= minimum_depth or _immediate_mate(board, move))
            and not info.get("lowerbound") and not info.get("upperbound"))


def _line(info: dict | None, board: chess.Board) -> tuple[list[str], list[str]]:
    position = board.copy(stack=False)
    ucis, sans = [], []
    for move in (info or {}).get("pv", [])[:12]:
        if move not in position.legal_moves:
            break
        ucis.append(move.uci())
        sans.append(position.san(move))
        position.push(move)
    return ucis, sans


def _terminal_expected(board: chess.Board, color: chess.Color) -> float | None:
    # Claimable threefold/fifty-move draws remain optional, unlike terminal outcomes.
    outcome = board.outcome(claim_draw=False)
    if outcome is None:
        return None
    return 0.5 if outcome.winner is None else float(outcome.winner == color)


def _search_exact(engine, board: chess.Board, limit: chess.engine.Limit, **kwargs) -> list[dict]:
    """Keep coherent exact-score events instead of python-chess's merged flags.

    ``analyse()`` aggregates dictionaries, so a bound from an earlier iteration
    can remain attached to a later exact score. The public event stream lets us
    retain score, PV and depth from the same message and compare MultiPV lines
    from the same completed depth. An unfinished final iteration is not scored.
    """
    selector = chess.engine.INFO_SCORE | chess.engine.INFO_PV | chess.engine.INFO_BASIC
    if not callable(getattr(engine, "analysis", None)):
        # Lightweight callers may implement only the blocking analysis interface.
        output = engine.analyse(board, limit, info=selector, **kwargs)
        return output if isinstance(output, list) else [output]
    root_moves = kwargs.get("root_moves")
    available = len(root_moves) if root_moves is not None else board.legal_moves.count()
    expected = min(kwargs.get("multipv", 1), available)
    iterations: dict[int, dict[int, dict]] = {}
    latest_root = None
    with engine.analysis(board, limit, info=selector, **kwargs) as analysis:
        # Streaming iteration itself has no SimpleEngine timeout. Preserve the
        # blocking analyse() watchdog, so a hung UCI process cannot hang a request.
        timeout = getattr(engine, "timeout", None)
        watchdog = threading.Timer(timeout + (limit.time or 0), engine.close) if timeout is not None else None
        if watchdog is not None:
            watchdog.daemon = True
            watchdog.start()
        try:
            for event in analysis:
                if not isinstance(event.get("depth"), int) or not _usable(event, board, 0):
                    continue
                rank = event.get("multipv", 1)
                if not 1 <= rank <= expected:
                    continue
                iterations.setdefault(event["depth"], {})[rank] = event
                if rank == 1:
                    latest_root = event
        finally:
            if watchdog is not None:
                watchdog.cancel()
    for depth in sorted(iterations, reverse=True):
        iteration = iterations[depth]
        if (len(iteration) == expected
                and len({_first(row, board) for row in iteration.values()}) == expected):
            return [iteration[rank] for rank in range(1, expected + 1)]
    # A checked root remains useful, but without a matching alternative no
    # special move designation is possible.
    return [latest_root] if latest_root is not None else []


def _brilliant_evidence(board: chess.Board, played: chess.Move, info: dict,
                        best_expected: float, probe, nodes: int, max_time: float) -> dict | None:
    """Recognize only a concrete, sound sacrifice accepted by the best reply.

    Merely leaving a piece attacked, exchanging equal material, recapturing, or
    making the only legal move cannot produce this label. Some real sacrifices
    are deliberately missed by this conservative, finite-search rule.
    """
    if board.is_check() or board.legal_moves.count() <= 1 or best_expected < 0.45:
        return None
    if info.get("depth", 0) < SPECIAL_DEPTH:
        return None
    if board.move_stack and board.is_capture(played) and played.to_square == board.peek().to_square:
        return None
    pv = info.get("pv", [])
    if len(pv) < 2 or pv[0] != played:
        return None
    after = board.copy(stack=False)
    after.push(played)
    reply = pv[1]
    if after.is_game_over() or reply not in after.legal_moves or not after.is_capture(reply):
        return None
    captured = after.piece_at(reply.to_square)
    if captured is None or captured.color != board.turn or VALUES[captured.piece_type] < 300:
        return None
    reply_san = after.san(reply)
    initial_material = _material(board, board.turn)
    after.push(reply)
    sacrificed = initial_material - _material(after, board.turn)
    if sacrificed < 200 or after.is_game_over():
        return None
    # Independent compensation check: the capture must not refute the sacrifice.
    verification = probe(after, max(nodes * 2, 400_000), max(max_time, 1.0), multipv=2)
    if not verification or not _usable(verification[0], after, SPECIAL_DEPTH):
        return None
    _, _, compensation = _values(verification[0], board.turn)
    if compensation < 0.45 or compensation < best_expected - 0.05:
        return None
    followup = _first(verification[0], after)
    final = after.copy(stack=False)
    final.push(followup)
    if (followup.to_square == reply.to_square and after.is_capture(followup)
            and _material(final, board.turn) >= initial_material - 100):
        return None
    return {"reply_uci": reply.uci(), "reply_san": reply_san,
            "sacrificed_piece": captured.symbol().upper(),
            "material_sacrificed_cp": sacrificed,
            "compensation_expected": round(compensation, 6),
            "verification_depth": verification[0].get("depth", 0)}


def analyse_game_move(game: dict, ply: int, engine, *, nodes: int = ANALYSIS_NODES,
                      max_time: float = ANALYSIS_SEARCH_SECONDS,
                      max_position_time: float = ANALYSIS_POSITION_SECONDS) -> dict:
    """Analyse one actual game move for either side with no database mutations."""
    if nodes < 1 or max_time <= 0 or max_position_time <= 0:
        raise ValueError("Некорректные ограничения анализа.")
    board = position_at(game, ply)
    played = chess.Move.from_uci(game["moves"][ply])
    if played not in board.legal_moves:
        raise ValueError("Недопустимый ход партии.")
    after = board.copy()
    after.push(played)
    result = {
        "ply": ply, "move_number": board.fullmove_number,
        "side": "white" if board.turn else "black", "before_fen": board.fen(), "after_fen": after.fen(),
        "played_uci": played.uci(), "played_san": board.san(played),
        "best_uci": None, "best_san": None, "category": "unassessed", "accuracy": None,
        "expected_loss": None, "best_expected": None, "played_expected": None,
        "score_before": None, "score_after": None, "mate_before": None, "mate_after": None,
        "best_line_uci": [], "best_line_san": [], "depth": 0,
        "assessed": False, "reason": "Позиция пока не получила надёжной оценки.", "brilliant_evidence": None,
    }
    if board.is_game_over():
        result["reason"] = "Перед этим ходом партия уже завершилась по правилам."
        return result
    if board.legal_moves.count() == 1:
        result.update(category="forced", assessed=True, best_uci=played.uci(), best_san=board.san(played),
                      best_line_uci=[played.uci()], best_line_san=[board.san(played)],
                      reason="Единственный допустимый ход. Он не входит в среднюю точность.")
        return result
    if after.is_checkmate():
        # This conclusion is exact and needs no numerical search or special label.
        result.update(category="best", assessed=True, best_uci=played.uci(), best_san=board.san(played),
                      accuracy=100.0, expected_loss=0.0, best_expected=1.0, played_expected=1.0,
                      mate_before=1, mate_after=1, best_line_uci=[played.uci()], best_line_san=[board.san(played)],
                      reason="Ход сразу ставит мат.")
        return result
    deadline = time.monotonic() + max_position_time
    retry_nodes = max(nodes * 3, 600_000)
    retry_seconds = max(max_time * 2, 2.0)

    def probe(position, budget, seconds, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0.03:
            raise _BudgetExceeded
        return _search_exact(engine, position,
                             chess.engine.Limit(nodes=budget, time=min(seconds, remaining)), **kwargs)

    def checked_root(budget, seconds):
        infos = probe(board, budget, seconds, multipv=2)
        if not infos or not _usable(infos[0], board):
            infos = probe(board, max(budget, retry_nodes), max(seconds, retry_seconds), multipv=2)
        return infos

    def evaluate_played(infos, budget, seconds):
        terminal = _terminal_expected(after, board.turn)
        if terminal is not None:
            return {"pv": [played], "depth": 99,
                    "score": chess.engine.PovScore(chess.engine.Cp(0), board.turn)}, terminal
        found = next((item for item in infos if _first(item, board) == played), None)
        if found is None or not _usable(found, board):
            checked = probe(board, budget, seconds, root_moves=[played])
            found = checked[0] if checked else None
        if found is None or not _usable(found, board):
            checked = probe(board, max(budget, retry_nodes), max(seconds, retry_seconds), root_moves=[played])
            found = checked[0] if checked else None
        return found, None

    try:
        infos = checked_root(nodes, max_time)
        if not infos or not _usable(infos[0], board):
            result["reason"] = "Недостаточная глубина расчёта. Точность этого хода не учитывается."
            return result
        played_info, terminal = evaluate_played(infos, nodes, max_time)
        if played_info is None or not _usable(played_info, board):
            result["reason"] = "Не удалось достаточно глубоко проверить сыгранный ход."
            return result
        best_cp, best_mate, best_expected = _values(infos[0], board.turn)
        played_cp, played_mate, played_expected = _values(played_info, board.turn)
        if terminal is not None:
            played_expected = terminal
        loss = max(0.0, best_expected - played_expected)
        # Verify mistakes and disagreements before publishing a category.
        if loss > 0.03 or played_expected > best_expected + 0.02:
            if "Clear Hash" in getattr(engine, "options", {}):
                engine.configure({"Clear Hash": None})
            infos = checked_root(max(nodes * 2, 400_000), max(max_time * 2, 1.0))
            if not infos or not _usable(infos[0], board):
                result["reason"] = "Повторный расчёт не дал достаточно глубокой оценки."
                return result
            played_info, terminal = evaluate_played(infos, max(nodes * 2, 400_000), max(max_time * 2, 1.0))
            if played_info is None or not _usable(played_info, board):
                result["reason"] = "Повторный расчёт сыгранного хода не завершён."
                return result
            best_cp, best_mate, best_expected = _values(infos[0], board.turn)
            played_cp, played_mate, played_expected = _values(played_info, board.turn)
            if terminal is not None:
                played_expected = terminal
        if played_expected > best_expected + 0.02:
            result["reason"] = "Оценки поисков расходятся. Точность этого хода не учитывается."
            return result
    except _BudgetExceeded:
        result["reason"] = "Достигнут предел времени проверки. Точность этого хода не учитывается."
        return result
    except (KeyError, TypeError, ValueError):
        result["reason"] = "Движок не вернул надёжную оценку хода."
        return result

    best = _first(infos[0], board)
    loss = max(0.0, best_expected - played_expected)
    category = ordinary_category(loss, played == best)
    reason = {
        "best": "Сыгран лучший ход найденного продолжения.",
        "excellent": "Ход почти не уступает лучшему найденному продолжению.",
        "good": "Ход немного снижает оценку позиции.",
        "inaccuracy": "Ход заметно снижает ожидаемый результат.",
        "mistake": "Ход существенно ухудшает позицию.",
        "blunder": "Ход резко ухудшает ожидаемый результат.",
    }[category]
    depth = min(infos[0].get("depth", 0), played_info.get("depth", 0))
    alternative = infos[1] if len(infos) > 1 and _usable(infos[1], board, SPECIAL_DEPTH) else None
    if played == best and loss <= 0.01 and depth >= SPECIAL_DEPTH and alternative is not None:
        alt_expected = _values(alternative, board.turn)[2]
        if best_expected - alt_expected >= 0.12 and 0.35 <= best_expected and alt_expected < 0.95:
            category = "great"
            reason = "Найден важный ход, заметно сильнее других проверенных продолжений."
        if alt_expected < 0.95:
            try:
                if "Clear Hash" in getattr(engine, "options", {}):
                    engine.configure({"Clear Hash": None})
                evidence = _brilliant_evidence(board, played, played_info, best_expected,
                                               probe, nodes, max_time)
            except (_BudgetExceeded, KeyError, ValueError, TypeError):
                evidence = None
            if evidence is not None:
                category = "brilliant"
                reason = "Проверенная жертва материала сохраняет сильное продолжение. Это эвристическая отметка тренажёра."
                result["brilliant_evidence"] = evidence
    ucis, sans = _line(infos[0], board)
    result.update(best_uci=best.uci(), best_san=board.san(best), category=category,
                  accuracy=round(move_accuracy(loss), 2), expected_loss=round(loss, 6),
                  best_expected=round(best_expected, 6), played_expected=round(played_expected, 6),
                  score_before=best_cp if best_mate is None else None,
                  score_after=played_cp if played_mate is None else None,
                  mate_before=best_mate, mate_after=played_mate,
                  best_line_uci=ucis, best_line_san=sans, depth=depth, assessed=True, reason=reason)
    return result


def summarize_moves(rows: list[dict], total_plies: int, starting_side: str = "white",
                    completed: bool = False) -> dict:
    """Return coverage and side summaries; never call a partial average final."""
    if total_plies < 0 or starting_side not in {"white", "black"}:
        raise ValueError("Некорректные параметры партии.")
    unique = {row["ply"]: row for row in rows if isinstance(row.get("ply"), int)
              and 0 <= row["ply"] < total_plies}
    result = {}
    for side in ("white", "black"):
        expected = (total_plies + (1 if side == starting_side else 0)) // 2
        plies = [ply for ply in range(total_plies)
                 if (starting_side if ply % 2 == 0 else ("black" if starting_side == "white" else "white")) == side]
        records = [unique[ply] for ply in plies if ply in unique]
        counts = {category: 0 for category in CATEGORY_NAMES}
        for row in records:
            category = row.get("category", "unassessed")
            counts[category if category in counts else "unassessed"] += 1
        assessed = sum(bool(row.get("assessed")) and row.get("category") in CATEGORY_NAMES
                       and row.get("category") != "unassessed" for row in records)
        scores = [float(row["accuracy"]) for row in records
                  if row.get("assessed") and row.get("category") not in {"forced", "unassessed"}
                  and isinstance(row.get("accuracy"), (int, float)) and math.isfinite(row["accuracy"])]
        provisional = round(sum(scores) / len(scores), 1) if scores else None
        complete = bool(completed and len(records) == expected)
        forced = sum(bool(row.get("assessed")) and row.get("category") == "forced" for row in records)
        accuracy_complete = complete and assessed == expected and len(scores) == assessed - forced
        result[side] = {"accuracy": provisional if accuracy_complete else None,
                        "provisional_accuracy": provisional, "counts": counts, "total_moves": expected,
                        "analysed_moves": len(records), "assessed_moves": assessed, "accuracy_moves": len(scores),
                        "coverage": round(100 * assessed / expected, 1) if expected else 100.0,
                        "complete": complete, "accuracy_complete": accuracy_complete}
    result["complete"] = result["white"]["complete"] and result["black"]["complete"]
    return result
