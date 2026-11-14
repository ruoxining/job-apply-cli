import pytest
from playwright.async_api import async_playwright

from job_apply_agent.ats.captcha import inspect_captcha
from job_apply_agent.ats.playwright_adapter import GreenhouseAdapter
from job_apply_agent.config import Settings


@pytest.fixture
async def page():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chromium", headless=True)
        page = await browser.new_page()
        await page.route("https://captcha.example/**", lambda route: route.fulfill(body=""))
        yield page
        await browser.close()


@pytest.mark.parametrize(
    "widget",
    [
        '<div class="grecaptcha-badge"><iframe src="https://captcha.example/anchor?size=invisible"></iframe></div>',
        '<button class="g-recaptcha" data-sitekey="public" data-size="invisible">Submit</button>',
        '<div class="h-captcha" data-size="invisible" style="height:80px">Protected by hCaptcha</div>',
        '<iframe src="https://captcha.example/anchor" style="display:none"></iframe>',
    ],
)
async def test_passive_widgets_allow_ordinary_fields(page, widget):
    await page.set_content("<label>Name<input required></label>" + widget)
    adapter = GreenhouseAdapter(Settings())
    inspection = await adapter.inspect(page)
    assert inspection.blocker is None
    assert len(inspection.questions) == 1
    assert adapter.captcha_evidence["blocking"] is False


@pytest.mark.parametrize(
    "widget",
    [
        '<iframe src="https://captcha.example/bframe?size=invisible"></iframe>',
        '<iframe src="https://captcha.example/widget#frame=challenge&size=invisible"></iframe>',
        '<div class="g-recaptcha" data-sitekey="public" style="height:80px">Challenge</div>',
        '<iframe src="https://captcha.example/anchor?size=normal"></iframe>',
    ],
)
async def test_visible_challenges_and_unknown_widgets_still_stop(page, widget):
    await page.set_content(widget)
    assert (await GreenhouseAdapter(Settings()).inspect(page)).blocker.value == "captcha"


async def test_challenge_appearing_after_passive_badge_is_rechecked(page):
    await page.set_content('<div class="grecaptcha-badge">Protected</div>')
    assert not (await inspect_captcha(page))["blocking"]
    await page.evaluate("""() => {
      const frame=document.createElement('iframe');
      frame.src='https://captcha.example/bframe?size=invisible';
      document.body.append(frame);
    }""")
    assert (await inspect_captcha(page))["blocking"]


async def test_navigation_snapshot_loss_is_reobserved_once(page, monkeypatch):
    from playwright.async_api import Error

    await page.set_content('<div class="g-recaptcha">Challenge</div>')
    original = page.main_frame.evaluate
    calls = []

    async def evaluate(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise Error("Execution context was destroyed, most likely because of a navigation")
        return await original(*args, **kwargs)

    monkeypatch.setattr(page.main_frame, "evaluate", evaluate)
    assert (await inspect_captcha(page))["blocking"]
    assert len(calls) == 2
