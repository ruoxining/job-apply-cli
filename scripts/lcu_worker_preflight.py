"""Exercise the production LCU adapter on synthetic, non-submitting form controls."""

import argparse
import asyncio
import json
import logging
import tempfile
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright

from job_apply_agent.ats.cua_harness import (
    FormReadiness,
    GLMActionPlanner,
    fill_application_with_driver,
    find_linux_browser_pid,
)
from job_apply_agent.ats.lcu_harness import LcuBrowserSession
from job_apply_agent.config import get_settings


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    with tempfile.TemporaryDirectory(prefix="lcu-preflight-") as directory:
        async with async_playwright() as pw:
            html = (
                "<title>LCU worker preflight</title><label>Email "
                '<input id="email"></label><button type="button" '
                "onclick=\"this.textContent='Completed'\">Continue</button>"
            )
            url = "data:text/html," + quote(html)
            context = await pw.chromium.launch_persistent_context(
                directory,
                headless=False,
                args=["--force-renderer-accessibility", "--app=" + url],
                ignore_default_args=["about:blank"],
            )
            page = context.pages[0]
            await page.goto(url)
            pid = find_linux_browser_pid(
                executable=Path(pw.chromium.executable_path), profile_dir=Path(directory)
            )
            await asyncio.sleep(1)
            browser = await LcuBrowserSession.attach(
                page=page,
                browser_pid=pid,
                prefix=settings.lcu_prefix,
                node_path=settings.lcu_node_path,
                timeout_seconds=60,
            )
            original_request = browser._request

            async def traced_request(request):
                result = await original_request(request)
                print(json.dumps({"request": request, "result": result}), flush=True)
                return result

            if args.debug:
                browser._request = traced_request
            planner = GLMActionPlanner(
                base_url=settings.glm_base_url,
                api_key=settings.glm_api_key,
                model=settings.glm_model,
                reasoning_effort=settings.glm_reasoning_effort,
                timeout_seconds=120,
            )
            if args.no_model:
                await planner.close()

                class FixturePlanner:
                    async def next_action(self, prompt):
                        del prompt
                        needs_email = (
                            await page.locator("#email").input_value() != "ada@example.test"
                        )
                        name = "Email" if needs_email else "Continue"
                        target = next(r for r in browser.latest["refs"] if r["name"] == name)
                        return {
                            "action": "fill" if needs_email else "click",
                            "ref": target["ref"],
                            "value": "ada@example.test" if needs_email else None,
                        }

                    async def close(self):
                        pass

                planner = FixturePlanner()

            async def ready():
                email_ok = await page.locator("#email").input_value() == "ada@example.test"
                clicked = await page.locator("button").inner_text() == "Completed"
                print(json.dumps({"email_ok": email_ok, "clicked": clicked}), flush=True)
                return FormReadiness(
                    ready=email_ok and clicked,
                    issues=tuple(
                        ([] if email_ok else ["Email is empty"])
                        + ([] if clicked else ["Click Continue"])
                    ),
                )

            try:
                if args.debug:
                    print(json.dumps(await browser.observe()), flush=True)
                await fill_application_with_driver(
                    browser,
                    planner=planner,
                    max_steps=5,
                    candidate_instructions="Synthetic fixture only. Fill email and click Continue.",
                    answer_lines=["email: ada@example.test"],
                    approved_fill_values=["ada@example.test"],
                    readiness_check=ready,
                )
                assert (await ready()).ready
                print(
                    json.dumps(
                        {
                            "status": "passed",
                            "backend": "lcu",
                            "model": None if args.no_model else settings.glm_model,
                            "real_submissions": 0,
                        }
                    )
                )
            finally:
                await planner.close()
                await browser.close()
                await context.close()


if __name__ == "__main__":
    asyncio.run(main())
