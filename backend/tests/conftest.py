"""Real PostgreSQL integration tests, isolated in a disposable schema."""
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import create_engine, delete, select, text, update
from sqlalchemy.engine import make_url


@pytest.fixture(scope="session", autouse=True)
def database(tmp_path_factory):
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("Set TEST_DATABASE_URL to a PostgreSQL test database")
    schema = "tactics_test_" + uuid.uuid4().hex
    raw = create_engine(test_url)
    with raw.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = make_url(test_url).update_query_dict({"options": f"-csearch_path={schema}"})
    frontend = tmp_path_factory.mktemp("frontend")
    (frontend / "index.html").write_text("<!doctype html><title>Tactics Lab test frontend</title>", encoding="utf-8")
    (frontend / "assets").mkdir()
    (frontend / "assets" / "app.js").write_text("/* static test asset */", encoding="utf-8")
    os.environ.update(DATABASE_URL=url.render_as_string(hide_password=False),
                      AUTH_USERNAME="tester", AUTH_PASSWORD="integration-test-only-password",
                      COOKIE_SECURE="false", ALLOWED_ORIGINS="http://testserver",
                      DATA_PATH=str(Path(__file__).resolve().parents[2] / "data" / "puzzles.csv"),
                      FRONTEND_DIST=str(frontend))
    from app.config import settings
    from app.db import engine
    settings.cache_clear()
    engine.cache_clear()
    from alembic.config import Config
    from alembic import command
    command.upgrade(Config("alembic.ini"), "head")
    from app.bootstrap import bootstrap
    bootstrap()
    from app.models import Puzzle
    from sqlalchemy.orm import Session
    with Session(engine()) as db:
        seed_rows = db.execute(select(Puzzle.id, Puzzle.collection, Puzzle.rating_estimated, Puzzle.rating)).all()
    yield seed_rows
    engine().dispose()
    with raw.begin() as db:
        db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    raw.dispose()


@pytest.fixture(autouse=True)
def clean_training(database):
    from app.db import engine
    from app.security import login_limiter
    with engine().begin() as db:
        db.execute(text("TRUNCATE game_analyses, improvements, imported_games, import_jobs, attempts, reviews, login_sessions RESTART IDENTITY"))
        from app.models import Puzzle, User
        db.execute(delete(Puzzle).where(Puzzle.id.not_in([row.id for row in database])))
        for collection in {row.collection for row in database}:
            ids = [row.id for row in database if row.collection == collection]
            db.execute(update(Puzzle).where(Puzzle.id.in_(ids)).values(collection=collection, owner_id=None, rating_estimated=collection != "lichess"))
        current_ratings = dict(db.execute(select(Puzzle.id, Puzzle.rating)).all())
        for row in database:
            if current_ratings[row.id] != row.rating:
                db.execute(update(Puzzle).where(Puzzle.id == row.id).values(rating=row.rating))
        db.execute(update(Puzzle).where(Puzzle.rated_attempts != 0).values(rated_attempts=0))
        db.execute(update(User).values(puzzle_rating=1200, rated_attempts=0))
    login_limiter.entries.clear()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        yield client


@pytest.fixture
def logged(client):
    result = client.post("/api/auth/login", json={"username": "tester", "password": "integration-test-only-password"})
    assert result.status_code == 200
    return client
