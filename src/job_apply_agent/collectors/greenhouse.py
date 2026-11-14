from __future__ import annotations

from datetime import datetime

from job_apply_agent.domain.enums import ATSKind
from job_apply_agent.domain.schemas import RawJob

from .base import CollectorError, JobSource


class GreenhouseSource(JobSource):
    def __init__(self, board_slug: str, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.board_slug = self.validate_slug(board_slug)

    async def fetch(self, cursor: str | None = None) -> list[RawJob]:
        url = (
            "https://boards-api.greenhouse.io/v1/boards/"
            f"{self.board_slug}/jobs?content=true"
        )
        payload = await self.get_json(url)
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise CollectorError("unexpected Greenhouse response")
        jobs: list[RawJob] = []
        for item in payload["jobs"]:
            location = (item.get("location") or {}).get("name", "")
            jobs.append(
                RawJob(
                    source=ATSKind.GREENHOUSE,
                    source_id=str(item["id"]),
                    company=self.board_slug,
                    title=item.get("title", ""),
                    url=item.get("absolute_url", ""),
                    location=location,
                    description=item.get("content", ""),
                    published_at=_parse_datetime(item.get("updated_at")),
                    metadata={
                        "departments": item.get("departments", []),
                        "offices": item.get("offices", []),
                    },
                    raw_snapshot=item,
                )
            )
        return jobs


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

