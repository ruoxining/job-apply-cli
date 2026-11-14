from pathlib import Path

import pytest
from playwright.async_api import Error, TimeoutError, async_playwright

from job_apply_agent.ats.page_readiness import FieldBindingError, wait_for_application_surface
from job_apply_agent.ats.playwright_adapter import GreenhouseAdapter
from job_apply_agent.config import Settings
from job_apply_agent.domain.schemas import ResolvedAnswer


@pytest.fixture
async def page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chromium", headless=True)
        page = await browser.new_page()
        yield page
        await browser.close()


async def test_wait_ignores_navigation_until_delayed_application_entry(page):
    await page.set_content('<nav><a href="/">Home</a></nav><main>Loading...</main>')
    await page.evaluate("""() => setTimeout(() => {
        document.querySelector('main').innerHTML = '<button>Apply for this job</button>';
    }, 400)""")
    result = await wait_for_application_surface(page, timeout_ms=2000)
    assert result["reason"] == "application_entry"
    assert await page.get_by_role("button", name="Apply for this job").is_visible()


async def test_permanently_blank_page_has_a_bounded_wait(page):
    await page.set_content("<main>Loading...</main>")
    with pytest.raises(TimeoutError):
        await wait_for_application_surface(page, timeout_ms=250)


async def test_passive_badge_does_not_end_loading_wait_early(page):
    await page.set_content('<main>Loading...</main><div class="grecaptcha-badge">Protected</div>')
    await page.evaluate("""() => setTimeout(() => {
        document.querySelector('main').innerHTML='<label>Name<input></label>';
    }, 400)""")
    state = await wait_for_application_surface(page, timeout_ms=2000)
    assert state["reason"] == "form_controls"


@pytest.mark.parametrize(
    "html,reason",
    [
        ('<div class="g-recaptcha">Challenge</div>', "challenge_visible"),
        ("<main>Job not found</main>", "unavailable_message"),
    ],
)
async def test_wait_preserves_challenge_and_closed_job_observation(page, html, reason):
    await page.set_content(html)
    assert (await wait_for_application_surface(page, timeout_ms=1000))["reason"] == reason


@pytest.mark.parametrize("attribute", ['id="first_name"', 'name="first_name"'])
async def test_fill_recovers_field_replaced_after_inspection(page, attribute):
    html = f'<input {attribute} aria-label="First name">'
    await page.set_content(html)
    adapter = GreenhouseAdapter(Settings())
    question = (await adapter.inspect(page)).questions[0]
    # A framework replaces the input, losing the injected data attribute.
    await page.locator("input").evaluate("(el, html) => el.outerHTML = html", html)
    assert await page.locator("[data-job-apply-field-id]").count() == 0
    await adapter.fill(
        page,
        [ResolvedAnswer(field_id=question.field_id, value="Approved")],
        resume_path=Path("unused.pdf"),
    )
    assert await page.locator("input").input_value() == "Approved"


async def test_fill_rejects_reused_id_for_a_different_question(page):
    await page.set_content('<input id="field" aria-label="First name">')
    adapter = GreenhouseAdapter(Settings())
    await adapter.inspect(page)
    await page.locator("input").evaluate("el => el.setAttribute('aria-label', 'Salary')")
    with pytest.raises(FieldBindingError, match="identity changed"):
        await adapter.fill(
            page,
            [ResolvedAnswer(field_id="field", value="Approved")],
            resume_path=Path("unused.pdf"),
        )
    assert await page.locator("input").input_value() == ""


async def test_fill_rejects_ambiguous_native_field_name(page):
    await page.set_content('<input name="field" aria-label="First name">')
    adapter = GreenhouseAdapter(Settings())
    await adapter.inspect(page)
    await page.evaluate(
        "document.body.insertAdjacentHTML('beforeend', '<input name=field aria-label=Other>')"
    )
    with pytest.raises(Error, match="strict mode violation"):
        await adapter.fill(
            page,
            [ResolvedAnswer(field_id="field", value="Approved")],
            resume_path=Path("unused.pdf"),
        )
    assert await page.locator("input").evaluate_all("els => els.every(el => !el.value)")
