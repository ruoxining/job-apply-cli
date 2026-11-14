from __future__ import annotations

import json
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class ModelRetry:
    reason_code: str
    retry_after_seconds: int | None = None


class TransientModelError(RuntimeError):
    """A provider failure that the worker supervisor may retry later."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.retry_after_seconds = retry_after_seconds


_TRANSIENT_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_QUOTA_MARKERS = (
    "quota",
    "rate limit",
    "rate_limit",
    "too many requests",
    "insufficient balance",
    "insufficient credit",
    "余额不足",
    "额度",
    "限流",
)


def classify_transient_model_error(exc: httpx.HTTPError) -> ModelRetry | None:
    """Classify only failures that are safe to retry without changing answers."""
    if isinstance(exc, httpx.RequestError):
        return ModelRetry("glm_network_error")
    if not isinstance(exc, httpx.HTTPStatusError):
        return None

    response = exc.response
    status = response.status_code
    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
    body = _response_text(response)
    quota_error = status == 429 or any(marker in body for marker in _QUOTA_MARKERS)
    if quota_error:
        return ModelRetry("glm_quota_or_rate_limit", retry_after)
    if status in _TRANSIENT_HTTP_STATUSES:
        return ModelRetry("glm_transient_http", retry_after)
    return None


def transient_model_error(
    exc: httpx.HTTPError,
    *,
    operation: str,
) -> TransientModelError | None:
    retry = classify_transient_model_error(exc)
    if retry is None:
        return None
    return TransientModelError(
        f"GLM {operation} is temporarily unavailable",
        reason_code=retry.reason_code,
        retry_after_seconds=retry.retry_after_seconds,
    )


def _retry_after_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        seconds = int(value)
    except ValueError:
        return None
    return max(0, seconds)


def _response_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return response.text.casefold()[:2000]
    return json.dumps(payload, ensure_ascii=False, default=str).casefold()[:2000]
