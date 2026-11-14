from __future__ import annotations

from job_apply_agent.domain.enums import ATSKind
from job_apply_agent.domain.schemas import RawJob

from .base import CollectorError, JobSource


class AshbySource(JobSource):
    def __init__(self, board_slug: str, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.board_slug = self.validate_slug(board_slug)

    async def fetch(self, cursor: str | None = None) -> list[RawJob]:
        payload = await self.get_json(
            "https://api.ashbyhq.com/posting-api/job-board/"
            f"{self.board_slug}?includeCompensation=true"
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise CollectorError("unexpected Ashby response")
        jobs: list[RawJob] = []
        for item in payload["jobs"]:
            if item.get("isListed") is False:
                continue
            compensation = item.get("compensation") or {}
            tiers = compensation.get("compensationTiers") or []
            first_tier = tiers[0] if tiers else {}
            jobs.append(
                RawJob(
                    source=ATSKind.ASHBY,
                    source_id=str(item["id"]),
                    company=self.board_slug,
                    title=item.get("title", ""),
                    url=item.get("jobUrl") or item.get("applyUrl", ""),
                    location=_locations(item),
                    workplace_type=item.get("workplaceType"),
                    employment_type=item.get("employmentType"),
                    description=item.get("descriptionPlain", ""),
                    compensation_min=_integer(first_tier.get("minAmount")),
                    compensation_max=_integer(first_tier.get("maxAmount")),
                    currency=first_tier.get("currencyCode"),
                    metadata={"department": item.get("department")},
                    raw_snapshot=item,
                )
            )
        return jobs


def _locations(item: dict) -> str:
    names = [item.get("location", "")]
    names.extend(
        entry.get("location", "") if isinstance(entry, dict) else str(entry)
        for entry in item.get("secondaryLocations", [])
    )
    return " · ".join(dict.fromkeys(name for name in names if name))


def _integer(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None

