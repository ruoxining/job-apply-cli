#!/usr/bin/env python3
"""Review PR data using trusted policy, without checking out or executing PR code."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_DIFF_BYTES = 90_000
MAX_FILES = 40
EXCLUDED = {"uv.lock", "CHANGELOG.md", "public-files.txt"}


class GitHub:
    def __init__(self, repository: str, token: str):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("Invalid repository")
        self.base = f"https://api.github.com/repos/{repository}/"
        self.token = token

    def request(self, path: str, data=None, method: str | None = None):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(data).encode() if data is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        # URL always starts with the fixed HTTPS GitHub API origin above.
        with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
            body = response.read()
        return json.loads(body) if body else None

    def pages(self, path: str):
        result = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 31):
            batch = self.request(f"{path}{separator}per_page=100&page={page}")
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise ValueError("Pagination limit exceeded")


def build_diff(files: list[dict]) -> tuple[str, list[str]]:
    if len(files) > MAX_FILES:
        raise ValueError("PR exceeds the 40-file review limit; split it for complete review")
    chunks, excluded = [], []
    for file in files:
        name = file["filename"]
        if name in EXCLUDED:
            excluded.append(name)
            continue
        previous = file.get("previous_filename", name)
        if any(character in name + previous for character in "\n\r\t\x00"):
            raise ValueError("Unsupported filename")
        patch = file.get("patch", "")
        if not patch and file["status"] == "renamed" and file["changes"] == 0:
            patch = ""
        elif not patch:
            raise ValueError("A changed file has no text patch; manual review is required")
        additions = sum(line.startswith("+") for line in patch.splitlines())
        deletions = sum(line.startswith("-") for line in patch.splitlines())
        if additions != file["additions"] or deletions != file["deletions"]:
            raise ValueError("GitHub returned a truncated patch; split the PR")
        chunks.append(
            f"diff --git a/{previous} b/{name}\n--- a/{previous}\n+++ b/{name}\n{patch}\n"
        )
    diff = "\n".join(chunks)
    if len(diff.encode()) > MAX_DIFF_BYTES:
        raise ValueError("PR exceeds the 90 KB review limit; split it for complete review")
    return diff, excluded


def latest_ci_passed(api: GitHub, sha: str) -> bool:
    runs = api.request(f"actions/workflows/ci.yml/runs?head_sha={sha}&per_page=100")[
        "workflow_runs"
    ]
    eligible = [run for run in runs if run["event"] in {"pull_request", "workflow_dispatch"}]
    if not eligible:
        return False
    latest = max(eligible, key=lambda run: (run["id"], run.get("run_attempt", 1)))
    return latest["status"] == "completed" and latest["conclusion"] == "success"


def marker(sha: str) -> str:
    return f"<!-- pr-agent-reviewed:{sha} -->"


def run_model(diff: str) -> dict:
    key = os.environ.get("PR_AGENT_API_KEY")
    if not key:
        raise ValueError("Missing PR_AGENT_API_KEY in the protected ai-review environment")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP"}
    }
    environment.update(
        {
            "OPENAI__KEY": key,
            "OPENAI__API_BASE": os.environ.get(
                "PR_AGENT_API_BASE", "https://open.bigmodel.cn/api/paas/v4"
            ),
            "CONFIG__MODEL": os.environ.get("PR_AGENT_MODEL", "openai/glm-5.3-flash"),
            "CONFIG__USE_REPO_SETTINGS_FILE": "false",
            "CONFIG__USE_GLOBAL_SETTINGS_FILE": "false",
            "CONFIG__ENABLE_AUTO_APPROVAL": "false",
            "CONFIG__FALLBACK_MODELS": "[]",
            "CONFIG__MAX_MODEL_TOKENS": "64000",
            "CONFIG__CUSTOM_MODEL_MAX_TOKENS": "64000",
            "CONFIG__MAX_OUTPUT_TOKENS": "6000",
            "CONFIG__AI_TIMEOUT": "180",
            "CONFIG__NUM_RETRIES": "1",
            "CONFIG__RETRY_SAME_MODEL_ON_TIMEOUT": "false",
            "CONFIG__PUBLISH_OUTPUT_PROGRESS": "false",
            "CONFIG__REPO_CONTEXT_FILES": "[]",
            "PR_REVIEWER__EXTRA_INSTRUCTIONS": (ROOT / "docs/review-policy.md").read_text(),
            "PR_REVIEWER__NUM_MAX_FINDINGS": "5",
            "PR_REVIEWER__REQUIRE_SECURITY_REVIEW": "true",
            "PR_REVIEWER__REQUIRE_TESTS_REVIEW": "true",
            "PR_REVIEWER__ENABLE_REVIEW_LABELS_EFFORT": "false",
            "PR_REVIEWER__ENABLE_REVIEW_LABELS_SECURITY": "false",
            "LITELLM_LOG": "ERROR",
        }
    )
    with tempfile.TemporaryDirectory(prefix="pr-agent-") as directory:
        root = Path(directory)
        diff_file, output = root / "input.diff", root / "review.json"
        diff_file.write_text(diff, encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pr_agent.cli",
                "--diff-file",
                str(diff_file),
                "--output",
                str(root / "review.md"),
                "--json-output",
                str(output),
                "review",
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        # Never print provider logs or environment values; incomplete output is a failure.
        if result.returncode or not output.is_file():
            raise ValueError("PR-Agent did not produce a complete structured review")
        data = json.loads(output.read_text())
        review = data.get("review")
        if not isinstance(review, dict) or not isinstance(review.get("key_issues_to_review"), list):
            raise ValueError("Invalid PR-Agent structured review")
        return review


def render_review(review: dict, sha: str, excluded: list[str]) -> str:
    # Keep model text inert: no embedded images, HTML, mentions, or rendered external links.
    data = json.dumps(review, ensure_ascii=False, indent=2)
    data = data.replace("`", "\\u0060").replace("<", "\\u003c").replace("@", "\\u0040")
    if len(data) > 45_000:
        raise ValueError("Review output exceeds the comment limit")
    return (
        f"{marker(sha)}\n## PR-Agent advisory review\n\n"
        f"Reviewed commit `{sha}` after CI passed. This is a comment, not an approval.\n\n"
        f"Generated files excluded: {', '.join(excluded) or 'none'}.\n\n"
        f"```json\n{data}\n```\n\nA contributor must assess findings and approve the latest change."
    )


def review_one(api: GitHub, number: int) -> None:
    pr = api.request(f"pulls/{number}")
    if pr["state"] != "open" or pr["draft"] or pr["base"]["ref"] != "main":
        print(f"PR {number}: not an open, ready main-branch PR; skipped")
        return
    sha = pr["head"]["sha"]
    if not latest_ci_passed(api, sha):
        print(f"PR {number}: latest CI has not passed; skipped")
        return
    reviews = api.pages(f"pulls/{number}/reviews")
    if any(
        r["user"]["login"] == "github-actions[bot]" and marker(sha) in r["body"] for r in reviews
    ):
        print(f"PR {number}: this commit already has a completed review; skipped")
        return
    diff, excluded = build_diff(api.pages(f"pulls/{number}/files"))
    if not diff.strip():
        print(f"PR {number}: only generated files; manual/dependency checks apply")
        return
    review = run_model(diff)
    current = api.request(f"pulls/{number}")
    if current["head"]["sha"] != sha or current["draft"] or current["state"] != "open":
        print(f"PR {number}: changed during review; discarded stale output")
        return
    if not latest_ci_passed(api, sha):
        raise ValueError("CI is no longer successful; review was not published")
    api.request(
        f"pulls/{number}/reviews",
        {"commit_id": sha, "event": "COMMENT", "body": render_review(review, sha, excluded)},
    )
    print(f"PR {number}: advisory review posted for {sha}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int)
    args = parser.parse_args()
    api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    if args.pr:
        numbers = [args.pr]
    elif "pull_request" in event:
        numbers = [event["pull_request"]["number"]]
    else:
        sha = event["workflow_run"]["head_sha"]
        numbers = [
            pr["number"] for pr in api.request(f"commits/{sha}/pulls") if pr["state"] == "open"
        ]
    for number in numbers:
        if number <= 0:
            raise ValueError("PR number must be positive")
        review_one(api, number)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        # API errors and model responses may contain secrets; emit only controlled messages.
        print(
            f"AI review incomplete ({type(error).__name__}); check credentials, CI and review limits.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
