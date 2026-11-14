"""DOM observations, checked stable identities, and independently read-back effects."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Error, expect

from .captcha import inspect_captcha
from .cua_harness import UnsafeComputerAction, scroll_amount


class RecoverableBrowserError(RuntimeError):
    """A fresh observation/replan is allowed, but the previous action is not replayed."""


DESCRIBE = r"""root => {
  const text = x => (x || '').replace(/\s+/g, ' ').trim();
  const path = el => {
    const parts = [];
    for (let node=el; node?.parentElement; node=node.parentElement) {
      const peers = [...node.parentElement.children].filter(x=>x.tagName===node.tagName);
      parts.unshift(node.tagName.toLowerCase()+':nth-of-type('+(peers.indexOf(node)+1)+')');
    }
    return parts.join(' > ');
  };
  const describe = el => {
    const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(id => el.ownerDocument.getElementById(id)?.textContent || '').join(' ');
    const group = el.closest('fieldset,[role=group],.ashby-application-form-field-entry,.application-question');
    const question = group?.querySelector('legend,.ashby-application-form-question-title,.application-label');
    const labels = el.labels ? [...el.labels].map(x => x.textContent).join(' ') : '';
    const tag = el.tagName.toLowerCase(), type = (el.type || '').toLowerCase();
    const role = el.getAttribute('role') || ({button:'button',a:'link',select:'combobox',textarea:'textbox'}[tag]) ||
      (tag === 'input' ? ({checkbox:'checkbox',radio:'radio',button:'button',submit:'button'}[type] || 'textbox') : '');
    const label = text(labels || el.getAttribute('aria-label') || labelled || el.placeholder ||
      (['button','a','option'].includes(tag) || el.getAttribute('role') ? el.textContent : '') || el.name);
    const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
    return {tag, type, role, label, group:text(question?.textContent || ''), form_bound:!!el.form,
      dom_id:el.id || '', dom_name:el.name || '', placeholder:el.getAttribute('placeholder') || '',
      value:el.value == null ? '' : String(el.value), checked:!!el.checked,
      required:!!el.required || el.getAttribute('aria-required') === 'true' ||
        [...(question?.classList || [])].some(name=>name.startsWith('_required_')),
      disabled:!!el.disabled || el.getAttribute('aria-disabled') === 'true',
      visible:rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden',
      invalid:el.getAttribute('aria-invalid') === 'true',
      validation:el.validationMessage || '', expanded:el.getAttribute('aria-expanded'),
      pressed:el.getAttribute('aria-pressed'), focused:el === el.ownerDocument.activeElement,
      listbox_id:el.closest('[role=listbox]')?.id || '',
      options:tag === 'select' ? [...el.options].map(o=>text(o.textContent)) : [],
      selected_labels:tag === 'select' ? [...el.selectedOptions].map(o=>text(o.textContent)) : [],
      href:tag === 'a' ? el.href : '', link_path:tag === 'a' ? path(el) : '',
      bounds:{x:rect.x,y:rect.y,width:rect.width,height:rect.height}};
  };
  if (root && root.nodeType === 1) return describe(root);
  const all = [];
  const collect = scope => {
    for (const el of scope.querySelectorAll('*')) {
      if (el.matches('input,textarea,select,button,a,[role=button],[role=combobox],[role=option],[role=checkbox],[role=radio]')) {
        const item = describe(el);
        if (item.visible && item.type !== 'hidden') all.push(item);
      }
      if (el.shadowRoot) collect(el.shadowRoot);
    }
  };
  collect(document);
  return {elements:all.slice(0,300), truncated:all.length > 300,
    scroll:{x:window.scrollX,y:window.scrollY}, text:text(document.body?.innerText).slice(-8000)};
}"""


def normalize(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def same_identity(actual: dict, expected: dict) -> bool:
    if "frame" in actual and "frame" in expected and actual["frame"] != expected["frame"]:
        return False
    if expected.get("tag") == "a" and any(
        actual.get(k) != expected.get(k) for k in ("href", "link_path")
    ):
        return False
    return all(
        normalize(actual.get(k)) == normalize(expected.get(k))
        for k in ("tag", "type", "role", "label", "group")
    )


def fingerprint(observation: dict) -> str:
    elements = [
        {k: v for k, v in e.items() if k not in {"ref", "bounds"}} for e in observation["elements"]
    ]
    state = {"url": observation["url"], "elements": elements, "scroll": observation["scroll"]}
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]


def is_final_control(element: dict, elements: list[dict]) -> bool:
    has_form = any(
        e["tag"] in {"input", "textarea", "select"} or e.get("pressed") is not None
        for e in elements
    )
    if element.get("role") not in {"button", "link"} or not has_form:
        return False
    if element.get("type") == "submit" and element.get("form_bound"):
        return True
    return bool(
        re.search(
            r"\bsubmit\b|send application|complete application|apply now",
            element.get("label", ""),
            re.IGNORECASE,
        )
    )


class DOMBrowser:
    def __init__(self, page: Any, *, action_timeout_ms: int = 10_000) -> None:
        self.page = page
        self.initial_host = urlparse(page.url).hostname
        self.timeout = action_timeout_ms
        self.generation = 0
        self.frames: dict[int, Any] = {}

    def assert_origin(self) -> None:
        if urlparse(self.page.url).hostname != self.initial_host:
            raise UnsafeComputerAction("browser left the initial ATS host")

    async def observe(self) -> dict:
        self.assert_origin()
        self.generation += 1
        self.frames = {}
        elements, excluded, scrolls = [], [], []
        main_text = ""
        truncated = False
        for index, frame in enumerate(self.page.frames):
            if frame != self.page.main_frame and urlparse(frame.url).hostname != self.initial_host:
                excluded.append({"frame": index, "reason": "outside_initial_host"})
                continue
            try:
                state = await frame.evaluate(DESCRIBE)
            except Error:
                excluded.append({"frame": index, "reason": "unavailable"})
                continue
            self.frames[index] = frame
            if frame == self.page.main_frame:
                main_text = state["text"]
            truncated |= state["truncated"]
            scrolls.append(state["scroll"])
            for item in state["elements"]:
                elements.append(
                    {
                        **item,
                        "frame": index,
                        "frame_url": frame.url,
                        "ref": f"d{self.generation}:{len(elements)}",
                    }
                )
        if 0 not in self.frames:
            raise RecoverableBrowserError("main DOM is unavailable")
        return {
            "url": self.page.url,
            "captcha": await inspect_captcha(self.page),
            "elements": elements,
            "text": main_text,
            "excluded_frames": excluded,
            "truncated": truncated,
            "scroll": scrolls,
        }

    async def locate(self, element: dict):
        frame = self.frames.get(element["frame"])
        if frame is None or frame.is_detached() or frame.url != element["frame_url"]:
            raise RecoverableBrowserError("target frame changed; observe again")
        candidates = []
        for attribute, key in (("id", "dom_id"), ("name", "dom_name")):
            if element.get(key):
                candidates.append(frame.locator(f"[{attribute}={json.dumps(element[key])}]"))
        if element["label"]:
            candidates += [frame.get_by_label(element["label"], exact=True)]
            if element["role"]:
                candidates += [
                    frame.get_by_role(element["role"], name=element["label"], exact=True)
                ]
        if element.get("placeholder"):
            candidates.append(frame.get_by_placeholder(element["placeholder"], exact=True))
        for candidate in candidates:
            count = await candidate.count()
            if count > 15:
                continue
            matches = []
            for i in range(count):
                current = candidate.nth(i)
                try:
                    actual = await current.evaluate(DESCRIBE, timeout=self.timeout)
                except Error:
                    continue
                if same_identity(actual, element) and actual["visible"] and not actual["disabled"]:
                    matches.append(current)
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise RecoverableBrowserError("target is ambiguous; observe again")
        raise RecoverableBrowserError("stable target not found; observe again")

    async def execute(self, action: dict, observation: dict) -> dict:
        self.assert_origin()
        kind = action["action"]
        if kind == "scroll":
            await self.page.mouse.wheel(
                0, max(-1200, min(1200, scroll_amount(action.get("value"))))
            )
            return {"outcome": "delivered", "effect": "unverified"}
        element = next((e for e in observation["elements"] if e["ref"] == action.get("ref")), None)
        if element is None:
            raise UnsafeComputerAction("action ref is not in the current DOM observation")
        if is_final_control(element, observation["elements"]):
            raise UnsafeComputerAction("planner attempted a final submit control")
        if element["type"] in {"password", "file"}:
            raise UnsafeComputerAction("planner cannot operate password or upload controls")
        if element.get("href") and urlparse(element["href"]).hostname != self.initial_host:
            raise UnsafeComputerAction("planner link leaves the initial ATS host")
        if kind == "fill" and (
            element["tag"] not in {"input", "textarea"}
            or element["type"] in {"checkbox", "radio", "submit", "button"}
        ):
            raise UnsafeComputerAction("fill is incompatible with the inspected control type")
        locator = await self.locate(element)
        try:
            if kind == "fill":
                await locator.fill(action["value"], timeout=self.timeout)
                await expect(locator).to_have_value(action["value"], timeout=self.timeout)
            elif kind == "select":
                if element["tag"] != "select" or action["value"] not in element["options"]:
                    raise UnsafeComputerAction("select does not match a native option")
                await locator.select_option(label=action["value"], timeout=self.timeout)
                actual = await locator.locator("option:checked").all_text_contents()
                if [normalize(x) for x in actual] != [normalize(action["value"])]:
                    raise RecoverableBrowserError("selected option did not persist")
            elif kind in {"check", "uncheck"}:
                if element["type"] not in {"checkbox", "radio"}:
                    raise UnsafeComputerAction("check action requires a native choice control")
                await locator.set_checked(kind == "check", timeout=self.timeout)
                await expect(locator).to_be_checked(checked=kind == "check", timeout=self.timeout)
            elif kind == "click":
                await locator.click(timeout=self.timeout)
                return {"outcome": "delivered", "effect": "unverified"}
            else:
                raise UnsafeComputerAction("unsupported DOM action")
        except (Error, AssertionError) as exc:
            # Reobserve before any retry: a timeout can occur after delivery.
            raise RecoverableBrowserError(
                type(exc).__name__ + ": effect requires reinspection"
            ) from exc
        return {"outcome": "verified", "effect": "field_readback"}


def semantic_issues(observation: dict) -> list[str]:
    """Reject the known past-employment narrative in a current-company field."""
    issues = []
    for item in observation["elements"]:
        if (
            re.search(r"\bcurrent (?:company|employer)\b", item.get("label", ""), re.IGNORECASE)
            and item.get("value")
            and re.search(
                r"most recent|previous|during my studies|graduate|master.s",
                item["value"],
                re.IGNORECASE,
            )
        ):
            issues.append("current company contains an unverified employment narrative")
    return issues
