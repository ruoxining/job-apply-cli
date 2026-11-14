from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .collectors.normalizer import JobNormalizer
from .config import get_settings
from .db import get_session
from .domain.enums import ApplicationState, RunStatus
from .domain.schemas import CandidateProfile, RawJob, SearchPolicy
from .models import JobPostingRecord
from .services.orchestrator import ApplicationOrchestrator
from .services.policy import PriorityPolicy
from .services.quota import DailyQuotaService


app = FastAPI(title="Job Apply Agent", version="0.2.0")
SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Any, Depends(get_settings)]


class AssessRequest(BaseModel):
    job: RawJob
    profile: CandidateProfile
    policy: SearchPolicy


class CollectionRequest(BaseModel):
    boards: list[dict[str, Any]] | None = None
    profile: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None


def get_dispatcher() -> Callable[[list[dict], dict, dict], Any]:
    def dispatch(boards: list[dict], profile: dict, policy: dict) -> Any:
        from .tasks import collect_configured_boards

        return collect_configured_boards.send(
            boards=boards, profile_payload=profile, policy_payload=policy
        )

    return dispatch


DispatcherDep = Annotated[Callable, Depends(get_dispatcher)]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _state_counts(session: Session) -> dict[str, int]:
    return dict(
        session.execute(
            select(JobPostingRecord.application_state, func.count())
            .group_by(JobPostingRecord.application_state)
            .order_by(JobPostingRecord.application_state)
        ).all()
    )


@app.get("/v1/ops/status")
def operations_status(session: SessionDep, settings: SettingsDep) -> dict:
    now = datetime.now(timezone.utc)
    today_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    useful = (
        JobPostingRecord.eligible.is_(True),
        JobPostingRecord.score >= settings.high_value_threshold,
    )
    jobs_total = session.scalar(select(func.count()).select_from(JobPostingRecord)) or 0
    jobs_today = session.scalar(
        select(func.count()).select_from(JobPostingRecord).where(
            JobPostingRecord.first_seen_at >= today_start
        )
    ) or 0
    useful_total = session.scalar(
        select(func.count()).select_from(JobPostingRecord).where(*useful)
    ) or 0
    useful_today = session.scalar(
        select(func.count()).select_from(JobPostingRecord).where(
            *useful, JobPostingRecord.first_seen_at >= today_start
        )
    ) or 0
    states = _state_counts(session)
    active_states = {ApplicationState.FILLING.value, ApplicationState.SUBMITTING.value}
    active_applications = sum(states.get(state, 0) for state in active_states)
    queued = sum(
        states.get(state, 0)
        for state in {ApplicationState.AUTO_APPLY_QUEUED.value, ApplicationState.MATERIALS_READY.value}
    )
    active_submissions = session.scalar(
        select(func.count()).select_from(JobPostingRecord).where(
            JobPostingRecord.submission_status == RunStatus.RUNNING.value
        )
    ) or 0
    quota = DailyQuotaService(
        fetch_limit=settings.daily_fetch_limit,
        apply_limit=settings.daily_auto_submit_limit,
    ).snapshot(session)
    top = list(
        session.scalars(
            select(JobPostingRecord)
            .where(*useful)
            .order_by(JobPostingRecord.score.desc(), JobPostingRecord.first_seen_at.desc())
            .limit(5)
        )
    )
    return {
        "checked_at": now.isoformat(),
        "service": {"status": "ok", "environment": settings.app_env},
        "database": {"status": "ok", "jobs": jobs_total, "tables": 2},
        "discovery": {
            "jobs_today": jobs_today,
            "useful_threshold": settings.high_value_threshold,
            "useful_jobs_today": useful_today,
            "useful_jobs_total": useful_total,
            "quota": {"fetched": quota.fetched_count, "fetch_limit": quota.fetch_limit},
            "top_useful_jobs": [
                {
                    "company": job.company,
                    "title": job.title,
                    "location": job.location,
                    "source": job.source,
                    "score": job.score,
                    "url": job.url,
                }
                for job in top
            ],
        },
        "applications": {
            "active": active_applications > 0 or active_submissions > 0,
            "active_applications": active_applications,
            "active_submissions": active_submissions,
            "queued": queued,
            "states": states,
        },
    }


@app.get("/v1/quotas/{quota_date}")
def get_quota(quota_date: date, session: SessionDep, settings: SettingsDep) -> dict:
    quota = DailyQuotaService(
        fetch_limit=settings.daily_fetch_limit,
        apply_limit=settings.daily_auto_submit_limit,
    ).snapshot(session, quota_date)
    return {
        "date": quota.quota_date.isoformat(),
        "fetched": quota.fetched_count,
        "fetch_limit": quota.fetch_limit,
        "remaining_fetches": max(0, quota.fetch_limit - quota.fetched_count),
        "applied": quota.applied_count,
        "apply_limit": quota.apply_limit,
        "remaining_applications": max(0, quota.apply_limit - quota.applied_count),
    }


@app.post("/v1/assessments/preview")
def preview_assessment(request: AssessRequest) -> dict:
    normalized = JobNormalizer().normalize(request.job)
    assessment = PriorityPolicy().score(normalized, request.profile, request.policy)
    return {"job": normalized.model_dump(mode="json"), "assessment": assessment.model_dump(mode="json")}


@app.post("/v1/jobs", status_code=201)
def ingest_job(request: AssessRequest, session: SessionDep) -> dict:
    result = ApplicationOrchestrator().ingest(session, request.job, request.profile, request.policy)
    session.commit()
    return result.__dict__


def _job_item(job: JobPostingRecord) -> dict:
    return {
        "job_id": job.id,
        "source": job.source,
        "company": job.company,
        "title": job.title,
        "url": job.url,
        "location": job.location,
        "score": job.score,
        "eligible": job.eligible,
        "state": job.application_state,
        "channel": job.application_channel,
        "created_at": job.created_at.isoformat(),
    }


@app.get("/v1/jobs")
def list_jobs(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    source: str | None = None,
    state: str | None = Query(None, max_length=64),
    min_score: float | None = Query(None, ge=0, le=100),
) -> dict:
    filters = []
    if source:
        filters.append(JobPostingRecord.source == source)
    if state:
        filters.append(JobPostingRecord.application_state == state)
    if min_score is not None:
        filters.append(JobPostingRecord.score >= min_score)
    total = session.scalar(select(func.count()).select_from(JobPostingRecord).where(*filters)) or 0
    rows = list(
        session.scalars(
            select(JobPostingRecord)
            .where(*filters)
            .order_by(JobPostingRecord.first_seen_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return {"total": total, "items": [_job_item(job) for job in rows]}


@app.get("/v1/applications")
def list_applications(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    state: str | None = Query(None, max_length=64),
    channel: str | None = Query(None, max_length=64),
) -> dict:
    filters = []
    if state:
        filters.append(JobPostingRecord.application_state == state)
    if channel:
        filters.append(JobPostingRecord.application_channel == channel)
    total = session.scalar(select(func.count()).select_from(JobPostingRecord).where(*filters)) or 0
    rows = list(
        session.scalars(
            select(JobPostingRecord).where(*filters).order_by(JobPostingRecord.id).limit(limit).offset(offset)
        )
    )
    return {
        "total": total,
        "items": [
            {
                "id": job.id,
                "state": job.application_state,
                "channel": job.application_channel,
                "job": {"company": job.company, "title": job.title, "url": job.url},
                "score": job.score,
            }
            for job in rows
        ],
    }


@app.post("/v1/collections/run")
def run_collections(request: CollectionRequest, session: SessionDep, dispatcher: DispatcherDep) -> dict:
    settings = get_settings()
    boards = request.boards if request.boards is not None else settings.source_boards()
    if not boards:
        raise HTTPException(status_code=400, detail="no source boards configured; pass boards or set SOURCE_BOARDS_JSON")
    profile = request.profile if request.profile is not None else settings.candidate_profile()
    policy = request.policy if request.policy is not None else settings.search_policy()
    try:
        dispatcher(boards, profile, policy)
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=f"queue broker unavailable: {exc}") from exc
    return {"dispatched": len(boards), "boards": [board.get("board_slug") for board in boards]}


@app.get("/v1/applications/{application_id}")
def get_application(application_id: str, session: SessionDep) -> dict:
    job = session.get(JobPostingRecord, application_id)
    if job is None:
        raise HTTPException(status_code=404, detail="application not found")
    return {
        "id": job.id,
        "state": job.application_state,
        "channel": job.application_channel,
        "job": {"company": job.company, "title": job.title, "url": job.url},
        "assessment": {
            "score": job.score,
            "breakdown": job.score_breakdown,
            "reasons": job.ranking_reasons,
        },
        "submission": {
            "status": job.submission_status,
            "last_error": job.last_error,
            "receipt": job.receipt,
        },
    }
