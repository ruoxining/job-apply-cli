# Review and release automation plan

Source: the final configuration decisions in the shared conversation supplied by the owner.
Target: the private collaboration repository; personal runtime data remains excluded.

## Required outcome

- [ ] Read-only CI on pull requests and pushes, with obsolete runs cancelled: formatting,
  lint, type checks, unit/browser fixtures, static security and secret checks.
- [ ] Build distributions and install the exact wheel into clean environments. Exercise real
  CLI processes, exit codes, stdout/stderr, configuration precedence, paths with spaces,
  non-interactive use, and controlled failures. Validate supported operating systems.
- [ ] PR-Agent performs the regular AI review only for ready PRs whose latest commit passed
  CI. Use trusted base-branch review policy and configuration; PR content is untrusted data.
  Disable automatic descriptions and duplicate full-review bots, bound cost, and cancel stale runs.
- [ ] Claude Code Action has an explicit maintainer-only invocation for investigation,
  reproduction, regression tests, or fixes. Include the specific findings/logs, bounded turns,
  and verification results; changes must pass the normal checks and human approval again.
- [ ] One shared review policy defines evidence, blocking findings, business invariants,
  compatibility, sensitive paths, and non-blocking style feedback.
- [ ] Main requires PRs, current required checks, and one other contributor's approval;
  new commits dismiss old approval. AI completion is not approval. Enable auto-merge after
  those conditions, without automatically approving application or workflow changes.
- [ ] Release-please maintains versions, changelog, release PRs, tags, and GitHub Releases.
  A person approves the release PR. Build and verify release packages before publication.
- [ ] Prepare PyPI OIDC trusted publishing with explicit environment and enablement;
  actual PyPI publication depends on the owner's release decision and publisher binding.
- [ ] Weekly grouped Dependabot updates for Python and GitHub Actions; configure available
  repository secret scanning/push protection and Python CodeQL, with availability verified.
- [ ] Add CODEOWNERS, contribution instructions, issue/PR templates, installation guidance,
  compatibility promises, and release instructions. Keep user documentation bilingual.
- [ ] Provide an idempotent CLI setup script for GitHub settings, with a read-only plan mode,
  explicit application, verification, and clear unsupported-plan or credential failures.
- [ ] Verify actual GitHub workflow runs, current-SHA AI review, repository protections,
  and release workflow wiring. Record prerequisites that still require external action.

Reviewdog is deferred as agreed; it adds inline presentation rather than another detector.
Automatic low-risk dependency approval is also deferred. No collaborator invitation or real
application submission is part of this task.

## Current prerequisites

- Second contributor: owner explicitly deferred adding the account.
- PR-Agent: existing GLM service selected; credential location requested.
- Claude credentials: required only to exercise its on-demand entry point.
- PyPI enabled by owner request for job-apply-cli; browser is at account login for binding.

## Baseline evidence

- Main initially points to `e6aecd4`; working tree is clean.
- Repository is private, with admin access for the setup session.
- No workflows, rulesets, hooks, or repository Actions secrets are configured.
- Main is unprotected and auto-merge is disabled.
- Existing application validation: 359 tests passed in the preceding security work.
- Formatting and type-check diagnostics require cleanup before they can be enforced.

Completion requires checking each requirement against current files and GitHub state, not
merely adding workflow YAML. This file is the implementation checklist, not a completion report.

## Implementation evidence (verification in progress)

- Repository renamed to `job-apply-cli`, still private. Existing `job-apply-agent` and
  `job-agent` repositories were occupied and were not modified.
- Format/lint and mypy checks pass; 368 Python tests and five JavaScript tests pass locally.
- Built wheel installs into a clean Python 3.12 environment; real CLI smoke checks pass.
- Wheel and sdist metadata pass Twine 7.0.0. Actionlint validates all workflows.
- Locked application/development dependencies have no known vulnerabilities in the local audit.
- Pinned PR-Agent CLI exercised against a local fake OpenAI-compatible endpoint; trusted
  policy and structured output verified without contacting a model provider.
- PR-Agent dependency audit found CVE-2026-126360 in opentelemetry-api 1.45.1; fixing the
  review environment before rollout.
- GitHub main-only `ai-review` and `pypi` environments created. Ruleset exists in disabled
  bootstrap state until actual CI succeeds; do not describe main as protected yet.

## 中文进度

采用已确认的 CI、PR-Agent、release-please 方案；Claude 保留按需补丁入口，reviewdog 后续再加。
第二位贡献者用户名暂不添加，模型沿用 GLM，PyPI 发布已获授权。项目改名为 job-apply-cli，仍为
私有仓库。主分支规则待首次 CI 实测通过后启用；本地检查结果不能代替 GitHub 上的实际执行结果。
GLM 密钥位置与 PyPI 登录、可信发布绑定仍需完成。未勾选项表示尚未完成全部验收。
