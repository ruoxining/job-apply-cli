#!/usr/bin/env python3
"""Plan/apply repository controls via gh; never print, collect or store credential values."""

from __future__ import annotations

import argparse
import json
import subprocess


def api(path: str, method: str = "GET", body=None):
    command = ["gh", "api", path, "--method", method]
    if body is not None:
        command += ["--input", "-"]
    result = subprocess.run(
        command,
        input=json.dumps(body) if body is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout.strip() else None


def desired_rules() -> dict:
    return {
        "name": "main-reviewed",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "required_linear_history"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 1,
                    "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": True,
                    "require_last_push_approval": True,
                    "required_review_thread_resolution": True,
                    "allowed_merge_methods": ["squash"],
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": "validate", "integration_id": 15368}],
                    "strict_required_status_checks_policy": True,
                    "do_not_enforce_on_create": False,
                },
            },
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repository")
    parser.add_argument(
        "--apply", action="store_true", help="apply and verify; default is read-only"
    )
    parser.add_argument(
        "--enable-pypi", action="store_true", help="enable publishing after release PR approval"
    )
    args = parser.parse_args()
    root = f"repos/{args.repo}"
    repository = api(root)
    if not repository["permissions"]["admin"]:
        raise RuntimeError("Repository admin permission is required")
    print(
        json.dumps(
            {
                "repository": repository["full_name"],
                "private": repository["private"],
                "apply": args.apply,
                "desired_rules": desired_rules(),
            },
            indent=2,
        )
    )
    if not args.apply:
        print("No settings changed. Secrets and PyPI account binding are separate steps.")
        return 0
    failures = []

    def attempt(label, operation):
        try:
            value = operation()
            print(f"OK: {label}")
            return value
        except RuntimeError as error:
            failures.append(label)
            print(f"UNAVAILABLE: {label}: {error}")
            return None

    attempt(
        "merge settings",
        lambda: api(
            root,
            "PATCH",
            {
                "allow_auto_merge": True,
                "allow_squash_merge": True,
                "allow_merge_commit": False,
                "allow_rebase_merge": False,
                "delete_branch_on_merge": True,
                "squash_merge_commit_title": "PR_TITLE",
                "squash_merge_commit_message": "PR_BODY",
            },
        ),
    )
    # GitHub uses one toggle for creating AND approving PRs. The release workflow
    # needs creation; no workflow grants AI approval. Required human review remains.
    attempt(
        "read-only default workflow token",
        lambda: api(
            root + "/actions/permissions/workflow",
            "PUT",
            {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": True},
        ),
    )
    if not repository["private"]:
        attempt(
            "external contributors need workflow approval",
            lambda: api(
                root + "/actions/permissions/fork-pr-contributor-approval",
                "PUT",
                {"approval_policy": "all_external_contributors"},
            ),
        )
    else:
        print("SKIP: external contributor approval policy applies to public repositories")
    attempt("Dependabot alerts", lambda: api(root + "/vulnerability-alerts", "PUT"))
    attempt("Dependabot security updates", lambda: api(root + "/automated-security-fixes", "PUT"))

    def environment(name: str):
        api(
            root + f"/environments/{name}",
            "PUT",
            {
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                },
            },
        )
        policies = api(root + f"/environments/{name}/deployment-branch-policies")["branch_policies"]
        if not any(item["name"] == "main" and item["type"] == "branch" for item in policies):
            api(
                root + f"/environments/{name}/deployment-branch-policies",
                "POST",
                {
                    "name": "main",
                    "type": "branch",
                },
            )
        verified = api(root + f"/environments/{name}")
        assert verified["deployment_branch_policy"]["custom_branch_policies"]
        return verified

    attempt("ai-review environment limited to main", lambda: environment("ai-review"))
    attempt("pypi environment limited to main", lambda: environment("pypi"))
    variables = {
        "PR_AGENT_API_BASE": "https://open.bigmodel.cn/api/paas/v4",
        "PR_AGENT_MODEL": "openai/glm-5.3-flash",
        "PYPI_PUBLISH_ENABLED": str(args.enable_pypi).lower(),
    }
    existing = {v["name"] for v in api(root + "/actions/variables?per_page=100")["variables"]}
    for name, value in variables.items():
        attempt(
            f"variable {name}",
            lambda name=name, value=value: api(
                root + "/actions/variables" + (f"/{name}" if name in existing else ""),
                "PATCH" if name in existing else "POST",
                {"name": name, "value": value},
            ),
        )

    security = repository.get("security_and_analysis") or {}
    # Enable supported features only; do not enroll private repositories into paid products.
    for feature in ("secret_scanning", "secret_scanning_push_protection"):
        if feature in security or not repository["private"]:
            attempt(
                feature,
                lambda feature=feature: api(
                    root,
                    "PATCH",
                    {
                        "security_and_analysis": {feature: {"status": "enabled"}},
                    },
                ),
            )
        else:
            print(f"UNAVAILABLE: {feature}: not exposed by this private repository's plan")

    def rules():
        current = api(root + "/rulesets")
        match = next((r for r in current if r["name"] == "main-reviewed"), None)
        suffix = f"/{match['id']}" if match else ""
        rule = api(root + "/rulesets" + suffix, "PUT" if match else "POST", desired_rules())
        verified = api(root + f"/rulesets/{rule['id']}")
        assert verified["enforcement"] == "active" and not verified["bypass_actors"]
        actual = {r["type"]: r for r in verified["rules"]}
        for rule in desired_rules()["rules"]:
            assert actual[rule["type"]] == rule, "Ruleset verification failed"
        return verified

    attempt("main review and current CI rules", rules)
    result = api(root)
    print(
        json.dumps(
            {
                "repository": result["full_name"],
                "private": result["private"],
                "auto_merge": result["allow_auto_merge"],
                "failed_controls": failures,
            }
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
