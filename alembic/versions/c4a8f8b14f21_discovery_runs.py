"""add durable discovery and source runs

Revision ID: c4a8f8b14f21
Revises: 3b8f3c6d7a21
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4a8f8b14f21"
down_revision: Union[str, None] = "3b8f3c6d7a21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "discovery_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("board_count", sa.Integer(), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_date", name="uq_discovery_run_date"),
    )
    op.create_index("ix_discovery_runs_run_date", "discovery_runs", ["run_date"])
    op.create_index("ix_discovery_runs_status", "discovery_runs", ["status"])
    op.create_table(
        "source_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("discovery_run_id", sa.String(length=36), nullable=False),
        sa.Column("ats", sa.String(length=32), nullable=False),
        sa.Column("board_slug", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("fetched_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["discovery_run_id"], ["discovery_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("discovery_run_id", "ats", "board_slug", name="uq_source_run_board"),
    )
    op.create_index("ix_source_runs_discovery_run_id", "source_runs", ["discovery_run_id"])
    op.create_index("ix_source_runs_status", "source_runs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_source_runs_status", table_name="source_runs")
    op.drop_index("ix_source_runs_discovery_run_id", table_name="source_runs")
    op.drop_table("source_runs")
    op.drop_index("ix_discovery_runs_status", table_name="discovery_runs")
    op.drop_index("ix_discovery_runs_run_date", table_name="discovery_runs")
    op.drop_table("discovery_runs")
