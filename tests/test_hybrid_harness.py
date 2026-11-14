import pytest
from playwright.async_api import async_playwright

from job_apply_agent.ats.cua_harness import FormReadiness, PlannerStopped, UnsafeComputerAction
from job_apply_agent.ats.dom_browser import DOMBrowser, RecoverableBrowserError
from job_apply_agent.ats.hybrid_harness import fill_application_hybrid, validate_value
from job_apply_agent.ats.playwright_adapter import GreenhouseAdapter
from job_apply_agent.config import Settings
from job_apply_agent.domain.schemas import ResolvedAnswer


@pytest.fixture
async def page():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chromium", headless=True)
        page = await browser.new_page()
        yield page
        await browser.close()


async def test_stable_dom_target_survives_replacement_without_markers(page):
    await page.set_content('<label for="name">Name</label><input id="name">')
    browser = DOMBrowser(page, action_timeout_ms=300)
    observation = await browser.observe()
    await page.locator("input").evaluate("el=>el.outerHTML='<input id=name>'")
    result = await browser.execute(
        {"action": "fill", "ref": observation["elements"][0]["ref"], "value": "Verified Name"},
        observation,
    )
    assert result["effect"] == "field_readback"
    assert await page.locator("input").input_value() == "Verified Name"


async def test_shared_filler_rebinds_anonymous_control_after_hydration(page, tmp_path):
    await page.set_content('<input aria-label="Full name">')
    adapter = GreenhouseAdapter(Settings())
    question = (await adapter.inspect(page)).questions[0]
    await page.locator("input").evaluate("el=>el.outerHTML='<input aria-label=\"Full name\">'")
    await adapter.fill(
        page,
        [ResolvedAnswer(field_id=question.field_id, value="Approved Name")],
        resume_path=tmp_path / "unused.pdf",
    )
    assert await page.locator("input").input_value() == "Approved Name"


async def test_changed_question_and_ambiguous_identity_are_not_written(page):
    await page.set_content('<input aria-label="Name">')
    browser = DOMBrowser(page)
    observation = await browser.observe()
    await page.locator("input").evaluate("el=>el.setAttribute('aria-label','Salary')")
    with pytest.raises(RecoverableBrowserError):
        await browser.execute(
            {"action": "fill", "ref": observation["elements"][0]["ref"], "value": "Approved"},
            observation,
        )
    await page.set_content('<input aria-label="Name"><input aria-label="Name">')
    with pytest.raises(RecoverableBrowserError, match="ambiguous"):
        await browser.locate(observation["elements"][0])
    assert await page.locator("input").evaluate_all("els=>els.every(el=>!el.value)")


async def test_duplicate_application_links_use_observed_position_and_destination(page):
    await page.set_content(
        '<header><a href="#application">Apply for this job</a></header>'
        '<footer><a href="#application">Apply for this job</a></footer>'
    )
    browser = DOMBrowser(page)
    observation = await browser.observe()
    target = observation["elements"][1]
    locator = await browser.locate(target)
    assert await locator.evaluate("el=>el.parentElement.tagName") == "FOOTER"
    await page.locator("footer a").evaluate("el=>el.href='#different'")
    with pytest.raises(RecoverableBrowserError):
        await browser.locate(target)


async def test_ambiguous_fallback_stops_without_repeating_full_budget(page, monkeypatch):
    await page.set_content('<input aria-label="Name"><input aria-label="Name">')

    async def ready():
        return FormReadiness(False, ("Name missing",))

    async def factory():
        pytest.fail("Ambiguous DOM target must not be sent to Driver")

    with pytest.raises(PlannerStopped) as caught:
        await fill_application_hybrid(
            page,
            planner=FieldPlanner(page),
            max_steps=20,
            candidate_instructions="",
            answer_lines=["full_name: Approved Name"],
            approved_fill_values=["Approved Name"],
            readiness_check=ready,
            driver_factory=factory,
        )
    assert caught.value.reason_code == "fallback_target_ambiguous"
    assert len(caught.value.trace) == 2


async def test_choice_containing_contact_words_is_not_a_text_field(page):
    await page.set_content("""<fieldset><legend>Where did you hear about us?</legend>
        <label><input type="checkbox">LinkedIn</label><label><input type="checkbox">Email</label>
        </fieldset><label>Name<input type="text"></label>""")
    adapter = GreenhouseAdapter(Settings())
    questions = (await adapter.inspect(page)).questions
    assert [q.semantic_key for q in questions] == [None, None, "full_name"]
    browser = DOMBrowser(page)
    observation = await browser.observe()
    with pytest.raises(UnsafeComputerAction, match="control type"):
        await browser.execute(
            {"action": "fill", "ref": observation["elements"][0]["ref"], "value": "a@b.example"},
            observation,
        )


async def test_composite_phone_helpers_are_not_deterministic_phone_answers(page):
    await page.set_content("""<fieldset><legend>Phone</legend>
        <input required><input id="phone" type="tel" aria-label="Phone">
        <input id="country" role="combobox" aria-label="Country">
        </fieldset>""")
    questions = (await GreenhouseAdapter(Settings()).inspect(page)).questions
    assert [q.semantic_key for q in questions] == [None, "phone", None]


def test_custom_option_click_requires_an_approved_value():
    with pytest.raises(UnsafeComputerAction, match="approved option"):
        validate_value({"action": "click"}, {"role": "option", "label": "Invented"}, {"Canada"})


async def test_upload_cosmetic_label_change_retains_question_identity(page, tmp_path):
    await page.set_content("""<fieldset><legend>Resume</legend><label for="resume">Resume</label>
        <input id="resume" type="file"></fieldset><fieldset><legend>Cover letter</legend>
        <label for="cover">Attach</label><input id="cover" type="file"></fieldset>""")
    adapter = GreenhouseAdapter(Settings())
    await adapter.inspect(page)
    await page.locator("label[for=resume]").evaluate("el=>el.textContent='Attach'")
    resume = tmp_path / "resume.txt"
    resume.write_text("fixture only")
    await adapter.fill(
        page, [ResolvedAnswer(field_id="resume", value="UPLOAD_RESUME")], resume_path=resume
    )
    assert await page.locator("#resume").evaluate("el=>el.files.length") == 1
    assert await page.locator("#cover").evaluate("el=>el.files.length") == 0


async def test_native_upload_allows_generic_caption_but_rejects_changed_document(page, tmp_path):
    from job_apply_agent.ats.page_readiness import FieldBindingError

    await page.set_content('<label for="resume">Resume/CV</label><input id="resume" type="file">')
    adapter = GreenhouseAdapter(Settings())
    await adapter.inspect(page)
    resume = tmp_path / "fixture.txt"
    resume.write_text("fixture")
    await page.locator("label").evaluate("el=>el.textContent='Attach'")
    await adapter.fill(
        page, [ResolvedAnswer(field_id="resume", value="UPLOAD_RESUME")], resume_path=resume
    )
    assert await page.locator("input").evaluate("el=>el.files.length") == 1
    await page.locator("label").evaluate("el=>el.textContent='Cover letter'")
    with pytest.raises(FieldBindingError, match="identity changed"):
        await adapter.fill(
            page, [ResolvedAnswer(field_id="resume", value="UPLOAD_RESUME")], resume_path=resume
        )


async def test_host_rejects_submit_and_wrong_control_fill(page):
    await page.set_content(
        '<input aria-label="Email"><button type="submit">Submit application</button>'
    )
    browser = DOMBrowser(page)
    observation = await browser.observe()
    with pytest.raises(UnsafeComputerAction, match="final submit"):
        await browser.execute(
            {"action": "click", "ref": observation["elements"][1]["ref"]}, observation
        )


def test_current_company_cannot_receive_past_employment_narrative():
    value = "Recent Master's graduate; most recent role was a research assistant"
    with pytest.raises(UnsafeComputerAction, match="current employer"):
        validate_value({"action": "fill", "value": value}, {"label": "Current company"}, {value})


def test_scroll_without_a_field_target_does_not_require_an_answer():
    validate_value({"action": "scroll", "value": "down"}, {}, set())


class FieldPlanner:
    def __init__(self, page):
        self.page = page
        self.prompts = []

    async def next_action(self, prompt, screenshot):
        import json

        assert screenshot.is_file()
        self.prompts.append(prompt)
        observation = json.loads(
            prompt.split("DOM elements and page state:\n")[1].split("\nDriver refs:")[0]
        )
        driver = prompt.split("Driver refs:\n")[1].strip()
        ref = (
            json.loads(driver)[0]["ref"]
            if driver != "(not active)"
            else observation["elements"][0]["ref"]
        )
        return {"action": "fill", "ref": ref, "value": "Approved Name"}


async def test_dom_success_never_initializes_driver(page):
    await page.set_content("<label>Name<input required></label>")

    async def ready():
        complete = await page.locator("input").input_value() == "Approved Name"
        return FormReadiness(complete, () if complete else ("Name missing",))

    async def unavailable():
        pytest.fail("Driver must be lazy")

    result = await fill_application_hybrid(
        page,
        planner=FieldPlanner(page),
        max_steps=2,
        candidate_instructions="",
        answer_lines=["full_name: Approved Name"],
        approved_fill_values=["Approved Name"],
        readiness_check=ready,
        driver_factory=unavailable,
    )
    assert len(result.trace) == 1 and result.trace[0]["outcome"] == "verified"


async def test_driver_handoff_uses_new_refs_and_verifies_actual_value(page, monkeypatch):
    await page.set_content("<label>Name<input required></label>")

    async def fail_primary(*args, **kwargs):
        raise RecoverableBrowserError("primary delivery unavailable")

    monkeypatch.setattr(DOMBrowser, "execute", fail_primary)
    calls = []

    class Driver:
        async def observe(self):
            return {"refs": [{"ref": "p5:7", "name": "Name", "actions": ["type"]}]}

        async def execute(self, action, observation):
            assert action["ref"] == "p5:7"
            calls.append(action["action"])
            await page.locator("input").fill(action["value"])

        async def close(self):
            calls.append("close")

    async def factory():
        calls.append("attach")
        return Driver()

    async def ready():
        return FormReadiness(await page.locator("input").input_value() == "Approved Name")

    planner = FieldPlanner(page)
    result = await fill_application_hybrid(
        page,
        planner=planner,
        max_steps=3,
        candidate_instructions="",
        answer_lines=["full_name: Approved Name"],
        approved_fill_values=["Approved Name"],
        readiness_check=ready,
        driver_factory=factory,
    )
    assert calls == ["attach", "fill", "close"]
    assert [r["route"] for r in result.trace] == ["playwright", "playwright", "driver"]
    assert result.trace[-1]["effect"] == "field_readback"


async def test_silent_noop_is_not_counted_as_recovery(page, monkeypatch):
    await page.set_content("<label>Name<input required></label>")

    async def noop(*args, **kwargs):
        return {"outcome": "delivered", "effect": "unverified"}

    monkeypatch.setattr(DOMBrowser, "execute", noop)

    async def ready():
        return FormReadiness(False, ("Name missing",))

    with pytest.raises(PlannerStopped) as caught:
        await fill_application_hybrid(
            page,
            planner=FieldPlanner(page),
            max_steps=3,
            candidate_instructions="",
            answer_lines=["full_name: Approved Name"],
            approved_fill_values=["Approved Name"],
            readiness_check=ready,
        )
    assert caught.value.reason_code == "dom_recovery_exhausted"
    assert all(row["outcome"] != "verified" for row in caught.value.trace)


async def test_value_cleared_after_tool_success_is_not_verified(page, monkeypatch):
    await page.set_content("<label>Name<input required></label>")

    async def transient_success(*args, **kwargs):
        await page.locator("input").fill("Approved Name")
        await page.locator("input").fill("")
        return {"outcome": "verified", "effect": "field_readback"}

    monkeypatch.setattr(DOMBrowser, "execute", transient_success)

    async def ready():
        return FormReadiness(False, ("Name missing",))

    with pytest.raises(PlannerStopped) as caught:
        await fill_application_hybrid(
            page,
            planner=FieldPlanner(page),
            max_steps=3,
            candidate_instructions="",
            answer_lines=["full_name: Approved Name"],
            approved_fill_values=["Approved Name"],
            readiness_check=ready,
        )
    assert all(row["outcome"] != "verified" for row in caught.value.trace)


def test_codex_configuration_does_not_require_glm():
    settings = Settings(
        cua_backend="codex", cua_enabled=True, glm_api_key=None, browser_headless=False
    )
    assert settings.cua_configuration_error() is None
