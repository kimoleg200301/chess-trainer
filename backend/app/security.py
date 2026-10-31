import base64
import hashlib
import secrets
import threading
import time
from collections import OrderedDict, deque
from datetime import timedelta

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import LoginSession, User, now


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, salt, expected = encoded.split("$")
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=16384, r=8, p=1)
        return secrets.compare_digest(actual, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(settings().cookie_name)
    if not token or len(token) > 128:
        raise HTTPException(401, "Войдите, чтобы продолжить тренировку.")
    user = db.scalar(
        select(User).join(LoginSession, LoginSession.user_id == User.id)
        .where(LoginSession.token_hash == token_hash(token), LoginSession.expires_at > now())
    )
    if not user:
        raise HTTPException(401, "Сессия закончилась. Войдите снова.")
    return user


def create_session(db: Session, user: User, response: Response):
    token = secrets.token_urlsafe(32)
    cfg = settings()
    db.execute(delete(LoginSession).where(LoginSession.expires_at <= now()))
    db.add(LoginSession(token_hash=token_hash(token), user_id=user.id,
                        expires_at=now() + timedelta(days=cfg.session_days)))
    response.set_cookie(cfg.cookie_name, token, max_age=cfg.session_days * 86400,
                        httponly=True, secure=cfg.cookie_secure, samesite="lax", path="/")


class LoginLimiter:
    """Bounded limiter for the single-worker personal deployment."""
    def __init__(self):
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def check(self, address: str, username: str):
        moment = time.monotonic()
        # Rate limit the network address, independent of user-supplied username.
        with self.lock:
            attempts = self.entries.setdefault(address, deque())
            while attempts and moment - attempts[0] > 60:
                attempts.popleft()
            self.entries.move_to_end(address)
            while len(self.entries) > 10000:
                self.entries.popitem(last=False)
            if len(attempts) >= 10:
                raise HTTPException(429, "Слишком много попыток входа. Подождите минуту.", headers={"Retry-After": "60"})
            attempts.append(moment)


login_limiter = LoginLimiter()
