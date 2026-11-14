from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application as TelegramApplication,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from job_apply_agent.collectors.base import CollectorError
from job_apply_agent.collectors.job_link import fetch_job_url
from job_apply_agent.config import get_settings
from job_apply_agent.db import get_session_factory
from job_apply_agent.domain.enums import ApplicationState
from job_apply_agent.domain.schemas import CandidateProfile, SearchPolicy
from job_apply_agent.models import JobPostingRecord
from job_apply_agent.services.orchestrator import ApplicationOrchestrator
from job_apply_agent.services.review_actions import ReviewActionService
from job_apply_agent.telegram.experiences import COMMAND as EXPERIENCE_COMMAND
from job_apply_agent.telegram.experiences import handle_experience

log = logging.getLogger(__name__)
FEEDBACK_JOB_PATTERN = re.compile(r"\[job:([0-9a-f-]{36})\]", re.IGNORECASE)
JD_COMMAND_PATTERN = re.compile(r"^/jd(?:@[A-Za-z0-9_]+)?(?:\s+|$)", re.IGNORECASE)


@dataclass(frozen=True)
class ReviewCard:
    application_id: str
    company: str
    title: str
    location: str
    url: str
    score: float
    reasons: list[str]
    match_summary: str
    fact_ids: list[str]
    gaps: list[str]
    pdf_path: Path | None
    docx_path: Path | None


def review_keyboard(application_id: str) -> InlineKeyboardMarkup:
    def button(label: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(label, callback_data=f"review:{application_id}:{action}")

    return InlineKeyboardMarkup([[button("批准投递", "apply"), button("不投", "skip")]])


def needs_human_keyboard(application_id: str) -> InlineKeyboardMarkup:
    def button(label: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(label, callback_data=f"review:{application_id}:{action}")

    return InlineKeyboardMarkup([[button("我自己投", "apply_manual"), button("重试", "retry")]])


def format_card(card: ReviewCard) -> str:
    reason_text = "、".join(card.reasons) or "综合评分"
    facts = ", ".join(card.fact_ids) or "无"
    gaps = "；".join(card.gaps) or "未识别到明显缺口"
    return (
        f"{card.company} · {card.title}\n"
        f"{card.location}\n"
        f"高价值分数：{card.score:.1f}（{reason_text}）\n"
        f"匹配摘要：{card.match_summary}\n"
        f"简历内容：模板与事实引用如下；附件是本次待审批版本。\n"
        f"事实引用：{facts}\n"
        f"潜在缺口：{gaps}\n"
        f"{card.url}"
    )


class TelegramReviewGateway:
    def __init__(
        self,
        token: str,
        chat_id: int,
        action_handler,
        *,
        session_factory=None,
        scan_interval_seconds: int = 30,
        fact_statements: dict[str, str] | None = None,
    ) -> None:
        self.chat_id = chat_id
        self.action_handler = action_handler
        self.session_factory = session_factory or get_session_factory()
        self.scan_interval_seconds = scan_interval_seconds
        self.fact_statements = fact_statements or {}
        self._notification_task: asyncio.Task[None] | None = None
        self.application = (
            TelegramApplication.builder()
            .token(token)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        self.application.add_error_handler(self._on_error)
        self.application.add_handler(CallbackQueryHandler(self._on_callback, pattern=r"^review:"))
        self.application.add_handler(CommandHandler(["exp", "experience"], self._on_experience))
        self.application.add_handler(
            MessageHandler(filters.Regex(EXPERIENCE_COMMAND), self._on_experience)
        )
        self.application.add_handler(CommandHandler("jd", self._on_jd))
        # CommandHandler intentionally requires a Telegram BOT_COMMAND entity.
        # Some clients/integrations deliver valid command text without that
        # entity; accept the text form too so the update is not silently lost.
        self.application.add_handler(MessageHandler(filters.Regex(JD_COMMAND_PATTERN), self._on_jd))
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )

    async def present(self, card: ReviewCard) -> int:
        bot = self.application.bot
        message = await bot.send_message(
            chat_id=self.chat_id,
            text=format_card(card),
            reply_markup=review_keyboard(card.application_id),
        )
        for path in (card.pdf_path, card.docx_path):
            if path and path.is_file():
                try:
                    with path.open("rb") as document:
                        await bot.send_document(chat_id=self.chat_id, document=document)
                except Exception:
                    log.exception(
                        "telegram_resume_attachment_failed job_id=%s", card.application_id
                    )
        return message.message_id

    async def _post_init(self, application) -> None:
        self._notification_task = asyncio.create_task(
            self._notification_loop(), name="telegram-notification-loop"
        )

    async def _post_shutdown(self, application) -> None:
        del application
        if self._notification_task is None:
            return
        self._notification_task.cancel()
        try:
            await self._notification_task
        except asyncio.CancelledError:
            pass
        self._notification_task = None

    async def _notification_loop(self) -> None:
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram_notification_scan_failed")
            await asyncio.sleep(self.scan_interval_seconds)

    async def scan_once(self) -> None:
        await self._send_pending_reviews()
        await self._send_pending_results()

    async def _send_pending_reviews(self) -> None:
        with self.session_factory() as session:
            job_ids = list(
                session.scalars(
                    select(JobPostingRecord.id)
                    .where(
                        JobPostingRecord.application_state
                        == ApplicationState.AWAITING_RESUME_REVIEW.value,
                        JobPostingRecord.telegram_review_notified_at.is_(None),
                        JobPostingRecord.resume_sha256.is_not(None),
                        or_(
                            JobPostingRecord.resume_pdf_path.is_not(None),
                            JobPostingRecord.resume_docx_path.is_not(None),
                        ),
                    )
                    .order_by(JobPostingRecord.score.desc(), JobPostingRecord.first_seen_at)
                    .limit(20)
                )
            )
        for job_id in job_ids:
            with self.session_factory() as session:
                job = session.get(JobPostingRecord, job_id)
                if job is None or job.telegram_review_notified_at is not None:
                    continue
                card = _review_card(job, self.fact_statements)
            message_id = await self.present(card)
            with self.session_factory() as session:
                job = session.get(JobPostingRecord, job_id)
                if job is not None and job.telegram_review_notified_at is None:
                    job.telegram_message_id = message_id
                    job.telegram_review_notified_at = datetime.now(UTC)
                    session.commit()

    async def _send_pending_results(self) -> None:
        terminal_states = (
            ApplicationState.SUBMITTED.value,
            ApplicationState.FAILED.value,
            ApplicationState.NEEDS_HUMAN.value,
        )
        with self.session_factory() as session:
            job_ids = list(
                session.scalars(
                    select(JobPostingRecord.id)
                    .where(
                        JobPostingRecord.application_state.in_(terminal_states),
                        JobPostingRecord.telegram_result_notified_at.is_(None),
                    )
                    .order_by(JobPostingRecord.updated_at)
                    .limit(20)
                )
            )
        for job_id in job_ids:
            with self.session_factory() as session:
                job = session.get(JobPostingRecord, job_id)
                if job is None or job.telegram_result_notified_at is not None:
                    continue
                text = _result_message(job)
                markup = (
                    needs_human_keyboard(job.id)
                    if job.application_state == ApplicationState.NEEDS_HUMAN.value
                    else None
                )
            await self.application.bot.send_message(
                chat_id=self.chat_id, text=text, reply_markup=markup
            )
            with self.session_factory() as session:
                job = session.get(JobPostingRecord, job_id)
                if job is not None and job.telegram_result_notified_at is None:
                    job.telegram_result_notified_at = datetime.now(UTC)
                    session.commit()

    async def _on_callback(self, update, context) -> None:
        query = update.callback_query
        if update.effective_chat is None or update.effective_chat.id != self.chat_id:
            await query.answer("无权操作这个 Bot。", show_alert=True)
            return
        await query.answer()
        _, application_id, action = query.data.split(":", 2)
        # `apply` calls the handler exactly once and queues submission immediately;
        # there is intentionally no second confirmation callback.
        message = await self.action_handler(application_id, action)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(message)
        if action == "skip" and message.startswith("已记录不投"):
            await query.message.reply_text(
                f"请回复这条消息，说明不投原因或排序建议。\n[job:{application_id}]",
                reply_markup=ForceReply(selective=True),
            )

    async def _on_message(self, update, context) -> None:
        if update.effective_chat is None or update.effective_chat.id != self.chat_id:
            return
        message = update.effective_message
        replied = message.reply_to_message
        match = FEEDBACK_JOB_PATTERN.search(replied.text or "") if replied else None
        if not match:
            return
        with self.session_factory() as session:
            decision = ReviewActionService().record_feedback(
                session, match.group(1), message.text or ""
            )
            if decision.accepted:
                session.commit()
            else:
                session.rollback()
        await message.reply_text(decision.message)

    async def _on_error(self, update, context) -> None:
        update_id = getattr(update, "update_id", None)
        log.error(
            "telegram_update_failed update_id=%s",
            update_id,
            exc_info=(type(context.error), context.error, context.error.__traceback__),
        )

    async def _on_experience(self, update, context) -> None:
        await handle_experience(self, update)

    async def _on_jd(self, update, context) -> None:
        if update.effective_chat is None or update.effective_chat.id != self.chat_id:
            return
        message = update.effective_message
        log.info("telegram_jd_received update_id=%s", update.update_id)
        match = JD_COMMAND_PATTERN.match(message.text or "")
        arguments = (message.text or "")[match.end() :].split() if match else []
        if len(arguments) != 1:
            await message.reply_text("用法：/jd https://company.example/jobs/123")
            return
        await message.reply_text("正在读取岗位、去重并重排……")
        try:
            raw = await fetch_job_url(arguments[0])
            settings = get_settings()
            profile = CandidateProfile.model_validate(settings.candidate_profile())
            policy = SearchPolicy.model_validate(settings.search_policy())
            with self.session_factory() as session:
                # Manual /jd additions are exempt from the daily fetch quota in
                # both directions: they are never rejected at the limit, and the
                # rows they create never consume the collect budget.
                result = ApplicationOrchestrator().ingest(
                    session, raw, profile, policy, ingested_via="telegram_jd"
                )
                job = session.get(JobPostingRecord, result.job_id)
                if job is None:
                    raise LookupError("ingested job was not persisted")
                summary: dict[str, Any] = {
                    "company": job.company,
                    "title": job.title,
                    "score": job.score,
                    "state": job.application_state,
                    "channel": job.application_channel,
                    "reasons": list(job.ranking_reasons or []),
                    "eligible": job.eligible,
                }
                session.commit()
            if summary["eligible"]:
                from job_apply_agent.tasks import prepare_application

                prepare_application.send(result.job_id, profile.model_dump(mode="json"))
            reason_text = "、".join(summary["reasons"]) or "无"
            created_text = "已新增" if result.created else "已去重并更新"
            await message.reply_text(
                f"{created_text}：{summary['company']} · {summary['title']}\n"
                f"评分：{summary['score']:.1f}\n"
                f"路由：{summary['channel']} / {summary['state']}\n"
                f"原因：{reason_text}\n"
                + (
                    "正在生成临时简历，随后进入审批或 shadow 投递流程。"
                    if summary["eligible"]
                    else "该岗位未通过硬性资格规则，不会投递。"
                )
            )
        except CollectorError as exc:
            await message.reply_text(f"无法加入这个岗位：{exc}")
        except Exception:
            log.exception("telegram_jd_ingest_failed")
            await message.reply_text("岗位处理失败；已记录服务器错误，请稍后重试。")

    def run(self) -> None:
        self.application.run_polling(allowed_updates=["callback_query", "message"])


def _review_card(
    job: JobPostingRecord, fact_statements: dict[str, str] | None = None
) -> ReviewCard:
    normalized = job.normalized or {}
    facts = list(job.resume_fact_ids or [])
    template = job.resume_template or "默认模板"
    known_facts = [
        fact_statements[fact_id]
        for fact_id in facts
        if fact_statements and fact_id in fact_statements
    ]
    fact_summary = "；".join(known_facts[:4]) or "请查看附件中的具体表述"
    summary = f"使用 {template}；引用 {len(facts)} 条 metadata 事实。主要内容：{fact_summary}"
    return ReviewCard(
        application_id=job.id,
        company=job.company,
        title=job.title,
        location=job.location,
        url=job.url,
        score=job.score,
        reasons=list(job.ranking_reasons or []),
        match_summary=summary,
        fact_ids=facts,
        gaps=list(job.resume_validation_errors or normalized.get("gaps", []) or []),
        pdf_path=Path(job.resume_pdf_path) if job.resume_pdf_path else None,
        docx_path=Path(job.resume_docx_path) if job.resume_docx_path else None,
    )


def _result_message(job: JobPostingRecord) -> str:
    labels = {
        ApplicationState.SUBMITTED.value: "自动投递成功",
        ApplicationState.FAILED.value: "自动投递失败",
        ApplicationState.NEEDS_HUMAN.value: "自动投递需要人工接管",
    }
    detail = job.last_error or ((job.blocker or {}).get("detail")) or "无额外信息"
    return (
        f"{labels.get(job.application_state, job.application_state)}\n"
        f"{job.company} · {job.title}\n"
        f"{detail}\n{job.url}"
    )


def build_action_handler(*, session_factory=None, enqueue=None):
    """Build the callback used by ``TelegramReviewGateway``.

    ``enqueue`` is injected so Telegram remains independent from Dramatiq in
    tests and can be replaced by another queue implementation later.
    """
    factory = session_factory or get_session_factory()
    if enqueue is None:
        from job_apply_agent.tasks import run_application

        def enqueue(application_id: str, key: str):
            return run_application.send(application_id, key)

    async def handle(application_id: str, action: str) -> str:
        if action == "skip":
            with factory() as session:
                decision = ReviewActionService().reject_application(session, application_id)
                if decision.accepted:
                    session.commit()
                else:
                    session.rollback()
            return decision.message
        if action in {"apply_manual", "retry"}:
            with factory() as session:
                service = ReviewActionService()
                if action == "apply_manual":
                    decision = service.mark_applied_manually(session, application_id)
                else:
                    decision = service.retry_application(session, application_id)
                if decision.accepted:
                    session.commit()
                else:
                    session.rollback()
            if not decision.accepted:
                return decision.message
            if decision.next_action == "run" and decision.idempotency_key:
                try:
                    enqueue(application_id, decision.idempotency_key)
                except Exception:
                    # The durable submit claim still guards against double
                    # submission; reconciliation can retry the same key.
                    return "已接受，但投递队列暂时不可用；请稍后重试或人工检查。"
            elif decision.next_action == "prepare":
                from job_apply_agent.tasks import prepare_application

                profile = CandidateProfile.model_validate(get_settings().candidate_profile())
                prepare_application.send(application_id, profile.model_dump(mode="json"))
            return decision.message
        if action != "apply":
            return "不支持的审核操作。"
        with factory() as session:
            decision = ReviewActionService().approve_application(session, application_id)
            if not decision.accepted:
                session.rollback()
                return decision.message
            session.commit()
        try:
            enqueue(application_id, decision.idempotency_key)
        except Exception:
            # The quota is deliberately not refunded here. Reconciliation can
            # safely retry the same idempotency key without double submission.
            return "已批准，但投递队列暂时不可用；请稍后重试或人工检查。"
        return decision.message

    return handle


def run_bot() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token or settings.telegram_chat_id is None:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    handler = build_action_handler()
    profile = settings.candidate_profile()
    fact_statements = {
        fact["id"]: fact["statement"]
        for fact in profile.get("facts", [])
        if isinstance(fact, dict)
        and isinstance(fact.get("id"), str)
        and isinstance(fact.get("statement"), str)
    }
    TelegramReviewGateway(
        settings.telegram_bot_token,
        settings.telegram_chat_id,
        handler,
        fact_statements=fact_statements,
    ).run()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs Telegram request URLs at INFO, and those URLs contain the bot
    # token. Keep transport details out of routine service logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    run_bot()
