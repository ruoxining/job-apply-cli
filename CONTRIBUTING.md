# Contributing / 贡献指南

Use Python 3.12+, Node.js 22 for the small JavaScript fixture suite, and uv 0.11.18.
The checkout contains synthetic examples only. Each contributor keeps candidate data and
credentials in their own ignored `private/` directory and `.env`.

```sh
uv sync --locked --extra test --extra browser
uv run playwright install chromium
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run bandit -r src scripts -ll
uv run python scripts/export_public.py --check
uv run python scripts/check_repository.py
uv run pytest
node --test tests/lcu_fixture_policy.test.mjs
uv build
```

For wheel verification, create a clean virtual environment outside the checkout, install
`dist/*.whl`, and run `scripts/check_package.py` using that environment's Python. The script
starts real CLI processes in a temporary path with spaces. It never submits an application.

Work on a branch, add every publishable new file to `public-files.txt`, and open a PR with a
Conventional Commit title such as `fix: reject ambiguous answers`. Describe behavior and
actual validation. Fix CI failures, assess PR-Agent comments, and ask the other contributor
to review. A reviewer cannot approve their own PR. New commits invalidate old approvals.
Once ready, `gh pr merge --auto --squash NUMBER` lets GitHub merge when all rules are met.
Do not use administrative bypasses. The second contributor must be added to CODEOWNERS
before they can approve owner-authored changes; this setup does not invite collaborators.

See [review policy](docs/review-policy.md), [automation setup](docs/automation.md),
[development](docs/en/development.md) and [security review](docs/en/security-review.md).
Report security issues privately to the repository owner without posting secrets or personal
data. Use the issue template for bugs with synthetic reproduction inputs.

## Compatibility

The package is `job-apply-cli`; `job-apply-cli` and the existing `job-apply-agent` command alias
call the same CLI. The Python import package remains `job_apply_agent`. The initial CI matrix
checks Python 3.12 wheel installation and core offline CLI commands on Linux, macOS and
Windows. Full unit/browser-fixture tests run on Linux. This does not promise cross-platform
support for live browser control, server deployment, external credentials or optional model
retrieval. JSON output and exit codes are interfaces; document changes and add useful
regressions before changing them. Before 1.0, breaking changes still require release notes.

## 中文

使用 Python 3.12+、uv 0.11.18，JavaScript fixture 测试使用 Node.js 22。仓库只放合成示例；
每位贡献者把个人资料和凭据保存在自己的 `private/` 与 `.env` 中，不共享真实投递数据库。
上方命令是本地必需检查。验证发行包时，在仓库外创建干净虚拟环境，安装 `dist/*.whl`，再用该环境
的 Python 运行 `scripts/check_package.py`。脚本会在带空格的临时路径启动真实 CLI 进程，不会投递。

从分支提交 PR，新文件加入 `public-files.txt`。标题采用 Conventional Commits，描述实际行为变化
及验证结果。先修复 CI，再评估 AI 意见，由另一位贡献者审核。新提交会清除旧批准；确定可合并后，
运行 `gh pr merge --auto --squash NUMBER` 等待规则满足。不要绕过分支规则。第二位贡献者加入
CODEOWNERS 后才能批准仓库所有者发起的改动；本次配置不会邀请协作者。

发行包名为 `job-apply-cli`，新旧两个命令名均保留，Python 模块名仍为 `job_apply_agent`。
CI 在 Linux、macOS、Windows 验证 Python 3.12 的安装及核心离线命令，完整测试在 Linux 运行。
该范围不代表真实浏览器控制、服务器部署、外部凭据或可选模型检索均支持所有平台。
JSON 和退出码属于接口；破坏性改动即使在 1.0 之前也要说明并增加必要回归覆盖。
