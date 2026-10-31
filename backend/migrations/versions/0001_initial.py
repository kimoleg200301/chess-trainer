"""PostgreSQL schema for accounts, puzzles and spaced repetition."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("users", sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("username", sa.String(80), nullable=False, unique=True),
                    sa.Column("password_hash", sa.String(255), nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("login_sessions", sa.Column("token_hash", sa.String(64), primary_key=True),
                    sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
                    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_login_sessions_user_id", "login_sessions", ["user_id"])
    op.create_index("ix_login_sessions_expires_at", "login_sessions", ["expires_at"])
    op.create_table("puzzles", sa.Column("id", sa.String(32), primary_key=True),
                    sa.Column("initial_fen", sa.String(160), nullable=False),
                    sa.Column("moves", postgresql.ARRAY(sa.String(5)), nullable=False),
                    sa.Column("rating", sa.Integer(), nullable=False),
                    sa.Column("themes", postgresql.ARRAY(sa.String(64)), nullable=False),
                    sa.Column("game_url", sa.Text(), nullable=False),
                    sa.Column("opening_tags", sa.Text(), nullable=False))
    op.create_index("ix_puzzles_rating", "puzzles", ["rating"])
    op.create_index("ix_puzzles_themes", "puzzles", ["themes"], postgresql_using="gin")
    op.create_table("attempts", sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
                    sa.Column("puzzle_id", sa.String(32), sa.ForeignKey("puzzles.id"), nullable=False),
                    sa.Column("fen", sa.String(160), nullable=False),
                    sa.Column("status", sa.String(16), nullable=False),
                    sa.Column("learner_white", sa.Boolean(), nullable=False),
                    sa.Column("ply_index", sa.Integer(), nullable=False),
                    sa.Column("mistakes", sa.Integer(), nullable=False),
                    sa.Column("hints_used", sa.Integer(), nullable=False),
                    sa.Column("last_move", sa.String(5), nullable=True),
                    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_attempts_user_id", "attempts", ["user_id"])
    op.create_index("ix_attempts_puzzle_id", "attempts", ["puzzle_id"])
    op.create_index("ix_attempts_user_completed", "attempts", ["user_id", "completed_at"])
    op.create_table("reviews", sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
                    sa.Column("puzzle_id", sa.String(32), sa.ForeignKey("puzzles.id"), nullable=False),
                    sa.Column("successes", sa.Integer(), nullable=False),
                    sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
                    sa.UniqueConstraint("user_id", "puzzle_id", name="uq_reviews_user_puzzle"))
    op.create_index("ix_reviews_due_at", "reviews", ["due_at"])


def downgrade():
    for table in ["reviews", "attempts", "puzzles", "login_sessions", "users"]:
        op.drop_table(table)
