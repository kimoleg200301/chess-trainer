from datetime import timedelta

import chess
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def start(client, **overrides):
    response = client.post("/api/training/start", json={"min_rating": 0, "max_rating": 4000, **overrides})
    assert response.status_code == 200, response.text
    return response.json()


def puzzle_for(attempt):
    from app.db import engine
    from app.models import Puzzle
    with Session(engine()) as db:
        return db.get(Puzzle, attempt["puzzle_id"])


def solve(client, attempt):
    puzzle = puzzle_for(attempt)
    for uci in puzzle.moves[1::2]:
        response = client.post(f"/api/training/{attempt['id']}/move", json={"move": uci})
        assert response.status_code == 200, response.text
        assert response.json()["accepted"]
        attempt = response.json()["attempt"]
    assert attempt["status"] == "completed"
    return attempt


def test_authentication_and_origin_protection(client):
    assert client.get("/api/meta").status_code == 401
    assert client.post("/api/auth/login", json={"username": "tester", "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", headers={"Origin": "https://attacker.invalid"},
                       json={"username": "tester", "password": "integration-test-only-password"}).status_code == 403
    assert client.get("/api/health").json()["database"] == "ok"


def test_session_cookie_is_hashed_and_revoked(logged):
    from app.db import engine
    from app.models import LoginSession
    token = logged.cookies.get("tactics_session")
    with Session(engine()) as db:
        stored = db.scalar(select(LoginSession.token_hash))
        assert len(stored) == 64 and stored != token
    assert logged.get("/api/auth/me").json() == {"username": "tester"}
    response = logged.post("/api/auth/logout")
    assert response.status_code == 204
    logged.cookies.set("tactics_session", token)
    assert logged.get("/api/auth/me").status_code == 401


def test_seed_is_idempotent_and_has_real_motifs(logged):
    from app.bootstrap import bootstrap
    before = logged.get("/api/meta").json()
    bootstrap()
    after = logged.get("/api/meta").json()
    assert before == after
    assert after["total_puzzles"] >= 100
    assert {"fork", "pin", "mateIn1"}.issubset({row["id"] for row in after["themes"]})


def test_fen_offset_and_answers_hidden_until_completion(logged):
    attempt = start(logged)
    puzzle = puzzle_for(attempt)
    board = chess.Board(puzzle.initial_fen)
    board.push_uci(puzzle.moves[0])
    assert attempt["fen"] == board.fen()
    assert attempt["turn"] == ("white" if board.turn else "black")
    assert not {"moves", "solution", "game_url"}.intersection(attempt)
    assert logged.get(f"/api/training/{attempt['id']}/analysis").status_code == 403


def test_full_solution_auto_replies_stats_and_spaced_repetition(logged):
    from app.db import engine
    from app.models import Review, now
    attempt = solve(logged, start(logged, theme="fork"))
    stats = logged.get("/api/stats").json()
    assert stats["solved"] == 1 and stats["accuracy"] == 100 and stats["streak"] == 1
    assert logged.post(f"/api/training/{attempt['id']}/move", json={"move": "e2e4"}).status_code == 409
    with Session(engine()) as db:
        review = db.scalar(select(Review))
        assert review.successes == 1 and review.due_at > now() + timedelta(hours=23)


def test_wrong_legal_move_does_not_mutate_board(logged):
    from app.db import engine
    from app.models import Review
    attempt = start(logged, theme="fork")
    puzzle = puzzle_for(attempt)
    board = chess.Board(attempt["fen"])
    wrong = None
    for move in board.legal_moves:
        probe = board.copy()
        probe.push(move)
        if move.uci() != puzzle.moves[1] and not probe.is_checkmate():
            wrong = move.uci()
            break
    assert wrong
    response = logged.post(f"/api/training/{attempt['id']}/move", json={"move": wrong})
    result = response.json()
    assert response.status_code == 200 and not result["accepted"]
    assert result["attempt"]["fen"] == attempt["fen"]
    assert result["attempt"]["mistakes"] == 1
    start(logged)
    with Session(engine()) as db:
        assert db.scalar(select(Review).where(Review.puzzle_id == attempt["puzzle_id"])) is not None
    solved = solve(logged, attempt)
    assert solved["mistakes"] == 1
    assert logged.get("/api/stats").json()["accuracy"] == 0


def test_hint_and_reveal_are_idempotent_and_not_clean_solve(logged):
    from app.db import engine
    from app.models import Review
    attempt = start(logged)
    endpoint = f"/api/training/{attempt['id']}"
    for _ in range(2):
        hint = logged.post(endpoint + "/hint").json()
        assert hint["attempt"]["hints_used"] == 1
        assert hint["from_square"] == puzzle_for(attempt).moves[1][:2]
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Review)) == 1
    first = logged.post(endpoint + "/reveal").json()
    second = logged.post(endpoint + "/reveal").json()
    assert first == second
    assert first["attempt"]["status"] == "revealed"
    assert first["solution"][0]["uci"] == puzzle_for(attempt).moves[1]
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Review)) == 1
    stats = logged.get("/api/stats").json()
    assert stats["accuracy"] == 0 and stats["solved"] == 0


def test_review_only_returns_due_puzzles(logged):
    from app.db import engine
    from app.models import Review, now
    assert logged.post("/api/training/start", json={"mode": "review", "min_rating": 0, "max_rating": 4000}).status_code == 404
    attempt = start(logged)
    logged.post(f"/api/training/{attempt['id']}/reveal")
    with Session(engine()) as db:
        review = db.scalar(select(Review))
        review.due_at = now() - timedelta(minutes=1)
        db.commit()
    assert logged.get("/api/stats").json()["due_count"] == 1
    review_attempt = start(logged, mode="review")
    assert review_attempt["puzzle_id"] == attempt["puzzle_id"]


def test_alternate_mate_is_accepted(logged):
    from app.db import engine
    from app.models import Attempt, Puzzle, User
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == "tester"))
        for puzzle in db.scalars(select(Puzzle).where(Puzzle.themes.contains(["mateIn1"]))):
            board = chess.Board(puzzle.initial_fen)
            board.push_uci(puzzle.moves[0])
            alternative = None
            for move in board.legal_moves:
                probe = board.copy()
                probe.push(move)
                if probe.is_checkmate() and move.uci() != puzzle.moves[1]:
                    alternative = move.uci()
                    break
            if alternative:
                attempt = Attempt(user_id=user.id, puzzle_id=puzzle.id, fen=board.fen(), learner_white=board.turn)
                db.add(attempt)
                db.commit()
                attempt_id = attempt.id
                break
        else:
            raise AssertionError("The fixture dataset needs an alternate-mate example")
    result = logged.post(f"/api/training/{attempt_id}/move", json={"move": alternative})
    assert result.status_code == 200
    assert result.json()["accepted"] and result.json()["attempt"]["status"] == "completed"


def test_attempt_owner_and_validation(logged):
    from app.db import engine
    from app.models import Attempt, User
    from app.security import hash_password
    attempt = start(logged)
    with Session(engine()) as db:
        other = User(username="other", password_hash=hash_password("other-private-password"))
        db.add(other)
        db.flush()
        row = db.get(Attempt, attempt["id"])
        row.user_id = other.id
        db.commit()
    assert logged.post(f"/api/training/{attempt['id']}/reveal").status_code == 404
    assert logged.post("/api/training/start", json={"min_rating": 2000, "max_rating": 1000}).status_code == 422
    assert logged.post("/api/training/start", json={"theme": "unknown"}).status_code == 422


def test_login_rate_limit(client):
    for _ in range(10):
        assert client.post("/api/auth/login", json={"username": "tester", "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "another", "password": "wrong"}).status_code == 429
