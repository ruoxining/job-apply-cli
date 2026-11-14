from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from telegram import Chat, Message, Update, User
from telegram.ext import MessageHandler

from job_apply_agent.config import Settings
from job_apply_agent.db import Base
from job_apply_agent.models import ExperienceRecord, ExperienceRevisionRecord
from job_apply_agent.telegram.experiences import handle_experience
from job_apply_agent.telegram.review import TelegramReviewGateway


def gateway(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(
        "job_apply_agent.telegram.experiences.get_settings",
        lambda: Settings(experience_library_path=tmp_path / "missing", profile_json="{}"),
    )
    return SimpleNamespace(chat_id=42, session_factory=factory)


def update(text, *, chat=42, update_id=1, message_id=10):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat),
        update_id=update_id,
        effective_message=SimpleNamespace(text=text, message_id=message_id, reply_text=AsyncMock()),
    )


async def test_capture_retry_and_update_preserve_old_description(tmp_path, monkeypatch):
    g = gateway(tmp_path, monkeypatch)
    first = update("/exp Built a Python service in 2024")
    await handle_experience(g, first)
    await handle_experience(g, first)
    changed = update(
        "/exp update telegram-42-10\nBuilt a Python and SQL service in 2024",
        update_id=2,
        message_id=11,
    )
    await handle_experience(g, changed)
    with g.session_factory() as session:
        rows = list(session.scalars(select(ExperienceRecord)))
        assert len(rows) == 1 and rows[0].payload["revision"] == 2
        history = list(session.scalars(select(ExperienceRevisionRecord)))
        assert len(history) == 2
        assert history[0].payload["description"] == ["Built a Python service in 2024"]
    assert "不会触发投递" in changed.effective_message.reply_text.call_args.args[0]


async def test_unauthorized_chat_cannot_read_or_write(tmp_path, monkeypatch):
    g = gateway(tmp_path, monkeypatch)
    for body in ["/exp private experience", "/exp list", "/exp show secret"]:
        u = update(body, chat=99)
        await handle_experience(g, u)
        u.effective_message.reply_text.assert_not_called()
    with g.session_factory() as session:
        assert not list(session.scalars(select(ExperienceRecord)))


async def test_bad_json_and_unknown_update_do_not_write(tmp_path, monkeypatch):
    g = gateway(tmp_path, monkeypatch)
    for body in ["/exp {bad", "/exp update unknown\nreplacement", '/exp {"description":[]}']:
        await handle_experience(g, update(body))
    with g.session_factory() as session:
        assert not list(session.scalars(select(ExperienceRecord)))


def test_exp_without_command_entity_routes_before_generic_message():
    g = TelegramReviewGateway("123:ABC", 42, AsyncMock(), session_factory=lambda: None)
    u = Update(
        7,
        message=Message(
            8,
            datetime.now(UTC),
            Chat(42, "private"),
            from_user=User(9, "Test", False),
            text="/exp Built an API",
            entities=[],
        ),
    )
    handlers = [
        h for h in g.application.handlers[0] if isinstance(h, MessageHandler) and h.check_update(u)
    ]
    assert handlers[0].callback == g._on_experience
