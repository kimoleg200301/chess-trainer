"""Persist explicit background execution intent; legacy jobs stay paused."""
from alembic import op
import sqlalchemy as sa

revision = "0006_background_analysis"
down_revision = "0005_separate_game_analysis"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("import_jobs", "game_analyses"):
        op.add_column(table, sa.Column("background_requested", sa.Boolean(),
                                      nullable=False, server_default=sa.false()))


def downgrade():
    for table in ("game_analyses", "import_jobs"):
        op.drop_column(table, "background_requested")
