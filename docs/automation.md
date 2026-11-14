# Automation setup / 自动化配置

The repository is private while collaboration is prepared. The package/repository name is
`job-apply-cli`. A PyPI upload makes the distribution public even while GitHub remains private.
The release PR is the human publication decision; do not merge it until the release is ready.

## Workflows

| Workflow | Trigger and responsibility |
| --- | --- |
| CI | PR, main push or explicit dispatch; format, lint, types, security, privacy, tests and same-wheel installation on three OSes. `validate` is required. |
| PR-Agent | Ready PR after its latest CI passes. Trusted main policy and plain diff mode; no PR checkout, no auto-description, no approval. |
| Claude on demand | Maintainer dispatch with PR number and specific task; bounded investigation/patch artifact. Apply the patch through a normal PR and CI. |
| Release | Successful main CI; release-please opens a version PR, then after its reviewed merge creates tag/release and verifies/publishes the package. |
| CodeQL | Runs only with `CODEQL_ENABLED=true` when the repository has access to code scanning. |

PR-Agent is pinned to a reviewed upstream commit. Its four OpenTelemetry 1.45.1 metadata
pins are patched coherently to 1.45.2 for CVE-2026-126360 by `scripts/install_reviewer.py`;
CI checks dependency compatibility, audits that environment and exercises the real reviewer
CLI against a local synthetic provider. Revalidate the patch when upgrading PR-Agent.

Actions use immutable commit pins. Dependabot updates uv and Actions weekly in groups.
The release workflow explicitly dispatches CI for the release PR, waits for that exact run
to pass, then dispatches advisory review from main: PRs created with
`GITHUB_TOKEN` create approval-required workflow placeholders. These contain no executed
jobs and are not CI results; the reviewer uses the latest executed PR/dispatched CI for the
exact SHA. Failed, cancelled or running checks still block review. Release packaging is called in the same workflow,
so the bot-created tag does not depend on a second implicit event. The pinned release-please
TOML updater wraps scalar fields; the uv.lock JSONPath deliberately matches `name.value`.
CI requires the lockfile, module and distribution versions to stay synchronized. The explicit
review dispatch also covers GitHub suppressing a downstream workflow_run from bot-started CI.

## GitHub initialization

```sh
gh auth status
python3 scripts/setup_github.py --repo OWNER/REPOSITORY
python3 scripts/setup_github.py --repo OWNER/REPOSITORY --apply --enable-pypi
```

Default mode is read-only. Apply mode enables auto-merge and squash merges, read-only default
tokens, required approval for external workflow runs, available security features, main-only
environments and a non-bypassable main ruleset. It does not change visibility or invite people.
The GitHub toggle for creating bot PRs also permits workflow review approvals; no workflow
uses that approval capability. Required CODEOWNER approval must come from a listed person.
Keep CODEOWNERS limited to human maintainers and review workflow changes carefully.

Private repositories may need a paid GitHub plan for rulesets/environments and additional
security products for CodeQL/secret scanning. The script reports unsupported controls instead
of claiming they are enabled or making the repository public. It does not enable paid products.
Do not store a model key if the main-only environment could not be created.

## GLM review credentials

Store the existing GLM API key in environment `ai-review`, secret `PR_AGENT_API_KEY`.
Use `gh secret set PR_AGENT_API_KEY --repo OWNER/REPOSITORY --env ai-review` and enter it at
the hidden prompt or pipe it from the local credential store. Never put it in command
arguments, Git, issues or logs. Variables:

```text
PR_AGENT_API_BASE=https://api.z.ai/api/coding/paas/v4
PR_AGENT_MODEL=openai/glm-5.3-flash
```

PR-Agent uses one configured model, a 90 KB / 40-file limit, five findings maximum, a 300-second
process limit and no fallback models. Missing credentials, truncated diffs or invalid output
fail the review. Generated files are listed as excluded. A green run means review execution,
never a human approval or proof of correctness. To retry: `gh workflow run ai-review.yml -f pr=N`.
Review output is posted as inert JSON to avoid model-generated links, images and mentions.

Claude requires `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` in the same protected
environment. It is optional and never starts from an arbitrary comment. Dispatch:

```sh
gh workflow run claude.yml -f pr=N -f task='Investigate the stated finding and add a regression test'
```

Download `claude-proposal` from the run, review/apply its patch, and submit a normal PR.
The current entry point disables command execution and GitHub writes while model credentials
are present; test execution happens in CI after the proposed change is submitted.

## PyPI Trusted Publishing

Sign in to [PyPI publishing](https://pypi.org/manage/account/publishing/) and add a pending
publisher for the new project, or configure publishing on an existing project you control:

| Field | Value for this repository |
| --- | --- |
| PyPI project | `job-apply-cli` |
| GitHub owner | `ruoxining` |
| GitHub repository | `job-apply-cli` |
| Workflow filename | `release.yml` |
| Environment | `pypi` |

`PYPI_PUBLISH_ENABLED=true` enables the final job. There is no PyPI token secret. PyPI's
account binding must exist before the first release. A 404 package lookup does not reserve
the name. The workflow publishes the exact wheel/sdist downloaded from its successful
three-OS verification run and requests attestations. A failed upload does not mean publication
succeeded; inspect PyPI and the run before retrying. Do not overwrite or recycle a version.

## 中文

协作仓库保持 private，项目名统一为 `job-apply-cli`。上传 PyPI 会公开发行包；合并发布 PR 是发布
决定，确认可以公开后再合并。CI 执行格式、lint、类型、安全、隐私、测试与三系统安装验证，
`validate` 是合并门槛。PR-Agent 只审核最新已通过 CI 的 ready PR，使用主分支规则，不执行 PR
代码、不写描述、不批准。Claude 由维护者针对具体问题手动调用，输出补丁，进入普通 PR 后再验证。

上方初始化命令默认只查看，`--apply --enable-pypi` 应用设置并启用 PyPI 发布任务，不改变仓库
可见性、不邀请协作者。私有仓库的规则、环境或安全功能可能受 GitHub 套餐限制，脚本会报告缺口，
不会静默降级或自动订购。创建机器人 PR 的 GitHub 开关同时允许机器人批准，但我们的流水线不用
该能力；CODEOWNERS 只列人工维护者，并要求其中一人审核。

当前沿用已有的 Z.ai Coding 服务；API 地址与模型见上方变量。GLM 密钥通过隐藏输入存入仅限 main 的 `ai-review` 环境，名称为 `PR_AGENT_API_KEY`；环境创建
失败时不要把密钥退回普通仓库 secret。审核限制为 40 文件、90 KB、最多 5 条问题及 300 秒；
失败、超限、截断或无效输出都不能算审核完成。成功执行也不等于批准。

Claude 凭据同样放入 `ai-review`，只保留按需入口。当前输出供维护者检查的补丁与验证说明，不直接
提交，也不在持有模型凭据的任务里执行 PR 代码。reviewdog 和低风险自动批准后续再考虑。

PyPI 必须由已登录账号在上方网页完成一次可信发布绑定，字段见表。`PYPI_PUBLISH_ENABLED=true`
启用 OIDC 发布，无需长期 PyPI token。首次上传前名称并未保留。release-please 创建的 PR 会留下等待工作流批准的空记录，因此发布流程显式调度 CI。审核器只按
同一 SHA 上最近实际执行的 CI 判断；失败、取消或仍运行的检查仍阻止审核。发布流程在同一流程中调用打包、三系统验证和上传任务。
上传失败要核对真实状态后处理，不能将已创建 tag 当作已成功发布到 PyPI。

## Sources / 依据

- [PR-Agent](https://github.com/The-PR-Agent/pr-agent)
- [Z.ai Coding Plan endpoint](https://zcode.z.ai/en/docs/configuration)
- [Claude Code Action security](https://github.com/anthropics/claude-code-action/blob/main/docs/security.md)
- [release-please Action](https://github.com/googleapis/release-please-action)
- [PyPI Trusted Publishers](https://docs.pypi.org/trusted-publishers/)
- [Dependabot with uv](https://docs.astral.sh/uv/guides/integration/dependabot/)
- [GitHub token workflow triggers](https://docs.github.com/en/actions/concepts/security/github_token)
- [GitHub rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets)
