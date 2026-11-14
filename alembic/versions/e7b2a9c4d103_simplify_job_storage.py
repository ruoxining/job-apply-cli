"""collapse job lifecycle into job_postings

Revision ID: e7b2a9c4d103
Revises: c4a8f8b14f21
"""

from __future__ import annotations

import hashlib
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "e7b2a9c4d103"
down_revision: Union[str, None] = "c4a8f8b14f21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _key(company: str, title: str, location: str, description: str) -> str:
    folded = "\x1f".join(
        " ".join((value or "").strip().casefold().split())
        for value in (company, title, location, description)
    )
    return hashlib.sha256(folded.encode()).hexdigest()


def upgrade() -> None:
    columns = [
        sa.Column("canonical_key", sa.String(64)),
        sa.Column("sources", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("raw_sha256", sa.String(64)),
        sa.Column("first_seen_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("eligible", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("score", sa.Float(), server_default="0", nullable=False),
        sa.Column("score_breakdown", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("ranking_reasons", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("ranker_version", sa.String(64)),
        sa.Column("ranked_at", sa.DateTime(timezone=True)),
        sa.Column("application_channel", sa.String(32), server_default="auto", nullable=False),
        sa.Column("application_state", sa.String(40), server_default="discovered", nullable=False),
        sa.Column("apply_claimed_on", sa.Date()),
        sa.Column("form_answers", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("blocker", sa.JSON()),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("resume_template", sa.String(100)),
        sa.Column("resume_fact_ids", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("resume_validation_errors", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("resume_docx_path", sa.Text()),
        sa.Column("resume_pdf_path", sa.Text()),
        sa.Column("resume_sha256", sa.String(64)),
        sa.Column("resume_generated_at", sa.DateTime(timezone=True)),
        sa.Column("resume_expires_at", sa.DateTime(timezone=True)),
        sa.Column("resume_purged_at", sa.DateTime(timezone=True)),
        sa.Column("submission_key", sa.String(128)),
        sa.Column("submission_status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("receipt", sa.JSON()),
        sa.Column("submit_started_at", sa.DateTime(timezone=True)),
        sa.Column("submit_completed_at", sa.DateTime(timezone=True)),
    ]
    for column in columns:
        op.add_column("job_postings", column)

    bind = op.get_bind()
    jobs = bind.execute(
        sa.text(
            "SELECT id, company, title, location, source, source_id, url, normalized, created_at, updated_at FROM job_postings ORDER BY created_at, id"
        )
    ).mappings()
    used: set[str] = set()
    for job in jobs:
        normalized = job["normalized"] or {}
        if isinstance(normalized, str):
            import json

            normalized = json.loads(normalized)
        canonical = _key(
            job["company"], job["title"], job["location"], normalized.get("description", "")
        )
        if canonical in used:
            canonical = hashlib.sha256(f"{canonical}:{job['id']}".encode()).hexdigest()
        used.add(canonical)
        bind.execute(
            sa.text("""
                UPDATE job_postings
                SET canonical_key=:canonical,
                    sources=:sources,
                    first_seen_at=created_at,
                    last_seen_at=updated_at
                WHERE id=:id
            """).bindparams(sa.bindparam("sources", type_=sa.JSON())),
            {
                "canonical": canonical,
                "sources": [
                    {"source": job["source"], "source_id": job["source_id"], "url": job["url"]}
                ],
                "id": job["id"],
            },
        )

    bind.execute(
        sa.text("""
        UPDATE job_postings SET
          eligible=COALESCE((SELECT eligible FROM priority_assessments p WHERE p.job_id=job_postings.id ORDER BY p.updated_at DESC LIMIT 1), eligible),
          score=COALESCE((SELECT score FROM priority_assessments p WHERE p.job_id=job_postings.id ORDER BY p.updated_at DESC LIMIT 1), score),
          score_breakdown=COALESCE((SELECT breakdown FROM priority_assessments p WHERE p.job_id=job_postings.id ORDER BY p.updated_at DESC LIMIT 1), score_breakdown),
          ranking_reasons=COALESCE((SELECT reasons FROM priority_assessments p WHERE p.job_id=job_postings.id ORDER BY p.updated_at DESC LIMIT 1), ranking_reasons),
          ranked_at=(SELECT updated_at FROM priority_assessments p WHERE p.job_id=job_postings.id ORDER BY p.updated_at DESC LIMIT 1),
          application_channel=COALESCE((SELECT channel FROM applications a WHERE a.job_id=job_postings.id ORDER BY a.updated_at DESC LIMIT 1), application_channel),
          application_state=COALESCE((SELECT state FROM applications a WHERE a.job_id=job_postings.id ORDER BY a.updated_at DESC LIMIT 1), application_state),
          form_answers=COALESCE((SELECT form_answers FROM applications a WHERE a.job_id=job_postings.id ORDER BY a.updated_at DESC LIMIT 1), form_answers),
          blocker=(SELECT blocker FROM applications a WHERE a.job_id=job_postings.id ORDER BY a.updated_at DESC LIMIT 1),
          submitted_at=(SELECT submitted_at FROM applications a WHERE a.job_id=job_postings.id ORDER BY a.updated_at DESC LIMIT 1)
    """)
    )
    bind.execute(
        sa.text("""
        UPDATE job_postings SET
          resume_template=(SELECT template FROM resume_variants r WHERE r.job_id=job_postings.id ORDER BY r.updated_at DESC LIMIT 1),
          resume_fact_ids=COALESCE((SELECT fact_ids FROM resume_variants r WHERE r.job_id=job_postings.id ORDER BY r.updated_at DESC LIMIT 1), resume_fact_ids),
          resume_validation_errors=COALESCE((SELECT validation_errors FROM resume_variants r WHERE r.job_id=job_postings.id ORDER BY r.updated_at DESC LIMIT 1), resume_validation_errors),
          resume_docx_path=(SELECT docx_path FROM resume_variants r WHERE r.job_id=job_postings.id ORDER BY r.updated_at DESC LIMIT 1),
          resume_pdf_path=(SELECT pdf_path FROM resume_variants r WHERE r.job_id=job_postings.id ORDER BY r.updated_at DESC LIMIT 1),
          resume_sha256=(SELECT sha256 FROM resume_variants r WHERE r.job_id=job_postings.id ORDER BY r.updated_at DESC LIMIT 1),
          resume_generated_at=(SELECT created_at FROM resume_variants r WHERE r.job_id=job_postings.id ORDER BY r.updated_at DESC LIMIT 1)
    """)
    )
    bind.execute(
        sa.text("""
        UPDATE job_postings SET
          submission_key=(SELECT ar.idempotency_key FROM application_runs ar JOIN applications a ON a.id=ar.application_id WHERE a.job_id=job_postings.id ORDER BY ar.updated_at DESC LIMIT 1),
          submission_status=COALESCE((SELECT ar.status FROM application_runs ar JOIN applications a ON a.id=ar.application_id WHERE a.job_id=job_postings.id ORDER BY ar.updated_at DESC LIMIT 1), submission_status),
          last_error=(SELECT ar.error FROM application_runs ar JOIN applications a ON a.id=ar.application_id WHERE a.job_id=job_postings.id ORDER BY ar.updated_at DESC LIMIT 1),
          receipt=(SELECT ar.receipt FROM application_runs ar JOIN applications a ON a.id=ar.application_id WHERE a.job_id=job_postings.id ORDER BY ar.updated_at DESC LIMIT 1),
          submit_started_at=(SELECT ar.submit_started_at FROM application_runs ar JOIN applications a ON a.id=ar.application_id WHERE a.job_id=job_postings.id ORDER BY ar.updated_at DESC LIMIT 1),
          submit_completed_at=(SELECT ar.submit_completed_at FROM application_runs ar JOIN applications a ON a.id=ar.application_id WHERE a.job_id=job_postings.id ORDER BY ar.updated_at DESC LIMIT 1)
    """)
    )

    for table in (
        "application_runs",
        "applications",
        "resume_variants",
        "priority_assessments",
        "search_policy_versions",
        "profile_facts",
        "daily_quotas",
        "source_runs",
        "discovery_runs",
    ):
        op.drop_table(table)
    # Older ingestion allowed repeated rows for an identical source posting.
    # Keep the most advanced/current row before adding the uniqueness guard.
    bind.execute(
        sa.text("""
        DELETE FROM job_postings WHERE id IN (
          SELECT id FROM (
            SELECT id, ROW_NUMBER() OVER (
              PARTITION BY source, source_id
              ORDER BY CASE application_state
                WHEN 'submitted' THEN 9 WHEN 'submitting' THEN 8 WHEN 'filling' THEN 7
                WHEN 'materials_ready' THEN 6 WHEN 'needs_human' THEN 5
                WHEN 'awaiting_resume_review' THEN 4 WHEN 'auto_apply_queued' THEN 3
                WHEN 'failed' THEN 2 ELSE 1 END DESC,
                updated_at DESC
            ) AS duplicate_rank FROM job_postings
          ) ranked WHERE duplicate_rank > 1
        )
    """)
    )
    with op.batch_alter_table("job_postings") as batch:
        batch.drop_column("raw_snapshot")
        batch.drop_column("possible_duplicate")
        batch.drop_column("duplicate_hint")
        batch.alter_column("canonical_key", nullable=False)
        batch.alter_column("first_seen_at", nullable=False)
        batch.alter_column("last_seen_at", nullable=False)
        batch.create_unique_constraint("uq_job_source_id", ["source", "source_id"])
        batch.create_unique_constraint("uq_job_canonical_key", ["canonical_key"])
        batch.create_unique_constraint("uq_job_submission_key", ["submission_key"])
        batch.create_index("ix_job_postings_canonical_key", ["canonical_key"])
        batch.create_index("ix_job_postings_eligible", ["eligible"])
        batch.create_index("ix_job_postings_application_state", ["application_state"])
        batch.create_index("ix_job_postings_apply_claimed_on", ["apply_claimed_on"])
        batch.create_index("ix_job_postings_resume_expires_at", ["resume_expires_at"])


def downgrade() -> None:
    raise RuntimeError("This data-consolidating migration is intentionally irreversible")
