from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Puzzle(Base):
    __tablename__ = "puzzles"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    initial_fen: Mapped[str] = mapped_column(String(160))
    moves: Mapped[list[str]] = mapped_column(ARRAY(String(5)))
    rating: Mapped[int] = mapped_column(Integer, index=True)
    themes: Mapped[list[str]] = mapped_column(ARRAY(String(64)))
    game_url: Mapped[str] = mapped_column(Text)
    opening_tags: Mapped[str] = mapped_column(Text, default="")
    collection: Mapped[str] = mapped_column(String(16), default="lichess", server_default="lichess", index=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    rating_estimated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    source_game_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    source_ply: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, server_default=func.now())
    __table_args__ = (
        Index("ix_puzzles_themes", "themes", postgresql_using="gin"),
        CheckConstraint("(collection = 'personal' AND owner_id IS NOT NULL) OR "
                        "(collection IN ('lichess', 'public') AND owner_id IS NULL)", name="ck_puzzles_collection_owner"),
    )


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    puzzle_id: Mapped[str] = mapped_column(ForeignKey("puzzles.id"), index=True)
    fen: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(16), default="active")
    learner_white: Mapped[bool] = mapped_column(Boolean)
    ply_index: Mapped[int] = mapped_column(Integer, default=1)
    mistakes: Mapped[int] = mapped_column(Integer, default=0)
    hints_used: Mapped[int] = mapped_column(Integer, default=0)
    last_move: Mapped[str | None] = mapped_column(String(5), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    puzzle: Mapped[Puzzle] = relationship(lazy="joined")
    __table_args__ = (Index("ix_attempts_user_completed", "user_id", "completed_at"),
                      CheckConstraint("elapsed_ms >= 0", name="ck_attempts_elapsed_ms_nonnegative"))


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    puzzle_id: Mapped[str] = mapped_column(ForeignKey("puzzles.id"))
    successes: Mapped[int] = mapped_column(Integer, default=0)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    __table_args__ = (UniqueConstraint("user_id", "puzzle_id", name="uq_reviews_user_puzzle"),)


class ImportJob(Base):
    __tablename__ = "import_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(16))
    username: Mapped[str | None] = mapped_column(String(40), nullable=True)
    month: Mapped[str | None] = mapped_column(String(7), nullable=True)
    identity_key: Mapped[str] = mapped_column(String(64))
    analysis_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(16), default="ready")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("user_id", "identity_key", name="uq_import_jobs_user_identity"),)


class ImportedGame(Base):
    __tablename__ = "imported_games"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("import_jobs.id", ondelete="CASCADE"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    analysis_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    data: Mapped[dict] = mapped_column(JSONB)
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (UniqueConstraint("job_id", "fingerprint", name="uq_imported_games_job_fingerprint"),)


class Improvement(Base):
    __tablename__ = "improvements"
    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("imported_games.id", ondelete="CASCADE"), index=True)
    ply: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSONB)
    puzzle_id: Mapped[str | None] = mapped_column(ForeignKey("puzzles.id", ondelete="SET NULL"), nullable=True)
    __table_args__ = (UniqueConstraint("game_id", "ply", name="uq_improvements_game_ply"),)
