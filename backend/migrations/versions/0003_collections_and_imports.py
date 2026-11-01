"""Separate puzzle collections and resumable real-game analysis."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_collections_and_imports"
down_revision = "0002_attempt_elapsed_time"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("puzzles", sa.Column("collection", sa.String(16), server_default="lichess", nullable=False))
    op.add_column("puzzles", sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True))
    op.add_column("puzzles", sa.Column("rating_estimated", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.create_index("ix_puzzles_collection", "puzzles", ["collection"])
    op.create_index("ix_puzzles_owner_id", "puzzles", ["owner_id"])
    op.create_check_constraint("ck_puzzles_collection_owner", "puzzles", "(collection = 'personal' AND owner_id IS NOT NULL) OR (collection IN ('lichess', 'public') AND owner_id IS NULL)")
    op.create_table("import_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("username", sa.String(40), nullable=True),
        sa.Column("month", sa.String(7), nullable=True),
        sa.Column("identity_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "identity_key", name="uq_import_jobs_user_identity"))
    op.create_index("ix_import_jobs_user_id", "import_jobs", ["user_id"])
    op.create_table("imported_games",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("import_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint("job_id", "fingerprint", name="uq_imported_games_job_fingerprint"))
    op.create_index("ix_imported_games_job_id", "imported_games", ["job_id"])
    op.create_table("improvements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("game_id", sa.Integer(), sa.ForeignKey("imported_games.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ply", sa.Integer(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("puzzle_id", sa.String(32), sa.ForeignKey("puzzles.id", ondelete="SET NULL"), nullable=True),
        sa.UniqueConstraint("game_id", "ply", name="uq_improvements_game_ply"))
    op.create_index("ix_improvements_game_id", "improvements", ["game_id"])


def downgrade():
    op.drop_table("improvements")
    op.drop_table("imported_games")
    op.drop_table("import_jobs")
    op.drop_constraint("ck_puzzles_collection_owner", "puzzles", type_="check")
    op.drop_index("ix_puzzles_owner_id", table_name="puzzles")
    op.drop_index("ix_puzzles_collection", table_name="puzzles")
    op.drop_column("puzzles", "rating_estimated")
    op.drop_column("puzzles", "owner_id")
    op.drop_column("puzzles", "collection")
