"""Bounded reads of the documented public Chess.com API; no caller-supplied URLs."""
import json
import io
import math
from datetime import datetime, timezone
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fastapi import HTTPException

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_CANDIDATE_GAMES = 200
API_ROOT = "https://api.chess.com/pub/player/"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def safe_game_url(value: str) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == "https" and parsed.hostname in {"lichess.org", "chess.com", "www.chess.com"}
                and parsed.username is None and parsed.password is None and parsed.port is None):
            return value[:1000]
    except ValueError:
        pass
    return ""



def source_game_time(game: dict) -> datetime | None:
    """Use real completion time, falling back to PGN timestamps for older imports."""
    value = game.get("source_game_at")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            pass
    stamp = game.get("end_time")
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and math.isfinite(stamp):
        try:
            return datetime.fromtimestamp(stamp, timezone.utc)
        except (ValueError, OverflowError, OSError):
            pass
    headers = {}
    if isinstance(game.get("pgn"), str):
        import chess.pgn
        try:
            headers = chess.pgn.read_headers(io.StringIO(game["pgn"])) or {}
        except (ValueError, TypeError):
            pass
    dates = [(headers.get("EndDate"), headers.get("EndTime")),
             (headers.get("UTCDate"), headers.get("UTCTime")),
             (headers.get("Date") or game.get("date"), headers.get("StartTime"))]
    for date, clock in dates:
        if not isinstance(date, str):
            continue
        try:
            parsed = datetime.strptime(date + " " + (clock[:8] if isinstance(clock, str) else "00:00:00"), "%Y.%m.%d %H:%M:%S")
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def fetch_json(path: str) -> dict:
    request = Request(API_ROOT + path, headers={
        "Accept": "application/json",
        "User-Agent": "TacticsLab/1.0 (+https://github.com/kimoleg200301/chess-trainer)",
    })
    deadline = time.monotonic() + 12
    try:
        with build_opener(NoRedirect()).open(request, timeout=8) as response:
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
                raise HTTPException(413, "Архив Chess.com слишком большой. Выберите другой месяц.")
            chunks, size = [], 0
            while True:
                chunk = response.read1(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise HTTPException(413, "Архив Chess.com слишком большой. Выберите другой месяц.")
                if time.monotonic() > deadline:
                    raise HTTPException(504, "Chess.com отвечает слишком долго. Повторите импорт позже.")
                chunks.append(chunk)
        data = json.loads(b"".join(chunks))
        if not isinstance(data, dict):
            raise ValueError("Expected object")
        return data
    except HTTPError as exc:
        if exc.code == 404:
            raise HTTPException(404, "Игрок или архив Chess.com не найден. Проверьте ник и месяц.") from None
        if exc.code == 429:
            raise HTTPException(429, "Chess.com ограничил частоту запросов. Повторите импорт позже.") from None
        raise HTTPException(502, "Не удалось загрузить архив Chess.com. Повторите позже.") from None
    except (URLError, TimeoutError, OSError):
        raise HTTPException(502, "Chess.com сейчас недоступен. Повторите импорт позже.") from None
    except (ValueError, UnicodeError):
        raise HTTPException(502, "Chess.com вернул некорректный архив.") from None


def chesscom_accuracies(value) -> dict[str, float] | None:
    """Validate optional published reference scores without deriving missing ones."""
    if not isinstance(value, dict):
        return None
    scores = {side: float(score) for side in ("white", "black")
              if isinstance(score := value.get(side), (int, float)) and not isinstance(score, bool)
              and 0 <= score <= 100 and math.isfinite(score)}
    return scores or None


def chesscom_game_url(value: str | None) -> str | None:
    """Canonical identity for cosmetic differences in a public game URL."""
    value = safe_game_url(value)
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.hostname not in {"chess.com", "www.chess.com"}:
        return None
    return urlunsplit(("https", "www.chess.com", parsed.path.rstrip("/"), "", ""))


def chesscom_metadata(game: dict, item: dict, username: str) -> dict:
    """Attach public game metadata without fabricating unavailable observations."""
    username = username.lower()
    result = dict(game)
    result.update(username=username,
                  game_url=safe_game_url(item.get("url", "")) or safe_game_url(game.get("game_url", "")),
                  end_time=item.get("end_time"),
                  time_class=item.get("time_class") if isinstance(item.get("time_class"), str) else None,
                  rated=item.get("rated") if isinstance(item.get("rated"), bool) else None,
                  chesscom_accuracies=chesscom_accuracies(item.get("accuracies")))
    for side in ("white", "black"):
        player = item.get(side) if isinstance(item.get(side), dict) else {}
        rating = player.get("rating")
        result[side + "_rating"] = rating if isinstance(rating, int) and not isinstance(rating, bool) and 0 <= rating <= 10000 else None
        outcome = player.get("result")
        result[side + "_result"] = outcome[:40] if isinstance(outcome, str) else None
    side = "white" if result["white"].lower() == username else "black"
    opponent = "black" if side == "white" else "white"
    result.update(player_side=side, player_result=result[side + "_result"],
                  player_rating=result[side + "_rating"], opponent=result[opponent],
                  opponent_rating=result[opponent + "_rating"])
    stamp = source_game_time(result)
    result["source_game_at"] = stamp.isoformat() if stamp else None
    opening_url = result.get("opening_url", "")
    if not opening_url.startswith("https://www.chess.com/openings/"):
        result["opening_url"] = ""
    elif not result.get("opening"):
        result["opening"] = urlsplit(opening_url).path.rsplit("/", 1)[-1].replace("-", " ")[:250]
    return result


def chesscom_games(username: str, month: str | None, limit: int, *, allow_empty: bool = False) -> tuple[str, list[dict]]:
    """Return the latest selected completed standard games, without parsing unrelated PGNs."""
    from .generation import parse_games
    username = username.lower()
    if not month:
        archives = fetch_json(f"{username}/games/archives").get("archives", [])
        if not isinstance(archives, list):
            raise HTTPException(502, "Chess.com вернул некорректный список архивов.")
        pattern = re.compile(r"https://api\.chess\.com/pub/player/" + re.escape(username) + r"/games/(\d{4})/(0[1-9]|1[0-2])", re.I)
        months = [f"{match[1]}-{match[2]}" for item in archives if isinstance(item, str) and (match := pattern.fullmatch(item))]
        if not months:
            raise HTTPException(404, "У этого игрока пока нет доступных архивов партий.")
        month = max(months)
    data = fetch_json(f"{username}/games/{month.replace('-', '/')}")
    raw_games = data.get("games", [])
    if not isinstance(raw_games, list):
        raise HTTPException(502, "Chess.com вернул некорректный архив партий.")
    eligible = [game for game in raw_games if isinstance(game, dict) and game.get("rules") == "chess"
                and isinstance(game.get("pgn"), str) and isinstance(game.get("end_time"), (int, float))
                and not isinstance(game["end_time"], bool) and 0 <= game["end_time"] <= 253402300799]
    eligible.sort(key=lambda game: game["end_time"], reverse=True)
    games = []
    for item in eligible[:MAX_CANDIDATE_GAMES]:
        try:
            parsed = parse_games(item["pgn"], max_games=1, max_plies=400,
                                 time_control_override=item.get("time_control") if isinstance(item.get("time_control"), str) else None)
        except ValueError:
            continue
        if not parsed:
            continue
        game = parsed[0]
        if username not in {game["white"].lower(), game["black"].lower()}:
            continue
        games.append(chesscom_metadata(game, item, username))
        if len(games) >= limit:
            break
    if not games and not allow_empty:
        raise HTTPException(404, "В этом месяце нет подходящих завершённых партий в обычные шахматы.")
    return month, games
