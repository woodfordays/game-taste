"""refresh progress

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-31 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Plain new columns, so SQLite adds them in place. A run from before this has no total
    # (NULL), nothing checked (0) and no profile in progress (NULL): right for a finished run.
    op.add_column(
        "refresh_run", sa.Column("profiles_total", sa.Integer(), nullable=True)
    )
    op.add_column(
        "refresh_run",
        sa.Column("profiles_checked", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("refresh_run", sa.Column("current_profile", sa.Text(), nullable=True))


def downgrade() -> None:
    # resolve_fks=False keeps the reflection to this one table (see 0006).
    with op.batch_alter_table(
        "refresh_run", schema=None, reflect_kwargs={"resolve_fks": False}
    ) as batch_op:
        batch_op.drop_column("current_profile")
        batch_op.drop_column("profiles_checked")
        batch_op.drop_column("profiles_total")
