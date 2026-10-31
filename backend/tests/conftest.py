"""Real PostgreSQL integration tests, isolated in a disposable schema."""
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.fixture(scope="session", autouse=True)
def database():
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("Set TEST_DATABASE_URL to a PostgreSQL test database")
    schema = "tactics_test_" + uuid.uuid4().hex
    raw = create_engine(test_url)
    with raw.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = make_url(test_url).update_query_dict({"options": f"-csearch_path={schema}"})
    os.environ.update(DATABASE_URL=url.render_as_string(hide_password=False),
                      AUTH_USERNAME="tester", AUTH_PASSWORD="integration-test-only-password",
                      COOKIE_SECURE="false", ALLOWED_ORIGINS="http://testserver",
                      DATA_PATH=str(Path(__file__).resolve().parents[2] / "data" / "puzzles.csv"))
    from app.config import settings
    from app.db import engine
    settings.cache_clear()
    engine.cache_clear()
    from alembic.config import Config
    from alembic import command
    command.upgrade(Config("alembic.ini"), "head")
    from app.bootstrap import bootstrap
    bootstrap()
    yield
    engine().dispose()
    with raw.begin() as db:
        db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    raw.dispose()


@pytest.fixture(autouse=True)
def clean_training(database):
    from app.db import engine
    from app.security import login_limiter
    with engine().begin() as db:
        db.execute(text("TRUNCATE attempts, reviews, login_sessions RESTART IDENTITY"))
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
