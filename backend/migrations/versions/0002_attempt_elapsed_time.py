"""Store measured active solving time; historical attempts remain unknown."""
from alembic import op
import sqlalchemy as sa

revision = "0002_attempt_elapsed_time"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("attempts", sa.Column("elapsed_ms", sa.BigInteger(), nullable=True))
    op.create_check_constraint("ck_attempts_elapsed_ms_nonnegative", "attempts", "elapsed_ms >= 0")


def downgrade():
    op.drop_constraint("ck_attempts_elapsed_ms_nonnegative", "attempts", type_="check")
    op.drop_column("attempts", "elapsed_ms")
