"""Read-only CAPTCHA evidence: passive badges do not prove an active challenge."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Error

CAPTCHA_EVIDENCE_JS = r"""() => {
  const selector = 'iframe[src*="captcha" i],iframe[src*="challenges.cloudflare.com" i],'
    + '.g-recaptcha,.h-captcha,.cf-turnstile,[data-sitekey],.grecaptcha-badge,.hcaptcha-overlay-badge';
  return [...document.querySelectorAll(selector)].map(el => {
    const rect = el.getBoundingClientRect();
    const visible = rect.width > 0 && rect.height > 0 &&
      ![el, ...function*(n){while(n.parentElement){n=n.parentElement;yield n;}}(el)]
        .some(n => ['hidden','collapse'].includes(getComputedStyle(n).visibility)
          || getComputedStyle(n).display === 'none');
    const src = el.getAttribute('src') || '';
    const url = new URL(src || location.href, location.href);
    const title = (el.getAttribute('title') || '').toLowerCase();
    const challenge = el.tagName === 'IFRAME' &&
      (/\/bframe(?:[/?]|$)/i.test(url.pathname) || /(?:[?#&])frame=challenge(?:&|$)/i.test(src)
        || /challenge/.test(title) && !/cloudflare/.test(url.hostname));
    const invisible = el.getAttribute('data-size') === 'invisible'
      || /(?:[?#&])size=invisible(?:&|$)/i.test(src);
    const badge = !!el.closest('.grecaptcha-badge,.hcaptcha-overlay-badge');
    // Challenge evidence wins even if the provider was configured as invisible.
    const state = !visible ? 'hidden' : challenge ? 'challenge' :
      badge || invisible ? 'passive' : 'visible_unknown';
    return {state, tag:el.tagName.toLowerCase(), provider_host:src ? url.hostname : '',
      path:src ? url.pathname : '', title, invisible, badge,
      width:Math.round(rect.width), height:Math.round(rect.height)};
  });
}"""


async def inspect_captcha(page: Any) -> dict:
    """No token reads, widget interaction, solving or challenge suppression."""
    try:
        return await _inspect_captcha(page)
    except Error as exc:
        if not any(
            message in str(exc)
            for message in (
                "Execution context was destroyed",
                "Frame was detached",
                "frame has been detached",
            )
        ):
            raise
        # Navigation may destroy a snapshot between request/response events.
        # Reobserve once after the new DOM; never classify missing evidence as safe.
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        return await _inspect_captcha(page)


async def _inspect_captcha(page: Any) -> dict:
    evidence = []
    for index, frame in enumerate(page.frames):
        if frame != page.main_frame:
            # Provider frames are identified from their outer iframe, never operated.
            if urlparse(frame.url).hostname != urlparse(page.url).hostname:
                continue
            handle = await frame.frame_element()
            if not await handle.is_visible():
                continue
        evidence.extend(
            {**item, "frame": index} for item in await frame.evaluate(CAPTCHA_EVIDENCE_JS)
        )
    blocking = any(e["state"] in {"challenge", "visible_unknown"} for e in evidence)
    return {
        "status": "manual_required" if blocking else "passive" if evidence else "absent",
        "blocking": blocking,
        "evidence": evidence,
    }
