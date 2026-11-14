from __future__ import annotations

import html
import re

from job_apply_agent.domain.schemas import JobPosting, RawJob

KNOWN_SKILLS = (
    "python",
    "java",
    "c++",
    "sql",
    "javascript",
    "go",
    "rust",
    "typescript",
    "react",
    "kubernetes",
    "docker",
    "postgresql",
    "aws",
    "gcp",
    "azure",
    "pytorch",
    "tensorflow",
    "numpy",
    "hugging face",
    "scikit-learn",
    "pandas",
    "langchain",
    "fastapi",
    "linux",
    "git",
    "slurm",
    "nginx",
    "vllm",
    "mlops",
    "etl",
    "rag",
)


class JobNormalizer:
    def normalize(self, raw: RawJob) -> JobPosting:
        description = _plain_text(raw.description)
        combined = f"{raw.title}\n{description}".casefold()
        years = _experience_years(combined)
        title_years = _minimum_title_experience(raw.title)
        required_skills = [skill for skill in KNOWN_SKILLS if _has_term(combined, skill)]
        return JobPosting(
            **raw.model_dump(exclude={"description"}),
            description=description,
            job_family=_job_family(raw.title),
            required_years=max([*years, title_years])
            if title_years is not None
            else (max(years) if years else None),
            required_skills=required_skills,
            requires_sponsorship_exclusion=bool(
                re.search(r"(?:no|without)\s+(?:visa\s+)?sponsorship|not\s+sponsor", combined)
            ),
            requires_current_enrollment=_requires_current_enrollment(combined),
            requires_citizenship=_requires_citizenship(combined),
        )


def _plain_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
    return re.sub(r"\s+", " ", without_tags).strip()


def _has_term(text: str, term: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text))


def _job_family(title: str) -> str | None:
    folded = title.casefold()
    if any(
        value in folded
        for value in ("software", "backend", "frontend", "full stack", "developer", "sde")
    ):
        return "software_engineering"
    if any(value in folded for value in ("machine learning", "ml engineer", "ai engineer")):
        return "machine_learning"
    if "data" in folded:
        return "data"
    if any(value in folded for value in ("research engineer", "research scientist", "scientist")):
        return "research"
    if "product" in folded:
        return "product"
    return None


def _requires_current_enrollment(text: str) -> bool:
    title = text.split("\n", 1)[0]
    if re.search(r"\b(?:intern(?:ship)?|co-?op|student)\b", title):
        return True
    return bool(
        re.search(
            r"(?:must|should) be currently enrolled|"
            r"currently pursuing (?:a|an|your) |"
            r"return(?:ing)? to (?:school|studies)",
            text,
        )
    )


def _requires_citizenship(text: str) -> bool:
    return bool(
        re.search(
            r"(?:must be|requires?|required to be) (?:a )?canadian citizen|"
            r"canadian citizenship (?:is )?required|"
            r"must hold canadian citizenship",
            text,
        )
    )


_YEARS_RE = re.compile(r"(\d+(?:\.\d+)?)\+?\s+years?")
# A duration can describe age, employer history, or a product roadmap. Only
# count durations with nearby work/experience wording in the same clause.
_AGE_CONTEXT_RE = re.compile(r"\b(?:age|old)\b|or older")
_WORK_CONTEXT_RE = re.compile(
    r"^\s*[),:]?\s*(?:(?:of|in|as|with)\s+)?"
    r"(?:[\w/+&'-]+\s+){0,8}"
    r"(?:experience|engineering|development|research|developer|engineer|"
    r"building|shipping|working|programming|managing|leading)\b"
)
_PRECEDING_EXPERIENCE_RE = re.compile(r"\bexperience\s*(?:of|for|:)?\s*(?:at least\s*)?$")


def _experience_years(text: str) -> list[float]:
    years: list[float] = []
    for match in _YEARS_RE.finditer(text):
        window = text[match.end() : match.end() + 40]
        if _AGE_CONTEXT_RE.search(window):
            continue
        after = re.split(r"[.!?;\n]", text[match.end() : match.end() + 130], maxsplit=1)[0]
        before = text[max(0, match.start() - 50) : match.start()]
        if _WORK_CONTEXT_RE.search(after) or _PRECEDING_EXPERIENCE_RE.search(before):
            years.append(float(match.group(1)))
    return years


# Seniority bands only count as a leading role qualifier. "Member of
# Technical Staff" is an individual-contributor title, not a staff band.
_SENIORITY_RE = re.compile(r"^(?:principal|staff|director|head of)\b")
_SENIOR_RE = re.compile(r"^(?:senior|sr\.?)\b")


def _minimum_title_experience(title: str) -> float | None:
    folded = re.sub(r"\s+", " ", title.strip().casefold())
    if _SENIORITY_RE.match(folded):
        return 6
    if _SENIOR_RE.match(folded):
        return 4
    return None
