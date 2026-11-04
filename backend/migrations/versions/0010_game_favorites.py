"""Account-scoped favorites independent of import batches and engine reports."""
from alembic import op
import sqlalchemy as sa

revision = "0010_game_favorites"
down_revision = "0009_chesscom_sync"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("game_favorites",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("fingerprint", sa.String(64), primary_key=True),
        sa.Column("is_favorite", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))


def downgrade():
    op.drop_table("game_favorites")
