from enum import StrEnum


class ATSKind(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    UNKNOWN = "unknown"


class ApplicationChannel(StrEnum):
    AUTO = "auto"
    TELEGRAM_REVIEW = "telegram_review"


class ApplicationState(StrEnum):
    DISCOVERED = "discovered"
    INELIGIBLE = "ineligible"
    AUTO_APPLY_QUEUED = "auto_apply_queued"
    AWAITING_RESUME_REVIEW = "awaiting_resume_review"
    MATERIALS_READY = "materials_ready"
    FILLING = "filling"
    NEEDS_HUMAN = "needs_human"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    FAILED = "failed"
    SKIPPED = "skipped"


class Sensitivity(StrEnum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"
    VOLUNTARY = "voluntary"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    NEEDS_HUMAN = "needs_human"
    SUBMITTED = "submitted"
    FAILED = "failed"


class BlockerKind(StrEnum):
    CAPTCHA = "captcha"
    TWO_FACTOR = "two_factor"
    LOGIN_REQUIRED = "login_required"
    UNKNOWN_QUESTION = "unknown_question"
    SENSITIVE_ANSWER_MISSING = "sensitive_answer_missing"
    SUBMIT_OUTCOME_UNKNOWN = "submit_outcome_unknown"
