# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libstdc++6 && rm -rf /var/lib/apt/lists/*
COPY backend/scripts/install-stockfish.py /tmp/install-stockfish.py
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    python /tmp/install-stockfish.py && rm /tmp/install-stockfish.py
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir uv==0.12.23 && uv sync --frozen --no-dev
COPY backend/app ./app
COPY backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY backend/start.sh ./start.sh
COPY data/puzzles.csv data/public-games.pgn data/generated-puzzles.json ./data/
ENV DATA_PATH=/app/data/puzzles.csv STOCKFISH_PATH=/usr/games/stockfish
RUN useradd --create-home --uid 10001 app && chmod +x /app/start.sh && chown -R app:app /app
USER app
EXPOSE 8000
CMD ["/app/start.sh"]
