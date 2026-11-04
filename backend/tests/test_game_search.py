"""Nickname search scopes the owned archive before the recent-game limit."""
from test_background_analysis import controlled
from test_game_analysis import PGN, imported_game
from test_game_favorites import favorite


def search(client, q, **params):
    response = client.get("/api/game-analysis/games", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return response.json()["games"]


def test_partial_case_insensitive_search_both_sides_and_literal_wildcards(controlled):
    from app.generation import parse_games
    game = parse_games(PGN)[0]
    normal, _, _ = imported_game()
    special, _, _ = imported_game(game={**game, "white": "Player_19", "black": "Win%Rate"})
    assert [g["id"] for g in search(controlled, "  rEvIeW  ")] == [normal]
    assert [g["id"] for g in search(controlled, "pPoNe")] == [normal]
    for query in ("_", "%", "PLAYER_", "win%"):
        assert [g["id"] for g in search(controlled, query)] == [special]
    assert search(controlled, "not-a-player") == []
    assert {g["id"] for g in search(controlled, "  ")} == {normal, special}
    assert controlled.get("/api/game-analysis/games", params={"q": "a" * 81}).status_code == 422


def test_search_finds_old_games_deduplicates_and_filters_old_favorites(controlled, monkeypatch):
    from app import game_analysis_api
    from app.generation import parse_games
    monkeypatch.setattr(game_analysis_api, "MAX_LIST_GAMES", 2)
    game = parse_games(PGN)[0]
    old = [imported_game(game={**game, "date": f"2025.0{month}.01", "source_game_at": f"2025-0{month}-01T12:00:00Z"})[0] for month in (1, 2, 3)]
    recent = [imported_game(game={**game, "white": "Recent", "black": "New", "date": f"2025.0{month}.01", "source_game_at": f"2025-0{month}-01T12:00:00Z"})[0] for month in (4, 5)]
    assert [g["id"] for g in search(controlled, "")] == recent[::-1]
    assert [g["id"] for g in search(controlled, "viewer")] == old[:0:-1]
    favorite(controlled, old[0]); favorite(controlled, recent[0])
    assert [g["id"] for g in search(controlled, "viewer")] == old[::-1]
    assert [g["id"] for g in search(controlled, "new")] == recent[::-1]
    newest, _, _ = imported_game(game={**game, "date": "2025.01.01", "source_game_at": "2025-01-01T12:00:00Z"})
    assert [g["id"] for g in search(controlled, "viewer")] == [old[2], old[1], newest]
    # A direct review URL stays available even with a different search in its URL.
    assert [g["id"] for g in search(controlled, "absent", selected_game_id=old[0])] == [old[0]]


def test_search_never_exposes_other_accounts_or_public_imports(controlled):
    mine, _, _ = imported_game()
    other, _, _ = imported_game(username="search-other")
    public, _, _ = imported_game(source="public")
    for selected in (other, public):
        assert [g["id"] for g in search(controlled, "review", selected_game_id=selected)] == [mine]
        assert search(controlled, "absent", selected_game_id=selected) == []
    controlled.post("/api/auth/logout")
    assert controlled.get("/api/game-analysis/games", params={"q": "review"}).status_code == 401
