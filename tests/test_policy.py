from job_apply_agent.domain.enums import ApplicationChannel, ATSKind
from job_apply_agent.domain.schemas import CandidateProfile, JobPosting, SearchPolicy
from job_apply_agent.services.policy import PriorityPolicy, skill_matches


def job(**overrides) -> JobPosting:
    values = {
        "source": ATSKind.GREENHOUSE,
        "source_id": "123",
        "company": "Acme",
        "title": "Backend Engineer",
        "url": "https://example.test/jobs/123",
        "description": "Build APIs with Python and PostgreSQL.",
        "country": "US",
        "workplace_type": "remote",
        "job_family": "software_engineering",
        "required_years": 4,
        "required_skills": ["python", "postgresql"],
    }
    values.update(overrides)
    return JobPosting(**values)


def profile() -> CandidateProfile:
    return CandidateProfile(
        allowed_countries=["US"],
        allowed_job_families=["software_engineering"],
        years_experience=4,
        max_years_gap=1,
        skills=["python", "postgresql"],
        work_authorized_countries=["US"],
        target_titles=["Backend Engineer"],
    )


def test_whitelist_always_routes_to_telegram_review():
    policy = SearchPolicy(version=1, company_whitelist=["Acme"], high_value_threshold=99)
    assessment = PriorityPolicy().score(job(), profile(), policy)
    assert assessment.channel is ApplicationChannel.TELEGRAM_REVIEW
    assert "company_whitelist" in assessment.reasons


def test_ordinary_eligible_job_routes_directly_to_auto_apply():
    policy = SearchPolicy(version=1, high_value_threshold=90)
    assessment = PriorityPolicy().score(
        job(title="Support Specialist", required_skills=["java"]), profile(), policy
    )
    assert assessment.eligibility.eligible
    assert assessment.channel is ApplicationChannel.AUTO


def test_low_stakes_title_routes_to_auto_even_when_match_score_is_high():
    policy = SearchPolicy(
        version=1,
        high_value_threshold=1,
        title_whitelist=["Technician"],
        low_stakes_title_keywords=["part-time", "technician"],
    )
    assessment = PriorityPolicy().score(
        job(title="Part-Time AI Lab Technician", required_skills=["python"]),
        profile(),
        policy,
    )
    assert assessment.channel is ApplicationChannel.AUTO
    assert "low_stakes_title" in assessment.reasons


def test_important_company_still_requires_review_for_low_stakes_title():
    policy = SearchPolicy(
        version=1,
        company_whitelist=["Acme"],
        low_stakes_title_keywords=["technician"],
    )
    assessment = PriorityPolicy().score(
        job(title="Part-Time Technician", required_skills=[]), profile(), policy
    )
    assert assessment.channel is ApplicationChannel.TELEGRAM_REVIEW
    assert "company_whitelist" in assessment.reasons


def test_experience_boundary_is_inclusive_and_one_step_over_fails():
    policy = SearchPolicy(version=1)
    at_boundary = PriorityPolicy().score(job(required_years=5), profile(), policy)
    over_boundary = PriorityPolicy().score(job(required_years=5.1), profile(), policy)
    assert at_boundary.eligibility.eligible
    assert not over_boundary.eligibility.eligible
    assert "experience_gap_too_large" in over_boundary.eligibility.reasons


def test_sponsorship_exclusion_is_hard_failure():
    candidate = profile().model_copy(update={"sponsorship_required": True})
    assessment = PriorityPolicy().score(
        job(requires_sponsorship_exclusion=True), candidate, SearchPolicy(version=1)
    )
    assert assessment.channel is None
    assert "sponsorship_incompatible" in assessment.eligibility.reasons


def test_student_only_and_citizenship_requirements_are_hard_failures():
    policy = SearchPolicy(
        version=2,
        exclude_student_only=True,
        exclude_citizenship_required=True,
    )
    assessment = PriorityPolicy().score(
        job(requires_current_enrollment=True, requires_citizenship=True), profile(), policy
    )
    assert not assessment.eligibility.eligible
    assert "current_enrollment_required" in assessment.eligibility.reasons
    assert "citizenship_required" in assessment.eligibility.reasons


def test_preferred_location_contributes_to_ranking():
    policy = SearchPolicy(version=2, preferred_locations=["Ontario", "Toronto"])
    preferred = PriorityPolicy().score(job(location="Toronto, Ontario"), profile(), policy)
    elsewhere = PriorityPolicy().score(job(location="Vancouver, BC"), profile(), policy)
    assert preferred.total_score > elsewhere.total_score
    assert "preferred_location" in preferred.reasons


def test_short_skill_names_do_not_match_unrelated_substrings():
    assert skill_matches("c", "C")
    assert not skill_matches("react", "C")
    assert not skill_matches("rust", "R")
    assert skill_matches("hugging face", "Hugging Face Transformers")
