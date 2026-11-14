#!/usr/bin/env python3
"""Check configured public ATS boards without writing application data."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx


async def check(path: Path) -> int:
    sources = json.loads(path.read_text())
    failures = 0
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for source in sources:
            kind = source["ats"]
            slug = source["board_slug"]
            if kind == "greenhouse":
                url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
            elif kind == "lever":
                host = "api.eu.lever.co" if source.get("region") == "eu" else "api.lever.co"
                url = f"https://{host}/v0/postings/{slug}?mode=json"
            elif kind == "ashby":
                url = (
                    f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
                    "?includeCompensation=true"
                )
            else:
                print(f"FAIL {kind}/{slug}: unsupported source kind")
                failures += 1
                continue

            try:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.json()
                jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
                if not isinstance(jobs, list):
                    raise ValueError("response has no job list")
                matching = _matching_jobs(jobs, kind, source)
                print(
                    f"OK   {kind}/{slug}: {len(jobs)} open postings, "
                    f"{len(matching)} match configured title/location filters"
                )
            except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                print(f"FAIL {kind}/{slug}: {exc}")
                failures += 1
    return failures


def _matching_jobs(jobs: list[dict], kind: str, source: dict) -> list[dict]:
    title_terms = [term.casefold() for term in source.get("title_keywords", [])]
    location_terms = [term.casefold() for term in source.get("location_keywords", [])]

    def fields(job: dict) -> tuple[str, str]:
        if kind == "greenhouse":
            return job.get("title", ""), (job.get("location") or {}).get("name", "")
        if kind == "lever":
            categories = job.get("categories") or {}
            return job.get("text", ""), categories.get("location", "")
        secondary = " ".join(
            entry.get("location", "") if isinstance(entry, dict) else str(entry)
            for entry in job.get("secondaryLocations", [])
        )
        return job.get("title", ""), f"{job.get('location', '')} {secondary}"

    result = []
    for job in jobs:
        title, location = fields(job)
        if title_terms and not any(term in title.casefold() for term in title_terms):
            continue
        if location_terms and not any(term in location.casefold() for term in location_terms):
            continue
        result.append(job)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(check(args.config)))


if __name__ == "__main__":
    main()
