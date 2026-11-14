import asyncio
from datetime import datetime, timezone

from telegram import Chat, Message, Update, User
from telegram.ext import MessageHandler

from job_apply_agent.domain.enums import ApplicationState
from job_apply_agent.models import JobPostingRecord
from job_apply_agent.telegram.review import (
    TelegramReviewGateway,
    _result_message,
    _review_card,
    build_action_handler,
    needs_human_keyboard,
)


def test_jd_text_without_command_entity_uses_fallback_handler():
    async def action_handler(*_args):
        return "unused"

    gateway = TelegramReviewGateway(
        "123:ABC",
        42,
        action_handler,
        session_factory=lambda: None,
    )
    update = Update(
        7,
        message=Message(
            8,
            datetime.now(timezone.utc),
            Chat(42, "private"),
            from_user=User(9, "Test", False),
            text="/jd https://example.test/job",
            entities=[],
        ),
    )

    matching_handlers = [
        handler
        for handler in gateway.application.handlers[0]
        if isinstance(handler, MessageHandler) and handler.check_update(update)
    ]

    assert matching_handlers[0].callback == gateway._on_jd
    assert gateway.application.error_handlers


def test_apply_callback_enqueues_only_after_approval(session):
    application = JobPostingRecord(
        id="job",
        canonical_key="j" * 64,
        source="lever",
        source_id="job",
        sources=[],
        company="Acme",
        title="Engineer",
        url="https://example.test/job",
        location="Toronto",
        normalized={},
        application_state=ApplicationState.AWAITING_RESUME_REVIEW.value,
        resume_sha256="abc",
        resume_pdf_path="/tmp/resume.pdf",
    )
    session.add(application)
    session.flush()
    queued = []

    async def run():
        handler = build_action_handler(
            session_factory=lambda: session,
            enqueue=lambda app_id, key: queued.append((app_id, key)),
        )
        return await handler(application.id, "apply")

    message = asyncio.run(run())
    assert "已批准" in message
    assert queued[0][0] == application.id
    assert queued[0][1].startswith(f"telegram:{application.id}:")


def test_non_apply_callback_does_not_enqueue(session):
    queued = []

    async def run():
        handler = build_action_handler(
            session_factory=lambda: session,
            enqueue=lambda app_id, key: queued.append((app_id, key)),
        )
        return await handler("missing", "technical")

    assert asyncio.run(run()) == "不支持的审核操作。"
    assert queued == []


def test_skip_callback_persists_state_without_enqueue(session):
    application = JobPostingRecord(
        id="skip-job",
        canonical_key="s" * 64,
        source="lever",
        source_id="skip-job",
        sources=[],
        company="Acme",
        title="Engineer",
        url="https://example.test/skip",
        location="Toronto",
        normalized={},
        application_state=ApplicationState.AWAITING_RESUME_REVIEW.value,
    )
    session.add(application)
    session.flush()
    queued = []

    async def run():
        handler = build_action_handler(
            session_factory=lambda: session,
            enqueue=lambda app_id, key: queued.append((app_id, key)),
        )
        return await handler(application.id, "skip")

    assert "已记录不投" in asyncio.run(run())
    assert application.application_state == ApplicationState.SKIPPED.value
    assert queued == []


def test_review_card_and_auto_result_summarize_job_row():
    application = JobPostingRecord(
        id="summary-job",
        canonical_key="r" * 64,
        source="ashby",
        source_id="summary-job",
        sources=[],
        company="Example",
        title="ML Engineer",
        url="https://example.test/result",
        location="Toronto",
        normalized={},
        score=72,
        application_state=ApplicationState.NEEDS_HUMAN.value,
        application_channel="auto",
        resume_template="technical",
        resume_fact_ids=["fact-one"],
        blocker={"detail": "unknown required question"},
    )

    card = _review_card(application, {"fact-one": "Built reproducible ML pipelines."})
    result = _result_message(application)

    assert "technical" in card.match_summary
    assert "Built reproducible ML pipelines" in card.match_summary
    assert card.fact_ids == ["fact-one"]
    assert "需要人工接管" in result
    assert "unknown required question" in result


def needs_human_record(job_id: str) -> JobPostingRecord:
    return JobPostingRecord(
        id=job_id,
        canonical_key="n" * 64,
        source="lever",
        source_id=job_id,
        sources=[],
        company="Acme",
        title="Engineer",
        url=f"https://example.test/{job_id}",
        location="Toronto",
        normalized={},
        application_state=ApplicationState.NEEDS_HUMAN.value,
        submission_status=ApplicationState.NEEDS_HUMAN.value,
        blocker={"kind": "browser_error", "detail": "edge crashed"},
        resume_sha256="abc",
        resume_pdf_path="/tmp/resume.pdf",
    )


def test_needs_human_keyboard_offers_manual_and_retry():
    markup = needs_human_keyboard("job-1")
    buttons = [(button.text, button.callback_data) for button in markup.inline_keyboard[0]]
    assert buttons == [
        ("我自己投", "review:job-1:apply_manual"),
        ("重试", "review:job-1:retry"),
    ]


def test_retry_callback_enqueues_with_auto_idempotency_key(session):
    record = needs_human_record("retry-job")
    session.add(record)
    session.flush()
    queued = []

    async def run():
        handler = build_action_handler(
            session_factory=lambda: session,
            enqueue=lambda app_id, key: queued.append((app_id, key)),
        )
        return await handler(record.id, "retry")

    message = asyncio.run(run())
    assert "已重新加入投递队列" in message
    assert queued == [(record.id, f"auto:{record.id}:abc")]
    assert record.application_state == ApplicationState.MATERIALS_READY.value


def test_manual_takeover_callback_persists_without_enqueue(session):
    record = needs_human_record("manual-job")
    session.add(record)
    session.flush()
    queued = []

    async def run():
        handler = build_action_handler(
            session_factory=lambda: session,
            enqueue=lambda app_id, key: queued.append((app_id, key)),
        )
        return await handler(record.id, "apply_manual")

    message = asyncio.run(run())
    assert "自行投递" in message
    assert queued == []
    assert record.application_state == ApplicationState.SKIPPED.value
