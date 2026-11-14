"""Wait for application UI and retain field identity across DOM replacement."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .captcha import CAPTCHA_EVIDENCE_JS


class FieldBindingError(RuntimeError):
    """A previously inspected field can no longer be identified safely."""


async def wait_for_application_surface(page: Any, *, timeout_ms: int = 30_000) -> dict:
    """Wait for useful application UI, not merely DOMContentLoaded or site navigation."""
    started = time.monotonic()
    handle = await page.wait_for_function(
        r"""() => {
          const visible = el => el.getClientRects().length > 0 &&
            getComputedStyle(el).visibility !== 'hidden';
          const fields = [...document.querySelectorAll(
            'input:not([type=hidden]),textarea,select,[role=combobox],'
            + '.ashby-application-form-field-entry [aria-pressed],fieldset [aria-pressed]'
          )].filter(el => !el.disabled && visible(el));
          if (fields.length) return {reason: 'form_controls', controls: fields.length};
          if ([...document.querySelectorAll('.file-upload__filename,.ashby-application-form-input-file-item')]
              .some(visible)) return {reason: 'uploaded_attachment'};
          const entry = [...document.querySelectorAll('a,button,[role=button],[role=tab]')]
            .find(el => visible(el) && !el.disabled &&
              /\bapply\b|^application$/i.test(
                (el.innerText || el.getAttribute('aria-label') || '').trim()));
          if (entry) return {reason: 'application_entry'};
          const challenge = (__CAPTCHA_PROBE__)().some(
            item => ['challenge','visible_unknown'].includes(item.state)) ||
            [...document.querySelectorAll('input[autocomplete="one-time-code"]')].some(visible);
          if (challenge) return {reason: 'challenge_visible'};
          const text = (document.body?.innerText || '').trim();
          const unavailable = /job not found|position (?:is )?(?:closed|no longer available)|job (?:is )?no longer available|page not found/i;
          if (unavailable.test(text)) {
            return {reason: 'unavailable_message'};
          }
          return false;
        }""".replace("__CAPTCHA_PROBE__", CAPTCHA_EVIDENCE_JS),
        polling=200,
        timeout=timeout_ms,
    )
    try:
        result = await handle.json_value()
    finally:
        await handle.dispose()
    return {**result, "waited_seconds": round(time.monotonic() - started, 3)}


async def locate_inspected_field(page: Any, binding: dict, *, timeout_ms: int = 30_000):
    """Prefer the original native ID/name; never rebind by generated field position."""
    if binding.get("dom_id"):
        attribute, value = "id", binding["dom_id"]
        locator = page.locator(f"[{attribute}={json.dumps(value)}]")
    elif binding.get("dom_name"):
        attribute, value = "name", binding["dom_name"]
        locator = page.locator(f"[{attribute}={json.dumps(value)}]")
    elif binding.get("label") and binding["input_type"] != "file":
        # Anonymous controls lose generated markers on hydration. Rebind only by
        # their observed accessible label/placeholder, with strict uniqueness and
        # the same type/label validation below.
        locator = page.get_by_label(binding["label"], exact=True).or_(
            page.get_by_placeholder(binding["label"], exact=True)
        )
    else:
        attribute, value = "data-job-apply-field-id", binding["field_id"]
        locator = page.locator(f"[{attribute}={json.dumps(value)}]")
    locator = locator.and_(
        page.locator(
            "input:not([disabled]),textarea:not([disabled]),select:not([disabled]),"
            "[role=combobox]:not([aria-disabled=true])"
        )
    )
    if binding["input_type"] != "file":
        locator = locator.and_(page.locator(":visible"))
    # Locator resolves again on each action, so React replacing the node does not
    # strand us on our temporary marker. Strict matching rejects duplicate IDs/names.
    await locator.wait_for(state="attached", timeout=timeout_ms)
    actual = await locator.evaluate(
        """el => {
          const described = el.getAttribute('aria-labelledby');
          const group = el.closest('fieldset,.ashby-application-form-field-entry,.application-question');
          const groupLabel = (group?.querySelector('legend,.ashby-application-form-question-title,.application-label')?.textContent || '').trim();
          return {
            tag_name: el.tagName.toLowerCase(), input_type: (el.type || '').toLowerCase(),
            label: ((el.type === 'file' && groupLabel ? groupLabel : '') ||
              (el.labels ? [...el.labels].map(x => x.innerText).join(' ') : '') ||
              el.getAttribute('aria-label') ||
              (described ? document.getElementById(described)?.innerText : '') ||
              el.placeholder || el.name || '').trim()
          };
        }""",
        timeout=timeout_ms,
    )
    for key in ("tag_name", "input_type", "label"):
        if " ".join(actual[key].split()) != " ".join(binding[key].split()):
            if (
                key == "label"
                and binding["input_type"] == "file"
                and (binding.get("dom_id") or binding.get("dom_name"))
                and re.search(r"resume|\bcv\b|cover letter", binding["label"], re.IGNORECASE)
                and re.fullmatch(
                    r"attach|upload|replace|choose file", actual["label"], re.IGNORECASE
                )
            ):
                # Native identity and file type were already proved. Greenhouse
                # may replace a document label with a generic upload action.
                continue
            raise FieldBindingError(
                f"inspected field identity changed: {binding['field_id']} ({key})"
            )
    return locator
