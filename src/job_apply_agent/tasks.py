from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from sqlalchemy import select

from .ats import AshbyAdapter, ATSBlocked, GreenhouseAdapter, LeverAdapter
from .ats.answer_binding import job_region, resolve_scoped_answer
from .ats.codex_planner import CodexCLIPlanner
from .ats.cua_harness import (
    CuaBrowserSession,
    FormReadiness,
    GLMActionPlanner,
    PlannerStopped,
    UnsafeComputerAction,
    fill_application_with_driver,
    find_linux_browser_pid,
)
from .ats.form_preparation import FormPreparation
from .ats.hybrid_harness import fill_application_hybrid
from .ats.lcu_harness import LcuBrowserSession
from .collectors import AshbySource, BoardConfig, GreenhouseSource, LeverSource
from .config import get_settings
from .db import get_session_factory
from .domain.enums import ApplicationState, ATSKind, RunStatus, Sensitivity
from .domain.schemas import (
    AnswerBankEntry,
    CandidateProfile,
    JobPosting,
    RawJob,
    ResolvedAnswer,
    SearchPolicy,
)
from .experience.library import load_experiences
from .experience.reasoning import AnswerReasoner
from .models import AnswerBankRecord, JobPostingRecord
from .repository import SubmissionRepository
from .services.answers import AnswerResolver
from .services.codex_narratives import CodexNarrativeComposer
from .services.model_errors import TransientModelError
from .services.narratives import (
    GLMNarrativeComposer,
    NarrativeCompositionError,
    is_narrative_question,
    narrative_question_key,
    validate_saved_narrative,
)
from .services.orchestrator import ApplicationOrchestrator
from .services.quota import DailyQuotaService, QuotaExceeded
from .services.resumes import ResumeVariantService

log = logging.getLogger(__name__)
settings = get_settings()
broker = RedisBroker(url=settings.redis_url)
dramatiq.set_broker(broker)

SOURCE_TYPES = {
    ATSKind.GREENHOUSE: GreenhouseSource,
    ATSKind.LEVER: LeverSource,
    ATSKind.ASHBY: AshbySource,
}
ADAPTER_TYPES = {
    ATSKind.GREENHOUSE.value: GreenhouseAdapter,
    ATSKind.LEVER.value: LeverAdapter,
    ATSKind.ASHBY.value: AshbyAdapter,
}


def _resolve_form_answers(questions: list, entries: list[AnswerBankEntry]) -> list[ResolvedAnswer]:
    resolver = AnswerResolver()
    answers: list[ResolvedAnswer] = []
    for question in questions:
        if question.semantic_key == "resume_upload":
            answers.append(ResolvedAnswer(field_id=question.field_id, value="UPLOAD_RESUME"))
        elif is_narrative_question(question):
            answers.append(
                ResolvedAnswer(
                    field_id=question.field_id,
                    value=None,
                    source_key="fact_grounded_narrative",
                    reason="awaiting fact-grounded narrative composition",
                )
            )
        else:
            answers.append(resolver.resolve(question, entries))
    return answers


@dramatiq.actor(max_retries=3, min_backoff=10_000, max_backoff=300_000)
def collect_board(
    ats: str,
    board_slug: str,
    profile_payload: dict,
    policy_payload: dict,
    max_jobs: int | None = None,
    source_run_id: str | None = None,
    title_keywords: list[str] | None = None,
    location_keywords: list[str] | None = None,
) -> int:
    """Collect one board; run details are structured logs, not database rows."""
    source_type = SOURCE_TYPES[ATSKind(ats)]

    async def fetch_jobs() -> list[RawJob]:
        source = source_type(board_slug)
        try:
            return await source.fetch()
        finally:
            await source.close()

    log.info("collection_started ats=%s board=%s", ats, board_slug)
    jobs = asyncio.run(fetch_jobs())
    jobs = _filter_jobs(jobs, title_keywords or [], location_keywords or [])
    fetch_limit = settings.daily_fetch_limit
    limit = min(max_jobs or fetch_limit, fetch_limit)
    jobs = jobs[:limit]
    profile = CandidateProfile.model_validate(profile_payload)
    policy = SearchPolicy.model_validate(policy_payload)
    fetched = 0
    with get_session_factory()() as session:
        orchestrator = ApplicationOrchestrator()
        quota = DailyQuotaService(
            fetch_limit=fetch_limit,
            apply_limit=settings.daily_auto_submit_limit,
        )
        prepared_job_ids: list[str] = []
        for raw in jobs:
            if not quota.has_fetch_capacity(session):
                break
            result = orchestrator.ingest(session, raw, profile, policy)
            if result.created and result.state in {
                ApplicationState.AUTO_APPLY_QUEUED.value,
                ApplicationState.AWAITING_RESUME_REVIEW.value,
            }:
                prepared_job_ids.append(result.job_id)
            fetched += 1
        session.commit()
    for job_id in prepared_job_ids:
        prepare_application.send(job_id, profile_payload)
    log.info("collection_completed ats=%s board=%s fetched=%s", ats, board_slug, fetched)
    return fetched


def _filter_jobs(
    jobs: list[RawJob], title_keywords: list[str], location_keywords: list[str]
) -> list[RawJob]:
    title_terms = [term.casefold() for term in title_keywords if term.strip()]
    location_terms = [term.casefold() for term in location_keywords if term.strip()]
    return [
        job
        for job in jobs
        if (not title_terms or any(term in job.title.casefold() for term in title_terms))
        and (not location_terms or any(term in job.location.casefold() for term in location_terms))
    ]


@dramatiq.actor
def collect_configured_boards(
    boards: list[dict], profile_payload: dict, policy_payload: dict, run_id: str | None = None
) -> int:
    configs = [
        BoardConfig(
            ats=ATSKind(board["ats"]),
            board_slug=board["board_slug"],
            enabled=board.get("enabled", True),
            max_jobs=board.get("max_jobs"),
            title_keywords=tuple(board.get("title_keywords", [])),
            location_keywords=tuple(board.get("location_keywords", [])),
        )
        for board in boards
    ]
    dispatched = 0
    for config in (item for item in configs if item.enabled):
        collect_board.send(
            config.ats.value,
            config.board_slug,
            profile_payload,
            policy_payload,
            config.max_jobs,
            None,
            list(config.title_keywords),
            list(config.location_keywords),
        )
        dispatched += 1
    log.info("collection_batch_dispatched boards=%s", dispatched)
    return dispatched


class DatabaseSubmissionClaim:
    """Durably records submit intent immediately before the ATS request is sent."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.repository = SubmissionRepository()

    def claim(self) -> bool:
        with get_session_factory()() as session:
            claimed = self.repository.claim_submit(session, self.job_id)
            session.commit()
            return claimed

    def complete(self, receipt: dict) -> None:
        with get_session_factory()() as session:
            self.repository.complete_submit(session, self.job_id, receipt)
            job = session.get(JobPostingRecord, self.job_id)
            if job is None:
                raise LookupError(f"application {self.job_id} not found")
            job.application_state = ApplicationState.SUBMITTED.value
            session.commit()


@dramatiq.actor(max_retries=3, min_backoff=10_000, max_backoff=300_000)
def prepare_application(application_id: str, profile_payload: dict) -> str:
    """Create a fact-grounded temporary resume, then route review or auto-submit."""
    should_enqueue = False
    idempotency_key: str | None = None
    with get_session_factory()() as session:
        job = session.get(JobPostingRecord, application_id)
        if job is None:
            raise LookupError(f"application {application_id} not found")
        if job.application_state not in {
            ApplicationState.AUTO_APPLY_QUEUED.value,
            ApplicationState.AWAITING_RESUME_REVIEW.value,
            ApplicationState.MATERIALS_READY.value,
        }:
            return job.application_state
        profile = CandidateProfile.model_validate(profile_payload)
        if not job.resume_sha256:
            normalized = dict(job.normalized or {})
            normalized.pop("raw_snapshot", None)
            ResumeVariantService(settings.artifacts_dir, provided_file=settings.resume_file).create(
                session,
                job_id=job.id,
                job=JobPosting.model_validate(normalized),
                facts=profile.facts,
            )
        if job.application_state == ApplicationState.AWAITING_RESUME_REVIEW.value:
            session.commit()
            return job.application_state
        if job.application_state == ApplicationState.AUTO_APPLY_QUEUED.value:
            quota = DailyQuotaService(
                fetch_limit=settings.daily_fetch_limit,
                apply_limit=settings.daily_auto_submit_limit,
            )
            try:
                quota.claim_application(session, job)
            except QuotaExceeded:
                job.application_state = ApplicationState.SKIPPED.value
                job.blocker = {
                    "kind": "daily_application_quota_exhausted",
                    "detail": "The daily automatic application limit has been reached.",
                }
                session.commit()
                return job.application_state
            job.application_state = ApplicationState.MATERIALS_READY.value
        should_enqueue = True
        idempotency_key = f"auto:{job.id}:{job.resume_sha256}"
        session.commit()
    if should_enqueue and idempotency_key:
        run_application.send(application_id, idempotency_key)
        return ApplicationState.MATERIALS_READY.value
    return ApplicationState.NEEDS_HUMAN.value


@dramatiq.actor(max_retries=2, min_backoff=30_000, max_backoff=300_000)
def run_application(application_id: str, idempotency_key: str) -> str:
    return asyncio.run(_run_application(application_id, idempotency_key))


async def _run_application(application_id: str, idempotency_key: str) -> str:
    with get_session_factory()() as session:
        job = session.get(JobPostingRecord, application_id)
        if job is None:
            raise LookupError(f"application {application_id} not found")
        if job.application_state not in {
            ApplicationState.MATERIALS_READY.value,
            ApplicationState.NEEDS_HUMAN.value,
            # A redelivered run after a mid-fill crash resumes safely: the
            # durable submit claim below rejects any run whose click already
            # happened, so only unclaimed fills can restart here.
            ApplicationState.FILLING.value,
        }:
            raise ValueError(f"application cannot run from state {job.application_state}")
        if not job.resume_sha256 or not (job.resume_pdf_path or job.resume_docx_path):
            raise ValueError("application has no locked resume artifact")
        live_error = settings.live_submission_error()
        if live_error:
            job.application_state = ApplicationState.NEEDS_HUMAN.value
            job.submission_status = RunStatus.NEEDS_HUMAN.value
            job.blocker = {
                "kind": (
                    "live_submit_disabled"
                    if not settings.allow_live_submit
                    else "live_submit_unacknowledged"
                ),
                "detail": live_error,
            }
            session.commit()
            return RunStatus.NEEDS_HUMAN.value
        cua_config_error = settings.cua_configuration_error()
        if cua_config_error:
            job.application_state = ApplicationState.NEEDS_HUMAN.value
            job.submission_status = RunStatus.NEEDS_HUMAN.value
            job.blocker = {
                "kind": "computer_use_not_configured",
                "detail": cua_config_error,
            }
            session.commit()
            return RunStatus.NEEDS_HUMAN.value
        if job.source not in ADAPTER_TYPES:
            job.application_state = ApplicationState.NEEDS_HUMAN.value
            job.submission_status = RunStatus.NEEDS_HUMAN.value
            job.blocker = {
                "kind": "unsupported_ats",
                "detail": f"No safe form adapter is configured for source {job.source}",
            }
            session.commit()
            return RunStatus.NEEDS_HUMAN.value
        run = SubmissionRepository().get_or_create_run(
            session, application_id=application_id, idempotency_key=idempotency_key
        )
        if run.submit_completed_at:
            return RunStatus.SUBMITTED.value
        if run.submit_started_at:
            job.application_state = ApplicationState.NEEDS_HUMAN.value
            job.blocker = {
                "kind": "submit_outcome_unknown",
                "detail": "submit was claimed previously; reconcile receipt manually",
            }
            session.commit()
            return RunStatus.NEEDS_HUMAN.value
        job.application_state = ApplicationState.FILLING.value
        job.blocker = None
        job.last_error = None
        session.commit()
        job_url = job.url
        if job.source == ATSKind.GREENHOUSE.value:
            job_url = f"https://boards.greenhouse.io/embed/job_app?token={job.source_id}"
        ats = job.source
        resume_filename = job.resume_pdf_path or job.resume_docx_path
        if resume_filename is None:
            raise ValueError("application has no locked resume artifact")
        resume_path = Path(resume_filename)
        answer_rows = list(session.scalars(select(AnswerBankRecord)))
        profile = CandidateProfile.model_validate(settings.candidate_profile())
        experiences = (
            load_experiences(profile, settings.experience_library_path, session)
            if settings.experience_reasoning_enabled
            else []
        )
        normalized_job = dict(job.normalized or {})
        normalized_job.pop("raw_snapshot", None)
        job_posting = JobPosting.model_validate(normalized_job)
        saved_form_answers = dict(job.form_answers or {})

    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright

    from job_apply_agent.ats.page_readiness import FieldBindingError

    adapter = ADAPTER_TYPES[ats](settings)
    settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
    browser_profile_dir = settings.browser_profile_dir.resolve()
    async with async_playwright() as playwright:
        context = None
        try:
            browser_args = ["--force-renderer-accessibility"]
            lcu_mode = settings.cua_enabled and settings.cua_backend == "lcu"
            if lcu_mode:
                # Keep native key routing out of Chromium's omnibox.
                browser_args.append(f"--app={job_url}")
            if settings.cua_enabled:
                # CUA Driver re-proves that this loopback endpoint belongs to
                # the exact browser process before granting any capability.
                browser_args.append("--remote-debugging-port=0")
            context = await playwright.chromium.launch_persistent_context(
                str(browser_profile_dir),
                channel=settings.browser_channel,
                headless=settings.browser_headless,
                args=browser_args,
                ignore_default_args=["about:blank"] if lcu_mode else None,
            )
            browser_executable = Path(playwright.chromium.executable_path)
            browser_pid = (
                find_linux_browser_pid(
                    executable=browser_executable,
                    profile_dir=browser_profile_dir,
                )
                if settings.cua_enabled
                else None
            )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(job_url, wait_until="domcontentloaded")
            await adapter.wait_until_ready(page)
            inspection = await adapter.inspect(page)
        except PlaywrightError as exc:
            if context is not None:
                await context.close()
            return _pause(application_id, "browser_error", str(exc))
        if inspection.blocker:
            await context.close()
            return _pause(application_id, inspection.blocker.value, inspection.blocker_detail or "")

        entries = [
            AnswerBankEntry(
                semantic_key=row.semantic_key,
                answer=row.answer,
                regions=row.regions,
                sensitivity=Sensitivity(row.sensitivity),
                valid_until=row.valid_until,
            )
            for row in answer_rows
        ]
        answers = _resolve_form_answers(inspection.questions, entries)
        blocker = next((answer for answer in answers if answer.requires_human), None)
        use_cua = settings.cua_enabled
        if blocker and not use_cua:
            await context.close()
            return _pause(
                application_id,
                blocker.blocker.value if blocker.blocker else "unknown_question",
                blocker.reason or "",
            )
        codex_preparation = None
        try:
            if use_cua:
                # Resolve standard ATS fields without model calls. CUA Driver
                # handles entry gates, dynamic widgets, and fields that only
                # appear after the first render.
                if settings.cua_backend != "codex":
                    await adapter.fill(
                        page,
                        [answer for answer in answers if not answer.requires_human],
                        resume_path=resume_path,
                    )
                deterministically_filled = {
                    answer.field_id
                    for answer in answers
                    if not answer.requires_human
                    and answer.value is not None
                    and settings.cua_backend != "codex"
                }

                narrative_records = dict(saved_form_answers.get("narratives") or {})
                narrative_composer = (
                    CodexNarrativeComposer(
                        binary=settings.codex_binary,
                        model=settings.codex_model,
                        timeout_seconds=settings.cua_step_timeout_seconds,
                    )
                    if settings.cua_backend == "codex"
                    else GLMNarrativeComposer(
                        base_url=settings.glm_base_url,
                        api_key=settings.glm_api_key or "",
                        model=settings.glm_model,
                        timeout_seconds=settings.cua_step_timeout_seconds,
                        reasoning_effort=settings.glm_reasoning_effort,
                    )
                )

                reasoner = (
                    AnswerReasoner(
                        CodexCLIPlanner(
                            binary=settings.codex_binary,
                            model=settings.codex_model,
                            timeout_seconds=settings.cua_step_timeout_seconds,
                        ),
                        experiences,
                        entries,
                        mode=settings.experience_retrieval_mode,
                        top_k=settings.experience_retrieval_top_k,
                        auto_char_threshold=settings.experience_auto_char_threshold,
                        cache_path=settings.experience_cache_path,
                    )
                    if settings.experience_reasoning_enabled and settings.cua_backend == "codex"
                    else None
                )

                async def resolve_with_narratives(questions: list) -> list[ResolvedAnswer]:
                    resolved = _resolve_form_answers(questions, entries)
                    by_field = {answer.field_id: answer for answer in resolved}
                    for question in questions:
                        if reasoner is not None:
                            if question.semantic_key == "resume_upload":
                                continue
                            scoped = resolve_scoped_answer(
                                question,
                                adapter._field_bindings.get(question.field_id, {}),
                                entries,
                                job_country=job_region(job_posting.country, job_posting.location),
                            )
                            if scoped.value is not None and not scoped.requires_human:
                                by_field[question.field_id] = scoped
                                continue
                            by_field[question.field_id] = await reasoner.resolve(
                                question,
                                region=job_region(job_posting.country, job_posting.location),
                                job_context={
                                    "company": job_posting.company,
                                    "title": job_posting.title,
                                    "country": job_posting.country,
                                    "location": job_posting.location,
                                    "description": job_posting.description,
                                },
                            )
                            if reasoner.records:
                                _save_reasoning_record(application_id, reasoner.records[-1])
                            continue
                        if not is_narrative_question(question):
                            continue
                        key = narrative_question_key(question)
                        saved = narrative_records.get(key)
                        if saved:
                            try:
                                narrative = validate_saved_narrative(
                                    saved,
                                    facts=profile.facts,
                                    maximum=question.max_length,
                                )
                            except NarrativeCompositionError:
                                log.warning(
                                    "discarding_invalid_saved_narrative "
                                    "application_id=%s question_key=%s",
                                    application_id,
                                    key,
                                )
                                saved = None
                        if not saved:
                            narrative = await narrative_composer.compose(
                                question=question,
                                job=job_posting,
                                facts=profile.facts,
                            )
                            saved = {
                                "question": question.label,
                                "answer": narrative.value,
                                "fact_ids": list(narrative.fact_ids),
                                "model": narrative.model,
                            }
                            narrative_records[key] = saved
                            _save_narrative_answer(application_id, key, saved)
                            log.info(
                                "fact_grounded_narrative_composed "
                                "application_id=%s question_key=%s fact_count=%s",
                                application_id,
                                key,
                                len(narrative.fact_ids),
                            )
                        by_field[question.field_id] = ResolvedAnswer(
                            field_id=question.field_id,
                            value=narrative.value,
                            source_key=f"fact_grounded_narrative:{key}",
                        )
                    return [by_field[question.field_id] for question in questions]

                async def prepare_and_check_form() -> FormReadiness:
                    await adapter.wait_until_ready(page)
                    current = await adapter.inspect(page)
                    if current.blocker:
                        raise ATSBlocked(
                            current.blocker,
                            current.blocker_detail or "browser safety blocker appeared",
                        )
                    current_answers = await resolve_with_narratives(current.questions)
                    newly_resolved = [
                        answer
                        for answer in current_answers
                        if not answer.requires_human
                        and answer.value is not None
                        and answer.field_id not in deterministically_filled
                    ]
                    if newly_resolved:
                        await adapter.fill(page, newly_resolved, resume_path=resume_path)
                        deterministically_filled.update(
                            answer.field_id for answer in newly_resolved
                        )
                        log.info(
                            "deterministic_dynamic_fill application_id=%s fields=%s",
                            application_id,
                            len(newly_resolved),
                        )
                    issues = await adapter.readiness_issues(page)
                    return FormReadiness(ready=not issues, issues=tuple(issues))

                answer_lines = [
                    f"{entry.semantic_key} [{', '.join(entry.regions) or 'all regions'}]: "
                    f"{entry.answer}"
                    for entry in entries
                ]
                if settings.cua_backend == "codex":
                    codex_preparation = FormPreparation(
                        page,
                        adapter,
                        resolve_with_narratives,
                        resume_path=resume_path,
                        entries=entries,
                        question_context=f"{ats}|{job_posting.company}|{job_posting.country or ''}|{job_posting.location}",
                        job_country=job_posting.country,
                        job_location=job_posting.location,
                    )

                async def attach_driver():
                    return await CuaBrowserSession.attach(
                        expected_url=page.url,
                        application_id=application_id,
                        timeout_seconds=settings.cua_driver_attach_timeout_seconds,
                        input_route=settings.cua_browser_input_route,
                        step_timeout_seconds=settings.cua_step_timeout_seconds,
                        browser_pid=browser_pid,
                        browser_executable=browser_executable,
                    )

                async def run_legacy_planner():
                    if settings.cua_backend == "lcu":
                        browser = await LcuBrowserSession.attach(
                            page=page,
                            browser_pid=browser_pid,
                            prefix=settings.lcu_prefix,
                            node_path=settings.lcu_node_path,
                            timeout_seconds=settings.cua_step_timeout_seconds,
                        )
                    else:
                        browser = await attach_driver()
                    log.info(
                        "application_browser_attached application_id=%s backend=%s",
                        application_id,
                        settings.cua_backend,
                    )
                    try:
                        planner = GLMActionPlanner(
                            base_url=settings.glm_base_url,
                            api_key=settings.glm_api_key or "",
                            model=settings.glm_model,
                            timeout_seconds=settings.cua_step_timeout_seconds,
                            reasoning_effort=settings.glm_reasoning_effort,
                        )
                        try:
                            await fill_application_with_driver(
                                browser,
                                planner=planner,
                                max_steps=settings.cua_max_steps,
                                candidate_instructions=settings.cua_instructions,
                                answer_lines=answer_lines,
                                approved_fill_values=[entry.answer for entry in entries],
                                readiness_check=prepare_and_check_form,
                            )
                        finally:
                            await planner.close()
                    finally:
                        await browser.close()

                try:
                    if settings.cua_backend == "codex":
                        planner = CodexCLIPlanner(
                            binary=settings.codex_binary,
                            model=settings.codex_model,
                            timeout_seconds=settings.cua_step_timeout_seconds,
                        )
                        await planner.check_ready()
                        try:
                            if codex_preparation is None:
                                raise UnsafeComputerAction("Codex form preparation is unavailable")
                            hybrid_result = await fill_application_hybrid(
                                page,
                                planner=planner,
                                max_steps=settings.cua_max_steps,
                                candidate_instructions=settings.cua_instructions,
                                answer_lines=answer_lines,
                                approved_fill_values=[entry.answer for entry in entries],
                                readiness_check=codex_preparation,
                                driver_factory=attach_driver
                                if settings.codex_driver_fallback
                                else None,
                                action_timeout_ms=settings.codex_action_timeout_seconds * 1000,
                            )
                            log.info(
                                "codex_hybrid_completed application_id=%s trace=%s",
                                application_id,
                                hybrid_result.trace,
                            )
                        finally:
                            await planner.close()
                    else:
                        await run_legacy_planner()
                finally:
                    await narrative_composer.close()
            else:
                await adapter.fill(page, answers, resume_path=resume_path)
            with get_session_factory()() as session:
                job = session.get(JobPostingRecord, application_id)
                if job is None:
                    raise LookupError(f"application {application_id} not found")
                job.application_state = ApplicationState.SUBMITTING.value
                session.commit()
            result = await adapter.submit(page, DatabaseSubmissionClaim(application_id))
            return RunStatus.SUBMITTED.value if result.submitted else RunStatus.NEEDS_HUMAN.value
        except ATSBlocked as exc:
            extra = {"evidence": exc.evidence} if exc.evidence else None
            return _pause(application_id, exc.kind.value, exc.detail, extra=extra)
        except TransientModelError as exc:
            return _pause(
                application_id,
                "transient_model_error",
                str(exc),
                extra={
                    "reason_code": exc.reason_code,
                    "retryable": True,
                    "retry_after_seconds": exc.retry_after_seconds,
                },
            )
        except PlannerStopped as exc:
            return _pause(
                application_id,
                "computer_use_stopped",
                str(exc),
                extra={
                    "reason_code": exc.reason_code,
                    "planner_trace": exc.trace,
                    "missing_questions": codex_preparation.missing_questions
                    if codex_preparation
                    else [],
                },
            )
        except NarrativeCompositionError as exc:
            return _pause(application_id, "narrative_generation_failed", str(exc))
        except UnsafeComputerAction as exc:
            return _pause(application_id, "computer_use_stopped", str(exc))
        except (PlaywrightError, FieldBindingError) as exc:
            return _pause(application_id, "browser_error", str(exc))
        finally:
            await context.close()


def _pause(
    application_id: str,
    kind: str,
    detail: str,
    *,
    extra: dict | None = None,
) -> str:
    with get_session_factory()() as session:
        job = session.get(JobPostingRecord, application_id)
        if job is None:
            raise LookupError(f"application {application_id} not found")
        job.application_state = ApplicationState.NEEDS_HUMAN.value
        job.submission_status = RunStatus.NEEDS_HUMAN.value
        job.blocker = {"kind": kind, "detail": detail, **(extra or {})}
        session.commit()
    return RunStatus.NEEDS_HUMAN.value


def _save_narrative_answer(application_id: str, key: str, record: dict) -> None:
    with get_session_factory()() as session:
        job = session.get(JobPostingRecord, application_id)
        if job is None:
            raise LookupError(f"application {application_id} not found")
        current = dict(job.form_answers or {})
        narratives = dict(current.get("narratives") or {})
        narratives[key] = record
        job.form_answers = {**current, "narratives": narratives}
        session.commit()


def _save_reasoning_record(application_id: str, record: dict) -> None:
    with get_session_factory()() as session:
        job = session.get(JobPostingRecord, application_id)
        if job is not None:
            answers = dict(job.form_answers or {})
            records = dict(answers.get("reasoning") or {})
            records[record["key"]] = record
            job.form_answers = {**answers, "reasoning": records}
            session.commit()
