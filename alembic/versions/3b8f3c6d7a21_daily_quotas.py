"""add daily quota counters

Revision ID: 3b8f3c6d7a21
Revises: 811a2bd52e30
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "3b8f3c6d7a21"
down_revision: Union[str, None] = "811a2bd52e30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "daily_quotas",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("quota_date", sa.Date(), nullable=False),
        sa.Column("fetched_count", sa.Integer(), nullable=False),
        sa.Column("applied_count", sa.Integer(), nullable=False),
        sa.Column("fetch_limit", sa.Integer(), nullable=False),
        sa.Column("apply_limit", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("quota_date", name="uq_daily_quota_date"),
    )
    op.create_index("ix_daily_quotas_quota_date", "daily_quotas", ["quota_date"])


def downgrade() -> None:
    op.drop_index("ix_daily_quotas_quota_date", table_name="daily_quotas")
    op.drop_table("daily_quotas")
