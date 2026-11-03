"""Personal coach: frozen baseline, monitored games and separate practice history."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008_personal_coach"
down_revision = "0007_rating_and_resume"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("coach_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("username", sa.String(40), nullable=False),
        sa.Column("time_control", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("baseline_ready", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sync_state", postgresql.JSONB(), nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.Column("next_sync_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sync_error", sa.Text()), sa.Column("error", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_coach_profiles_next_sync_at", "coach_profiles", ["next_sync_at"])
    op.create_table("coach_games",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("coach_id", sa.Integer(), sa.ForeignKey("coach_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("cohort", sa.String(16), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("source_game_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("moves", postgresql.JSONB(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("analysis_version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("coach_id", "fingerprint", name="uq_coach_games_identity"),
        sa.CheckConstraint("cohort IN ('baseline', 'followup')", name="ck_coach_game_cohort"),
        sa.CheckConstraint("cursor >= 0", name="ck_coach_game_cursor"))
    op.create_index("ix_coach_games_coach_id", "coach_games", ["coach_id"])
    op.create_index("ix_coach_games_source_game_at", "coach_games", ["source_game_at"])
    op.create_table("coach_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("coach_id", sa.Integer(), sa.ForeignKey("coach_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("skill_id", sa.String(40), nullable=False),
        sa.Column("task_key", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("game_id", sa.Integer(), sa.ForeignKey("coach_games.id", ondelete="SET NULL")),
        sa.Column("puzzle_id", sa.String(32), sa.ForeignKey("puzzles.id", ondelete="SET NULL")),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("fen", sa.String(160), nullable=False),
        sa.Column("starting_fen", sa.String(160), nullable=False),
        sa.Column("learner_white", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("ply_index", sa.Integer(), nullable=False),
        sa.Column("mistakes", sa.Integer(), nullable=False),
        sa.Column("hints_used", sa.Integer(), nullable=False),
        sa.Column("elapsed_ms", sa.BigInteger()),
        sa.Column("is_new", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("elapsed_ms >= 0", name="ck_coach_elapsed_nonnegative"))
    op.create_index("ix_coach_attempts_coach_id", "coach_attempts", ["coach_id"])
    op.create_index("ix_coach_attempts_task_key", "coach_attempts", ["task_key"])
    op.create_table("coach_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("coach_id", sa.Integer(), sa.ForeignKey("coach_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_key", sa.String(160), nullable=False),
        sa.Column("skill_id", sa.String(40), nullable=False),
        sa.Column("successes", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("coach_id", "task_key", name="uq_coach_review_task"))
    op.create_index("ix_coach_reviews_coach_id", "coach_reviews", ["coach_id"])
    op.create_index("ix_coach_reviews_due_at", "coach_reviews", ["due_at"])


def downgrade():
    for table in ("coach_reviews", "coach_attempts", "coach_games", "coach_profiles"):
        op.drop_table(table)
