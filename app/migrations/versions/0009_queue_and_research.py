"""queue and research

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-01 16:30:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # All new tables, so their NOT NULL columns need no server default beyond the ones the
    # models give to counters and flags.
    op.create_table(
        "research_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("targets", sa.JSON(), nullable=False),
        sa.Column("rounds_allowed", sa.Integer(), server_default="3", nullable=False),
        sa.Column("rounds_done", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "cancel_requested", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("refresh_run_id", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["refresh_run_id"],
            ["refresh_run.id"],
            name=op.f("fk_research_run_refresh_run_id_refresh_run"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_run")),
    )
    op.create_table(
        "research_choice",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("choice", sa.String(), nullable=False),
        sa.Column("lane", sa.String(), nullable=True),
        sa.Column("made_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["research_run.id"],
            name=op.f("fk_research_choice_run_id_research_run"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_choice")),
    )
    op.create_table(
        "research_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=True),
        sa.Column("lane", sa.String(), nullable=True),
        sa.Column("step", sa.String(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["research_run.id"],
            name=op.f("fk_research_event_run_id_research_run"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_event")),
    )
    op.create_index("ix_research_event_run_id", "research_event", ["run_id"])
    op.create_table(
        "candidate",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("lane", sa.String(), nullable=False),
        sa.Column("appid", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("fits", sa.JSON(), nullable=False),
        sa.Column("risk", sa.Text(), nullable=False),
        sa.Column("version", sa.JSON(), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("profile_version_id", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "list_rejected", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("added", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("recommender_call_id", sa.Integer(), nullable=True),
        sa.Column("judge_call_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["research_run.id"],
            name=op.f("fk_candidate_run_id_research_run"),
        ),
        sa.ForeignKeyConstraint(
            ["appid"], ["game.appid"], name=op.f("fk_candidate_appid_game")
        ),
        sa.ForeignKeyConstraint(
            ["profile_version_id"],
            ["profile_version.id"],
            name=op.f("fk_candidate_profile_version_id_profile_version"),
        ),
        sa.ForeignKeyConstraint(
            ["recommender_call_id"],
            ["ai_call.id"],
            name=op.f("fk_candidate_recommender_call_id_ai_call"),
        ),
        sa.ForeignKeyConstraint(
            ["judge_call_id"],
            ["ai_call.id"],
            name=op.f("fk_candidate_judge_call_id_ai_call"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate")),
    )
    op.create_index("ix_candidate_lane_appid", "candidate", ["lane", "appid"])
    op.create_table(
        "inventory_entry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lane", sa.String(), nullable=False),
        sa.Column("appid", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["appid"], ["game.appid"], name=op.f("fk_inventory_entry_appid_game")
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidate.id"],
            name=op.f("fk_inventory_entry_candidate_id_candidate"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_entry")),
        sa.UniqueConstraint(
            "lane", "appid", name=op.f("uq_inventory_entry_lane_appid")
        ),
    )
    op.create_table(
        "inventory_status_change",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("candidate_id", sa.Integer(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["entry_id"],
            ["inventory_entry.id"],
            name=op.f("fk_inventory_status_change_entry_id_inventory_entry"),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidate.id"],
            name=op.f("fk_inventory_status_change_candidate_id_candidate"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_status_change")),
    )


def downgrade() -> None:
    op.drop_table("inventory_status_change")
    op.drop_table("inventory_entry")
    op.drop_index("ix_candidate_lane_appid", table_name="candidate")
    op.drop_table("candidate")
    op.drop_index("ix_research_event_run_id", table_name="research_event")
    op.drop_table("research_event")
    op.drop_table("research_choice")
    op.drop_table("research_run")
