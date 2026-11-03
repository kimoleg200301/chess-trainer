"""One bounded Chess.com archive read at a time for the durable coach worker.

Selection across months and the 40-game baseline cutoff belong to the worker.
This module must not silently stop at the first 200 mixed-control games.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException

from .game_sources import chesscom_metadata, fetch_json
from .generation import parse_games


def _username(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value):
        raise HTTPException(422, "Некорректный ник Chess.com.")
    return value.lower()


def _month(value: str) -> str:
    if (not isinstance(value, str) or not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", value)
            or value < "2007-01" or value > datetime.now(timezone.utc).strftime("%Y-%m")):
        raise HTTPException(422, "Некорректный месяц архива Chess.com.")
    return value


def archive_months(username: str) -> list[str]:
    username = _username(username)
    raw = fetch_json(f"{username}/games/archives").get("archives")
    if not isinstance(raw, list):
        raise HTTPException(502, "Chess.com вернул некорректный список архивов.")
    pattern = re.compile(r"https://api\.chess\.com/pub/player/" + re.escape(username)
                         + r"/games/(20\d{2})/(0[1-9]|1[0-2])", re.I)
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    months = set()
    for value in raw:
        match = pattern.fullmatch(value) if isinstance(value, str) else None
        if match:
            month = f"{match[1]}-{match[2]}"
            if "2007-01" <= month <= current_month:
                months.add(month)
    return sorted(months, reverse=True)


def month_games(username: str, month: str, time_control: str) -> list[dict]:
    """All playable standard games of the *exact* control, newest first.

    The upstream JSON and each PGN have byte limits. Games with corrupt records
    are skipped; an invalid archive response raises a retriable upstream error.
    """
    username, month = _username(username), _month(month)
    if not isinstance(time_control, str) or not re.fullmatch(r"[1-9]\d{0,5}(?:\+\d{1,4})?", time_control):
        raise HTTPException(422, "Нужен контроль времени в секундах, например 300+5.")
    raw = fetch_json(f"{username}/games/{month.replace('-', '/')}").get("games")
    if not isinstance(raw, list):
        raise HTTPException(502, "Chess.com вернул некорректный архив партий.")
    eligible = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        stamp = item.get("end_time")
        if (item.get("rules") != "chess"
                or item.get("time_control") != time_control or not isinstance(item.get("pgn"), str)
                or not isinstance(stamp, (int, float)) or isinstance(stamp, bool)
                or not 0 <= stamp <= 253402300799 or not math.isfinite(stamp)):
            continue
        eligible.append(item)
    eligible.sort(key=lambda item: item["end_time"], reverse=True)
    games, seen = [], set()
    for item in eligible:
        try:
            game = parse_games(item["pgn"], max_games=1, max_plies=1000,
                               time_control_override=time_control)[0]
        except (ValueError, TypeError, IndexError):
            continue
        if username not in {game["white"].lower(), game["black"].lower()}:
            continue
        # A mismatched API/PGN identity must not attribute a move to the wrong side.
        if any(not isinstance(item.get(side), dict)
               or not isinstance(item[side].get("username"), str)
               or item[side]["username"].lower() != game[side].lower()
               or not isinstance(item[side].get("result"), str)
               or item[side]["result"] in {"", "*"}
               for side in ("white", "black")):
            continue
        outcome = game["result"]
        if ((item["white"]["result"] == "win") != (outcome == "1-0")
                or (item["black"]["result"] == "win") != (outcome == "0-1")):
            continue
        game = chesscom_metadata(game, item, username)
        url = game["game_url"]
        if url:
            parsed_url = urlsplit(url)
            # Query/fragment and the optional www prefix do not identify a new game.
            host = "www.chess.com" if parsed_url.hostname == "chess.com" else parsed_url.hostname
            url = urlunsplit(("https", host, parsed_url.path.rstrip("/"), "", ""))
            game["game_url"] = url
        identity = [url] if url else [game["white"].lower(), game["black"].lower(),
                                      game["initial_fen"], game["moves"], game["end_time"]]
        key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        games.append(game)
    return games
