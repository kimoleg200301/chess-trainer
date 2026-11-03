"""Persist puzzle ratings and the last opened training position without replaying history."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007_rating_and_resume"
down_revision = "0006_background_analysis"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("puzzle_rating", sa.Integer(), nullable=False, server_default="1200"))
    op.add_column("users", sa.Column("rated_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("puzzles", sa.Column("rated_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("attempts", sa.Column("last_opened_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("attempts", sa.Column("training_options", postgresql.JSONB(), nullable=False, server_default="{}"))
    op.add_column("attempts", sa.Column("rated", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("attempts", sa.Column("rating_change", postgresql.JSONB(), nullable=True))
    op.create_index("ix_attempts_user_opened", "attempts", ["user_id", "last_opened_at"])


def downgrade():
    op.drop_index("ix_attempts_user_opened", table_name="attempts")
    for field in ("rating_change", "rated", "training_options", "last_opened_at"):
        op.drop_column("attempts", field)
    op.drop_column("puzzles", "rated_attempts")
    op.drop_column("users", "rated_attempts")
    op.drop_column("users", "puzzle_rating")
