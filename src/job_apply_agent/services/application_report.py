"""Read-only analysis of durable application outcomes, never browser readiness."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apply_agent.models import JobPostingRecord


def _code(value: Any) -> str:
    # Error details, model output and filled answers must not enter this report.
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value)
        else "unknown"
    )


def _outcome(job: JobPostingRecord) -> tuple[str, str]:
    receipt = job.receipt if isinstance(job.receipt, dict) else {}
    confirmation = receipt.get("confirmation")
    has_confirmation = (
        receipt.get("status") == "submission_confirmed"
        and isinstance(confirmation, str)
        and bool(confirmation.strip())
    )
    if job.submit_completed_at:
        if job.submit_started_at and has_confirmation:
            return "confirmed_submission", "none"
        return "inconsistent_submission_evidence", "reconcile_receipt"
    if receipt.get("status") == "previous_application_confirmed":
        # The ATS can identify an older application only after the guarded click.
        # Preserve that claim, but do not count the older application as a new success
        # or leave a known duplicate outcome classified as an uncertain submission.
        if (
            job.submit_started_at
            and isinstance(confirmation, str)
            and confirmation.strip()
            and not job.submitted_at
            and job.application_state != "submitted"
        ):
            return "existing_application", "respect_reapplication_policy"
        return "inconsistent_submission_evidence", "reconcile_receipt"
    if job.submit_started_at:
        # A live process may still own this claim. Do not call it terminal or retry it.
        return "claim_without_confirmation", "inspect_process_and_reconcile_receipt"
    if job.application_state == "submitted" or job.submitted_at or has_confirmation:
        return "inconsistent_submission_evidence", "reconcile_receipt"
    if job.closed_at:
        return "closed_posting", "none"
    if job.application_state in {"filling", "submitting"}:
        return "execution_state_unverified", "inspect_worker_process"
    if job.application_state in {"auto_apply_queued", "materials_ready"}:
        return "queued", "inspect_queue"
    blocker = job.blocker or {}
    kind = blocker.get("kind")
    if kind == "submit_outcome_unknown":
        return "inconsistent_submission_evidence", "reconcile_receipt"
    if kind in {"captcha", "two_factor", "login_required"}:
        return "external_intervention", "human_action_required"
    if kind == "ats_application_limit":
        return "external_application_limit", "verify_platform_reset"
    if kind in {"unknown_question", "sensitive_answer_missing"}:
        return "unresolved_answer", "inspect_available_facts_and_question_mapping"
    if kind == "transient_model_error":
        return "model_service_failure", "inspect_retry_budget"
    if kind in {"browser_error", "computer_use_stopped", "narrative_generation_failed"}:
        return "pipeline_stopped", "inspect_trace_before_retry"
    if kind in {"live_submit_disabled", "computer_use_not_configured"}:
        return "configuration_blocked", "fix_deployment_configuration"
    if job.application_state == "awaiting_resume_review":
        return "awaiting_review", "review_materials"
    if job.application_state in {"skipped", "ineligible"}:
        return "excluded", "inspect_exclusion_reason"
    if job.application_state in {"needs_human", "failed"}:
        return "unclassified_failure", "inspect_private_evidence"
    return "not_attempted", "none"


def application_report(
    session: Session, *, application_ids: list[str] | None = None
) -> dict[str, Any]:
    statement = select(JobPostingRecord).order_by(JobPostingRecord.id)
    if application_ids is not None:
        if not application_ids:
            raise ValueError("application scope must not be empty")
        statement = statement.where(JobPostingRecord.id.in_(application_ids))
    jobs = list(session.scalars(statement))
    if application_ids is not None:
        missing = set(application_ids) - {job.id for job in jobs}
        if missing:
            raise ValueError(f"application scope contains {len(missing)} missing job(s)")
    outcomes: Counter[str] = Counter()
    blockers: Counter[str] = Counter()
    by_source: dict[str, Counter[str]] = defaultdict(Counter)
    items = []
    for job in jobs:
        outcome, action = _outcome(job)
        blocker = job.blocker or {}
        kind = _code(blocker.get("kind")) if blocker else None
        outcomes[outcome] += 1
        by_source[_code(job.source)][outcome] += 1
        if kind:
            blockers[kind] += 1
        items.append(
            {
                "application_id": job.id,
                "source": _code(job.source),
                "state": _code(job.application_state),
                "outcome": outcome,
                "next_action": action,
                "blocker_kind": kind,
                "reason_code": _code(blocker["reason_code"])
                if blocker.get("reason_code")
                else None,
                "missing_question_count": len(blocker.get("missing_questions") or []),
                "submit_claimed": job.submit_started_at is not None,
                "submit_completed": job.submit_completed_at is not None,
                "updated_at": job.updated_at.isoformat(),
            }
        )
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "explicit_job_ids" if application_ids is not None else "entire_database",
        "unit": "unique_jobs_latest_durable_state",
        "total": len(jobs),
        "outcomes": dict(sorted(outcomes.items())),
        "blockers": dict(sorted(blockers.items())),
        "by_source": {key: dict(sorted(value.items())) for key, value in sorted(by_source.items())},
        "limits": [
            "Confirmed means a persisted ATS confirmation plus submit timestamps; full-form semantics are not audited here.",
            "Existing applications are ATS-confirmed historical submissions, not new submissions in this run.",
            "Database execution states do not prove a process is live; inspect worker/queue handles separately.",
            "Latest job rows are not an attempt history or a pipeline comparison.",
            "Unresolved answers can be mapping or evidence gaps; they are not automatically missing user facts.",
        ],
        "items": items,
    }
