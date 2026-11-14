from __future__ import annotations

import asyncio
import random
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from job_apply_agent.domain.enums import ATSKind
from job_apply_agent.domain.schemas import RawJob


@dataclass(frozen=True)
class BoardConfig:
    ats: ATSKind
    board_slug: str
    enabled: bool = True
    max_jobs: int | None = None
    title_keywords: tuple[str, ...] = ()
    location_keywords: tuple[str, ...] = ()


BOARD_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class CollectorError(RuntimeError):
    pass


class JobSource(ABC):
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": "job-apply-agent/0.1 (+local personal job search)"},
        )

    @abstractmethod
    async def fetch(self, cursor: str | None = None) -> list[RawJob]: ...

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    @staticmethod
    def validate_slug(slug: str) -> str:
        if not BOARD_SLUG_RE.fullmatch(slug):
            raise CollectorError(f"invalid ATS board slug: {slug!r}")
        return slug

    async def get_json(self, url: str, *, retries: int = 2) -> object:
        for attempt in range(retries + 1):
            response = await self.client.get(url)
            if response.status_code == 429 and attempt < retries:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                await asyncio.sleep(delay + random.random() * 0.2)
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise CollectorError(f"ATS request failed ({response.status_code})") from exc
            return response.json()
        raise CollectorError("ATS retry budget exhausted")


async def collect_all(sources: list[JobSource]) -> AsyncIterator[RawJob]:
    results = await asyncio.gather(*(source.fetch() for source in sources))
    for jobs in results:
        for job in jobs:
            yield job
