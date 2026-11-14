from __future__ import annotations

from job_apply_agent.domain.enums import ATSKind
from job_apply_agent.domain.schemas import RawJob

from .base import CollectorError, JobSource


class LeverSource(JobSource):
    def __init__(self, board_slug: str, *args, region: str = "us", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.board_slug = self.validate_slug(board_slug)
        if region not in {"us", "eu"}:
            raise CollectorError("Lever region must be 'us' or 'eu'")
        self.region = region

    async def fetch(self, cursor: str | None = None) -> list[RawJob]:
        host = "api.eu.lever.co" if self.region == "eu" else "api.lever.co"
        payload = await self.get_json(
            f"https://{host}/v0/postings/{self.board_slug}?mode=json"
        )
        if not isinstance(payload, list):
            raise CollectorError("unexpected Lever response")
        jobs: list[RawJob] = []
        for item in payload:
            categories = item.get("categories") or {}
            description = item.get("descriptionPlain") or "\n".join(
                part.get("content", "") for part in item.get("lists", [])
            )
            jobs.append(
                RawJob(
                    source=ATSKind.LEVER,
                    source_id=str(item["id"]),
                    company=self.board_slug,
                    title=item.get("text", ""),
                    url=item.get("hostedUrl") or item.get("applyUrl", ""),
                    location=categories.get("location", ""),
                    employment_type=categories.get("commitment"),
                    description=description,
                    metadata={"team": categories.get("team")},
                    raw_snapshot=item,
                )
            )
        return jobs

