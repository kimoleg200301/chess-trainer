"""Track analysis versions and preserve the chronological source of generated tasks."""
from alembic import op
import sqlalchemy as sa

revision = "0004_analysis_revision_and_order"
down_revision = "0003_collections_and_imports"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("puzzles", sa.Column("source_game_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("puzzles", sa.Column("source_ply", sa.Integer(), nullable=True))
    op.add_column("puzzles", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_puzzles_source_game_at", "puzzles", ["source_game_at"])
    op.add_column("import_jobs", sa.Column("analysis_version", sa.Integer(), server_default="1", nullable=False))
    op.add_column("imported_games", sa.Column("analysis_version", sa.Integer(), server_default="1", nullable=False))


def downgrade():
    op.drop_column("imported_games", "analysis_version")
    op.drop_column("import_jobs", "analysis_version")
    op.drop_index("ix_puzzles_source_game_at", table_name="puzzles")
    op.drop_column("puzzles", "created_at")
    op.drop_column("puzzles", "source_ply")
    op.drop_column("puzzles", "source_game_at")
