"""Private, durable favorites over logical games rather than import rows."""
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_background_analysis import controlled
from test_game_analysis import PGN, fake_engine, imported_game, start, step


def favorite(client, game_id, value=True):
    response = client.post(f"/api/game-analysis/games/{game_id}/favorite", json={"is_favorite": value})
    assert response.status_code == 200, response.text
    return response.json()


def test_saved_favorite_is_idempotent_and_does_not_change_report_or_progress(controlled, fake_engine):
    from app.db import engine
    from app.models import GameFavorite
    game_id, _, _ = imported_game()
    report = start(controlled, game_id)
    for _ in range(4):
        report = step(controlled, report["id"])
    assert report["status"] == "completed"
    before = {path: controlled.get(path).json() for path in ("/api/stats", "/api/training/current", "/api/imports", "/api/coach")}
    saved = favorite(controlled, game_id)
    assert saved["is_favorite"] and saved["favorite_revision"] == 1
    assert favorite(controlled, game_id) == saved
    listed = controlled.get("/api/game-analysis/games").json()["games"][0]
    assert listed["is_favorite"] and listed["favorite_key"] == saved["favorite_key"]
    assert controlled.get(f"/api/game-analysis/{report['id']}").json() == report
    for path, value in before.items():
        assert controlled.get(path).json() == value
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(GameFavorite)) == 1
    removed = favorite(controlled, game_id, False)
    assert not removed["is_favorite"] and removed["favorite_revision"] == 2
    assert favorite(controlled, game_id, False) == removed
    assert favorite(controlled, game_id)["favorite_revision"] == 3
    controlled.post(f"/api/game-analysis/{report['id']}/restart", json={})
    assert controlled.get("/api/game-analysis/games").json()["games"][0]["is_favorite"]


def test_reimport_and_another_account_do_not_lose_or_share_favorites(controlled):
    old, _, _ = imported_game()
    saved = favorite(controlled, old)
    newest, _, _ = imported_game()
    other, _, _ = imported_game(username="favorite-other")
    games = controlled.get("/api/game-analysis/games").json()["games"]
    assert len(games) == 1 and games[0]["id"] == newest and games[0]["is_favorite"]
    assert games[0]["favorite_key"] == saved["favorite_key"]
    assert controlled.post(f"/api/game-analysis/games/{other}/favorite", json={"is_favorite": True}).status_code == 404
    # An old URL still resolves to that exact owned game without duplicating it.
    direct = controlled.get(f"/api/game-analysis/games?selected_game_id={old}").json()["games"]
    assert len(direct) == 1 and direct[0]["id"] == old and direct[0]["is_favorite"]
    favorite(controlled, old, False)
    assert not controlled.get("/api/game-analysis/games").json()["games"][0]["is_favorite"]
    controlled.post("/api/auth/logout")
    controlled.post("/api/auth/login", json={"username":"favorite-other", "password":"another-test-only-password"})
    assert not controlled.get("/api/game-analysis/games").json()["games"][0]["is_favorite"]
    favorite(controlled, other)
    controlled.post("/api/auth/logout")
    controlled.post("/api/auth/login", json={"username":"tester", "password":"integration-test-only-password"})
    assert not controlled.get("/api/game-analysis/games").json()["games"][0]["is_favorite"]


def test_old_favorites_survive_recent_limit_and_can_be_opened_after_removal(controlled, monkeypatch):
    from app import game_analysis_api
    from app.generation import parse_games
    monkeypatch.setattr(game_analysis_api, "MAX_LIST_GAMES", 2)
    game = parse_games(PGN)[0]
    ids = [imported_game(game={**game, "date":f"2025.0{month}.01"})[0] for month in (1, 2, 3)]
    assert [g["id"] for g in controlled.get("/api/game-analysis/games").json()["games"]] == ids[:0:-1]
    favorite(controlled, ids[0])
    assert [g["id"] for g in controlled.get("/api/game-analysis/games").json()["games"]] == ids[::-1]
    favorite(controlled, ids[0], False)
    assert len(controlled.get("/api/game-analysis/games").json()["games"]) == 2
    assert len(controlled.get(f"/api/game-analysis/games?selected_game_id={ids[0]}").json()["games"]) == 3
    other, _, _ = imported_game(username="favorite-hidden")
    assert other not in [g["id"] for g in controlled.get(f"/api/game-analysis/games?selected_game_id={other}").json()["games"]]


def test_favorite_auth_origin_validation_and_public_source(controlled):
    game_id, _, _ = imported_game()
    public, _, _ = imported_game(source="public")
    endpoint = f"/api/game-analysis/games/{game_id}/favorite"
    assert controlled.post(endpoint, json={"is_favorite":True}, headers={"Origin":"https://evil.invalid"}).status_code == 403
    for body in ({}, {"is_favorite":"yes"}, {"is_favorite":1}, {"is_favorite":None}, {"is_favorite":True,"user_id":2}):
        assert controlled.post(endpoint, json=body).status_code == 422
    assert controlled.post(f"/api/game-analysis/games/{public}/favorite", json={"is_favorite":True}).status_code == 404
    assert controlled.post("/api/game-analysis/games/999999/favorite", json={"is_favorite":True}).status_code == 404
    assert controlled.get("/api/game-analysis/games?selected_game_id=0").status_code == 422
    controlled.post("/api/auth/logout")
    assert controlled.post(endpoint, json={"is_favorite":True}).status_code == 401


def test_concurrent_repeated_requests_make_one_favorite(controlled):
    from app.db import engine
    from app.models import GameFavorite
    game_id, _, _ = imported_game()
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: favorite(controlled, game_id), range(4)))
    assert all(row["is_favorite"] and row["favorite_revision"] == 1 for row in responses)
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(GameFavorite)) == 1
