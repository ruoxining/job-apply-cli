import json
from pathlib import Path

import httpx
import pytest

from job_apply_agent.collectors import AshbySource, GreenhouseSource, JobNormalizer, LeverSource
from job_apply_agent.collectors.base import CollectorError
from job_apply_agent.domain.enums import ATSKind
from job_apply_agent.domain.schemas import RawJob

FIXTURES = Path(__file__).parent / "fixtures"


def client_for(payload):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )


@pytest.mark.parametrize(
    ("source_cls", "fixture", "expected_title"),
    [
        (GreenhouseSource, "greenhouse_jobs.json", "Backend Engineer"),
        (LeverSource, "lever_jobs.json", "ML Engineer"),
        (AshbySource, "ashby_jobs.json", "Platform Engineer"),
    ],
)
async def test_public_ats_collectors_parse_anonymized_payloads(source_cls, fixture, expected_title):
    payload = json.loads((FIXTURES / fixture).read_text())
    client = client_for(payload)
    source = source_cls("acme", client=client)
    jobs = await source.fetch()
    await client.aclose()
    assert len(jobs) == 1
    assert jobs[0].title == expected_title
    assert jobs[0].source_id
    assert jobs[0].raw_snapshot


def test_board_slug_is_not_an_ssrf_escape():
    with pytest.raises(CollectorError):
        GreenhouseSource("../example.com?redirect=evil")


def test_normalizer_detects_student_only_and_canadian_citizenship_requirements():
    raw = RawJob(
        source=ATSKind.GREENHOUSE,
        source_id="student-role",
        company="Example",
        title="Machine Learning Co-op",
        url="https://example.test/student-role",
        description="Applicants must be Canadian citizens.",
    )
    job = JobNormalizer().normalize(raw)
    assert job.requires_current_enrollment
    assert job.requires_citizenship


def test_normalizer_infers_minimum_experience_for_staff_title():
    raw = RawJob(
        source=ATSKind.GREENHOUSE,
        source_id="staff-role",
        company="Example",
        title="Staff Machine Learning Engineer",
        url="https://example.test/staff-role",
        description="Build machine learning systems.",
    )
    assert JobNormalizer().normalize(raw).required_years == 6


def test_normalizer_ignores_age_requirements_in_years():
    raw = RawJob(
        source=ATSKind.GREENHOUSE,
        source_id="intern-role",
        company="Example",
        title="Software Development Engineer Intern",
        url="https://example.test/intern-role",
        description=(
            "Basic Qualifications - Are 18 years of age or older - "
            "3+ years of programming experience."
        ),
    )
    assert JobNormalizer().normalize(raw).required_years == 3


def test_normalizer_member_of_technical_staff_is_not_staff_band():
    raw = RawJob(
        source=ATSKind.ASHBY,
        source_id="mts-role",
        company="Example",
        title="Member of Technical Staff - ML Data",
        url="https://example.test/mts-role",
        description="Build machine learning data systems.",
    )
    assert JobNormalizer().normalize(raw).required_years is None


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Our company has had a singular mission for 25 years and is dedicated to it.", None),
        (
            "For 25 years we have served customers. Developers typically have 5+ years experience.",
            5,
        ),
        (
            "We have 25 years of company history. Requires 8+ years of software engineering experience.",
            8,
        ),
        ("You have 3+ years building production ML systems.", 3),
        ("You have 2+ years as a full stack developer.", 2),
        ("An experienced (5+ years) developer who codes embedded software.", 5),
        ("Required experience: at least 4 years.", 4),
        ("Our product roadmap spans 10 years. Experience in Python required.", None),
    ],
)
def test_normalizer_distinguishes_work_experience_from_other_durations(description, expected):
    raw = RawJob(
        source=ATSKind.GREENHOUSE,
        source_id="duration-regression",
        company="Example",
        title="Software Developer",
        url="https://example.test/duration-regression",
        description=description,
    )
    assert JobNormalizer().normalize(raw).required_years == expected
