# Review and release automation: accepted plan and verification

Target: [ruoxining/job-apply-cli](https://github.com/ruoxining/job-apply-cli), kept private.
The owner approved the shared conversation's CI, PR-Agent and release-please plan, selected
existing GLM credentials, enabled PyPI, and deferred adding the second contributor.
Personal runtime data and the original private application repository remain separate.

## Implemented controls

- Read-only CI validates formatting, lint, types, unit/browser fixtures, static security,
  secret history, repository privacy, dependencies and package metadata. Obsolete runs cancel.
- The same built wheel is installed on clean Linux, macOS and Windows runners. Real CLI
  processes verify exit codes, JSON/stdout/stderr, precedence, paths with spaces and failures.
- PR-Agent reviews ready PRs only after successful CI for the exact commit. Trusted main
  policy governs bounded plain-diff review; PR code is never executed with model credentials.
  Output is advisory COMMENT, with duplicate/stale guards and inert model-generated text.
- Claude has a maintainer-only, bounded, on-demand patch entry point. A maintainer applies
  the patch through a normal PR; tests and human review remain required.
- The setup script configures main PRs, current `validate` checks, one human CODEOWNER
  approval, stale-approval dismissal, latest-push review, resolved threads and no bypasses.
  Auto-merge becomes available only after GitHub's requirements pass.
- Release-please creates version PRs. The workflow explicitly dispatches CI, waits for that
  new run to pass on the unchanged PR commit, and dispatches advisory review from main.
  Human approval/merge of the release PR is the publication decision.
- Release packages undergo the same three-OS installation checks before OIDC PyPI upload.
  Publishing uses the verified wheel/sdist and requests attestations, without a PyPI token.
- Weekly grouped Dependabot updates cover uv and Actions. Bilingual contribution/review/
  automation documents, CODEOWNERS and issue/PR templates describe the contributor workflow.

Apply and verify repository settings with `scripts/setup_github.py --repo OWNER/REPO --apply
--enable-pypi`. The default command is read-only. GitHub settings and current workflow runs
are the authoritative live state; this document records the implementation and acceptance
observations rather than freezing a particular PR head as permanently approved.

## Observed verification

- [Main CI 43556124600](https://github.com/ruoxining/job-apply-cli/actions/runs/43556124600):
  373 Python tests, five JavaScript fixtures, lint/types/privacy/security checks, dependency
  audits, package build and all three installation jobs passed.
- [Release PR #4](https://github.com/ruoxining/job-apply-cli/pull/4) was created automatically.
  Its manifest, distribution, module and uv.lock project version synchronize to 0.2.0.
- [Release PR CI 43556358303](https://github.com/ruoxining/job-apply-cli/actions/runs/43556358303)
  passed. [GLM review 5901596022](https://github.com/ruoxining/job-apply-cli/pull/4#pullrequestreview-5901596022)
  was posted by github-actions as COMMENTED for that exact head, without approving the PR.
- PyPI displayed successful pending-publisher registration for `job-apply-cli`, repository
  `ruoxining/job-apply-cli`, workflow `release.yml`, environment `pypi`.
- Windows smoke checks caught an executable-path assumption and an unclosed SQLite handle;
  both were fixed without relaxing assertions. Release CI caught a stale uv.lock version;
  the pinned updater was exercised locally to verify that only the project version changes.
- The official PyTorch CPU wheel's `+cpu` suffix is mapped to its public version only for
  PyPI advisory lookup, preserving every requirement and environment marker.
- The pinned reviewer requires an OpenTelemetry 1.45.2 metadata patch for CVE-2026-126360.
  Its dependency compatibility/audit and synthetic provider contract pass. The existing
  Z.ai service also passed both a live connectivity check and structured reviewer execution.
- Bot-created PR events produce approval-required placeholders without executed jobs.
  These are not treated as CI results; failed, cancelled and running actual CI still block
  review. Explicit review dispatch covers suppressed downstream workflow_run events.

## Deliberate handoffs and limits

- The first release remains unmerged and unpublished. Pending PyPI registration does not
  reserve the package name. Inspect the release PR and workflow before deciding to publish.
- The second contributor account is deferred. Owner-authored PRs need another authorized
  human CODEOWNER before they can satisfy review requirements; no bypass is configured.
- Claude credentials are optional and not configured. Its entry point is installed but has
  not been exercised against the live Claude service.
- GitHub rejected native secret scanning/push protection for this private repository.
  Gitleaks runs in CI; CodeQL remains prepared but disabled because access is unavailable.
- Reviewdog and automated low-risk dependency approval remain deferred. No collaborators
  were invited and no real job application was submitted by this configuration work.

## 中文验收说明

协作仓库统一命名为 `job-apply-cli`，保持 private。已实现 CI、三系统同包安装验证、GLM PR-Agent
审核、按需 Claude 补丁入口、release-please、PyPI OIDC、Dependabot 和双语贡献文档。
主分支规则由初始化脚本应用并回读核对：必须走 PR、通过当前 CI、获得人工 CODEOWNER 批准；
新提交使旧批准失效，不设置绕过权限。实时状态以 GitHub 设置和运行记录为准。

远端已通过 373 项 Python 测试、5 项 JavaScript 测试和 Linux/macOS/Windows 安装验证。
发布 PR #4 已自动生成并通过 CI，真实 GLM 审核以 COMMENTED 发布到准确提交，没有代替人工批准。
PyPI 已确认保存项目 `job-apply-cli`、仓库 `ruoxining/job-apply-cli`、工作流 `release.yml`、
环境 `pypi` 的可信发布绑定。后续提交会重新触发检查与审核，历史成功不代表新提交已通过。

首次发布仍由人工决定是否合并发布 PR。第二位贡献者暂不添加；Claude 仅配置入口，尚缺可选凭据。
当前私有仓库不能开启 GitHub 原生 secret scanning/push protection，使用 CI 中的 Gitleaks；
CodeQL 暂未启用。reviewdog 和低风险自动批准继续延期。未邀请协作者，也未执行实际岗位投递。
