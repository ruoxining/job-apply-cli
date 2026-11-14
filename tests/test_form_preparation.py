import json

import pytest
from playwright.async_api import async_playwright

from job_apply_agent.ats.answer_binding import question_key
from job_apply_agent.ats.base import ATSBlocked
from job_apply_agent.ats.cua_harness import UnsafeComputerAction
from job_apply_agent.ats.form_preparation import FormPreparation
from job_apply_agent.ats.hybrid_harness import fill_application_hybrid, validate_value
from job_apply_agent.ats.page_readiness import FieldBindingError
from job_apply_agent.ats.playwright_adapter import GreenhouseAdapter
from job_apply_agent.config import Settings
from job_apply_agent.domain.schemas import AnswerBankEntry
from job_apply_agent.services.answers import AnswerResolver


@pytest.fixture
async def page():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chromium", headless=True)
        page = await browser.new_page()
        yield page
        await browser.close()


def prepare(page, tmp_path, entries, adapter=None):
    async def resolve(questions):
        return [AnswerResolver().resolve(q, entries) for q in questions]

    return FormPreparation(
        page,
        adapter or GreenhouseAdapter(Settings()),
        resolve,
        resume_path=tmp_path / "unused.pdf",
        entries=entries,
    )


async def test_prefill_error_reaches_codex_and_recovers_with_fresh_dom(page, tmp_path):
    await page.set_content(
        '<label>Name<input id="name" required></label>'
        '<label>Email<input id="email" required></label>'
        '<button type="submit">Submit</button>'
    )
    adapter = GreenhouseAdapter(Settings())
    original_fill = adapter.fill
    calls = []

    async def fill(p, answers, **kwargs):
        calls.append(answers[0].field_id)
        if answers[0].field_id == "name":
            raise FieldBindingError("simulated hydration target loss")
        await original_fill(p, answers, **kwargs)

    adapter.fill = fill
    entries = [
        AnswerBankEntry(semantic_key="full_name", answer="Approved Name"),
        AnswerBankEntry(semantic_key="email", answer="approved@example.test"),
    ]
    preparation = prepare(page, tmp_path, entries, adapter)

    class Planner:
        async def next_action(self, prompt, screenshot):
            assert "FieldBindingError" in prompt
            observation = json.loads(
                prompt.split("DOM elements and page state:\n")[1].split("\nDriver refs:")[0]
            )
            target = next(e for e in observation["elements"] if e["dom_id"] == "name")
            return {"action": "fill", "ref": target["ref"], "value": "Approved Name"}

    result = await fill_application_hybrid(
        page,
        planner=Planner(),
        max_steps=2,
        candidate_instructions="",
        answer_lines=[],
        approved_fill_values=[],
        readiness_check=preparation,
    )
    assert calls == ["name", "email"]  # Failed prefill was not blindly repeated.
    assert result.trace[0]["route"] == "shared_prefill"
    assert result.trace[-1]["outcome"] == "verified"
    assert (await preparation()).ready


async def test_uncertain_fill_effect_is_read_back_before_any_retry(page, tmp_path):
    await page.set_content('<label>Name<input id="name" required></label><button>Submit</button>')
    adapter = GreenhouseAdapter(Settings())
    original = adapter.fill
    calls = []

    async def fill(p, answers, **kwargs):
        calls.append(1)
        await original(p, answers, **kwargs)
        raise FieldBindingError("response lost after write")

    adapter.fill = fill
    preparation = prepare(
        page, tmp_path, [AnswerBankEntry(semantic_key="full_name", answer="A")], adapter
    )
    assert not (await preparation()).ready
    assert (await preparation()).ready
    assert len(calls) == 1


async def test_policy_blockers_are_not_recoverable_prefill_errors(page, tmp_path):
    await page.set_content('<div class="g-recaptcha" style="height:80px">Challenge</div>')
    with pytest.raises(ATSBlocked):
        await prepare(page, tmp_path, [])()


async def test_current_employer_does_not_use_recent_employer_answer(page, tmp_path):
    await page.set_content(
        "<label>Current company<input required></label>"
        "<label>Will you require sponsorship?<select required>"
        "<option></option><option>No</option></select></label>"
        "<button>Submit</button>"
    )
    entries = [
        AnswerBankEntry(semantic_key="recent_employer", answer="Former employer"),
        AnswerBankEntry(semantic_key="work_authorization", answer="No"),
    ]
    state = await prepare(page, tmp_path, entries)()
    assert len(state.missing_questions) == 2
    assert not state.ready
    assert not state.answer_bindings
    assert await page.locator("input").input_value() == ""
    assert all(q["answer_key"].startswith("question:") for q in state.missing_questions)


async def test_exact_question_answer_and_expiry_are_enforced(page, tmp_path):
    from datetime import date

    await page.set_content("<label>Current company<input required></label><button>Submit</button>")
    key = question_key("Current company")
    expired = AnswerBankEntry(semantic_key=key, answer="Expired", valid_until=date(2000, 1, 1))
    assert not (await prepare(page, tmp_path, [expired])()).ready
    current = AnswerBankEntry(semantic_key=key, answer="Confirmed company")
    assert (await prepare(page, tmp_path, [current])()).ready
    assert await page.locator("input").input_value() == "Confirmed company"


async def test_contact_word_in_referral_question_cannot_receive_contact_value(page, tmp_path):
    await page.set_content(
        "<label>Which email referred you?<input required></label><button>Submit</button>"
    )
    state = await prepare(
        page, tmp_path, [AnswerBankEntry(semantic_key="email", answer="me@example.test")]
    )()
    assert not state.answer_bindings
    assert not state.ready
    assert await page.locator("input").input_value() == ""


def test_identical_yes_answer_for_other_question_does_not_authorize_target():
    binding = {
        "tag_name": "select",
        "input_type": "select-one",
        "label": "Authorized?",
        "group_label": "",
        "dom_id": "a",
        "value": "Yes",
    }
    target = {
        "tag": "select",
        "type": "select-one",
        "label": "Need sponsorship?",
        "group": "",
        "dom_id": "b",
    }
    with pytest.raises(UnsafeComputerAction, match="specific question"):
        validate_value({"action": "select", "value": "Yes"}, target, {"Yes"}, (binding,))


async def test_widget_helpers_are_not_user_questions(page, tmp_path):
    await page.set_content(
        '<label>Country<input id="country" role="combobox" required></label>'
        '<input required aria-hidden="true"><button>Submit</button>'
    )
    state = await prepare(page, tmp_path, [])()
    assert [q["question"] for q in state.missing_questions] == ["Country"]
    assert not state.ready


async def test_owned_custom_choice_selects_exact_option_and_reads_display(page, tmp_path):
    await page.set_content("""<div class="select__container">
      <label for="eligible">Are you legally eligible to work in Canada?</label>
      <input id="eligible" role="combobox" aria-expanded="false" required
        onclick="this.setAttribute('aria-expanded','true'); this.setAttribute('aria-controls','eligibility-options'); document.getElementById('eligibility-options').hidden=false">
      <span class="select__single-value"></span>
      <div id="eligibility-options" role="listbox" hidden>
        <div role="option" onclick="document.querySelector('.select__single-value').textContent='Yes'; eligible.value=''; eligible.required=false; eligible.setAttribute('aria-expanded','false'); this.parentElement.hidden=true">Yes</div>
        <div role="option">No</div>
      </div></div><div role="listbox" id="unrelated"><div role="option">Yes</div></div>
      <button>Submit</button>""")
    preparation = prepare(
        page,
        tmp_path,
        [
            AnswerBankEntry(
                semantic_key="work_authorization_canada",
                answer="Yes — authorized.",
                regions=["Canada"],
            )
        ],
    )
    state = await preparation()
    assert state.ready
    assert await page.locator(".select__single-value").inner_text() == "Yes"
    assert await page.locator("#eligible").input_value() == ""
    assert len(preparation.events) == 1
    assert (await preparation()).ready
    assert len(preparation.events) == 1


async def test_autocomplete_requires_option_click_even_when_typed_value_matches(page, tmp_path):
    from job_apply_agent.ats.playwright_adapter import AshbyAdapter

    await page.set_content("""<div class="ashby-application-form-field-entry">
      <label class="ashby-application-form-question-title _required_abc">What is the location where you permanently reside?</label>
      <input class="ashby-application-form-input-autocomplete" placeholder="Start typing..." role="combobox"
        aria-expanded="false" value="Canada" onclick="this.setAttribute('aria-expanded','true'); this.setAttribute('aria-controls','residence-options'); setTimeout(()=>document.getElementById('residence-options').hidden=false,250)">
      <div id="residence-options" role="listbox" hidden>
        <div role="option" onclick="window.selectionCount=(window.selectionCount||0)+1; const input=document.querySelector('input'); input.value='Canada'; input.setAttribute('aria-expanded','false'); this.parentElement.hidden=true">Canada</div>
        <div role="option">Canada, Kentucky, United States</div>
      </div></div><button>Submit</button>""")
    preparation = prepare(
        page,
        tmp_path,
        [AnswerBankEntry(semantic_key="country_of_residence", answer="Canada")],
        AshbyAdapter(Settings()),
    )
    state = await preparation()
    assert state.ready
    assert await page.evaluate("window.selectionCount") == 1
    assert (await preparation()).ready
    assert await page.evaluate("window.selectionCount") == 1


async def test_option_mismatch_is_reported_without_essay_or_repeated_selection(page, tmp_path):
    await page.set_content("""<div class="select__container">
      <label for="ai">Which statement best describes how you currently use AI tools in work/life?</label>
      <input id="ai" role="combobox" aria-expanded="false" required
        onclick="window.opens=(window.opens||0)+1; this.setAttribute('aria-expanded','true'); this.setAttribute('aria-controls','ai-options'); document.getElementById('ai-options').hidden=false">
      <div id="ai-options" role="listbox" hidden><div role="option">I can give concrete examples.</div></div>
      </div><button>Submit</button>""")
    preparation = prepare(
        page, tmp_path, [AnswerBankEntry(semantic_key="ai_tool_usage", answer="Yes — I use AI.")]
    )
    for _ in range(2):
        state = await preparation()
        assert not state.ready
        assert not state.answer_bindings
        assert state.missing_questions[0]["category"] == "option_mismatch"
        assert state.missing_questions[0]["options"] == ["I can give concrete examples."]
    assert await page.evaluate("window.opens") == 1
    assert await page.locator("input").input_value() == ""


async def test_generic_attach_does_not_put_resume_in_cover_letter_or_autofill(page, tmp_path):
    from job_apply_agent.ats.answer_binding import scope_question

    await page.set_content(
        '<input type="file"><label for="resume">Attach</label><input id="resume" type="file">'
        '<label for="cover_letter">Attach</label><input id="cover_letter" type="file">'
    )
    adapter = GreenhouseAdapter(Settings())
    questions = (await adapter.inspect(page)).questions
    keys = [
        scope_question(q, adapter._field_bindings[q.field_id], []).semantic_key for q in questions
    ]
    assert keys[0].startswith("question:")
    assert keys[1:] == ["resume_upload", "cover_letter_upload"]


async def test_button_only_required_question_is_collected_and_exact_answer_works(page, tmp_path):
    label = "Are you eligible to work in Canada?"
    await page.set_content(f"""<div class="ashby-application-form-field-entry">
      <div class="ashby-application-form-question-title _required_123">{label}</div>
      <button type="button" aria-pressed="false" onclick="this.setAttribute('aria-pressed','true')">Yes</button>
      <button type="button" aria-pressed="false">No</button></div><button type="submit">Submit</button>""")
    first = await prepare(page, tmp_path, [])()
    assert len(first.missing_questions) == 1
    assert first.missing_questions[0]["options"] == ["Yes", "No"]
    key = first.missing_questions[0]["answer_key"]
    state = await prepare(page, tmp_path, [AnswerBankEntry(semantic_key=key, answer="Yes")])()
    assert state.ready
    assert (
        await page.get_by_role("button", name="Yes", exact=True).get_attribute("aria-pressed")
        == "true"
    )


def test_plain_yes_button_cannot_bypass_question_approval():
    with pytest.raises(UnsafeComputerAction, match="specific question"):
        validate_value({"action": "click"}, {"role": "button", "label": "Yes"}, {"Yes"}, ())


def test_same_question_in_different_job_countries_has_separate_approval():
    label = "Are you authorized to work in the country where this job is located?"
    assert question_key(label, context="Employer|Canada") != question_key(
        label, context="Employer|US"
    )


async def test_existing_canadian_authorization_is_reused_only_for_canadian_question(page, tmp_path):
    await page.set_content(
        '<label for="canada">Are you legally eligible to work in Canada?</label><select id="canada" required>'
        "<option></option><option>Yes</option><option>No</option></select>"
        '<label for="us">Are you legally eligible to work in the US?</label><select id="us" required>'
        "<option></option><option>Yes</option><option>No</option></select><button>Submit</button>"
    )
    entry = AnswerBankEntry(
        semantic_key="work_authorization_canada",
        regions=["Canada"],
        answer="Yes — legally authorized to work in Canada under a valid PGWP.",
    )
    state = await prepare(page, tmp_path, [entry])()
    assert await page.locator("select").nth(0).input_value() == "Yes"
    assert await page.locator("select").nth(1).input_value() == ""
    assert len(state.missing_questions) == 1 and "US" in state.missing_questions[0]["question"]


def test_custom_option_requires_current_listbox_owned_by_approved_question():
    binding = {
        "role": "combobox",
        "controls": "relocation-options",
        "expanded": "true",
        "value": "Yes",
    }
    action = {"action": "click"}
    target = {"role": "option", "label": "Yes", "listbox_id": "relocation-options"}
    validate_value(action, target, set(), (binding,))
    with pytest.raises(UnsafeComputerAction):
        validate_value(action, {**target, "listbox_id": "sponsorship-options"}, {"Yes"}, (binding,))
    with pytest.raises(UnsafeComputerAction):
        validate_value(action, target, {"Yes"}, ({**binding, "expanded": "false"},))


async def test_upload_verified_after_input_is_replaced_by_delayed_filename(page, tmp_path):
    await page.set_content("""<div class="file-upload"><h3>Resume/CV*</h3>
      <label for="resume">Attach</label><input id="resume" type="file"></div>
      <div class="file-upload"><h3>Cover Letter</h3></div><button>Submit</button>""")
    resume = tmp_path / "unused.pdf"
    resume.write_bytes(b"fixture")
    await page.locator("#resume").evaluate("""el => el.addEventListener('change', () => {
      const root=el.closest('.file-upload');el.remove();
      setTimeout(()=>root.innerHTML='<h3>Resume/CV*</h3><div class="file-upload__filename">'
        +'<p>unused.pdf</p><button aria-label="Remove file">Remove</button></div>', 300);
    })""")
    preparation = prepare(
        page, tmp_path, [AnswerBankEntry(semantic_key="resume_upload", answer="UPLOAD_RESUME")]
    )
    assert (await preparation()).ready
    assert await page.locator("#resume").count() == 0
    assert (await preparation()).ready  # Recheck upload even after the input disappears.
    await page.locator(".file-upload__filename p").evaluate("el=>el.textContent='wrong.pdf'")
    assert not (await preparation()).ready


async def test_ashby_upload_uses_actual_chooser_and_acknowledgement(page, tmp_path):
    await page.set_content("""<div class="ashby-application-form-field-entry">
      <div class="ashby-application-form-question-title _required_123">Resume</div>
      <input id="resume" type="file"><button type="button" onclick="choose()">Upload File</button>
      </div><button type="submit">Submit</button>""")
    await page.evaluate("""() => window.choose=()=>{
      const input=document.createElement('input');input.type='file';
      input.onchange=()=>{
        const item=document.createElement('div');item.className='ashby-application-form-input-file-item';
        item.innerHTML='<span class="ashby-application-form-input-file-item-name">unused.pdf</span>'
          +'<button class="ashby-application-form-input-file-item-delete">Remove</button>';
        document.querySelector('.ashby-application-form-field-entry').append(item);
      };input.click();
    }""")
    (tmp_path / "unused.pdf").write_bytes(b"fixture")
    preparation = prepare(
        page, tmp_path, [AnswerBankEntry(semantic_key="resume_upload", answer="UPLOAD_RESUME")]
    )
    assert (await preparation()).ready
    assert await page.locator("#resume").evaluate("el=>el.files.length") == 0
    assert (await preparation()).ready


async def test_verified_reasoning_survives_scoped_answer_override_and_dom_readback(page, tmp_path):
    from job_apply_agent.domain.schemas import ResolvedAnswer

    await page.set_content(
        '<label>Which query language?<input id="query_language" required></label>'
        '<button type="submit">Submit</button>'
    )

    async def resolve(questions):
        return [
            ResolvedAnswer(
                field_id=q.field_id,
                value="SQL",
                source_key="verified_reasoning:test",
                reason="cited db experience",
            )
            for q in questions
        ]

    preparation = FormPreparation(
        page,
        GreenhouseAdapter(Settings()),
        resolve,
        resume_path=tmp_path / "unused.pdf",
        entries=[],
    )
    result = await preparation()
    assert await page.locator("#query_language").input_value() == "SQL"
    assert any(b.get("source_key") == "verified_reasoning:test" for b in result.answer_bindings)
    assert not result.missing_questions
