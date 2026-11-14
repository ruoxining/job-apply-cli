"""Versioned experiences and idempotent ingestion events."""

import sqlalchemy as sa

from alembic import op

revision = "f2c318b901ad"
down_revision = "b5e8d2a1f9c3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "experiences",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "experience_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("experience_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(255), nullable=False, unique=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_experience_revisions_experience_id", "experience_revisions", ["experience_id"]
    )


def downgrade():
    op.drop_index("ix_experience_revisions_experience_id", "experience_revisions")
    op.drop_table("experience_revisions")
    op.drop_table("experiences")
