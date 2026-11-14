from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from job_apply_agent.domain.enums import ApplicationState, BlockerKind
from job_apply_agent.models import JobPostingRecord

from .quota import DailyQuotaService, QuotaExceeded


@dataclass(frozen=True)
class ApplyDecision:
    accepted: bool
    message: str
    idempotency_key: str | None = None
    # Set when accepting the decision requires enqueueing work afterwards:
    # "run" resumes submission with `idempotency_key`, "prepare" regenerates
    # the resume first.
    next_action: str | None = None


class ReviewActionService:
    """Approve the current application state stored on a canonical job."""

    def __init__(self, quota: DailyQuotaService | None = None) -> None:
        self.quota = quota or DailyQuotaService()

    def approve_application(self, session: Session, application_id: str) -> ApplyDecision:
        job = session.get(JobPostingRecord, application_id)
        if job is None:
            return ApplyDecision(False, "找不到这条申请。")
        if job.application_state != ApplicationState.AWAITING_RESUME_REVIEW.value:
            return ApplyDecision(False, f"当前状态为 {job.application_state}，不能重复批准。")
        if not job.resume_sha256 or not (job.resume_pdf_path or job.resume_docx_path):
            return ApplyDecision(False, "简历尚未锁定，暂不能投递；请先完成简历审核。")
        try:
            self.quota.claim_application(session, job)
        except QuotaExceeded:
            return ApplyDecision(False, "今日投递额度已用完（最多 20 个）。")
        job.application_state = ApplicationState.MATERIALS_READY.value
        job.reviewed_at = datetime.now(timezone.utc)
        key = f"telegram:{application_id}:{uuid4()}"
        session.flush()
        return ApplyDecision(True, "已批准，正在加入投递队列。", key)

    def reject_application(
        self, session: Session, application_id: str, feedback: str | None = None
    ) -> ApplyDecision:
        job = session.get(JobPostingRecord, application_id)
        if job is None:
            return ApplyDecision(False, "找不到这条申请。")
        if job.application_state != ApplicationState.AWAITING_RESUME_REVIEW.value:
            return ApplyDecision(False, f"当前状态为 {job.application_state}，不能重复拒绝。")
        job.application_state = ApplicationState.SKIPPED.value
        job.review_feedback = feedback.strip() if feedback and feedback.strip() else None
        job.reviewed_at = datetime.now(timezone.utc)
        session.flush()
        return ApplyDecision(True, "已记录不投；请回复一条消息说明原因或调整建议。")

    def record_feedback(
        self, session: Session, application_id: str, feedback: str
    ) -> ApplyDecision:
        job = session.get(JobPostingRecord, application_id)
        if job is None:
            return ApplyDecision(False, "找不到这条申请。")
        if job.application_state != ApplicationState.SKIPPED.value:
            return ApplyDecision(False, "该职位还没有被标记为不投。")
        value = feedback.strip()
        if not value:
            return ApplyDecision(False, "反馈不能为空。")
        job.review_feedback = value
        job.reviewed_at = datetime.now(timezone.utc)
        session.flush()
        return ApplyDecision(True, "已保存反馈，后续可以用于调整排序和路由规则。")

    def mark_applied_manually(self, session: Session, application_id: str) -> ApplyDecision:
        """Record that the candidate takes over; the pipeline stands down."""
        job = session.get(JobPostingRecord, application_id)
        if job is None:
            return ApplyDecision(False, "找不到这条申请。")
        if job.application_state != ApplicationState.NEEDS_HUMAN.value:
            return ApplyDecision(False, f"当前状态为 {job.application_state}，无需人工接管。")
        job.application_state = ApplicationState.SKIPPED.value
        job.review_feedback = "用户决定自行投递"
        job.reviewed_at = datetime.now(timezone.utc)
        job.blocker = None
        session.flush()
        return ApplyDecision(True, "已记录为你自行投递；系统不再处理这条。")

    def retry_application(self, session: Session, application_id: str) -> ApplyDecision:
        """Re-queue a paused application; the submit claim still guards it."""
        job = session.get(JobPostingRecord, application_id)
        if job is None:
            return ApplyDecision(False, "找不到这条申请。")
        if job.application_state != ApplicationState.NEEDS_HUMAN.value:
            return ApplyDecision(False, f"当前状态为 {job.application_state}，不能重试。")
        kind = (job.blocker or {}).get("kind")
        if kind == BlockerKind.SUBMIT_OUTCOME_UNKNOWN.value:
            # Recovery boundary: an unknown outcome must be reconciled by a
            # human first; enqueueing again risks a duplicate submission.
            return ApplyDecision(False, "提交结果未知，系统禁止自动重试；请先人工核实是否已提交。")
        job.blocker = None
        # If this retry pauses again, the result should notify with fresh buttons.
        job.telegram_result_notified_at = None
        if job.resume_sha256 and (job.resume_pdf_path or job.resume_docx_path):
            job.application_state = ApplicationState.MATERIALS_READY.value
            session.flush()
            return ApplyDecision(
                True,
                "已重新加入投递队列。",
                f"auto:{application_id}:{job.resume_sha256}",
                "run",
            )
        job.application_state = ApplicationState.AUTO_APPLY_QUEUED.value
        session.flush()
        return ApplyDecision(True, "简历需要重新生成；已重新排队。", None, "prepare")
