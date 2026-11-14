# Security review and collaboration limits

English | [简体中文](../security-review.md)

Review date: 2026-11-13. Baseline: `d0953c0`; the fixes below are committed with this report.
The scope covers the collaboration source repository, reachable Git history, locked Python
dependencies, local API, submission entry points, and selected external input handling.
No production server was contacted, no real application was submitted, and no collaborator
was invited.

This repository can support isolated code development. The owner should continue to manage
real candidate data, browser sessions, model accounts, and application bots separately.
The application is not a multi-user service. Open risks remain; this review is not a complete
penetration test or certification for unattended production use.

## Fixed in this review

| Issue | Previous impact | Fix and evidence |
| --- | --- | --- |
| Unauthenticated API | Any process able to reach the port could read jobs and application receipts or dispatch collection; loopback only restricted network exposure | All business routes in the [API](../../src/job_apply_agent/api.py) require a Bearer Token and fail closed when unconfigured; [tests](../../tests/test_api.py) verify rejected requests cannot access the database or dispatcher |
| Live submission acknowledgment enforced only by preflight | Starting a worker directly or calling an adapter could bypass the acknowledgment with `ALLOW_LIVE_SUBMIT=true` alone | [Settings](../../src/job_apply_agent/config.py), the worker, managed entry points, and final submission enforce `LIVE_SUBMIT_ACK`; Compose passes it through; [tests](../../tests/test_submission_safety.py) verify rejection before browser access or a submission claim |
| Late response size check | `client.get()` buffered the whole response before the 2 MB check | The [collector](../../src/job_apply_agent/collectors/job_link.py) streams the response, stops early, and explicitly disables redirects; [tests](../../tests/test_job_link.py) verify early closure and enforce redirect policy even with a differently configured injected client |
| Unquoted SSH remote path | Spaces and shell characters in paths could be interpreted by the remote shell | The [script](../../scripts/server_status.py) quotes remote arguments and rejects option-shaped hostnames; it reads the API token inside the container instead of passing it in SSH arguments; [tests](../../tests/test_server_status_script.py) cover special-character paths |
| Known dependency advisories | Published issues affected the TLS trust bundle, telemetry parsing, and test temporary directories | Updated the lockfile to `certifi 2026.10.23`, `opentelemetry-api 1.45.2`, and `pytest 9.2.0`; updated `pytest-asyncio` to `1.5.0` for compatibility |

Dependency references: [certifi advisory](https://github.com/advisories/GHSA-6q48-6c7w-9gw2),
[OpenTelemetry advisory](https://github.com/advisories/GHSA-mf5v-w3w3-f6cg), and
[pytest fix](https://docs.pytest.org/en/9.0.x/changelog.html). These are affected dependency
versions, not evidence that every exploit path was reproduced in this application.

API users must now set a random `API_TOKEN` in their own `.env`; see [onboarding](onboarding.md).
It remains a shared instance secret, without per-user permissions or data isolation.
Dependency updates take effect only after synchronizing an environment or rebuilding an
image. No remote deployment was updated during this review.

## Open findings

| Priority and exposure condition | Finding and evidence | Next step |
| --- | --- | --- |
| High: untrusted generic job URLs | [URL validation and fetching](../../src/job_apply_agent/collectors/job_link.py) resolve DNS separately. In a controlled test, the first lookup returned a public address and the second returned an owned loopback service; the request reached that service. Private-address checks and disabled redirects do not prevent this rebinding | Pin connections to validated addresses while preserving Host, TLS SNI, and certificate checks, with network egress isolation. Until then, keep generic URL fetching away from environments containing sensitive internal services |
| High: model subprocesses sharing real credentials | The [Codex planner](../../src/job_apply_agent/ats/codex_planner.py) inherits the process environment and rejects unexpected tools only after the child exits. Read-only configuration and retrospective detection do not establish isolation of file reads, environment access, or data egress | Use a separate user or container, minimal environment variables and mounts, tool restrictions applied before execution, and adversarial tests. This finding is based on code review; no real-model exploit was attempted |
| High: granting collaborator write access | At review time, `main` was unprotected, rulesets were empty, and no CI workflow existed; the PR workflow was not enforced | Where the account plan supports it, enable main-branch protection, owner review, and required checks. Add CI before requiring its status for merging. No GitHub administrative settings or invitations were changed in this review |
| Medium: Telegram configured for a group | [Callbacks and commands](../../src/job_apply_agent/telegram/review.py) and [experience editing](../../src/job_apply_agent/telegram/experiences.py) check chat ID, not the acting user ID; other group members share those privileges | Use only the owner's private chat for now. Group collaboration requires an explicit user allowlist and per-user authorization tests |

DNS defenses should cover both application and network egress controls; see the
[OWASP SSRF guide](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html).
Private repositories owned by personal accounts give collaborators write access; an invitation
is not read-only sharing. See [GitHub's permission reference](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/repository-access-and-collaboration/permission-levels-for-a-personal-account-repository).
Private-repository branch protection also depends on the account plan; see
[GitHub's branch protection documentation](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches).

The API also lacks TLS, rate limits, and tenant isolation. Browsers can load cross-origin
resources, and network egress has not been fully audited. Streaming HTML checks do not provide
complete resource budgets for decompression bombs, slow responses, or every ATS JSON response.
Keep the API on loopback and avoid sharing an instance containing real data.

## Collaboration workflow

1. Clone only the clean source repository. Do not share the original workspace, old Git history, `private/`, `.env`, databases, screenshots, or browser profiles.
2. Each collaborator uses a separate test directory, fictional candidate data, and their own API token. Keep `ALLOW_LIVE_SUBMIT=false` and `CUA_ENABLED=false`, without production bot or model credentials.
3. Deliver work through feature branches and pull requests. The owner reviews changes to submission controls, claims, fact and answer provenance, uploads, network access, and permissions.
4. Keep production credentials out of CI and do not run unreviewed branches in environments containing real data. Do not delegate live submissions through a shared instance yet.

## Verification and limits

- Scanned 306 file versions across the baseline's two reachable commits against the export allowlist and 11 local private markers, with no rule matches. This does not rule out every indirect identifying detail; GitHub account and commit attribution remain repository metadata.
- Read-only GitHub checks confirmed private visibility, zero repository Actions secrets, a read-only default workflow token, and no workflow permission to approve PRs. Organization or environment secrets, external systems, and the original private repository were not audited.
- `uv run pytest --tb=short --disable-warnings`: 359 tests passed with 275 warnings, including deprecations; covers regressions and the added security boundaries.
- Exported all extras with `uv export --frozen --no-dev --all-extras --no-emit-project --no-hashes`, then used `pip-audit --no-deps --disable-pip` on 80 locked packages applicable to the current platform. After upgrades, no known advisories matched and no packages were skipped. Duplicate database records were not counted as separate issues among the baseline's three distinct advisories.
- The DNS rebinding reproduction contacted only an owned local HTTP fixture, with no external or production internal targets.
- No full production deployment, container OS vulnerability scan, private CLI/LCU runtime audit, or real-model adversarial test was performed.
