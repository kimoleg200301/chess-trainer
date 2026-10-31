import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    database_url: str
    auth_username: str
    auth_password: str
    allowed_origins: frozenset[str]
    cookie_secure: bool
    data_path: str
    stockfish_path: str
    session_days: int = 7
    cookie_name: str = "tactics_session"


@lru_cache
def settings() -> Settings:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if not url.startswith("postgresql+psycopg://"):
        raise RuntimeError("DATABASE_URL must reference PostgreSQL using psycopg")
    origins = {
        item.strip().rstrip("/")
        for item in os.environ.get(
            "ALLOWED_ORIGINS", "http://localhost:8000,http://localhost:8080,http://localhost:5173"
        ).split(",") if item.strip()
    }
    public_host = os.environ.get("PUBLIC_HOST", "").strip().rstrip("/")
    if public_host:
        origins.add("https://" + public_host.removeprefix("https://"))
    return Settings(
        database_url=url,
        auth_username=os.environ.get("AUTH_USERNAME", "trainer"),
        auth_password=os.environ.get("AUTH_PASSWORD", ""),
        allowed_origins=frozenset(origins),
        cookie_secure=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
        data_path=os.environ.get("DATA_PATH", "/app/data/puzzles.csv"),
        stockfish_path=os.environ.get("STOCKFISH_PATH", "/usr/games/stockfish"),
    )
