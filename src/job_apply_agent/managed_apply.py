from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from job_apply_agent.config import get_settings
from job_apply_agent.db import get_session_factory
from job_apply_agent.domain.enums import ApplicationState
from job_apply_agent.models import JobPostingRecord
from job_apply_agent.services.review_actions import ReviewActionService


class ManagedApplicationError(RuntimeError):
    """A server-owned run cannot be safely queued from its current state."""


@dataclass(frozen=True)
class ManagedApplicationDispatch:
    application_id: str
    action: str
    state: str
    idempotency_key: str | None


def dispatch_managed_application(
    application_id: str,
    *,
    session_factory=None,
    enqueue_run: Callable[[str, str], object] | None = None,
    enqueue_prepare: Callable[[str, dict], object] | None = None,
) -> ManagedApplicationDispatch:
    """Queue one complete worker-owned application run and return immediately.

    The Dramatiq worker owns every browser/model step after this durable queue
    handoff. Terminal results are persisted in PostgreSQL and delivered by the
    always-on Telegram service; the caller must not orchestrate with polling.
    """
    factory = session_factory or get_session_factory()
    settings = get_settings()
    if not settings.allow_live_submit:
        raise ManagedApplicationError("ALLOW_LIVE_SUBMIT=false; managed dispatch is disabled")
    configuration_error = settings.cua_configuration_error()
    if configuration_error:
        raise ManagedApplicationError(configuration_error)

    with factory() as session:
        job = session.get(JobPostingRecord, application_id)
        if job is None:
            raise ManagedApplicationError(f"application {application_id} was not found")
        if job.submit_started_at or job.submit_completed_at:
            raise ManagedApplicationError(
                "submit was already claimed; reconcile the existing outcome instead of retrying"
            )

        action = "run"
        idempotency_key: str | None = None
        if job.application_state == ApplicationState.NEEDS_HUMAN.value:
            existing_key = job.submission_key
            decision = ReviewActionService().retry_application(session, application_id)
            if not decision.accepted:
                raise ManagedApplicationError(decision.message)
            action = decision.next_action or "run"
            idempotency_key = existing_key or decision.idempotency_key
        elif job.application_state in {
            ApplicationState.MATERIALS_READY.value,
            ApplicationState.FILLING.value,
        }:
            if not job.resume_sha256 or not (job.resume_pdf_path or job.resume_docx_path):
                raise ManagedApplicationError("application has no locked resume artifact")
            idempotency_key = job.submission_key or (
                f"managed:{application_id}:{job.resume_sha256}"
            )
            job.telegram_result_notified_at = None
        elif job.application_state == ApplicationState.AUTO_APPLY_QUEUED.value:
            action = "prepare"
        else:
            raise ManagedApplicationError(
                f"application cannot be managed from state {job.application_state}"
            )
        state = job.application_state
        session.commit()

    if action == "prepare":
        if enqueue_prepare is None:
            from job_apply_agent.tasks import prepare_application

            enqueue_prepare = prepare_application.send
        enqueue_prepare(application_id, settings.candidate_profile())
    else:
        if not idempotency_key:
            raise ManagedApplicationError("managed run has no idempotency key")
        if enqueue_run is None:
            from job_apply_agent.tasks import run_application

            enqueue_run = run_application.send
        enqueue_run(application_id, idempotency_key)

    return ManagedApplicationDispatch(
        application_id=application_id,
        action=action,
        state=state,
        idempotency_key=idempotency_key,
    )
