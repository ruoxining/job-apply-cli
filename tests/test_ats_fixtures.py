from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from job_apply_agent.ats.base import ATSBlocked
from job_apply_agent.ats.playwright_adapter import AshbyAdapter, GreenhouseAdapter, LeverAdapter
from job_apply_agent.config import Settings
from job_apply_agent.domain.enums import BlockerKind, Sensitivity
from job_apply_agent.domain.schemas import ResolvedAnswer

FIXTURES = Path(__file__).parent / "fixtures"


async def test_ashby_uploaded_attachment_survives_empty_native_file_input(tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("""
          <div class="ashby-application-form-field-entry" data-field-path="resume">
            <label class="ashby-application-form-question-title _required_fixture" for="resume">
              Resume</label><input id="resume" type="file" required>
            <div class="ashby-application-form-input-file-item">
              <span class="ashby-application-form-input-file-item-name">resume.docx</span>
              <button class="ashby-application-form-input-file-item-delete">Delete file</button>
            </div>
          </div><button type="submit">Submit Application</button>
        """)
        adapter = AshbyAdapter(Settings())
        await adapter.inspect(page)
        # A matching filename on the page is insufficient without our own upload.
        assert await adapter.verify(page) == ["resume"]
        resume = tmp_path / "resume.docx"
        resume.write_bytes(b"fixture")
        await adapter.fill(
            page, [ResolvedAnswer(field_id="resume", value="UPLOAD_RESUME")], resume_path=resume
        )
        await page.locator("#resume").evaluate("el => {el.value = ''}")
        assert await adapter.verify(page) == []
        await page.locator(".ashby-application-form-input-file-item-name").evaluate(
            "el => {el.textContent = 'wrong.docx'}"
        )
        assert await adapter.verify(page) == ["resume"]
        await page.locator(".ashby-application-form-input-file-item").evaluate("el => el.remove()")
        assert await adapter.verify(page) == ["resume"]
        await browser.close()


async def test_ashby_custom_required_fields_need_explicit_selection():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("""
          <div class="ashby-application-form-field-entry" data-field-path="location">
            <label class="ashby-application-form-question-title _required_fixture">Location</label>
            <input role="combobox" aria-expanded="true" value="Toronto">
          </div>
          <fieldset data-field-path="authorization">
            <legend class="_required_fixture">Authorized to work?</legend>
            <input id="authorized" type="radio" name="authorized" value="yes">Yes
          </fieldset>
          <div class="ashby-application-form-field-entry" data-field-path="sponsorship">
            <label class="ashby-application-form-question-title _required_fixture">Sponsorship</label>
            <button aria-pressed="false">Yes</button><button aria-pressed="false">No</button>
            <input type="checkbox" tabindex="-1">
          </div><button type="submit">Submit Application</button>
        """)
        adapter = AshbyAdapter(Settings())
        assert set(await adapter.verify(page)) == {"location", "authorization", "sponsorship"}
        assert any("(Sponsorship)" in issue for issue in await adapter.readiness_issues(page))
        await page.locator("[role=combobox]").evaluate(
            "el=>el.setAttribute('aria-expanded','false')"
        )
        await page.locator("#authorized").check()
        await page.get_by_role("button", name="No", exact=True).evaluate(
            "el=>el.setAttribute('aria-pressed','true')"
        )
        assert await adapter.verify(page) == []
        await page.locator("[role=combobox]").fill("")
        assert await adapter.verify(page) == ["location"]
        await browser.close()


class RecordingClaim:
    def __init__(self) -> None:
        self.claimed = False
        self.receipt = None

    def claim(self) -> bool:
        self.claimed = True
        return True

    def complete(self, receipt: dict) -> None:
        self.receipt = receipt


@pytest.mark.parametrize(
    ("adapter_cls", "fixture", "sensitive_key"),
    [
        (GreenhouseAdapter, "greenhouse_form.html", "eeo_gender"),
        (LeverAdapter, "lever_form.html", "work_authorization"),
        (AshbyAdapter, "ashby_form.html", "salary_expectation"),
    ],
)
async def test_adapter_inspects_fixture_and_uploads_resume(
    adapter_cls, fixture, sensitive_key, tmp_path
):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content((FIXTURES / fixture).read_text())
        adapter = adapter_cls(Settings(allow_live_submit=False))
        inspection = await adapter.inspect(page)
        assert inspection.blocker is None
        sensitive = [q for q in inspection.questions if q.semantic_key == sensitive_key]
        assert sensitive and sensitive[0].sensitivity is Sensitivity.SENSITIVE
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"fixture")
        await adapter.fill(
            page,
            [ResolvedAnswer(field_id="resume", value="UPLOAD_RESUME")],
            resume_path=resume,
        )
        assert await page.locator("#resume").evaluate("el => el.files.length") == 1
        await browser.close()


async def test_captcha_is_handed_to_a_human():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            '<form><div class="g-recaptcha" data-sitekey="x" '
            'style="width: 200px; height: 80px">Challenge</div></form>'
        )
        result = await GreenhouseAdapter(Settings()).inspect(page)
        assert result.blocker.value == "captcha"
        await browser.close()


async def test_hidden_captcha_component_is_not_a_manual_blocker():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            '<form><iframe src="https://captcha.example.test/anchor" '
            'style="display: none"></iframe></form>'
        )

        result = await GreenhouseAdapter(Settings()).inspect(page)

        assert result.blocker is None
        await browser.close()


async def test_file_inputs_distinguish_resume_from_cover_letter():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            '<label for="resume">Resume/CV</label><input id="resume" type="file">'
            '<label for="cover">Cover letter</label><input id="cover" type="file">'
        )

        inspection = await GreenhouseAdapter(Settings()).inspect(page)

        assert [question.semantic_key for question in inspection.questions] == [
            "resume_upload",
            "cover_letter_upload",
        ]
        await browser.close()


async def test_hidden_textarea_and_hidden_step_are_not_questions():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            '<textarea name="g-recaptcha-response" style="display:none"></textarea>'
            '<div hidden><input name="future" required></div>'
            '<label>Email<input name="email"></label>'
            '<input type="file" name="resume" style="display:none">'
        )
        inspection = await AshbyAdapter(Settings()).inspect(page)
        assert [q.field_id for q in inspection.questions] == ["email", "resume"]
        await browser.close()


async def test_readiness_requires_filled_required_fields_and_visible_submit():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            '<form><label for="name">Name</label><input id="name" required>'
            '<button type="submit">Submit</button></form>'
        )
        adapter = AshbyAdapter(Settings())

        assert await adapter.readiness_issues(page) == ["required field is empty: name"]
        await page.locator("#name").fill("Approved Candidate")
        assert await adapter.readiness_issues(page) == []

        await browser.close()


async def test_adapter_fills_dynamic_field_without_native_id_or_name(tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            '<form><label>Resume <input type="file" required></label>'
            '<button type="submit">Submit</button></form>'
        )
        adapter = AshbyAdapter(Settings())
        inspection = await adapter.inspect(page)
        resume_question = next(
            question
            for question in inspection.questions
            if question.semantic_key == "resume_upload"
        )
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"fixture")

        await adapter.fill(
            page,
            [ResolvedAnswer(field_id=resume_question.field_id, value="UPLOAD_RESUME")],
            resume_path=resume,
        )

        assert await page.locator('input[type="file"]').evaluate("el => el.files.length") == 1
        await browser.close()


async def test_adapter_preserves_control_metadata_for_narrative_classification():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            '<label for="impact">Share a high-impact project</label>'
            '<textarea id="impact" required maxlength="500"></textarea>'
        )

        inspection = await AshbyAdapter(Settings()).inspect(page)
        question = inspection.questions[0]

        assert question.tag_name == "textarea"
        assert question.input_type == "textarea"
        assert question.max_length == 500
        await browser.close()


async def test_ashby_submit_recognizes_current_success_container_and_message():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            """
            <form onsubmit="event.preventDefault(); this.remove();
              document.querySelector('#result').style.display = 'block'">
              <button type="submit">Submit Application</button>
            </form>
            <div id="result" class="ashby-application-form-success-container"
                 style="display:none">
              Your application was successfully submitted. We'll contact you if there are next steps.
            </div>
            """
        )
        claim = RecordingClaim()

        result = await AshbyAdapter(
            Settings(allow_live_submit=True, submit_confirmation_timeout_seconds=5)
        ).submit(page, claim)

        assert result.submitted
        assert result.receipt["confirmation"] == "ashby_success_container"
        assert claim.claimed
        assert claim.receipt == result.receipt
        await browser.close()


async def test_lever_claims_only_when_real_form_post_is_released():
    url = "https://lever.test/company/posting/apply"
    form = """
    <form id="application-form" method="POST">
      <input id="name" name="name" required value="Approved Candidate">
      <button id="btn-submit" type="button"
        onclick="document.querySelector('#real-submit').click()">Submit application</button>
      <button id="real-submit" type="submit" hidden>Send</button>
    </form>
    """

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()

        async def serve(route, request):
            if request.method == "POST":
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body="<main>Application submitted!</main>",
                )
            else:
                await route.fulfill(status=200, content_type="text/html", body=form)

        await page.route("https://lever.test/**", serve)
        await page.goto(url)
        claim = RecordingClaim()

        result = await LeverAdapter(
            Settings(allow_live_submit=True, submit_confirmation_timeout_seconds=5)
        ).submit(page, claim)

        assert result.submitted
        assert claim.claimed
        assert claim.receipt == result.receipt
        assert result.receipt["confirmation"] == "success_text"
        assert result.receipt["submission"] == {
            "request_observed": True,
            "request_url": url,
            "claim_acquired": True,
            "response_status": 200,
            "response_url": url,
        }
        await browser.close()


async def test_lever_hcaptcha_without_post_does_not_claim_submit():
    url = "https://lever.test/company/posting/apply"
    form = """
    <form id="application-form" method="POST">
      <input id="name" name="name" required value="Approved Candidate">
      <div class="h-captcha" style="display:none">Complete challenge</div>
      <input name="h-captcha-response" type="hidden">
      <button id="btn-submit" type="button"
        onclick="document.querySelector('.h-captcha').style.display='block'">
        Submit application
      </button>
      <button id="real-submit" type="submit" hidden>Send</button>
    </form>
    """

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route(
            "https://lever.test/**",
            lambda route: route.fulfill(status=200, content_type="text/html", body=form),
        )
        await page.goto(url)
        claim = RecordingClaim()

        with pytest.raises(ATSBlocked) as exc_info:
            await LeverAdapter(
                Settings(allow_live_submit=True, submit_confirmation_timeout_seconds=5)
            ).submit(page, claim)

        assert exc_info.value.kind is BlockerKind.CAPTCHA
        assert exc_info.value.evidence["request_observed"] is False
        assert not claim.claimed
        await browser.close()


async def test_lever_post_without_confirmation_keeps_claim_and_response_evidence(monkeypatch):
    url = "https://lever.test/company/posting/apply"
    form = """
    <form id="application-form" method="POST">
      <input id="name" name="name" required value="Approved Candidate">
      <button id="btn-submit" type="submit">Submit application</button>
    </form>
    """

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()

        async def serve(route, request):
            body = "<main>Still processing</main>" if request.method == "POST" else form
            await route.fulfill(status=200, content_type="text/html", body=body)

        await page.route("https://lever.test/**", serve)
        await page.goto(url)
        claim = RecordingClaim()
        adapter = LeverAdapter(
            Settings(allow_live_submit=True, submit_confirmation_timeout_seconds=5)
        )

        async def no_confirmation(page, before_url):
            return None

        monkeypatch.setattr(adapter, "_wait_for_submission_confirmation", no_confirmation)

        with pytest.raises(ATSBlocked) as exc_info:
            await adapter.submit(page, claim)

        assert exc_info.value.kind is BlockerKind.SUBMIT_OUTCOME_UNKNOWN
        assert exc_info.value.evidence["request_observed"] is True
        assert exc_info.value.evidence["claim_acquired"] is True
        assert exc_info.value.evidence["response_status"] == 200
        assert claim.claimed
        assert claim.receipt is None
        await browser.close()


def test_lever_recognizes_terminal_urls():
    before = "https://jobs.lever.co/company/posting/apply"
    adapter = LeverAdapter(Settings())

    assert (
        adapter._url_confirmation("https://jobs.lever.co/company/posting/thanks", before)
        == "lever_thanks_url"
    )
    assert (
        adapter._url_confirmation("https://jobs.lever.co/company/posting/already-received", before)
        == "lever_already_received_url"
    )
