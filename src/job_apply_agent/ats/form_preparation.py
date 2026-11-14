"""Bounded deterministic prefill that leaves recoverable failures to the planner."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import expect

from job_apply_agent.domain.schemas import FormQuestion

from .answer_binding import job_region, question_key, resolve_scoped_answer, scope_question
from .base import ATSBlocked
from .cua_harness import FormReadiness
from .dom_browser import DOMBrowser, RecoverableBrowserError
from .page_readiness import FieldBindingError, locate_inspected_field

UPLOAD_READY_JS = r"""expected => {
  const ashby = expected.provider === 'ashby';
  const title = el => ashby ? el.querySelector('.ashby-application-form-question-title')?.textContent.trim() :
    (el.innerText || '').split('\n').map(x=>x.trim()).find(Boolean) || '';
  const matches = [...document.querySelectorAll(ashby ? '.ashby-application-form-field-entry' : '.file-upload')]
    .filter(el=>title(el) === expected.question);
  if (matches.length !== 1) return false;
  const field = matches[0];
  const items = [...field.querySelectorAll(ashby ? '.ashby-application-form-input-file-item' : '.file-upload__filename')]
    .filter(el=>el.getClientRects().length > 0);
  return items.length === 1 && items[0].querySelector(ashby ? '.ashby-application-form-input-file-item-name' : 'p')?.textContent.trim() === expected.filename
    && !!items[0].querySelector(ashby ? '.ashby-application-form-input-file-item-delete' : 'button[aria-label="Remove file"]')
    && !field.querySelector('[aria-busy=true],[aria-invalid=true],[role=alert]');
}"""


def recoverable_prefill_error(exc: Exception) -> bool:
    if isinstance(exc, (FieldBindingError, PlaywrightTimeout, RecoverableBrowserError)):
        return True
    # Closed browsers, navigation failures and programming errors are not a retry.
    return isinstance(exc, PlaywrightError) and any(
        text in str(exc)
        for text in (
            "strict mode violation",
            "Element is not attached",
            "not an <input>",
            "Input of type",
            "not an <input>, <textarea>",
        )
    )


class FormPreparation:
    def __init__(
        self,
        page: Any,
        adapter: Any,
        resolve: Any,
        *,
        resume_path: Path,
        entries: list | None = None,
        question_context: str = "",
        job_country: str | None = None,
        job_location: str = "",
    ):
        self.page, self.adapter, self.resolve = page, adapter, resolve
        self.resume_path = resume_path
        self.entries = entries
        self.question_context = question_context
        self.region = job_region(job_country, job_location)
        self.failed: set[tuple] = set()
        self.events: list[dict] = []
        self.missing_questions: list[dict] = []
        self.upload_scopes: dict[str, dict] = {}
        self.choice_options: dict[tuple, list[str]] = {}
        self.choice_receipts: dict[tuple, str] = {}

    async def _select_choice(self, question: FormQuestion, binding: dict):
        """Open a known widget, resolve against its own options, then verify selection."""
        locator = await locate_inspected_field(self.page, binding, timeout_ms=2_000)
        await locator.click(timeout=10_000)
        if binding.get("widget") == "ashby_autocomplete":
            approved = resolve_scoped_answer(
                question, binding, self.entries or [], job_country=self.region
            )
            if not approved.value or approved.requires_human:
                raise FieldBindingError("autocomplete query has no approved value")
            await locator.fill(approved.value, timeout=10_000)
        await expect(locator).to_have_attribute("aria-expanded", "true", timeout=5_000)
        owned = (
            (await locator.get_attribute("aria-controls") or "")
            + " "
            + (await locator.get_attribute("aria-owns") or "")
        ).split()
        if len(owned) != 1:
            raise FieldBindingError("choice list is not uniquely owned by its question")
        menu = self.page.locator(f'[role="listbox"][id={json.dumps(owned[0])}]')
        await expect(menu).to_be_visible(timeout=5_000)
        options = menu.get_by_role("option")
        await expect(options.first).to_be_visible(timeout=5_000)
        labels = [text.strip() for text in await options.all_text_contents()]
        resolved = resolve_scoped_answer(
            question.model_copy(update={"options": labels}),
            binding,
            self.entries or [],
            job_country=self.region,
        )
        if resolved.requires_human or not resolved.value:
            await locator.press("Escape")
            return resolved, labels
        selected = options.filter(has_text=re.compile(r"^" + re.escape(resolved.value) + r"$"))
        if await selected.count() != 1:
            raise FieldBindingError("approved choice is not unique within its question")
        await selected.click(timeout=10_000)
        await expect(locator).not_to_have_attribute("aria-expanded", "true", timeout=5_000)
        if binding.get("widget") == "ashby_autocomplete":
            await expect(locator).to_have_value(resolved.value, timeout=5_000)
            self.choice_receipts[(binding["field_id"], binding.get("group_label", ""))] = (
                resolved.value
            )
        return resolved, labels

    async def _remember_upload_scope(self, binding: dict) -> None:
        if binding["field_id"] in self.upload_scopes:
            return
        locator = await locate_inspected_field(self.page, binding, timeout_ms=2_000)
        scope = await locator.evaluate(r"""el => {
          const ashby=el.closest('.ashby-application-form-field-entry');
          if (ashby) return {provider:'ashby',question:ashby.querySelector('.ashby-application-form-question-title')?.textContent.trim() || ''};
          const root=el.closest('.file-upload');
          return {provider:'greenhouse',question:root ? (root.innerText || '').split('\n').map(x=>x.trim()).find(Boolean) : ''};
        }""")
        if scope["question"] and re.search(
            r"resume|curriculum vitae|\bcv\b", scope["question"], re.IGNORECASE
        ):
            self.upload_scopes[binding["field_id"]] = {**scope, "filename": self.resume_path.name}

    async def _fill_resume(self, binding: dict, answer: Any) -> None:
        scope = self.upload_scopes.get(binding["field_id"], {})
        if scope.get("provider") != "ashby":
            await self.adapter.fill(self.page, [answer], resume_path=self.resume_path)
            return
        locator = await locate_inspected_field(self.page, binding)
        group = self.page.locator(".ashby-application-form-field-entry").filter(has=locator)
        button = group.get_by_role(
            "button", name=re.compile(r"^(upload file|replace)$", re.IGNORECASE)
        )
        if await group.count() != 1 or await button.count() != 1:
            raise FieldBindingError("resume upload button is not uniquely bound to its question")
        async with self.page.expect_file_chooser(timeout=10_000) as pending:
            await button.click(timeout=10_000)
        chooser = await pending.value
        await chooser.set_files(str(self.resume_path))
        self.adapter._uploaded_file_names[binding["field_id"]] = self.resume_path.name

    async def _matches(self, binding: dict, value: str) -> bool:
        if value == "UPLOAD_RESUME" and binding["field_id"] in self.upload_scopes:
            return await self.page.evaluate(
                UPLOAD_READY_JS, self.upload_scopes[binding["field_id"]]
            )
        if binding["tag_name"] == "choice_group":
            state = await DOMBrowser(self.page).observe()
            selected = [
                e["label"]
                for e in state["elements"]
                if e["frame"] == 0
                and e["group"] == binding["group_label"]
                and e.get("pressed") == "true"
            ]
            return selected == [value]
        if binding.get("role") == "combobox" and binding["tag_name"] != "select":
            locator = await locate_inspected_field(self.page, binding, timeout_ms=2_000)
            if binding.get("widget") == "ashby_autocomplete":
                receipt = self.choice_receipts.get(
                    (binding["field_id"], binding.get("group_label", ""))
                )
                return (
                    receipt == value
                    and await locator.input_value() == value
                    and await locator.get_attribute("aria-expanded") == "false"
                )
            return await locator.evaluate(
                r"""(el, value) => {
              const root=el.closest('.select__container');
              const selected=root?.querySelector('.select__single-value,[class*="singleValue"],[class*="single-value"]');
              return !!selected && selected.textContent.trim() === value
                && el.getAttribute('aria-expanded') !== 'true';
            }""",
                value,
            )
        locator = await locate_inspected_field(self.page, binding, timeout_ms=2_000)
        if value == "UPLOAD_RESUME":
            names = await locator.evaluate("el => [...(el.files || [])].map(f=>f.name)")
            return names == [self.resume_path.name]
        if binding["tag_name"] == "select":
            labels = await locator.evaluate("el => [...el.selectedOptions].map(o=>o.text.trim())")
            return labels == [value]
        return await locator.input_value(timeout=2_000) == value

    async def __call__(self) -> FormReadiness:
        await self.adapter.wait_until_ready(self.page)
        current = await self.adapter.inspect(self.page)
        if current.blocker:
            raise ATSBlocked(current.blocker, current.blocker_detail or "ATS blocker")
        # Some ATS Yes/No questions have only buttons, with no input element.
        state = await DOMBrowser(self.page).observe()
        choice_groups: dict[str, list[dict]] = {}
        for element in state["elements"]:
            if element["frame"] == 0 and element["group"] and element.get("pressed") is not None:
                choice_groups.setdefault(element["group"], []).append(element)
        for label, targets in choice_groups.items():
            key = "choice:" + question_key(label)
            binding: dict[str, Any] = {
                "field_id": key,
                "label": label,
                "group_label": label,
                "tag_name": "choice_group",
                "input_type": "choice",
                "required": any(e["required"] for e in targets),
                "options": [e["label"] for e in targets],
                "choice_targets": targets,
            }
            self.adapter._field_bindings[key] = binding
            current.questions.append(FormQuestion(**binding))
        questions = [
            scope_question(
                q,
                self.adapter._field_bindings[q.field_id],
                self.entries,
                self.question_context,
                region=self.region,
            )
            if self.entries is not None
            else q
            for q in current.questions
        ]
        answers = {a.field_id: a for a in await self.resolve(questions)}
        if self.entries is not None:
            for question in questions:
                if question.semantic_key and question.semantic_key != "resume_upload":
                    scoped = resolve_scoped_answer(
                        question,
                        self.adapter._field_bindings[question.field_id],
                        self.entries,
                        job_country=self.region,
                    )
                    candidate = answers.get(question.field_id)
                    if not _use_verified_reasoning(scoped, candidate, question):
                        answers[question.field_id] = scoped
        issues, feedback, bindings, missing = [], [], [], []
        for question in questions:
            answer = answers[question.field_id]
            binding = self.adapter._field_bindings[question.field_id]
            if not (binding.get("label") or binding.get("dom_id") or binding.get("dom_name")):
                # These are widget internals, not independently named questions.
                # Required-control checks remain active; never fabricate an answer.
                continue
            identity = tuple(
                binding.get(k, "")
                for k in (
                    "field_id",
                    "dom_id",
                    "dom_name",
                    "label",
                    "group_label",
                    "input_type",
                )
            )
            if identity in self.choice_options:
                question = question.model_copy(update={"options": self.choice_options[identity]})
                answer = resolve_scoped_answer(
                    question, binding, self.entries or [], job_country=self.region
                )
                if answer.requires_human or answer.value is None:
                    candidate = (await self.resolve([question]))[0]
                    if _use_verified_reasoning(answer, candidate, question):
                        answer = candidate
            if answer.requires_human:
                missing.append(
                    {
                        "field_id": question.field_id,
                        "question": question.label,
                        "group": binding.get("group_label", ""),
                        "required": question.required,
                        "semantic_key": question.semantic_key,
                        "options": question.options,
                        "answer_key": question_key(
                            question.label, binding.get("group_label", ""), self.question_context
                        ),
                        "context": self.question_context,
                        "reason": answer.reason,
                        "category": (
                            "job_context_missing"
                            if answer.reason
                            == "job country needs resolution, not a new candidate fact"
                            else "unmapped_question"
                            if (question.semantic_key or "").startswith("question:")
                            else "option_mismatch"
                            if answer.reason == "stored answer does not match available options"
                            else "answer_conflict"
                            if answer.reason == "multiple conflicting answer-bank values"
                            else "approved_fact_unavailable"
                        ),
                        "job_region": self.region,
                    }
                )
            if answer.value is None or answer.requires_human:
                continue
            if binding["tag_name"] == "choice_group":
                for target in binding["choice_targets"]:
                    if target["label"] == answer.value:
                        bindings.append(
                            {
                                **target,
                                "tag_name": target["tag"],
                                "input_type": target["type"],
                                "group_label": target["group"],
                                "value": answer.value,
                                "source_key": answer.source_key,
                                "derivation": answer.reason,
                            }
                        )
            else:
                bindings.append(
                    {
                        **binding,
                        "value": answer.value,
                        "source_key": answer.source_key,
                        "derivation": answer.reason,
                    }
                )
            operation = "read_before_fill"
            try:
                if answer.value == "UPLOAD_RESUME":
                    await self._remember_upload_scope(binding)
                if await self._matches(binding, answer.value):
                    continue
                if identity in self.failed:
                    issues.append(f"prefill needs recovery: {question.label or question.field_id}")
                    continue
                operation = "fill"
                if answer.value == "UPLOAD_RESUME":
                    await self._fill_resume(binding, answer)
                elif binding.get("role") == "combobox" and binding["tag_name"] != "select":
                    try:
                        answer, options = await self._select_choice(question, binding)
                    except AssertionError as exc:
                        raise FieldBindingError(
                            "choice widget did not acknowledge its state"
                        ) from exc
                    self.choice_options[identity] = options
                    bindings.pop()  # Replace raw text authority with the exact observed option.
                    if answer.requires_human or not answer.value:
                        issues.append(f"approved answer does not match options: {question.label}")
                        missing.append(
                            {
                                "field_id": question.field_id,
                                "question": question.label,
                                "group": binding.get("group_label", ""),
                                "required": question.required,
                                "semantic_key": question.semantic_key,
                                "options": options,
                                "answer_key": question_key(
                                    question.label,
                                    binding.get("group_label", ""),
                                    self.question_context,
                                ),
                                "context": self.question_context,
                                "reason": answer.reason,
                                "category": "option_mismatch",
                                "job_region": self.region,
                            }
                        )
                        continue
                    bindings.append(
                        {
                            **binding,
                            "value": answer.value,
                            "source_key": answer.source_key,
                            "derivation": answer.reason,
                        }
                    )
                elif binding["tag_name"] == "choice_group":
                    browser = DOMBrowser(self.page)
                    observed = await browser.observe()
                    targets = [
                        e
                        for e in observed["elements"]
                        if e["frame"] == 0
                        and e["group"] == binding["group_label"]
                        and e["label"] == answer.value
                        and e.get("pressed") is not None
                    ]
                    if len(targets) != 1:
                        raise FieldBindingError("choice question is ambiguous")
                    await browser.execute({"action": "click", "ref": targets[0]["ref"]}, observed)
                else:
                    await self.adapter.fill(self.page, [answer], resume_path=self.resume_path)
                operation = "read_after_fill"
                if answer.value == "UPLOAD_RESUME" and question.field_id in self.upload_scopes:
                    handle = await self.page.wait_for_function(
                        UPLOAD_READY_JS,
                        arg=self.upload_scopes[question.field_id],
                        polling=200,
                        timeout=self.adapter.settings.browser_page_ready_timeout_seconds * 1000,
                    )
                    await handle.dispose()
                if not await self._matches(binding, answer.value):
                    raise FieldBindingError("prefill effect did not survive readback")
                self.events.append({"field_id": question.field_id, "outcome": "verified"})
            except Exception as exc:
                if not recoverable_prefill_error(exc):
                    raise
                self.failed.add(identity)
                event = {
                    "field_id": question.field_id,
                    "target": question.label,
                    "error_type": type(exc).__name__,
                    "operation": operation,
                    "outcome": "recoverable",
                    "route": "shared_prefill",
                }
                feedback.append(event)
                self.events.append(event)
                issues.append(f"prefill needs recovery: {question.label or question.field_id}")
        self.missing_questions = missing
        for key, expected in self.upload_scopes.items():
            if not await self.page.evaluate(UPLOAD_READY_JS, expected):
                issues.append(f"uploaded attachment is not verified: {key}")
        issues.extend(await self.adapter.readiness_issues(self.page))
        # Native required validation alone cannot certify that the answer is approved.
        issues.extend(f"missing approved answer: {q['question']}" for q in missing)
        return FormReadiness(
            ready=not issues,
            issues=tuple(issues),
            recovery_feedback=tuple(feedback),
            answer_bindings=tuple(bindings),
            missing_questions=tuple(missing),
        )


def _use_verified_reasoning(scoped, candidate, question) -> bool:
    return bool(
        (scoped.requires_human or scoped.value is None)
        and scoped.reason != "multiple conflicting answer-bank values"
        and candidate is not None
        and not candidate.requires_human
        and candidate.value is not None
        and (candidate.source_key or "").startswith("verified_reasoning:")
        and (not question.options or candidate.value in question.options)
    )
