from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

from job_apply_agent.config import Settings, get_settings
from job_apply_agent.domain.enums import BlockerKind, Sensitivity
from job_apply_agent.domain.schemas import FormQuestion, ResolvedAnswer

from .base import ApplicationResult, ATSAdapter, ATSBlocked, InspectionResult, SubmissionClaim
from .captcha import inspect_captcha
from .page_readiness import FieldBindingError, locate_inspected_field, wait_for_application_surface

SENSITIVE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"visa|sponsor|work authori[sz]", "work_authorization"),
    (r"salary|compensation|pay expectation", "salary_expectation"),
    (r"gender|sex", "eeo_gender"),
    (r"race|ethnic", "eeo_race_ethnicity"),
    (r"disab", "eeo_disability"),
    (r"veteran", "eeo_veteran"),
    (r"criminal|background check", "background_check"),
)

NORMAL_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"first name", "first_name"),
    (r"last name|surname|family name", "last_name"),
    (r"full name|your name|^name\s*\*?$", "full_name"),
    (r"e-?mail", "email"),
    (r"phone|mobile", "phone"),
    (r"linkedin", "linkedin_url"),
    (r"github", "github_url"),
    (r"portfolio|website", "website_url"),
    (r"resume|curriculum vitae|\bcv\b", "resume_upload"),
    (r"cover letter", "cover_letter_upload"),
)


class PlaywrightATSAdapter(ATSAdapter):
    field_selector = "input, textarea, select, [role=combobox]"
    submit_selectors = (
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Submit application")',
        'button:has-text("Submit")',
    )
    success_url_pattern = re.compile(
        r"(?:confirmation|application[-_/]?submitted|thank[-_/]?you|success)", re.IGNORECASE
    )
    success_text_pattern = re.compile(
        r"(?:thank you for (?:your )?(?:application|applying)|"
        r"application (?:has been |was )?(?:successfully )?(?:submitted|received)|"
        r"we(?:'ve| have) received your application|"
        r"successfully submitted)",
        re.IGNORECASE,
    )

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._uploaded_file_names: dict[str, str] = {}
        self._field_bindings: dict[str, dict[str, Any]] = {}
        self.captcha_evidence: dict[str, Any] = {}

    async def wait_until_ready(self, page: Any) -> dict:
        return await wait_for_application_surface(
            page, timeout_ms=self.settings.browser_page_ready_timeout_seconds * 1000
        )

    async def inspect(self, page: Any) -> InspectionResult:
        blocker = await self._detect_blocker(page)
        if blocker:
            return InspectionResult(questions=[], blocker=blocker[0], blocker_detail=blocker[1])
        raw_fields = await page.eval_on_selector_all(
            self.field_selector,
            """
            els => els.filter(el => {
              if (el.type === 'hidden' || el.disabled) return false;
              // Upload controls may be intentionally hidden behind an upload button.
              if (el.type === 'file') return true;
              return el.getClientRects().length > 0 &&
                window.getComputedStyle(el).visibility !== 'hidden';
            }).map((el, index) => {
              const fieldId = (
                el.getAttribute('data-job-apply-field-id') || el.id || el.name || `field-${index}`
              );
              el.setAttribute('data-job-apply-field-id', fieldId);
              const labels = el.labels ? Array.from(el.labels).map(x => x.innerText).join(' ') : '';
              const described = el.getAttribute('aria-labelledby');
              const ariaText = described
                ? (document.getElementById(described)?.innerText || '')
                : '';
              const group = el.closest('fieldset,.ashby-application-form-field-entry,.application-question');
              const groupLabel = (group?.querySelector('legend,.ashby-application-form-question-title,.application-label')?.textContent || '').trim();
              return {
                field_id: fieldId,
                dom_id: el.id || '',
                dom_name: el.name || '',
                role: el.getAttribute('role') || '',
                controls: el.getAttribute('aria-controls') || '',
                owns: el.getAttribute('aria-owns') || '',
                expanded: el.getAttribute('aria-expanded') || '',
                widget: el.matches('.ashby-application-form-input-autocomplete') ? 'ashby_autocomplete' : '',
                group_label: groupLabel,
                label: (
                  (el.type === 'file' && groupLabel ? groupLabel : '') ||
                  labels || el.getAttribute('aria-label') || ariaText ||
                  el.placeholder || el.name || ''
                ).trim(),
                input_type: (el.type || '').toLowerCase(),
                tag_name: el.tagName.toLowerCase(),
                max_length: el.maxLength >= 0 ? el.maxLength : null,
                required: !!el.required || el.getAttribute('aria-required') === 'true' ||
                  [...(group?.querySelector('.ashby-application-form-question-title')?.classList || [])]
                    .some(name => name.startsWith('_required_')),
                options: el.tagName === 'SELECT'
                  ? Array.from(el.options).map(o => o.text.trim()).filter(Boolean)
                  : []
              };
            })
            """,
        )
        self._field_bindings = {item["field_id"]: item for item in raw_fields}
        return InspectionResult(questions=[self._question(item) for item in raw_fields])

    async def fill(
        self,
        page: Any,
        answers: list[ResolvedAnswer],
        *,
        resume_path: Path,
        cover_letter_path: Path | None = None,
    ) -> None:
        for answer in answers:
            if answer.requires_human:
                raise ATSBlocked(
                    answer.blocker or BlockerKind.UNKNOWN_QUESTION, answer.reason or ""
                )
            if answer.value is None:
                continue
            binding = self._field_bindings.get(answer.field_id)
            if binding is None:
                raise FieldBindingError(f"field was not inspected: {answer.field_id}")
            locator = await locate_inspected_field(page, binding)
            if answer.value == "UPLOAD_RESUME":
                await locator.set_input_files(str(resume_path))
                self._uploaded_file_names[answer.field_id] = resume_path.name
            elif answer.value == "UPLOAD_COVER_LETTER":
                if cover_letter_path is None:
                    raise ATSBlocked(
                        BlockerKind.UNKNOWN_QUESTION, "cover letter requested but absent"
                    )
                await locator.set_input_files(str(cover_letter_path))
                self._uploaded_file_names[answer.field_id] = cover_letter_path.name
            else:
                tag = await locator.evaluate("el => el.tagName")
                if tag == "SELECT":
                    await locator.select_option(label=answer.value)
                else:
                    await locator.fill(answer.value)

    async def verify(self, page: Any) -> list[str]:
        return await page.eval_on_selector_all(
            "input[required], textarea[required], select[required], [aria-required=true]",
            """
            els => els.filter(el => {
              if (el.disabled || el.type === 'hidden') return false;
              const style = window.getComputedStyle(el);
              if (style.display === 'none' || style.visibility === 'hidden') return false;
              if (el.type === 'checkbox') return !el.checked;
              if (el.type === 'radio') {
                if (!el.name) return !el.checked;
                const group = Array.from(document.querySelectorAll('input[type=radio]'))
                  .filter(item => item.name === el.name && !item.disabled);
                return !group.some(item => item.checked);
              }
              const value = el.value ?? el.getAttribute('aria-valuetext') ?? '';
              return !String(value).trim();
            }).map(el => el.id || el.name || el.getAttribute('aria-label') || 'unknown')
            """,
        )

    async def readiness_issues(self, page: Any) -> list[str]:
        """Return deterministic reasons the host must not submit yet."""
        blocker = await self._detect_blocker(page)
        if blocker:
            raise ATSBlocked(*blocker)
        issues = [f"required field is empty: {field_id}" for field_id in await self.verify(page)]
        visible_submit = None
        for selector in self.submit_selectors:
            button = page.locator(selector).first
            if await button.count() and await button.is_visible():
                visible_submit = button
                break
        if visible_submit is None:
            issues.append("final submit control is not visible")
        elif not await visible_submit.is_enabled():
            issues.append("final submit control is disabled")
        return issues

    async def submit(self, page: Any, claim: SubmissionClaim) -> ApplicationResult:
        if not self.settings.allow_live_submit:
            raise PermissionError("live submit is disabled; set ALLOW_LIVE_SUBMIT=true explicitly")
        blocker = await self._detect_blocker(page)
        if blocker:
            raise ATSBlocked(*blocker)
        missing = await self.verify(page)
        if missing:
            raise ATSBlocked(BlockerKind.UNKNOWN_QUESTION, f"required fields missing: {missing}")
        button = await self._visible_submit_control(page)
        if button is None:
            raise ATSBlocked(BlockerKind.UNKNOWN_QUESTION, "submit control not found")
        if not claim.claim():
            return ApplicationResult(submitted=False, receipt={"reason": "already_claimed"})
        before_url = page.url
        await button.click()
        confirmed = await self._wait_for_submission_confirmation(page, before_url)
        if not confirmed:
            raise ATSBlocked(
                BlockerKind.SUBMIT_OUTCOME_UNKNOWN,
                "submit was clicked but no ATS confirmation was observed; do not retry",
                evidence={"before_url": before_url, "final_url": page.url},
            )
        receipt = {
            "url": page.url,
            "status": "submission_confirmed",
            "confirmation": confirmed,
        }
        claim.complete(receipt)
        return ApplicationResult(submitted=True, receipt=receipt)

    async def _visible_submit_control(self, page: Any) -> Any | None:
        for selector in self.submit_selectors:
            button = page.locator(selector).first
            if await button.count() and await button.is_visible():
                return button
        return None

    async def _wait_for_submission_confirmation(self, page: Any, before_url: str) -> str | None:
        deadline = (
            asyncio.get_running_loop().time() + self.settings.submit_confirmation_timeout_seconds
        )
        while asyncio.get_running_loop().time() < deadline:
            current_url = page.url
            url_confirmation = self._url_confirmation(current_url, before_url)
            if url_confirmation:
                return url_confirmation
            ashby_success = page.locator(".ashby-application-form-success-container").first
            if await ashby_success.count() and await ashby_success.is_visible():
                return "ashby_success_container"
            ashby_failure = page.locator(".ashby-application-form-failure-container").first
            if await ashby_failure.count() and await ashby_failure.is_visible():
                raise ATSBlocked(
                    BlockerKind.SUBMIT_OUTCOME_UNKNOWN,
                    "Ashby displayed a submission failure after the submit claim; "
                    "reconcile before retrying",
                )
            try:
                body_text = await page.locator("body").inner_text(timeout=2_000)
            except Exception:  # noqa: BLE001 - normalize optional Playwright failures
                body_text = ""
            if self.success_text_pattern.search(body_text):
                return "success_text"
            await asyncio.sleep(0.5)
        return None

    def _url_confirmation(self, current_url: str, before_url: str) -> str | None:
        if current_url != before_url and self.success_url_pattern.search(current_url):
            return "success_url"
        return None

    async def _detect_blocker(self, page: Any) -> tuple[BlockerKind, str] | None:
        self.captcha_evidence = await inspect_captcha(page)
        if self.captcha_evidence["blocking"]:
            return (
                BlockerKind.CAPTCHA,
                "active or unclassified visible CAPTCHA requires manual takeover",
            )
        if await page.locator(
            'input[autocomplete="one-time-code"], input[name*="otp" i], input[id*="otp" i]'
        ).count():
            return BlockerKind.TWO_FACTOR, "2FA requires manual takeover"
        if await page.locator('input[type="password"]').count():
            return BlockerKind.LOGIN_REQUIRED, "login requires manual takeover"
        return None

    @staticmethod
    def _question(item: dict[str, Any]) -> FormQuestion:
        input_type = item.get("input_type", "")
        if input_type == "file":
            semantic_key = (
                "cover_letter_upload"
                if re.search(r"cover letter", item["label"], re.IGNORECASE)
                else "resume_upload"
            )
            return FormQuestion(**item, semantic_key=semantic_key)
        label = item.get("group_label") or item["label"]
        # Composite selectors can contain anonymous helper inputs inside a Phone
        # or Country question. Group context alone does not make them writable
        # answers; leave custom selectors and unnamed helpers to inspected actions.
        if (
            item.get("tag_name", "input") not in {"input", "textarea", "select"}
            or (item.get("role") == "combobox" and item.get("tag_name") != "select")
            or not (item["label"] or item.get("dom_id") or item.get("dom_name"))
        ):
            return FormQuestion(**item)
        # A choice mentioning Email/LinkedIn is not a contact text field. Keep
        # choices unresolved until a question-specific answer is available.
        if input_type in {"checkbox", "radio", "button", "submit"}:
            return FormQuestion(**item)
        for pattern, semantic_key in SENSITIVE_PATTERNS:
            if re.search(pattern, label, re.IGNORECASE):
                return FormQuestion(
                    **item,
                    semantic_key=semantic_key,
                    sensitivity=Sensitivity.SENSITIVE,
                )
        for pattern, semantic_key in NORMAL_PATTERNS:
            if re.search(pattern, label, re.IGNORECASE):
                return FormQuestion(**item, semantic_key=semantic_key)
        return FormQuestion(**item)


class GreenhouseAdapter(PlaywrightATSAdapter):
    pass


class LeverAdapter(PlaywrightATSAdapter):
    """Lever hosted forms put hCaptcha in front of the real form POST.

    The visible button only asks hCaptcha for a token. Intercept the actual
    application POST and acquire the durable claim immediately before that
    request is released to the network. A CAPTCHA that never releases a POST
    therefore remains safely retryable instead of becoming an unknown submit.
    """

    async def submit(self, page: Any, claim: SubmissionClaim) -> ApplicationResult:
        if not self.settings.allow_live_submit:
            raise PermissionError("live submit is disabled; set ALLOW_LIVE_SUBMIT=true explicitly")
        blocker = await self._detect_blocker(page)
        if blocker:
            raise ATSBlocked(*blocker)
        missing = await self.verify(page)
        if missing:
            raise ATSBlocked(BlockerKind.UNKNOWN_QUESTION, f"required fields missing: {missing}")
        button = await self._visible_submit_control(page)
        if button is None:
            raise ATSBlocked(BlockerKind.UNKNOWN_QUESTION, "submit control not found")

        before_url = page.url
        form = page.locator("form#application-form, form").first
        action = await form.get_attribute("action") if await form.count() else None
        submission_url = urldefrag(urljoin(before_url, action or before_url)).url
        request_seen = asyncio.Event()
        response_seen = asyncio.Event()
        evidence: dict[str, Any] = {
            "request_observed": False,
            "request_url": submission_url,
        }
        claim_error: Exception | None = None

        async def guard_submission(route: Any, request: Any) -> None:
            nonlocal claim_error
            if request.method.upper() != "POST":
                await route.fallback()
                return
            if evidence["request_observed"]:
                evidence["duplicate_request_blocked"] = True
                await route.abort("blockedbyclient")
                return
            evidence["request_observed"] = True
            try:
                evidence["claim_acquired"] = claim.claim()
            except Exception as exc:  # noqa: BLE001 - surface durable-claim failures to caller
                claim_error = exc
                evidence["claim_acquired"] = False
            request_seen.set()
            if not evidence["claim_acquired"]:
                await route.abort("blockedbyclient")
                return
            await route.fallback()

        def record_response(response: Any) -> None:
            request = response.request
            if request.method.upper() == "POST" and urldefrag(request.url).url == submission_url:
                evidence["response_status"] = response.status
                evidence["response_url"] = response.url
                response_seen.set()

        await page.route(submission_url, guard_submission)
        page.on("response", record_response)
        try:
            # The click itself is not a submit boundary on Lever: it may only
            # launch hCaptcha. The route above owns the real boundary.
            await button.click(no_wait_after=True)
            await self._wait_for_lever_submission_request(page, request_seen)
            if claim_error is not None:
                raise claim_error
            if not evidence["request_observed"]:
                invalid = await page.eval_on_selector_all(
                    "form#application-form :invalid, form :invalid",
                    "els => els.map(el => el.id || el.name || el.getAttribute('aria-label') || 'unknown')",
                )
                evidence["invalid_fields"] = invalid
                if invalid:
                    raise ATSBlocked(
                        BlockerKind.UNKNOWN_QUESTION,
                        "Lever rejected the form before sending a submit request because fields "
                        f"remain invalid: {invalid}",
                        evidence=evidence,
                    )
                if await page.locator(
                    '#h-captcha, .h-captcha, iframe[src*="hcaptcha" i], '
                    'iframe[src*="captcha" i], [name="h-captcha-response"]'
                ).count():
                    raise ATSBlocked(
                        BlockerKind.CAPTCHA,
                        "Lever hCaptcha did not release the application POST; no submit request "
                        "was sent and no durable submit claim was created",
                        evidence=evidence,
                    )
                raise ATSBlocked(
                    BlockerKind.UNKNOWN_QUESTION,
                    "Lever submit control did not produce an application POST; no durable submit "
                    "claim was created",
                    evidence=evidence,
                )
            if not evidence.get("claim_acquired"):
                return ApplicationResult(
                    submitted=False,
                    receipt={"reason": "already_claimed", "submission": evidence},
                )

            confirmed = await self._wait_for_submission_confirmation(page, before_url)
            if not confirmed:
                # A claimed POST can still be in flight when confirmation exits.
                # Collect a bounded response signal without releasing/retrying it.
                try:
                    await asyncio.wait_for(response_seen.wait(), timeout=2)
                except TimeoutError:
                    pass
                raise ATSBlocked(
                    BlockerKind.SUBMIT_OUTCOME_UNKNOWN,
                    "Lever application POST was sent but no ATS confirmation was observed; "
                    "do not retry",
                    evidence=evidence,
                )
            receipt = {
                "url": page.url,
                "status": "submission_confirmed",
                "confirmation": confirmed,
                "submission": evidence,
            }
            claim.complete(receipt)
            return ApplicationResult(submitted=True, receipt=receipt)
        finally:
            page.remove_listener("response", record_response)
            await page.unroute(submission_url, guard_submission)

    async def _wait_for_lever_submission_request(
        self, page: Any, request_seen: asyncio.Event
    ) -> None:
        deadline = (
            asyncio.get_running_loop().time() + self.settings.submit_confirmation_timeout_seconds
        )
        while not request_seen.is_set() and asyncio.get_running_loop().time() < deadline:
            blocker = await self._detect_blocker(page)
            if blocker:
                return
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                await asyncio.wait_for(request_seen.wait(), timeout=min(0.25, remaining))
            except TimeoutError:
                pass

    def _url_confirmation(self, current_url: str, before_url: str) -> str | None:
        if current_url != before_url:
            path = urlparse(current_url).path.lower().rstrip("/")
            if path.endswith("/thanks"):
                return "lever_thanks_url"
            if path.endswith("/already-received"):
                return "lever_already_received_url"
        return super()._url_confirmation(current_url, before_url)


class AshbyAdapter(PlaywrightATSAdapter):
    async def verify(self, page: Any) -> list[str]:
        missing = await super().verify(page)
        fields = await page.locator(".ashby-application-form-field-entry, fieldset").evaluate_all(
            """(fields, uploaded) => {
              const visible = el => el && el.getClientRects().length > 0 &&
                getComputedStyle(el).visibility !== 'hidden';
              return fields.filter(visible).flatMap(field => {
                const heading = field.querySelector(
                  '.ashby-application-form-question-title, legend'
                );
                if (!heading || !Array.from(heading.classList).some(
                  name => name.startsWith('_required_')
                )) return [];
                const controls = Array.from(field.querySelectorAll('input, textarea, select'));
                const ids = controls.flatMap(el => [el.id, el.name,
                  el.getAttribute('data-job-apply-field-id')]).filter(Boolean);
                const key = field.getAttribute('data-field-path') || ids[0] ||
                  heading.textContent.trim();
                let ready = false;
                const file = field.querySelector('input[type=file]');
                const toggles = field.querySelectorAll('button[aria-pressed]');
                if (file) {
                  const expected = uploaded[file.getAttribute('data-job-apply-field-id')] ||
                    uploaded[file.id];
                  const items = Array.from(field.querySelectorAll(
                    '.ashby-application-form-input-file-item'
                  )).filter(visible);
                  ready = !!expected && items.length === 1 &&
                    items[0].querySelector('.ashby-application-form-input-file-item-name')
                      ?.textContent.trim() === expected &&
                    !!items[0].querySelector('.ashby-application-form-input-file-item-delete');
                } else if (toggles.length) {
                  ready = Array.from(toggles).filter(
                    el => el.getAttribute('aria-pressed') === 'true'
                  ).length === 1;
                } else if (field.querySelector('input[type=radio]')) {
                  ready = !!field.querySelector('input[type=radio]:checked');
                } else if (field.querySelector('input[type=checkbox]')) {
                  ready = !!field.querySelector('input[type=checkbox]:checked');
                } else {
                  const values = controls.filter(el => el.type !== 'hidden' &&
                    !el.disabled && visible(el));
                  ready = values.length > 0 && values.every(el =>
                    String(el.value || '').trim() && el.getAttribute('aria-expanded') !== 'true');
                }
                if (field.querySelector('[aria-invalid=true], [aria-busy=true]') ||
                    Array.from(field.querySelectorAll('[role=alert]')).some(visible)) ready = false;
                return [{key, ids, ready, label: heading.textContent.trim()}];
              });
            }""",
            self._uploaded_file_names,
        )
        self._required_field_labels = {
            key: field["label"] for field in fields for key in [field["key"], *field["ids"]]
        }
        for field in fields:
            if field["ready"]:
                missing = [value for value in missing if value not in field["ids"]]
            elif field["key"] not in missing:
                missing.append(field["key"])
        return missing

    async def readiness_issues(self, page: Any) -> list[str]:
        issues = await super().readiness_issues(page)
        labels = getattr(self, "_required_field_labels", {})
        prefix = "required field is empty: "
        return [
            f"{issue} ({labels[issue.removeprefix(prefix)]})"
            if issue.startswith(prefix) and issue.removeprefix(prefix) in labels
            else issue
            for issue in issues
        ]
