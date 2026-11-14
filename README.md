# Job Apply Agent

[English](README.en.md) | 简体中文

个人求职流程的采集、筛选、材料准备和投递编排工具，支持 Greenhouse、Lever、Ashby。
候选人资料与代码分开保存。默认关闭真实投递，也不自动启用 Telegram 或模型服务。

项目仍处于实验阶段。已有受监督的实际投递经验，但不能据此承诺无人值守运行的可靠性。
浏览器连接、必填问题和各网站规则仍可能需要人工处理；提交结果不明时保留记录，禁止盲目重投。

## 从这里开始

需要 Python 3.12+ 和 uv。无需个人 resume 仓库或 Git 子模块。

```bash
uv sync --frozen --extra test
cp .env.example .env
uv run job-apply-agent onboard init
```

初始化只在 `private/candidate/` 创建本地模板，不上传文件、不请求模型、不采集或投递。
接下来按 [首次使用指引](docs/onboarding.md) 填写资料、放入已审核的 PDF/DOCX，再执行：

```bash
uv run job-apply-agent onboard check
uv run job-apply-agent init-db
uv run job-apply-agent onboard import-answers
uv run job-apply-agent serve
```

模板含有待替换标记，首次检查失败是预期行为。默认 SQLite 数据库、经历、浏览器状态和
输出都放在被 Git 排除的 `private/` 中。API 默认只监听 `127.0.0.1:8000`。
调用 API 前，在 `.env` 设置随机生成、至少 32 字符的 `API_TOKEN`；请求携带
`Authorization: Bearer <token>`。未配置时业务接口拒绝请求，`/health` 仍可用于探活。
生成方法见首次使用指引。协作前请阅读 [安全检查与协作边界](docs/security-review.md)。

## 首次需要准备什么

| 输入 | 本地位置 | 用途 |
| --- | --- | --- |
| 已审核的完整简历 | `private/candidate/resume.pdf` 或 `.docx` | 实际申请附件，配置 `RESUME_FILE` |
| 可核实的经历事实、技能、资格及求职范围 | `private/candidate/profile.json` | 职位筛选和有依据的回答 |
| 姓名、联系方式、已确认的表单答案 | `private/candidate/answer-bank.json` | 直接填表；按地区和有效期限制复用 |
| 公司 ATS 来源与搜索条件 | `private/candidate/source-boards.json` | 明确采集范围；示例默认禁用 |
| 优先级、排除项和审核分流规则 | `private/candidate/search-policy.json` | 可解释排序及人工审核 |
| 更详细的经历资料（可选） | `private/candidate/experiences.json` | 实验性经历检索及长答案支持 |
| 服务凭证（按需） | `.env` | PostgreSQL、Telegram、模型等选用服务 |

完整简历和结构化事实各有用途：不会从上传简历中自动认定所有经历属实，也不会把模型
生成的内容当作事实。表单提出新的资格或授权问题时，需补充相应证据或明确选择。

## 运行方式与边界

- `onboard`、SQLite 初始化及本地检查不需要外部账号。
- 采集使用公开 ATS 接口；队列、定时任务需要 Redis，部署方案见 [运行说明](docs/operations.md)。
- `ALLOW_LIVE_SUBMIT=false` 时，worker 在启动申请浏览器前停止。此开关不禁止你单独启用的 Telegram 通知或其他外部服务。
- 提供 `RESUME_FILE` 时，每个岗位保存文件副本、哈希和有效期。修改源简历不会悄然改变已准备的附件。
- 没有提供完整简历时，保留从事实生成简版 DOCX 的开发功能；它不是完整个人简历的替代品。
- 浏览器依赖通过 `uv sync --frozen --extra browser` 单独安装。Driver、Codex、LCU 后端的前提不同，见运行说明。
- Telegram、模型服务和 ATS 会接收不同范围的个人资料，见 [数据边界](docs/data-privacy.md)。

## 开发与文档

[项目结构](docs/project-structure.md) · [首次使用](docs/onboarding.md) ·
[运行说明](docs/operations.md) · [数据与发布边界](docs/data-privacy.md) ·
[开发及验证](docs/development.md)

```bash
uv sync --frozen --extra test --extra browser
uv run playwright install chromium
uv run pytest
python3 scripts/export_public.py --check
```

公开源文件由 `public-files.txt` 明确列出。发布前生成不含旧 Git 历史的全新本地目录：

```bash
python3 scripts/export_public.py --output ../job-apply-agent-public
```

该命令不会创建远程仓库、提交、推送或上传。已有目录不会被覆盖。原始工作区的历史可能
包含此前的个人配置，不能用一次当前文件检查代替历史清理；共享时使用检查后的导出目录。

保留现有 Apache-2.0 许可证，第三方浏览器和模型运行时不随项目分发。
