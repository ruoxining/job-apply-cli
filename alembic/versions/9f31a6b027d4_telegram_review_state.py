"""store Telegram review state on canonical jobs

Revision ID: 9f31a6b027d4
Revises: e7b2a9c4d103
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "9f31a6b027d4"
down_revision: Union[str, None] = "e7b2a9c4d103"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("job_postings", sa.Column("telegram_message_id", sa.BigInteger()))
    op.add_column(
        "job_postings", sa.Column("telegram_review_notified_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "job_postings", sa.Column("telegram_result_notified_at", sa.DateTime(timezone=True))
    )
    op.add_column("job_postings", sa.Column("review_feedback", sa.Text()))
    op.add_column("job_postings", sa.Column("reviewed_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_job_postings_telegram_review_notified_at",
        "job_postings",
        ["telegram_review_notified_at"],
    )
    op.create_index(
        "ix_job_postings_telegram_result_notified_at",
        "job_postings",
        ["telegram_result_notified_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_job_postings_telegram_result_notified_at", table_name="job_postings")
    op.drop_index("ix_job_postings_telegram_review_notified_at", table_name="job_postings")
    op.drop_column("job_postings", "reviewed_at")
    op.drop_column("job_postings", "review_feedback")
    op.drop_column("job_postings", "telegram_result_notified_at")
    op.drop_column("job_postings", "telegram_review_notified_at")
    op.drop_column("job_postings", "telegram_message_id")
