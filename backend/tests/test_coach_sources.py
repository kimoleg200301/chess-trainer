"""Exact-control import and observable clocks, independent of PostgreSQL."""
from copy import deepcopy

from fastapi import HTTPException
import pytest

from app import coach_sources
from app.generation import parse_games


@pytest.fixture(scope="session", autouse=True)
def database():
    yield


@pytest.fixture(autouse=True)
def clean_training():
    yield


PGN = '''[Event "Clock fixture"]
[Site "https://www.chess.com/game/live/123"]
[Date "2025.01.02"]
[White "Tester"]
[Black "Opponent"]
[Result "0-1"]
[TimeControl "300+5"]
[ECO "A00"]
[ECOUrl "https://www.chess.com/openings/Barnes-Opening"]

1. f3 {[%clk 0:04:59.2]} e5 {[%clk 0:05:03]} 2. g4 {[%clk 0:04:40.1]} Qh4# {[%clk 0:05:06]} 0-1
'''


def raw_game(identifier=123, end_time=1735833600, **changes):
    return {"rules": "chess", "rated": True, "time_control": "300+5", "time_class": "blitz",
            "url": f"https://www.chess.com/game/live/{identifier}", "end_time": end_time,
            "pgn": PGN.replace("/123", f"/{identifier}"),
            "white": {"username": "Tester", "rating": 270, "result": "checkmated"},
            "black": {"username": "Opponent", "rating": 310, "result": "win"}, **changes}


def test_clock_metadata_preserves_observations_and_corrects_for_increment():
    game = parse_games(PGN)[0]
    assert game["clocks"] == [299.2, 303, 280.1, 306]
    assert game["clock_before"] == [300, 300, 299.2, 303]
    assert game["spent_seconds"] == [5.8, 2, 24.1, 2]
    assert game["move_seconds"] == game["spent_seconds"]
    assert game["base_seconds"] == 300 and game["increment_seconds"] == 5
    assert game["time_control"] == "300+5"
    assert "[%clk 0:04:59.2]" in game["pgn"]
    assert game["eco"] == "A00"


def test_missing_clock_breaks_duration_inference_for_only_that_side():
    game = parse_games(PGN.replace("{[%clk 0:04:59.2]}", ""))[0]
    assert game["clocks"] == [None, 303, 280.1, 306]
    assert game["clock_before"] == [300, 300, None, 303]
    assert game["spent_seconds"] == [None, 2, None, 2]


def test_unknown_or_multi_stage_time_controls_do_not_fabricate_spent_time():
    for control in ("?", "40/7200:3600", "1/86400"):
        game = parse_games(PGN.replace('"300+5"', f'"{control}"'))[0]
        assert game["clocks"] == [299.2, 303, 280.1, 306]
        assert game["spent_seconds"] == [None] * 4
        assert game["base_seconds"] is None


def test_impossible_clock_increase_is_unknown_not_zero_spent_time():
    game = parse_games(PGN.replace("0:04:40.1", "0:08:40.1"))[0]
    assert game["clocks"][2] == 520.1
    assert game["spent_seconds"][2] is None


def test_api_time_control_override_is_used_for_clocks_and_saved_pgn():
    game = parse_games(PGN.replace('"300+5"', '"300"'), time_control_override="300+5")[0]
    assert game["spent_seconds"] == [5.8, 2, 24.1, 2]
    assert '[TimeControl "300+5"]' in game["pgn"]


def test_archives_use_fixed_host_player_valid_months_unique_and_descending(monkeypatch):
    paths = []

    def fetch(path):
        paths.append(path)
        return {"archives": [
            "https://api.chess.com/pub/player/Tester/games/2025/01",
            "https://api.chess.com/pub/player/tester/games/2025/02",
            "https://api.chess.com/pub/player/tester/games/2025/02",
            "https://api.chess.com/pub/player/tester/games/2025/13",
            "https://api.chess.com/pub/player/other/games/2025/03",
            "https://api.chess.com.evil/pub/player/tester/games/2025/04",
            "https://api.chess.com/pub/player/tester/games/2099/01",
            "https://api.chess.com/pub/player/tester/games/2006/01",
            "http://localhost/private", None,
        ]}

    monkeypatch.setattr(coach_sources, "fetch_json", fetch)
    assert coach_sources.archive_months("Tester") == ["2025-02", "2025-01"]
    assert paths == ["tester/games/archives"]


def test_empty_archives_and_valid_empty_month_are_not_errors(monkeypatch):
    monkeypatch.setattr(coach_sources, "fetch_json", lambda path: {"archives": [], "games": []})
    assert coach_sources.archive_months("Tester") == []
    assert coach_sources.month_games("Tester", "2025-01", "300+5") == []


def test_month_selects_exact_control_standard_completed_and_owner(monkeypatch):
    valid = raw_game()
    bad_identity = raw_game(10)
    bad_identity["white"]["username"] = "SomebodyElse"
    bad_outcome = raw_game(11)
    bad_outcome["white"]["result"] = "win"
    records = [valid, raw_game(1, time_control="300"), raw_game(2, time_control="180+2"),
               raw_game(4, rules="chess960"),
               raw_game(5, pgn=PGN.replace('"0-1"', '"*"').replace("0-1\n", "*\n")),
               raw_game(6, pgn=PGN.replace("Tester", "OtherPlayer")),
               raw_game(7, end_time=float("nan")), raw_game(8, end_time=True),
               raw_game(9, pgn=PGN.replace("f3", "f5")), bad_identity, bad_outcome,
               raw_game(12, end_time=float("inf")), raw_game(13, end_time=10 ** 400), None]
    monkeypatch.setattr(coach_sources, "fetch_json", lambda path: {"games": records})
    games = coach_sources.month_games("Tester", "2025-01", "300+5")
    assert len(games) == 1 and games[0]["game_url"].endswith("/123")
    game = games[0]
    assert game["username"] == "tester" and game["player_side"] == "white"
    assert game["player_result"] == "checkmated" and game["player_rating"] == 270
    assert game["opponent"] == "Opponent" and game["opponent_rating"] == 310
    assert game["white_result"] == "checkmated" and game["black_result"] == "win"
    assert game["source_game_at"] == "2025-01-02T16:00:00+00:00"
    assert game["opening"] == "Barnes Opening" and game["eco"] == "A00"


def test_rated_and_unrated_games_both_belong_to_the_exact_control(monkeypatch):
    records = [raw_game(1, 1000), raw_game(2, 2000, rated=False)]
    monkeypatch.setattr(coach_sources, "fetch_json", lambda path: {"games": records})
    games = coach_sources.month_games("Tester", "2025-01", "300+5")
    assert [game["rated"] for game in games] == [False, True]
    assert [game["end_time"] for game in games] == [2000, 1000]


def test_filters_before_considering_all_games_and_keeps_latest_beyond_200(monkeypatch):
    # A dense month of other controls must not hide this player's 5+5 games.
    records = [raw_game(index, end_time=5000 - index, time_control="180+2") for index in range(205)]
    records.extend([raw_game(999, end_time=1000), raw_game(998, end_time=2000)])
    monkeypatch.setattr(coach_sources, "fetch_json", lambda path: {"games": records})
    assert [game["end_time"] for game in coach_sources.month_games("Tester", "2025-01", "300+5")] == [2000, 1000]
    # No implicit first-200 ceiling even when every game has the right control.
    monkeypatch.setattr(coach_sources, "fetch_json", lambda path: {"games": [raw_game(index, 3000 - index) for index in range(205)]})
    assert len(coach_sources.month_games("Tester", "2025-01", "300+5")) == 205


def test_duplicate_urls_are_canonicalized_without_collapsing_distinct_games(monkeypatch):
    first = raw_game(1, 2000)
    duplicate = raw_game(1, 2000, url="https://chess.com/game/live/1/?utm_source=test#moves")
    second = raw_game(2, 1000)
    # Same players and move sequence can occur in two genuine games.
    monkeypatch.setattr(coach_sources, "fetch_json", lambda path: {"games": [second, duplicate, first]})
    games = coach_sources.month_games("Tester", "2025-01", "300+5")
    assert [game["game_url"] for game in games] == ["https://www.chess.com/game/live/1", "https://www.chess.com/game/live/2"]


def test_duplicate_without_safe_url_falls_back_to_game_identity(monkeypatch):
    game = raw_game(url="https://evil.invalid/private", pgn=PGN.replace("https://www.chess.com/game/live/123", "Chess.com"))
    monkeypatch.setattr(coach_sources, "fetch_json", lambda path: {"games": [game, deepcopy(game)]})
    games = coach_sources.month_games("Tester", "2025-01", "300+5")
    assert len(games) == 1 and games[0]["game_url"] == ""


def test_own_black_side_and_missing_ratings_are_explicit(monkeypatch):
    raw = raw_game()
    raw["white"].pop("rating")
    raw["black"]["rating"] = None
    monkeypatch.setattr(coach_sources, "fetch_json", lambda path: {"games": [raw]})
    game = coach_sources.month_games("Opponent", "2025-01", "300+5")[0]
    assert game["player_side"] == "black" and game["player_result"] == "win"
    assert game["player_rating"] is None and game["opponent_rating"] is None


def test_source_does_not_fetch_caller_supplied_paths(monkeypatch):
    calls = []
    monkeypatch.setattr(coach_sources, "fetch_json", lambda path: calls.append(path))
    for username in ("../other", "tester/games", "", None):
        with pytest.raises(HTTPException) as exc:
            coach_sources.archive_months(username)
        assert exc.value.status_code == 422
    for month in ("../archives", "2025-13", "2001-01", "2099-01"):
        with pytest.raises(HTTPException):
            coach_sources.month_games("Tester", month, "300+5")
    for control in ("1/86400", "300?query", "", None):
        with pytest.raises(HTTPException):
            coach_sources.month_games("Tester", "2025-01", control)
    assert not calls


@pytest.mark.parametrize("response", [{}, {"archives": {}}, {"archives": None}])
def test_invalid_archives_fail_instead_of_misreporting_no_games(monkeypatch, response):
    monkeypatch.setattr(coach_sources, "fetch_json", lambda path: response)
    with pytest.raises(HTTPException) as exc:
        coach_sources.archive_months("tester")
    assert exc.value.status_code == 502


@pytest.mark.parametrize("response", [{}, {"games": {}}, {"games": None}])
def test_invalid_month_fails_instead_of_advancing_import_cursor(monkeypatch, response):
    monkeypatch.setattr(coach_sources, "fetch_json", lambda path: response)
    with pytest.raises(HTTPException) as exc:
        coach_sources.month_games("tester", "2025-01", "300+5")
    assert exc.value.status_code == 502
