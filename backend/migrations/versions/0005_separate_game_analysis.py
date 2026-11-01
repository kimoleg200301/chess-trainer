"""Persist separate per-move game reviews without changing puzzle training."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_separate_game_analysis"
down_revision = "0004_analysis_revision_and_order"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("game_analyses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("game_id", sa.Integer(), sa.ForeignKey("imported_games.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("analysis_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("cursor", sa.Integer(), server_default="0", nullable=False),
        sa.Column("moves", postgresql.JSONB(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "fingerprint", name="uq_game_analyses_user_fingerprint"),
        sa.CheckConstraint("cursor >= 0", name="ck_game_analyses_cursor_nonnegative"))
    op.create_index("ix_game_analyses_user_id", "game_analyses", ["user_id"])
    op.create_index("ix_game_analyses_game_id", "game_analyses", ["game_id"])


def downgrade():
    op.drop_table("game_analyses")
