"""Evidence-based, deterministic coaching over a player's own assessed moves.

A motif describes a visible feature of the checked line, not the player's mental
cause. Rates are observation rates in comparable game windows, not skill ratings.
No language model, network calls or database mutations are needed here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math

import chess

from .generation import _move_themes

SKILLS = {
    "hanging_piece": {
        "title": "Безопасность фигур",
        "description": "Замечать незащищённые фигуры у обеих сторон и проверять взятия перед ходом.",
        "recommendation": "Перед ходом проверь, что соперник может взять. Затем проверь свои взятия и возможный ответ.",
        "practice_tip": "Назови доступные взятия обеих сторон, выбери ход и рассчитай хотя бы один ответ соперника.",
        "themes": ["hangingPiece", "trappedPiece", "capturingDefender"],
    },
    "fork": {
        "title": "Двойной удар",
        "description": "Находить ходы, создающие две угрозы одновременно.",
        "recommendation": "В позициях из своих партий ищи шахи и двойные нападения; после решения закрепи мотив на новой позиции.",
        "practice_tip": "Проверь ходы коня и ферзя: на какие две цели будет нападение и чем ответит соперник?",
        "themes": ["fork", "doubleCheck"],
    },
    "pin": {
        "title": "Связки и линии",
        "description": "Учитывать связанные фигуры и открывающиеся линии нападения.",
        "recommendation": "Проверь линии слонов, ладей и ферзя. Связанная фигура может не иметь права защитить другую цель.",
        "practice_tip": "Найди фигуру перед королём или более ценной фигурой и проверь, можно ли усилить давление.",
        "themes": ["pin", "skewer", "discoveredAttack", "xRayAttack"],
    },
    "mate": {
        "title": "Матовые возможности",
        "description": "Находить подтверждённые движком матовые продолжения.",
        "recommendation": "Начни с матов в один и два хода. Для каждого шаха проверяй взятие, закрытие и уход короля.",
        "practice_tip": "Перебери все шахи и убедись, что у короля не остаётся законного спасения.",
        "themes": ["mateIn1", "mateIn2", "mateIn3", "smotheredMate"],
    },
    "king_safety": {
        "title": "Безопасность короля",
        "description": "Замечать угрозы мата и не допускать решающей атаки на своего короля.",
        "recommendation": "Перед активным ходом проверь шахи соперника, последнюю горизонталь и поля отхода своего короля.",
        "practice_tip": "Сначала назови самый опасный шах соперника, затем выбери защиту.",
        "themes": ["defensiveMove"],
    },
    "defense": {
        "title": "Защита позиции",
        "description": "Сохранять равновесие или находить сопротивление в худшей позиции.",
        "recommendation": "Сначала определи угрозу. Сравни защиту, размен атакующей фигуры и встречную угрозу.",
        "practice_tip": "Найди безопасное продолжение и проверь самый сильный ответ соперника.",
        "themes": ["defensiveMove"],
    },
    "conversion": {
        "title": "Реализация перевеса",
        "description": "Сохранять достигнутое преимущество и доводить его до результата.",
        "recommendation": "В выигрышной позиции проверь контригру соперника, безопасность фигур и возможность безопасного упрощения.",
        "practice_tip": "Выбери продолжение, которое сохраняет перевес; лишний риск не требуется.",
        "themes": ["hangingPiece", "promotion", "advancedPawn"],
    },
    "opening": {
        "title": "Дебютные решения",
        "description": "Принимать надёжные решения в первых десяти ходах партии.",
        "recommendation": "Разбери ранние ошибки из своих партий. Проверяй угрозы, развивай фигуры и обеспечивай безопасность короля.",
        "practice_tip": "Перед развитием фигуры проверь взятия и шахи; объясни, какую задачу решает выбранный ход.",
        "themes": ["opening"],
    },
    "endgame": {
        "title": "Эндшпиль",
        "description": "Точно играть позиции с небольшим количеством материала.",
        "recommendation": "Отработай окончания, встречавшиеся в твоих партиях: активность короля, проходные пешки и безопасные размены.",
        "practice_tip": "Оцени активность королей и продвижение пешек, затем проверь конкретный вариант.",
        "themes": ["endgame", "pawnEndgame", "rookEndgame", "bishopEndgame", "knightEndgame", "queenEndgame", "queenRookEndgame"],
    },
    "time_management": {
        "title": "Управление временем",
        "description": "Сохранять запас времени и принимать надёжные решения в цейтноте.",
        "recommendation": "После партии проверь самые долгие решения и момент, когда осталось мало времени. Сначала решай точно, затем добавляй умеренный таймер.",
        "practice_tip": "Проверь шахи, взятия и угрозы. Выбери безопасный ход, не ускоряясь ценой проверки.",
        "themes": ["hangingPiece", "defensiveMove", "mateIn1"],
    },
    "calculation": {
        "title": "Расчёт вариантов",
        "description": "Сравнивать ходы-кандидаты и учитывать лучший ответ соперника.",
        "recommendation": "Рассмотри два хода-кандидата и лучший ответ соперника на каждый. После попытки сравни свой расчёт с проверенной линией.",
        "practice_tip": "Сначала назови угрозу, затем рассчитай свой ход, ответ соперника и следующий ход.",
        "themes": ["quietMove", "intermezzo", "deflection", "attraction"],
    },
}
_COMMON_OPPORTUNITIES = ["hanging_piece", "fork", "pin", "mate", "king_safety", "calculation"]
_KNOWN_RESULTS = {"win", "checkmated", "resigned", "timeout", "abandoned", "agreed", "repetition",
                  "stalemate", "insufficient", "50move", "timevsinsufficient", "lose"}


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _at(game, field, ply):
    values = game.get(field)
    if isinstance(values, list) and 0 <= ply < len(values):
        value = _number(values[ply])
        return value if value is not None and value >= 0 else None
    return None


def _own_side(game, username=None):
    username = username or game.get("username") or game.get("player_username")
    if isinstance(username, str):
        for side in ("white", "black"):
            if str(game.get(side, "")).lower() == username.lower():
                return side
        # An explicit unmatched username cannot silently become another player.
        return None
    return game.get("player_side") if game.get("player_side") in {"white", "black"} else None


def _phase(board):
    non_pawns = sum(p.piece_type not in {chess.KING, chess.PAWN} for p in board.piece_map().values())
    if len(board.piece_map()) <= 12 or (not board.queens and non_pawns <= 6):
        return "endgame"
    return "opening" if board.fullmove_number <= 10 else "middlegame"


def _loose_captures(board, owner):
    """Exact immediate legal captures with no legal immediate recapture.

    This does not evaluate compensation and is presented as a visible feature,
    never as proof that the capture wins the game or caused the engine's loss.
    """
    if board.turn == owner:
        return set()
    targets = set()
    for move in list(board.generate_legal_captures()):
        piece = board.piece_at(move.to_square)
        if piece is None or piece.color != owner or piece.piece_type in {chess.PAWN, chess.KING}:
            continue
        after = board.copy(stack=False)
        after.push(move)
        if not any(reply.to_square == move.to_square for reply in after.generate_legal_captures()):
            targets.add((move.to_square, piece.piece_type))
    return targets


def inspect_move(game: dict, row: dict, username: str | None = None) -> dict:
    """Annotate one reliable own move with contextual opportunities and evidence.

    Clock values are after-move seconds from the source. Unknown clocks remain
    unknown; time pressure is defined by <=30 seconds before making the move.
    """
    result = {"skill_ids": [], "opportunity_ids": [], "explanation": "", "phase": None,
              "clock_before": None, "clock_after": None, "spent_seconds": None,
              "time_pressure": None, "motifs": [], "eligible": False}
    ply = row.get("ply")
    if not isinstance(ply, int) or isinstance(ply, bool) or ply < 0:
        return result
    result["clock_before"] = _at(game, "clock_before", ply)
    result["clock_after"] = _at(game, "clocks", ply)
    result["spent_seconds"] = _at(game, "spent_seconds", ply)
    if result["spent_seconds"] is None:
        result["spent_seconds"] = _at(game, "move_seconds", ply)
    if result["clock_before"] is not None:
        result["time_pressure"] = result["clock_before"] <= 30
    try:
        board = chess.Board(row["before_fen"])
        if not board.is_valid():
            return result
    except (KeyError, ValueError, TypeError):
        return result
    result["phase"] = _phase(board)
    side = "white" if board.turn else "black"
    own_side = _own_side(game, username)
    # If no identity was supplied, the caller's own-only rows remain usable.
    has_identity = username is not None or any(game.get(k) for k in ("username", "player_username", "player_side"))
    if (has_identity and own_side != side) or row.get("side", side) != side:
        return result
    loss = _number(row.get("expected_loss"))
    if (not row.get("assessed") or row.get("category") in {"unassessed", "forced"}
            or loss is None or not 0 <= loss <= 1):
        return result
    result["eligible"] = True
    opportunities = list(_COMMON_OPPORTUNITIES)
    before = _number(row.get("score_before"))
    mate_before = _number(row.get("mate_before"))
    mate_after = _number(row.get("mate_after"))
    if board.is_check() or (before is not None and before <= 0) or (mate_before is not None and mate_before < 0):
        opportunities.append("defense")
    if (before is not None and before >= 200) or (mate_before is not None and mate_before > 0):
        opportunities.append("conversion")
    if result["phase"] in {"opening", "endgame"}:
        opportunities.append(result["phase"])
    result["opportunity_ids"] = opportunities
    if loss <= .03:
        return result
    try:
        best = chess.Move.from_uci(row["best_uci"])
        played = chess.Move.from_uci(row["played_uci"])
        if best not in board.legal_moves or played not in board.legal_moves or best == played:
            result.update(eligible=False, opportunity_ids=[])
            return result
    except (ValueError, KeyError, TypeError):
        result.update(eligible=False, opportunity_ids=[])
        return result

    motifs = set()
    line = row.get("best_line_uci") or [best.uci()]
    # Only inspect a legal, coherent prefix actually beginning with the best move.
    if isinstance(line, list) and line and line[0] == best.uci():
        current = board.copy(stack=False)
        for uci in line[:8]:
            try:
                move = chess.Move.from_uci(uci)
                if move not in current.legal_moves:
                    break
            except (ValueError, TypeError):
                break
            before_move = current.copy(stack=False)
            current.push(move)
            if before_move.turn == board.turn:
                motifs.update(_move_themes(before_move, move, current))

    skills, explanations = [], []
    if "hangingPiece" in motifs:
        skills.append("hanging_piece")
        explanations.append("В проверенном лучшем варианте есть взятие незащищённой фигуры или пешки.")
    best_board, played_board = board.copy(stack=False), board.copy(stack=False)
    best_board.push(best)
    played_board.push(played)
    loose = _loose_captures(played_board, board.turn) - _loose_captures(best_board, board.turn)
    if loose:
        skills.append("hanging_piece")
        squares = ", ".join(chess.square_name(sq) for sq, _ in sorted(loose))
        explanations.append(f"После сыгранного хода фигуру на {squares} можно взять без немедленного ответного взятия; проверь безопасность фигур.")
    if motifs & {"fork", "doubleCheck"}:
        skills.append("fork")
        explanations.append("В проверенной линии встречается двойное нападение или двойной шах.")
    if motifs & {"pin", "discoveredAttack"}:
        skills.append("pin")
        explanations.append("В лучшем варианте возникает связка или вскрытое нападение.")
    if mate_before is not None and mate_before > 0:
        skills.append("mate")
        explanations.append("Движок находил матовое продолжение, а сыгранный ход ухудшил оценку.")
    if mate_after is not None and mate_after < 0:
        skills.append("king_safety")
        explanations.append("После сыгранного хода движок находит мат против твоего короля.")
    # Phase and starting advantage describe where errors happened, not their cause.
    for contextual in ("defense", "conversion", "opening", "endgame"):
        if contextual in opportunities:
            skills.append(contextual)
    if not any(skill in skills for skill in ("hanging_piece", "fork", "pin", "mate", "king_safety")):
        skills.append("calculation")
        explanations.append("Сыгранное продолжение уступает проверенному варианту. Сравни ходы-кандидаты и ответы соперника.")
    if result["time_pressure"] is True:
        skills.append("time_management")
        explanations.append("Перед этим ходом оставалось не больше 30 секунд; это обстоятельство ошибки, а не доказательство её причины.")
    result.update(skill_ids=list(dict.fromkeys(skills)), motifs=sorted(motifs), explanation=" ".join(explanations))
    return result


def _stamp(game):
    data = game.get("data", {})
    value = _number(data.get("end_time"))
    if value is not None:
        return value
    value = data.get("source_game_at") or data.get("date")
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace(".", "-", 2) if "." in value[:10] else value.replace("Z", "+00:00"))
            return dt.replace(tzinfo=timezone.utc).timestamp() if dt.tzinfo is None else dt.timestamp()
        except (ValueError, OverflowError):
            pass
    return 0


def _own_move_total(data):
    try:
        initial = chess.Board(data.get("initial_fen", chess.STARTING_FEN)).turn
        side = _own_side(data)
        if side is None and any(data.get(k) for k in ("username", "player_username", "player_side")):
            return 0
        starts = side is None or (side == "white") == initial
        total = len(data.get("moves", []))
        return (total + int(starts)) // 2
    except (ValueError, TypeError):
        return 0


def _result(data):
    value = data.get("player_result")
    if not value:
        side = _own_side(data)
        value = data.get(f"{side}_result") if side else None
    return value if value in _KNOWN_RESULTS else None


def _bucket():
    return {"games": 0, "opportunities": 0, "errors": 0, "rate": None,
            "pressure_errors": 0, "clock_moves": 0, "severity": 0.0}


def build_insights(games: list[dict], attempts: list[dict]) -> dict:
    """Summarize fixed baseline and newest 40 completed post-start games.

    Games still being assessed do not enter comparisons. A meaningful trend
    requires >=10 games and >=50 relevant assessed moves in each window (for
    clocks, >=10 known game outcomes), with >=90% own-move assessment coverage.
    """
    unique = {}
    for game in sorted(games, key=lambda g: g.get("cohort") != "baseline"):
        data = game.get("data") or {}
        identity = game.get("fingerprint") or data.get("game_url") or game.get("id")
        if identity is None:
            continue
        previous = unique.get(identity)
        if previous is None or (previous.get("status") != "completed" and game.get("status") == "completed"):
            unique[identity] = game
    all_games = list(unique.values())
    windows = {cohort: sorted([g for g in all_games if g.get("cohort") == cohort and g.get("status") == "completed"],
                              key=_stamp, reverse=True)[:40] for cohort in ("baseline", "followup")}
    measures = {key: {"baseline": _bucket(), "recent": _bucket()} for key in SKILLS}
    evidence = {key: [] for key in SKILLS}
    totals = {}
    controls = set()
    for cohort, window in windows.items():
        label = "baseline" if cohort == "baseline" else "recent"
        own_moves = assessed = eligible_moves = clock_moves = 0
        for game in window:
            data = game.get("data") or {}
            if data.get("time_control"):
                controls.add(data["time_control"])
            own_moves += _own_move_total(data)
            touched = set()
            seen_plies = set()
            for row in game.get("moves") or []:
                ply = row.get("ply")
                if not isinstance(ply, int) or isinstance(ply, bool) or ply in seen_plies:
                    continue
                seen_plies.add(ply)
                if not 0 <= ply < len(data.get("moves", [])):
                    continue
                annotation = inspect_move(data, row, data.get("username"))
                side = _own_side(data)
                if annotation["phase"] is None:
                    continue
                actual_side = "white" if chess.Board(row["before_fen"]).turn else "black"
                if (side is not None and (actual_side != side or row.get("side") not in (None, side))
                        or side is None and any(data.get(k) for k in ("username", "player_username", "player_side"))):
                    continue
                if row.get("assessed") and row.get("category") != "unassessed":
                    assessed += 1
                if not annotation["eligible"]:
                    continue
                eligible_moves += 1
                if annotation["clock_before"] is not None:
                    clock_moves += 1
                for key in annotation["opportunity_ids"]:
                    measures[key][label]["opportunities"] += 1
                    touched.add(key)
                for key in annotation["skill_ids"]:
                    bucket = measures[key][label]
                    if key == "time_management":
                        bucket["pressure_errors"] += 1
                    else:
                        bucket["errors"] += 1
                        bucket["severity"] += float(row.get("expected_loss") or 0)
                    if sum(e["cohort"] == cohort for e in evidence[key]) < 5:
                        evidence[key].append({"game_id": game.get("id"), "ply": ply,
                            "move_number": row.get("move_number"), "played_san": row.get("played_san"),
                            "best_san": row.get("best_san"), "explanation": annotation["explanation"],
                            "game_url": data.get("game_url", ""), "phase": annotation["phase"],
                            "cohort": cohort, "clock_before": annotation["clock_before"],
                            "spent_seconds": annotation["spent_seconds"]})
            for key in touched:
                measures[key][label]["games"] += 1
            result = _result(data)
            if result is not None:
                clock = measures["time_management"][label]
                clock["games"] += 1
                clock["opportunities"] += 1
                clock["errors"] += int(result == "timeout")
                clock["severity"] += int(result == "timeout")
                if result == "timeout" and sum(e["cohort"] == cohort for e in evidence["time_management"]) < 5:
                    evidence["time_management"].append({"game_id": game.get("id"), "ply": None,
                        "move_number": None, "played_san": None, "best_san": None,
                        "explanation": "Партия завершилась поражением по времени. Оценку последней позиции нужно рассмотреть отдельно.",
                        "game_url": data.get("game_url", ""), "phase": None, "cohort": cohort,
                        "clock_before": None, "spent_seconds": None})
        totals[label] = {"games": len(window), "own_moves": own_moves, "assessed_moves": assessed,
                         "eligible_moves": eligible_moves, "clock_moves": clock_moves,
                         "coverage": round(min(100, 100 * assessed / own_moves), 1) if own_moves else 0,
                         "clock_coverage": round(100 * clock_moves / eligible_moves, 1) if eligible_moves else None}
        measures["time_management"][label]["clock_moves"] = clock_moves

    skills = []
    for key, definition in SKILLS.items():
        baseline, recent = measures[key]["baseline"], measures[key]["recent"]
        for bucket in (baseline, recent):
            bucket["rate"] = round(100 * bucket["errors"] / bucket["opportunities"], 1) if bucket["opportunities"] else None
            bucket["severity"] = round(bucket["severity"], 4)
        minimum = 10 if key == "time_management" else 50
        comparable = (len(controls) <= 1 and all(b["games"] >= 10 and b["opportunities"] >= minimum for b in (baseline, recent))
                      and all(totals[label]["coverage"] >= 90 for label in ("baseline", "recent")))
        delta = round(recent["rate"] - baseline["rate"], 1) if comparable else None
        errors = baseline["errors"] + recent["errors"]
        signals = errors + (baseline["pressure_errors"] + recent["pressure_errors"] if key == "time_management" else 0)
        enough = any(b["games"] >= 3 and b["opportunities"] >= (3 if key == "time_management" else 20) for b in (baseline, recent))
        if not enough or not signals:
            status = "insufficient"
        elif comparable and recent["errors"] == 0 and recent["games"] >= 20 and not recent["pressure_errors"]:
            status = "monitor"
        elif comparable and delta is not None and delta <= -max(1.0, baseline["rate"] * .25):
            status = "reinforce"
        else:
            status = "focus"
        current = recent if recent["opportunities"] >= minimum and recent["games"] >= 10 else baseline
        priority = min(100, round((current["rate"] or 0) * 1.5 + min(current["errors"], 10) * 2
                                  + min(current["severity"], 5) * 3 + min(current["pressure_errors"], 5) * 2, 1))
        if not signals:
            priority = 0
        if status == "reinforce":
            priority = max(priority, min(15, round((baseline["rate"] or 0) * .3 + 3, 1)))
        elif status == "monitor":
            priority = min(priority, 3)
        if status == "reinforce":
            change = "поражений по времени" if key == "time_management" else "ошибок"
            recommendation = f"В новых партиях частота {change} снизилась. Закрепи навык на новых позициях и продолжай проверять его в игре. " + definition["recommendation"]
        elif status == "monitor":
            absent = "поражений по времени не было" if key == "time_management" else "ошибки по этой теме не обнаружены"
            recommendation = f"В сопоставимой новой выборке {absent}. Сохрани редкие повторения и наблюдение в следующих партиях."
        elif not signals:
            recommendation = "В выборке пока нет подтверждённых ошибок по этой теме. Это не оценка полного владения навыком. " + definition["recommendation"]
        elif not enough:
            recommendation = "Пока мало примеров для устойчивого вывода. Разбери найденные позиции и продолжай собирать партии. " + definition["recommendation"]
        else:
            recommendation = definition["recommendation"]
        matching = [a for a in attempts if a.get("skill_id") == key and a.get("status") in {"completed", "revealed"}]
        clean = [a for a in matching if a.get("status") == "completed" and not a.get("mistakes") and not a.get("hints_used")]
        metric_label = ("Поражений по времени на 100 партий с известной причиной результата" if key == "time_management" else
                        "Ошибок на 100 оценённых ходов " + {"opening": "в дебюте", "endgame": "в эндшпиле", "defense": "при защите", "conversion": "с перевесом"}.get(key, "игрока"))
        # Prefer post-start examples; every example has the source position.
        examples = sorted(evidence[key], key=lambda e: e["cohort"] != "followup")[:5]
        skills.append({"id": key, **definition, "recommendation": recommendation,
                       "priority": priority, "status": status, "baseline": baseline, "recent": recent,
                       "delta": delta, "comparable": comparable, "metric_label": metric_label,
                       "evidence": examples, "training": {"completed": sum(a.get("status") == "completed" for a in matching),
                        "clean": len(clean), "new_clean": sum(a.get("is_new") is True for a in clean)}})
    skills.sort(key=lambda item: (-item["priority"], list(SKILLS).index(item["id"])))
    priorities = [s["id"] for s in skills if s["priority"] > 0][:3]
    return {"skills": skills, "priorities": priorities,
        "summary": {"baseline": totals["baseline"], "recent": totals["recent"],
                    "baseline_games": len(windows["baseline"]), "recent_games": len(windows["followup"]),
                    "tracked_games": len(all_games), "completed_games": sum(g.get("status") == "completed" for g in all_games),
                    "pending_games": sum(g.get("status") != "completed" for g in all_games),
                    "assessed_moves": sum(t["assessed_moves"] for t in totals.values()),
                    "own_moves": sum(t["own_moves"] for t in totals.values()), "window_size": 40,
                    "comparable_control": len(controls) <= 1},
        "methodology": "Стартовая выборка фиксирована: до 40 последних партий выбранного контроля. Новая выборка — до 40 последних полностью разобранных партий после запуска. Частоты считаются только по оценённым невынужденным ходам игрока; дебют, эндшпиль, защита и перевес имеют отдельные знаменатели. Темы могут пересекаться, их числа не суммируются. Для времени считаются поражения по времени среди партий с известной причиной результата. Тенденция показывается после 10 партий в каждой выборке, достаточного числа ходов и покрытия анализа не ниже 90%. Решение упражнений само по себе не доказывает улучшение игры. Фазы и мотивы определяются консервативными правилами; причина мыслительной ошибки не устанавливается."}
