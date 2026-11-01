"""game facts

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-01 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A new table, so its NOT NULL columns need no server default.
    op.create_table(
        "game_fact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("appid", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["appid"], ["game.appid"], name=op.f("fk_game_fact_appid_game")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_game_fact")),
    )
    op.create_index(
        "ix_game_fact_appid_kind_fetched_at",
        "game_fact",
        ["appid", "kind", "fetched_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_game_fact_appid_kind_fetched_at", table_name="game_fact")
    op.drop_table("game_fact")
