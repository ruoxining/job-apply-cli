# 运行说明

[English](en/operations.md) | 简体中文

## 本地开发与 shadow 模式

先完成 [onboarding](onboarding.md)。API 和 SQLite 查询可以单独运行：

```bash
uv run job-apply-agent serve
uv run job-apply-agent application-report
```

API 默认绑定 loopback，业务接口要求配置 `API_TOKEN` 并携带 Bearer Token，`/health` 除外。
这是单实例共享密钥，没有逐人权限、租户隔离、TLS 或限流。不要直接暴露到公网。
启动队列和调度功能时需要 Redis；可选择本地 Compose 基础设施：

```bash
docker compose -f docker-compose.dev.yml up -d
```

开发 Compose 的 PostgreSQL 使用主机端口 5433，Redis 使用 6379，均绑定 loopback。
如使用 PostgreSQL，将 `.env` 的 `DATABASE_URL` 改为开发连接，再执行 Alembic 迁移。
没有特殊需求时可继续使用本地 SQLite 验证配置。

## 可选自托管 Compose

生产 Compose 包含 PostgreSQL、Redis、迁移、API、worker、scheduler 和 Telegram bot。
配置 `APP_ENV=production`、随机 `API_TOKEN`、非默认数据库密码和 Telegram 凭证；候选人目录默认是
`./private/candidate`，以只读方式挂载到 `/candidate`，不复制进镜像。
`RESUME_FILENAME` 设为该目录内的真实附件名。不要将包含私有数据的镜像推送到 registry。

```bash
uv run job-apply-agent onboard check
python3 scripts/validate_deployment.py
docker compose config --quiet
docker compose up -d --build
```

本次开源整理不会执行这些部署命令。已有数据库使用迁移链，升级前先做私有备份；
`init-db` 不能代替已有部署的迁移。备份 PostgreSQL、artifact 卷和候选人输入时保持访问控制。

## 真实申请和浏览器

真实申请需显式设置 `ALLOW_LIVE_SUBMIT=true`，部署检查及 worker、调度入口、最终提交均要求：

```dotenv
LIVE_SUBMIT_ACK=I_UNDERSTAND_REAL_APPLICATIONS_WILL_BE_SENT
```

Compose 会将确认值传入运行时。缺少或不匹配时，在打开申请浏览器前停止。

先用监督的小样本核对字段、附件和回执。默认队列额度与来源配额在 `.env` 中配置，不因
提高额度而跳过审核。`managed-apply <application-id>` 将一条任务交给 worker；
`managed-backlog start|status|stop` 管理明确范围的队列。已 claim 的申请不能作为普通失败重试。

安装浏览器依赖后，Driver 路径使用独立 Chromium profile；Linux Compose 在 Xvfb 与
AT-SPI 环境运行。配置 `BROWSER_CHANNEL=chromium`、`BROWSER_HEADLESS=false`，需要模型规划时
启用 `CUA_ENABLED` 并提供对应服务凭证。

| 后端 | 必要前提 | 当前范围 |
| --- | --- | --- |
| `driver` | browser extra、Chromium、CUA Driver、配置的 GLM 服务 | 标准可选执行路径；先验证运行环境 |
| `codex` | 自行安装并认证的 Codex CLI，以及相应浏览器执行依赖 | 实验路径，不附带私有 CLI 认证或旧镜像 |
| `lcu` | 具有使用权限的本地 LCU/ChatGPT 运行时，配置路径 | 实验适配器；专有运行时不随代码分发 |

后端不可用时停止并报告，不能把 mock 测试通过当作实机可用。`EXPERIENCE_REASONING_ENABLED`
仅用于相应 Codex 路径；`hybrid` / `auto` 检索还需 retrieval extra。模型服务可能接收候选人
事实，启用前阅读 [数据边界](data-privacy.md)。

## Telegram 与状态检查

Telegram 只接受配置的单一 chat ID，但没有额外的用户 ID 授权；使用本人私聊，不要把真实
投递 bot 接入协作群。`/jd <url>` 可加入单条岗位；审核按钮将锁定材料
加入队列，默认 shadow 开关仍生效。启动 bot 会产生与 Telegram 的通信，并可能发送简历。

远程只读状态脚本不再预设任何人的主机或路径：

```bash
python3 scripts/server_status.py --host YOUR_SSH_ALIAS --remote-dir /srv/job-apply-agent
```

该命令仅在你执行时连接所指定的服务器，在 API 容器内读取环境中的 token 发起状态查询，
不会把 token 放入 SSH 参数。`application-report` 可在本地核对新增确认、
历史申请、未知结果和排除项；数据库状态本身不等于外部 ATS 回执。
