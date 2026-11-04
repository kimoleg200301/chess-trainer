"""Remember the analysis account and durable game synchronization intent."""
from alembic import op
import sqlalchemy as sa

revision = "0009_chesscom_sync"
down_revision = "0008_personal_coach"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("chesscom_sync",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("username", sa.String(40)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="idle"),
        sa.Column("background_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("requested_at", sa.DateTime(timezone=True)),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("added_games", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checked_games", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    # Preserve the most recently specified import account. Coach is a fallback,
    # never an instruction to alter or restart that user's training baseline.
    op.execute("""INSERT INTO chesscom_sync (user_id, username, updated_at)
        SELECT u.id, COALESCE(
            (SELECT lower(j.username) FROM import_jobs j WHERE j.user_id = u.id
             AND j.source = 'chesscom' AND j.username ~ '^[A-Za-z0-9_-]{1,40}$'
             ORDER BY j.created_at DESC, j.id DESC LIMIT 1),
            (SELECT lower(c.username) FROM coach_profiles c WHERE c.user_id = u.id
             AND c.username ~ '^[A-Za-z0-9_-]{1,40}$')),
            now() FROM users u""")


def downgrade():
    op.drop_table("chesscom_sync")
