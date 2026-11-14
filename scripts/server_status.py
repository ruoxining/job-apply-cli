#!/usr/bin/env python3
"""Show the job-apply-agent status on a remote Docker Compose host."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


REMOTE_SCRIPT = r'''
remote_dir=$1
printf '%s\n' '__REPOSITORY__'
if [ ! -d "$remote_dir" ]; then
  printf '%s\n' 'missing'
  exit 0
fi
printf '%s\n' 'present'
cd "$remote_dir" || exit 1
printf '%s\n' '__REVISION__'
git rev-parse --short HEAD 2>/dev/null || printf '%s\n' 'unknown'
printf '%s\n' '__COMPOSE__'
docker compose ps --format json 2>&1 || true
printf '%s\n' '__API__'
curl -fsS --max-time 5 http://127.0.0.1:8000/v1/ops/status 2>&1 || true
'''


def _sections(output: str) -> dict[str, str]:
    result: dict[str, list[str]] = {}
    current: str | None = None
    for line in output.splitlines():
        if line.startswith("__") and line.endswith("__"):
            current = line.strip("_").lower()
            result[current] = []
        elif current is not None:
            result[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in result.items()}


def _compose_rows(raw: str) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else [value]
    except json.JSONDecodeError:
        rows = []
        for line in raw.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows


def _print_report(host: str, sections: dict[str, str]) -> int:
    print(f"Job Apply Agent @ {host}")
    if sections.get("repository") != "present":
        print("\nServer: NOT DEPLOYED (repository directory not found)")
        return 2

    print(f"Revision: {sections.get('revision', 'unknown')}")
    rows = _compose_rows(sections.get("compose", ""))
    expected = {"api", "worker", "scheduler", "telegram-bot", "postgres", "redis"}
    running = set()
    print("\nProcesses:")
    if not rows:
        print("  no Compose containers found")
    for row in rows:
        service = row.get("Service") or row.get("Name") or "unknown"
        state = str(row.get("State") or row.get("Status") or "unknown")
        health = str(row.get("Health") or "-")
        if service in expected and state.lower() == "running":
            running.add(service)
        print(f"  {service:<12} {state:<12} health={health}")
    missing = sorted(expected - running)
    print(f"  overall: {'OK' if not missing else 'NOT OK'}")
    if missing:
        print(f"  missing/not running: {', '.join(missing)}")

    try:
        status = json.loads(sections.get("api", ""))
    except json.JSONDecodeError:
        print("\nAPI/database: UNAVAILABLE")
        detail = sections.get("api", "").splitlines()
        if detail:
            print(f"  {detail[-1]}")
        return 1

    database = status["database"]
    discovery = status["discovery"]
    applications = status["applications"]
    print("\nDatabase:")
    print(
        f"  {database['status']}; jobs={database['jobs']}, "
        f"tables={database['tables']}"
    )
    print("\nDiscovery:")
    print(
        f"  today={discovery['jobs_today']}; useful_today={discovery['useful_jobs_today']} "
        f"(eligible and score >= {discovery['useful_threshold']})"
    )
    quota = discovery["quota"]
    print(f"  fetch quota={quota['fetched']}/{quota['fetch_limit']}")
    for job in discovery["top_useful_jobs"]:
        print(f"  [{job['score']:.0f}] {job['company']} — {job['title']} ({job['source']})")

    print("\nApplications:")
    print(
        f"  actively applying={'YES' if applications['active'] else 'NO'}; "
        f"active applications={applications['active_applications']}; "
        f"active submissions={applications['active_submissions']}; queued={applications['queued']}"
    )
    states = applications["states"]
    if states:
        print("  states: " + ", ".join(f"{key}={value}" for key, value in states.items()))
    return 0 if not missing else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="SSH config host alias")
    parser.add_argument(
        "--remote-dir",
        required=True,
        help="absolute repository path on the server",
    )
    parser.add_argument(
        "--connect-timeout",
        type=int,
        default=20,
        help="seconds allowed for each SSH connection attempt",
    )
    parser.add_argument(
        "--ssh-attempts",
        type=int,
        default=3,
        help="number of SSH connection attempts",
    )
    args = parser.parse_args()
    if not args.remote_dir.startswith("/"):
        parser.error("--remote-dir must be an absolute path")
    if args.connect_timeout <= 0 or args.ssh_attempts <= 0:
        parser.error("--connect-timeout and --ssh-attempts must be positive")

    for attempt in range(1, args.ssh_attempts + 1):
        completed = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={args.connect_timeout}",
                args.host,
                "sh",
                "-s",
                "--",
                args.remote_dir,
            ],
            input=REMOTE_SCRIPT,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0:
            break
        if attempt < args.ssh_attempts:
            print(
                f"SSH attempt {attempt}/{args.ssh_attempts} failed; retrying...",
                file=sys.stderr,
            )
    if completed.returncode != 0:
        print(f"SSH check failed: {completed.stderr.strip()}", file=sys.stderr)
        return completed.returncode or 1
    return _print_report(args.host, _sections(completed.stdout))


if __name__ == "__main__":
    raise SystemExit(main())
