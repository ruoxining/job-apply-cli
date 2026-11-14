"""Question-scoped approvals for the Codex path; values alone are not authority."""

from __future__ import annotations

import hashlib
import re

from job_apply_agent.domain.schemas import FormQuestion
from job_apply_agent.services.answer_derivation import derive_employment_answer
from job_apply_agent.services.answers import AnswerResolver
from job_apply_agent.services.narratives import is_narrative_question


def question_key(label: str, group: str = "", context: str = "") -> str:
    text = " ".join((context + " | " + group + " | " + label).casefold().split()).strip(" |*")
    return "question:" + hashlib.sha256(text.encode()).hexdigest()[:20]


def job_region(country: str | None, location: str) -> str | None:
    """A narrow location vocabulary; unknown or multi-country locations stay unknown."""
    names = {
        "canada": "Canada",
        "united states": "United States",
        "usa": "United States",
        "us": "United States",
        "japan": "Japan",
        "china": "China",
        "cn": "China",
    }
    declared = (country or "").strip()
    explicit = names.get(declared.casefold())
    if declared and not explicit:
        # A provider's single full country name is context, not a candidate fact.
        # Avoid a growing list of per-country candidate approvals. Ambiguous
        # regions/codes still need internal job-location resolution.
        if (
            len(declared) <= 2
            or not re.fullmatch(r"[\wÀ-ÿ .'-]+", declared)
            or re.search(r"\b(?:and|or)\b", declared, re.IGNORECASE)
            or declared.casefold()
            in {
                "remote",
                "unknown",
                "global",
                "worldwide",
                "anywhere",
                "emea",
                "apac",
                "europe",
                "asia",
                "africa",
                "americas",
                "north america",
                "south america",
            }
        ):
            return None
        explicit = declared
    regions = set()
    unknown_location = False
    for part in re.split(r"[·;|]", location.casefold()):
        part = part.strip()
        if re.fullmatch(r"(?:remote\s*\()?canada\)?|(?:toronto|vancouver|montreal), canada", part):
            regions.add("Canada")
        elif part in {"san francisco, ca", "new york, ny", "united states", "usa"}:
            regions.add("United States")
        elif part in {"tokyo, japan", "japan"}:
            regions.add("Japan")
        elif part in {"china", "beijing, china", "shanghai, china"}:
            regions.add("China")
        elif part:
            unknown_location = True
    if explicit:
        return explicit if not regions or regions == {explicit} else None
    return next(iter(regions)) if len(regions) == 1 and not unknown_location else None


def scope_question(
    question: FormQuestion,
    binding: dict,
    entries: list,
    context: str = "",
    *,
    region: str | None = None,
) -> FormQuestion:
    """Use a confirmed exact question, or a small unambiguous fact vocabulary."""
    exact = question_key(question.label, binding.get("group_label", ""), context)
    if any(e.semantic_key == exact for e in entries):
        return question.model_copy(update={"semantic_key": exact})
    label = " ".join((binding.get("group_label") or question.label).casefold().split())
    label = label.strip(" *:")
    question_label = " ".join(question.label.casefold().split()).strip(" *:?")
    # A widget placeholder is not the question. Keep specific sublabels such as
    # Phone / Country intact instead of blindly inheriting group authority.
    if question_label in {"start typing...", "select...", "select", ""}:
        question_label = label.strip(" *:?")
    # Reuse approved specific facts; never route country-specific authorization
    # through the historical generic work_authorization key.
    known_questions = (
        (
            r"are you (?:legally eligible|legally authorized|authorised) to work in canada",
            "work_authorization_canada",
        ),
        (
            r"(?:what is the location where you permanently reside|country of residence)",
            "country_of_residence",
        ),
        (r"are you open to relocation for this role", "relocation_willingness"),
        (
            r"are you open to working in-person in one of our offices 25% of the time",
            "onsite_availability",
        ),
        (r"have you ever interviewed at anthropic before", "prior_anthropic_interviews"),
        (r"ai policy for application", "ai_policy_acknowledgment"),
        (
            r"please read the arbitration agreement below|agreement to arbitrate",
            "arbitration_agreement",
        ),
        (r"what are your salary expectations \(annually\)", "salary_expectations"),
        (r"have you previously worked at stackadapt", "prior_employer_relationship"),
        (
            r"which statement best describes how you currently use ai tools in (?:your )?(?:work/life|work or life|work and life)",
            "ai_tool_usage",
        ),
    )
    for pattern, key in known_questions:
        if re.fullmatch(pattern, question_label):
            return question.model_copy(update={"semantic_key": key})
    country_questions = (
        (
            r"are you legally authorized to work in the country the job you are applying for is primarily located",
            "authorization",
        ),
        (r"do you require visa sponsorship", "sponsorship"),
        (
            r"will you now or will you in the future require employment visa sponsorship to work in the country in which the job you're applying for is located",
            "sponsorship",
        ),
        (
            r"do you now or will you in the future require stackadapt to sponsor you for authorization to work in the country the job you are applying for is primarily located",
            "sponsorship",
        ),
    )
    for pattern, kind in country_questions:
        if re.fullmatch(pattern, question_label):
            keys = {
                ("Canada", "authorization"): "work_authorization_canada",
                ("China", "authorization"): "work_authorization_china",
                ("United States", "authorization"): "us_work_authorized",
                ("Canada", "sponsorship"): "sponsorship_required_canada",
                ("China", "sponsorship"): "sponsorship_required_china",
                ("United States", "sponsorship"): "sponsorship_required_united_states",
            }
            fallback = (
                "sponsorship_by_country"
                if kind == "sponsorship"
                else "work_authorization_by_country"
            )
            return question.model_copy(update={"semantic_key": keys.get((region, kind), fallback)})
    if question_label == (
        "if hired by stackadapt, do you intend to hold any secondary employment, advisory position "
        "(e.g. membership on a board of directors), or volunteer position that (1) is on behalf of a "
        "business that would be competitive in nature to the business of stackadapt, (2) conflict "
        "with your ability to perform your duties at stackadapt, or (3) conflict with your working "
        "hours at stackadapt"
    ):
        return question.model_copy(update={"semantic_key": "secondary_positions"})
    patterns = (
        (r"(?:first|given) name", "first_name"),
        (r"(?:last|family) name|surname", "last_name"),
        (r"(?:full |your )?name", "full_name"),
        (r"(?:your )?e-?mail(?: address)?", "email"),
        (r"(?:your )?(?:phone|mobile)(?: number)?", "phone"),
        (r"linkedin(?: (?:profile|url|profile url))?", "linkedin_url"),
        (r"github(?: (?:profile|url|profile url))?", "github_url"),
        (r"(?:personal )?(?:website|portfolio)(?: url)?", "website_url"),
        (r"current (?:company|employer)", "current_employer"),
        (r"(?:most recent|previous|last) (?:company|employer)", "most_recent_employer"),
    )
    # Choice labels do not inherit contact-field authority from their words/group.
    simple = binding.get("input_type") not in {"checkbox", "radio", "button", "submit"}
    simple &= binding.get("role") != "combobox" or binding.get("tag_name") == "select"
    simple &= bool(binding.get("label") or binding.get("dom_id") or binding.get("dom_name"))
    if simple:
        for pattern, key in patterns:
            if re.fullmatch(pattern, label):
                return question.model_copy(update={"semantic_key": key})
    if question.input_type == "file":
        # Generic Attach buttons and anonymous resume-autofill inputs are not
        # proof of a resume question. Prefer the document label, then native ID.
        document = label
        if not re.search(r"resume|curriculum vitae|\bcv\b|cover letter", document):
            document = re.sub(
                r"[_-]+", " ", binding.get("dom_id", "") + " " + binding.get("dom_name", "")
            )
        key = (
            "cover_letter_upload"
            if re.search(r"cover letter", document, re.IGNORECASE)
            else (
                "resume_upload"
                if re.search(r"resume|curriculum vitae|\bcv\b", document, re.IGNORECASE)
                else exact
            )
        )
        return question.model_copy(update={"semantic_key": key})
    # Preserve fact-grounded essay composition, but never turn a fact/choice or
    # an employment/contact question into an essay to fill a missing fact.
    fact_words = r"company|employer|sponsor|visa|authori[sz]|citizen|residen|salary|gender|race|disab|veteran|hear about|referr|e-?mail|phone|linkedin|github"
    choice = (
        binding.get("role") == "combobox"
        or binding.get("widget")
        or binding.get("tag_name") in {"select", "choice_group"}
    )
    if not choice and not re.search(fact_words, label) and is_narrative_question(question):
        return question
    return question.model_copy(update={"semantic_key": exact})


def resolve_scoped_answer(
    question: FormQuestion, binding: dict, entries: list, *, job_country: str | None = None
):
    """Normalize an explicit affirmative/negative only on a bound choice question."""
    key = question.semantic_key or ""
    if key in {"sponsorship_by_country", "work_authorization_by_country"}:
        return derive_employment_answer(question, entries, region=job_country)
    is_choice = (
        bool(binding.get("widget"))
        or binding.get("role") == "combobox"
        or binding.get("tag_name")
        in {
            "select",
            "choice_group",
        }
    )
    region = {
        "work_authorization_canada": "Canada",
        "sponsorship_required_canada": "Canada",
        "us_work_authorized": "United States",
        "sponsorship_required_united_states": "United States",
        "work_authorization_china": "China",
        "sponsorship_required_china": "China",
    }.get(key)
    # Resolve conflicts/expiry/region BEFORE normalization. Two different Yes
    # statements must not collapse into a single apparently approved answer.
    resolver = AnswerResolver()
    result = resolver.resolve(question.model_copy(update={"options": []}), entries, region=region)
    if not result.value or result.requires_human:
        return result
    value = result.value
    if is_choice:
        match = re.match(r"^(yes|no)\b", value.strip(), re.IGNORECASE)
        if match:
            value = match[1].capitalize()
        elif key == "ai_policy_acknowledgment" and value == (
            "I acknowledge and agree to the company's policy on AI use in the application process."
        ):
            value = "Yes"
        elif key == "arbitration_agreement" and value == "I agree to the arbitration agreement.":
            # Only exact observed wording of this explicitly approved agreement.
            options = {
                "I will read the arbitration agreement below.",
                "I understand and agree to the terms of the Agreement to Arbitrate set forth above.",
            }
            available = options.intersection(question.options)
            if len(available) == 1:
                value = next(iter(available))
    if question.options and value not in question.options:
        return resolver._block(question, "stored answer does not match available options")
    return result.model_copy(update={"value": value})


def matching_bindings(target: dict, bindings: tuple[dict, ...]) -> list[dict]:
    """Only the exact observed question may authorize a value on this control."""

    def norm(value):
        return " ".join(str(value or "").split()).casefold()

    matches = []
    for binding in bindings:
        # Shared inspection currently covers the main frame only.
        if target.get("frame", 0) != 0:
            continue
        if target.get("role") == "option" and target.get("listbox_id"):
            owned = (binding.get("controls", "") + " " + binding.get("owns", "")).split()
            if (
                binding.get("role") == "combobox"
                and binding.get("expanded") == "true"
                and target["listbox_id"] in owned
            ):
                matches.append(binding)
            continue
        if any(
            norm(target.get(a)) != norm(binding.get(b))
            for a, b in (
                ("tag", "tag_name"),
                ("type", "input_type"),
                ("label", "label"),
                ("group", "group_label"),
            )
        ):
            continue
        native = next((k for k in ("dom_id", "dom_name") if binding.get(k)), None)
        if native and target.get(native) != binding[native]:
            continue
        matches.append(binding)
    return matches
