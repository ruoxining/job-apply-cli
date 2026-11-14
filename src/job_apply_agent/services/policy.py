from __future__ import annotations

import re

from job_apply_agent.domain.enums import ApplicationChannel
from job_apply_agent.domain.schemas import (
    CandidateProfile,
    EligibilityResult,
    JobPosting,
    PriorityAssessment,
    ScoreBreakdown,
    SearchPolicy,
)


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().casefold())


def _contains_any(text: str, values: list[str]) -> bool:
    folded = _norm(text)
    return any(_norm(value) in folded for value in values if _norm(value))


def skill_matches(required: str, owned: str) -> bool:
    """Match equivalent skill names without single-letter substring false positives."""
    required_skill = _norm(required)
    owned_skill = _norm(owned)
    if required_skill == owned_skill:
        return True
    if min(len(required_skill), len(owned_skill)) < 3:
        return False
    return required_skill in owned_skill or owned_skill in required_skill


class EligibilityPolicy:
    def check(
        self, job: JobPosting, profile: CandidateProfile, policy: SearchPolicy
    ) -> EligibilityResult:
        reasons: list[str] = []
        combined = f"{job.title}\n{job.description}"

        if not job.is_open:
            reasons.append("posting_closed")
        if _contains_any(job.company, policy.company_blacklist):
            reasons.append("company_blacklisted")
        if _contains_any(combined, policy.excluded_keywords):
            reasons.append("excluded_keyword")
        if policy.exclude_student_only and job.requires_current_enrollment:
            reasons.append("current_enrollment_required")
        if policy.exclude_citizenship_required and job.requires_citizenship:
            reasons.append("citizenship_required")
        if profile.allowed_job_families and job.job_family:
            allowed = {_norm(item) for item in profile.allowed_job_families}
            if _norm(job.job_family) not in allowed:
                reasons.append("job_family_not_allowed")
        if job.country and profile.allowed_countries:
            allowed_countries = {_norm(item) for item in profile.allowed_countries}
            if _norm(job.country) not in allowed_countries:
                reasons.append("country_not_allowed")
        if _norm(job.workplace_type) == "remote" and not profile.accepts_remote:
            reasons.append("remote_not_accepted")
        if job.required_years is not None:
            maximum = profile.years_experience + profile.max_years_gap
            if job.required_years > maximum:
                reasons.append("experience_gap_too_large")
        if job.requires_sponsorship_exclusion and profile.sponsorship_required:
            reasons.append("sponsorship_incompatible")
        if job.country and profile.work_authorized_countries:
            authorized = {_norm(item) for item in profile.work_authorized_countries}
            if _norm(job.country) not in authorized and profile.sponsorship_required:
                reasons.append("work_authorization_incompatible")

        return EligibilityResult(eligible=not reasons, reasons=reasons)


class PriorityPolicy:
    def __init__(self, eligibility_policy: EligibilityPolicy | None = None) -> None:
        self.eligibility_policy = eligibility_policy or EligibilityPolicy()

    def score(
        self, job: JobPosting, profile: CandidateProfile, policy: SearchPolicy
    ) -> PriorityAssessment:
        eligibility = self.eligibility_policy.check(job, profile, policy)
        if not eligibility.eligible:
            scores = ScoreBreakdown(company=0, role=0, match=0, opportunity=0)
            return PriorityAssessment(
                policy_version=policy.version,
                eligibility=eligibility,
                scores=scores,
                total_score=0,
                channel=None,
                reasons=list(eligibility.reasons),
            )

        company_whitelisted = _contains_any(job.company, policy.company_whitelist)
        title_whitelisted = _contains_any(job.title, policy.title_whitelist)
        low_stakes_company = _contains_any(job.company, policy.low_stakes_companies)
        low_stakes_title = _contains_any(job.title, policy.low_stakes_title_keywords)
        company_preference = policy.preferred_companies.get(_norm(job.company), 0.0)
        company_ratio = 1.0 if company_whitelisted else max(0.0, min(company_preference, 1.0))

        title_words = set(re.findall(r"[a-z0-9+#.]+", _norm(job.title)))
        target_words = set(
            word
            for title in profile.target_titles
            for word in re.findall(r"[a-z0-9+#.]+", _norm(title))
        )
        role_ratio = len(title_words & target_words) / max(1, len(target_words))
        if title_whitelisted:
            role_ratio = 1.0

        required = {_norm(skill) for skill in job.required_skills if _norm(skill)}
        owned = {_norm(skill) for skill in profile.skills if _norm(skill)}
        matched = {
            required_skill
            for required_skill in required
            if any(skill_matches(required_skill, owned_skill) for owned_skill in owned)
        }
        match_ratio = len(matched) / len(required) if required else 0.5

        opportunity_ratio = 0.0
        if job.compensation_max:
            opportunity_ratio += 0.5
        if _contains_any(job.description, ["distributed systems", "platform", "ai", "ml"]):
            opportunity_ratio += 0.3
        if _contains_any(job.location, policy.preferred_locations):
            opportunity_ratio += 0.2
        opportunity_ratio = min(opportunity_ratio, 1.0)

        weights = policy.weights
        scores = ScoreBreakdown(
            company=round(weights.company * company_ratio, 2),
            role=round(weights.role * min(role_ratio, 1.0), 2),
            match=round(weights.match * match_ratio, 2),
            opportunity=round(weights.opportunity * opportunity_ratio, 2),
        )
        total = scores.total
        # Routing models application opportunity cost, not merely the chance of
        # passing a resume screen. Explicitly important companies always get
        # human review. Explicitly low-stakes roles may auto-apply even when the
        # title happens to overlap strongly with the profile. Otherwise, a
        # strategic title or high match score is worth reviewing carefully.
        is_low_stakes = low_stakes_company or low_stakes_title
        is_high_value = company_whitelisted or (
            not is_low_stakes and (title_whitelisted or total >= policy.high_value_threshold)
        )
        reasons = []
        if company_whitelisted:
            reasons.append("company_whitelist")
        if title_whitelisted:
            reasons.append("title_whitelist")
        if low_stakes_company:
            reasons.append("low_stakes_company")
        if low_stakes_title:
            reasons.append("low_stakes_title")
        if total >= policy.high_value_threshold:
            reasons.append("score_at_or_above_threshold")
        if _contains_any(job.location, policy.preferred_locations):
            reasons.append("preferred_location")
        if not reasons:
            reasons.append("eligible_below_high_value_threshold")

        return PriorityAssessment(
            policy_version=policy.version,
            eligibility=eligibility,
            scores=scores,
            total_score=total,
            channel=(
                ApplicationChannel.TELEGRAM_REVIEW if is_high_value else ApplicationChannel.AUTO
            ),
            reasons=reasons,
        )
