"""One-message experience capture. Replies are sent only to the configured chat."""

from __future__ import annotations

import json
import re

from job_apply_agent.config import get_settings
from job_apply_agent.domain.schemas import CandidateProfile
from job_apply_agent.experience.library import (
    Experience,
    fingerprint,
    load_experiences,
    save_experience,
)
from job_apply_agent.models import ExperienceRecord

COMMAND = re.compile(r"^/(?:exp|experience)(?:@[A-Za-z0-9_]+)?(?:\s+|$)", re.IGNORECASE)
HELP = (
    "新增：/exp 这段经历的完整描述（可换行，可写日期、角色、成果）\n"
    "查看：/exp list 或 /exp show ID\n"
    "更新：/exp update ID 后换行输入完整新内容。更新会完整替换内容，旧版保留。\n"
    '也支持 JSON：{"description":["描述"],"metadata":{"start_date":"2026-10-01"}}'
)


async def handle_experience(gateway, update):
    if update.effective_chat is None or update.effective_chat.id != gateway.chat_id:
        return
    message = update.effective_message
    text = message.text or ""
    match = COMMAND.match(text)
    body = text[match.end() :].strip() if match else ""
    if not body or body == "help":
        await message.reply_text(HELP)
        return
    settings = get_settings()
    with gateway.session_factory() as session:
        library = {
            e.id: e
            for e in load_experiences(
                CandidateProfile.model_validate(settings.candidate_profile()),
                settings.experience_library_path,
                session,
            )
        }
        if body == "list":
            lines = [f"{e.id} · v{e.revision} · {e.description[0][:70]}" for e in library.values()]
            output = "\n".join(lines) or "经历库为空。"
            # Each reply remains below Telegram's message size limit.
            for start in range(0, len(output), 3500):
                await message.reply_text(output[start : start + 3500])
            return
        if body.startswith("show "):
            item = library.get(body[5:].strip())
            output = (
                json.dumps(item.model_dump(mode="json"), ensure_ascii=False, indent=2)
                if item
                else "未找到这个 ID。"
            )
            for start in range(0, len(output), 3500):
                await message.reply_text(output[start : start + 3500])
            return
        if body.startswith("update "):
            parts = body.split(maxsplit=2)
            if len(parts) != 3 or parts[1] not in library:
                await message.reply_text("更新需要已有 ID 和完整新内容。\n" + HELP)
                return
            identifier, body = parts[1:]
            existing = library[identifier]
            # Persist the imported original before overriding it, for rollback/audit.
            if session.get(ExperienceRecord, identifier) is None:
                save_experience(
                    session,
                    existing,
                    event_key="import:" + identifier + ":" + fingerprint(existing.model_dump()),
                )
        else:
            identifier = f"telegram-{update.effective_chat.id}-{message.message_id}"
            existing = None
            body = body.removeprefix("add ")
        try:
            payload = json.loads(body) if body.startswith("{") else {"description": [body]}
            if not isinstance(payload, dict):
                raise TypeError("experience must be an object")
            if isinstance(payload.get("description"), str):
                payload["description"] = [payload["description"]]
            # IDs, source and revision cannot be supplied by message content.
            item = Experience(
                id=identifier,
                kind=payload.get("kind", existing.kind if existing else "experience"),
                description=payload.get("description", []),
                metadata=payload.get("metadata", {}),
                source=f"telegram:{update.effective_chat.id}:{message.message_id}",
            )
            saved = save_experience(session, item, event_key=f"telegram:update:{update.update_id}")
            session.commit()
        except (ValueError, TypeError):
            session.rollback()
            await message.reply_text("内容格式有误，未保存。\n" + HELP)
            return
    await message.reply_text(
        f"已保存经历 {saved.id} · v{saved.revision}。\n"
        "经历推理启用后，填表会读取新版本并增量更新向量。此次操作不会触发投递。"
    )
