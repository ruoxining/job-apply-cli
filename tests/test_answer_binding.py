from datetime import date

import pytest

from job_apply_agent.ats.answer_binding import job_region, resolve_scoped_answer, scope_question
from job_apply_agent.domain.schemas import AnswerBankEntry, FormQuestion
from job_apply_agent.services.answer_derivation import (
    EMPLOYMENT_SCOPE_KEY,
    EmploymentAuthorizationScope,
)
from job_apply_agent.services.narratives import is_narrative_question


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Canada", "Canada"),
        ("Remote (Canada) · Toronto, Canada ", "Canada"),
        ("San Francisco, CA", "United States"),
        ("Tokyo, Japan", "Japan"),
        ("Toronto · Zürich · Seattle · California", None),
        ("Canada · Japan", None),
        ("Canada · Unknown", None),
        ("CA", None),
        ("Remote", None),
    ],
)
def test_only_unambiguous_job_locations_supply_region(location, expected):
    assert job_region(None, location) == expected


def test_conflicting_country_metadata_stays_unresolved():
    assert job_region("Canada", "Tokyo, Japan") is None
    assert job_region("Canada", "Unknown · Tokyo, Japan") is None


@pytest.mark.parametrize("country", ["Germany", "Australia", "Singapore", "United Kingdom"])
def test_full_country_metadata_does_not_need_a_candidate_answer_for_each_country(country):
    assert job_region(country, "") == country


@pytest.mark.parametrize("country", ["EMEA", "APAC", "Canada or Japan", "Canada/Japan", "CA"])
def test_regional_or_ambiguous_metadata_stays_an_internal_context_gap(country):
    assert job_region(country, "") is None


@pytest.mark.parametrize(
    ("region", "key", "value"),
    [
        ("Canada", "sponsorship_required_canada", "No"),
        ("United States", "sponsorship_required_united_states", "Yes"),
        ("Japan", None, None),
        (None, None, None),
    ],
)
def test_job_scoped_sponsorship_does_not_borrow_other_country_answer(region, key, value):
    q = FormQuestion(field_id="s", label="Do you require visa sponsorship? *", required=True)
    binding = {"role": "combobox"}
    entries = [
        AnswerBankEntry(
            semantic_key="sponsorship_required_canada", answer="No.", regions=["Canada"]
        ),
        AnswerBankEntry(
            semantic_key="sponsorship_required_united_states",
            answer="Yes, now or in the future.",
            regions=["United States"],
        ),
    ]
    q = scope_question(q, binding, entries, region=region)
    result = resolve_scoped_answer(q, binding, entries)
    assert result.value == value
    assert result.requires_human == (value is None)
    if key:
        assert result.source_key == key


def test_placeholder_uses_residence_question_but_phone_country_does_not():
    q = FormQuestion(field_id="a", label="Start typing...")
    b = {"role": "combobox", "group_label": "What is the location where you permanently reside?"}
    assert scope_question(q, b, []).semantic_key == "country_of_residence"
    assert scope_question(
        q.model_copy(update={"label": "Country*"}), {"group_label": "Phone"}, []
    ).semantic_key.startswith("question:")


def test_describe_in_custom_choice_never_generates_an_essay():
    q = FormQuestion(
        field_id="a",
        label="Which statement best describes how you currently use AI tools in work/life?*",
        input_type="text",
        required=True,
    )
    scoped = scope_question(q, {"role": "combobox"}, [])
    assert scoped.semantic_key == "ai_tool_usage"
    assert not is_narrative_question(scoped)
    unknown = scope_question(
        q.model_copy(update={"label": "Describe your level of expertise"}), {"role": "combobox"}, []
    )
    assert not is_narrative_question(unknown)
    prose = scope_question(
        q.model_copy(
            update={
                "label": "Describe your relevant project",
                "tag_name": "textarea",
                "input_type": "textarea",
            }
        ),
        {},
        [],
    )
    assert is_narrative_question(prose)


def test_choice_normalization_preserves_expiry_and_conflicts():
    q = FormQuestion(
        field_id="a",
        label="Eligible?",
        semantic_key="work_authorization_canada",
        required=True,
        options=["Yes", "No"],
    )
    b = {"role": "combobox"}
    a = AnswerBankEntry(semantic_key=q.semantic_key, answer="Yes — approved.", regions=["Canada"])
    assert resolve_scoped_answer(q, b, [a]).value == "Yes"
    assert resolve_scoped_answer(
        q, b, [a.model_copy(update={"valid_until": date(2000, 1, 1)})]
    ).requires_human
    conflict = a.model_copy(update={"answer": "Yes, only after approval."})
    assert (
        resolve_scoped_answer(q, b, [a, conflict]).reason
        == "multiple conflicting answer-bank values"
    )


def test_richer_ai_option_is_not_inferred_from_generic_yes():
    q = FormQuestion(
        field_id="a",
        label="AI tools",
        semantic_key="ai_tool_usage",
        required=True,
        options=["I regularly use AI and can give concrete examples.", "I do not use AI."],
    )
    entry = AnswerBankEntry(
        semantic_key="ai_tool_usage", answer="Yes — I regularly use AI assistants."
    )
    result = resolve_scoped_answer(q, {"role": "combobox"}, [entry])
    assert (
        result.requires_human and result.reason == "stored answer does not match available options"
    )


def employment_scope_entry():
    return AnswerBankEntry(
        semantic_key=EMPLOYMENT_SCOPE_KEY,
        answer=EmploymentAuthorizationScope(
            currently_authorized_countries=["China", "Canada"],
            authorized_countries_exhaustive=True,
            sponsorship_required_elsewhere=True,
            sponsorship_timeframe="now_and_future",
        ).model_dump_json(),
    )


@pytest.mark.parametrize("region", ["Japan", "Germany", "Australia", "Singapore"])
@pytest.mark.parametrize(
    "label",
    [
        "Do you require visa sponsorship?",
        "Will you now or will you in the future require employment visa sponsorship to work in the country in which the job you're applying for is located?",
    ],
)
def test_one_confirmed_scope_answers_other_countries_without_per_country_entries(region, label):
    q = FormQuestion(field_id="visa", label=label, required=True, options=["Yes", "No"])
    binding = {"role": "combobox"}
    entries = [employment_scope_entry()]
    q = scope_question(q, binding, entries, region=region)
    result = resolve_scoped_answer(q, binding, entries, job_country=region)
    assert result.value == "Yes"
    assert result.source_key == EMPLOYMENT_SCOPE_KEY
    assert region in result.reason and "now and in the future" in result.reason


def test_unknown_job_country_is_an_internal_context_gap():
    q = FormQuestion(field_id="visa", label="Do you require visa sponsorship?", required=True)
    entries = [employment_scope_entry()]
    q = scope_question(q, {}, entries)
    result = resolve_scoped_answer(q, {}, entries)
    assert result.requires_human
    assert result.reason == "job country needs resolution, not a new candidate fact"


@pytest.mark.parametrize("region", ["China", "Canada"])
def test_outside_country_rule_does_not_override_inside_country_answers(region):
    q = FormQuestion(field_id="visa", label="Do you require visa sponsorship?", required=True)
    entries = [
        employment_scope_entry(),
        AnswerBankEntry(
            semantic_key="sponsorship_required_" + region.lower(), answer="No.", regions=[region]
        ),
    ]
    q = scope_question(q, {"role": "combobox"}, entries, region=region)
    assert resolve_scoped_answer(q, {"role": "combobox"}, entries, job_country=region).value == "No"


def test_inference_still_checks_scope_expiry_conflicts_and_completeness():
    q = FormQuestion(
        field_id="visa", label="Visa?", semantic_key="sponsorship_by_country", required=True
    )
    entry = employment_scope_entry()
    expired = entry.model_copy(update={"valid_until": date(2000, 1, 1)})
    incomplete = entry.model_copy(
        update={
            "answer": entry.answer.replace(
                '"authorized_countries_exhaustive":true', '"authorized_countries_exhaustive":false'
            )
        }
    )
    for entries in ([expired], [incomplete], [entry, incomplete]):
        assert resolve_scoped_answer(q, {}, entries, job_country="Japan").requires_human


def test_confirmed_advanced_ai_option_resolves_exactly():
    option = "I am an advanced AI user and have directly used AI to completely systemize actions and workflows."
    q = FormQuestion(
        field_id="ai",
        label="Which statement best describes how you currently use AI tools in work/life?",
        required=True,
        options=[option, "I occasionally use AI tools."],
    )
    entries = [AnswerBankEntry(semantic_key="ai_tool_usage", answer=option)]
    q = scope_question(q, {"role": "combobox"}, entries)
    assert resolve_scoped_answer(q, {"role": "combobox"}, entries).value == option


@pytest.mark.parametrize(
    ("country", "value"), [("China", "Yes"), ("Canada", "Yes"), ("Japan", "No")]
)
def test_current_permission_follows_the_confirmed_exhaustive_set(country, value):
    q = FormQuestion(
        field_id="auth",
        label="Currently authorized?",
        semantic_key="work_authorization_by_country",
        required=True,
    )
    result = resolve_scoped_answer(q, {}, [employment_scope_entry()], job_country=country)
    assert result.value == value and result.source_key == EMPLOYMENT_SCOPE_KEY
