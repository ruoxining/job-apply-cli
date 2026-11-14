"""add job ingested_via column

Revision ID: b5e8d2a1f9c3
Revises: 9f31a6b027d4
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "b5e8d2a1f9c3"
down_revision: Union[str, None] = "9f31a6b027d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("job_postings", sa.Column("ingested_via", sa.String(length=32)))


def downgrade() -> None:
    op.drop_column("job_postings", "ingested_via")
