from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import ApplicationChannel, ATSKind, BlockerKind, Sensitivity


class RawJob(BaseModel):
    source: ATSKind
    source_id: str
    company: str
    title: str
    url: str
    location: str = ""
    description: str = ""
    country: str | None = None
    workplace_type: str | None = None
    employment_type: str | None = None
    compensation_min: int | None = None
    compensation_max: int | None = None
    currency: str | None = None
    published_at: datetime | None = None
    is_open: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw_snapshot: dict[str, Any] = Field(default_factory=dict)


class JobPosting(RawJob):
    job_family: str | None = None
    required_years: float | None = None
    required_skills: list[str] = Field(default_factory=list)
    requires_sponsorship_exclusion: bool = False
    requires_current_enrollment: bool = False
    requires_citizenship: bool = False
    duplicate_hint: str | None = None


class ProfileFact(BaseModel):
    id: str
    statement: str
    start_date: date | None = None
    end_date: date | None = None
    skills: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    source: str
    allowed_rewrites: list[str] = Field(default_factory=list)


class CandidateProfile(BaseModel):
    allowed_countries: list[str] = Field(default_factory=list)
    accepts_remote: bool = True
    allowed_job_families: list[str] = Field(default_factory=list)
    years_experience: float = 0
    company_experience_years: float = 0
    industry_experience_years: float = 0
    max_years_gap: float = 2
    accepted_employment_modes: list[str] = Field(
        default_factory=lambda: ["full_time", "project", "intern"]
    )
    skills: list[str] = Field(default_factory=list)
    work_authorized_countries: list[str] = Field(default_factory=list)
    sponsorship_required: bool = False
    target_titles: list[str] = Field(default_factory=list)
    facts: list[ProfileFact] = Field(default_factory=list)


class PolicyWeights(BaseModel):
    company: int = 35
    role: int = 25
    match: int = 25
    opportunity: int = 15

    @model_validator(mode="after")
    def total_100(self) -> PolicyWeights:
        if sum((self.company, self.role, self.match, self.opportunity)) != 100:
            raise ValueError("policy weights must sum to 100")
        return self


class SearchPolicy(BaseModel):
    version: int = Field(ge=1)
    high_value_threshold: int = Field(default=65, ge=0, le=100)
    company_whitelist: list[str] = Field(default_factory=list)
    title_whitelist: list[str] = Field(default_factory=list)
    low_stakes_companies: list[str] = Field(default_factory=list)
    low_stakes_title_keywords: list[str] = Field(default_factory=list)
    company_blacklist: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    exclude_student_only: bool = False
    exclude_citizenship_required: bool = False
    preferred_companies: dict[str, float] = Field(default_factory=dict)
    preferred_locations: list[str] = Field(default_factory=list)
    weights: PolicyWeights = Field(default_factory=PolicyWeights)


class EligibilityResult(BaseModel):
    eligible: bool
    reasons: list[str] = Field(default_factory=list)


class ScoreBreakdown(BaseModel):
    company: float = Field(ge=0)
    role: float = Field(ge=0)
    match: float = Field(ge=0)
    opportunity: float = Field(ge=0)

    @property
    def total(self) -> float:
        return round(self.company + self.role + self.match + self.opportunity, 2)


class PriorityAssessment(BaseModel):
    policy_version: int
    eligibility: EligibilityResult
    scores: ScoreBreakdown
    total_score: float = Field(ge=0, le=100)
    channel: ApplicationChannel | None
    reasons: list[str] = Field(default_factory=list)


class AnswerBankEntry(BaseModel):
    semantic_key: str
    answer: str
    regions: list[str] = Field(default_factory=list)
    sensitivity: Sensitivity = Sensitivity.NORMAL
    valid_until: date | None = None


class FormQuestion(BaseModel):
    field_id: str
    label: str
    semantic_key: str | None = None
    input_type: str = ""
    tag_name: str = ""
    max_length: int | None = None
    required: bool = False
    options: list[str] = Field(default_factory=list)
    sensitivity: Sensitivity = Sensitivity.NORMAL


class ResolvedAnswer(BaseModel):
    field_id: str
    value: str | None
    source_key: str | None = None
    requires_human: bool = False
    blocker: BlockerKind | None = None
    reason: str | None = None


class ResumeContent(BaseModel):
    headline: str
    summary: str
    bullets: list[str]
    fact_ids: list[str]


class ResumeArtifact(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    variant_id: str
    docx_path: str
    pdf_path: str | None = None
    sha256: str
    fact_ids: list[str]
    validation_errors: list[str] = Field(default_factory=list)


class ReviewAction(BaseModel):
    action: str
    instruction: str | None = None
