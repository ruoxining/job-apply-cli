# 安全检查与协作边界

[English](../security-review.md) | 简体中文

检查日期：2026-11-13。基线为 `d0953c0`，下列修复与本报告一同提交。
范围是准备给协作者使用的源码仓库、可达 Git 历史、Python 锁定依赖、本地 API、
投递入口和部分外部输入处理。没有连接生产服务器、投递真实申请或邀请协作者。

可以用这份仓库开展隔离的代码开发。真实候选人资料、浏览器会话、模型账号和投递 bot
应继续由所有者单独管理；当前程序不是多用户服务。下面仍有未解决风险，不能把本次
检查理解为完整渗透测试或无人值守生产认证。

## 本次已修复

| 问题 | 原有影响 | 修复与证据 |
| --- | --- | --- |
| API 无鉴权 | 能访问监听端口的进程可读取岗位、申请回执或触发采集；loopback 只限制网络暴露 | [API](../../src/job_apply_agent/api.py) 的业务路由统一要求 Bearer Token；配置缺失时关闭访问；[测试](../../tests/test_api.py) 验证未授权请求无法访问数据库或调度器 |
| 真实投递确认只在预检脚本执行 | 直接启动 worker 或调用适配器时，单独设置 `ALLOW_LIVE_SUBMIT=true` 就能越过二次确认 | [设置](../../src/job_apply_agent/config.py)、worker、managed 入口和最终提交同时检查 `LIVE_SUBMIT_ACK`；Compose 传入确认值；[测试](../../tests/test_submission_safety.py) 验证在访问浏览器和提交 claim 前停止 |
| 网页大小限制执行过晚 | `client.get()` 先读完整响应，2 MB 检查无法限制完整大响应的缓冲 | [采集器](../../src/job_apply_agent/collectors/job_link.py) 改为流式读取并提早中止，显式禁用跳转；[测试](../../tests/test_job_link.py) 验证大流不会被完整读取、注入客户端的跳转配置不能覆盖规则 |
| SSH 状态脚本未引用远程路径参数 | 路径中的空格或 shell 字符会被远程 shell 解释 | [脚本](../../scripts/server_status.py) 使用 shell 参数引用并拒绝选项形态的主机名；读取 API 容器内的 token，不将其写入 SSH 参数；[测试](../../tests/test_server_status_script.py) 覆盖特殊字符路径 |
| 已知依赖漏洞 | TLS 信任库、遥测解析和测试临时目录分别存在已公布问题 | 锁定版本升级为 `certifi 2026.10.23`、`opentelemetry-api 1.45.2`、`pytest 9.2.0`；为 pytest 兼容性升级 `pytest-asyncio 1.5.0` |

依赖依据：[certifi 公告](https://github.com/advisories/GHSA-6q48-6c7w-9gw2)、
[OpenTelemetry 公告](https://github.com/advisories/GHSA-mf5v-w3w3-f6cg)、
[pytest 修复记录](https://docs.pytest.org/en/9.0.x/changelog.html)。这些是依赖版本命中，
不表示已在本项目中复现所有利用路径。

API 现在要求在各自的 `.env` 中设置随机 `API_TOKEN`，具体步骤见 [首次使用](onboarding.md)。
它是单实例共享密钥，仍没有逐人权限或数据隔离。依赖更新只有重新同步环境或重建镜像
后才会应用；本次没有更新任何远程部署。

## 仍需处理

| 优先级与条件 | 发现及证据 | 建议的下一步 |
| --- | --- | --- |
| 高：读取不可信通用职位 URL | [URL 检查与请求](../../src/job_apply_agent/collectors/job_link.py) 分别解析 DNS。受控测试第一次返回公网地址，第二次返回自建 loopback 服务；请求成功到达本机服务。已有私网检查和禁止跳转无法防止这种重绑定 | 将连接固定到已经验证的地址，并保持正确的 Host、TLS SNI 和证书验证；配合网络出口隔离。在完成前不要让通用链接读取器接触包含敏感内网服务的环境 |
| 高：模型子进程与真实凭证共用环境 | [Codex planner](../../src/job_apply_agent/ats/codex_planner.py) 继承进程环境，并在子进程结束后才拒绝 unexpected tools。只读配置与事后检查没有证明文件读取、环境变量读取或数据外传已被隔离 | 在独立用户或容器中运行，最小化环境变量和挂载；增加执行前的工具禁用与对抗测试。此项是代码边界审查，未运行真实模型利用实验 |
| 高：向协作者开放写权限 | 检查时 `main` 未保护、rulesets 为空、仓库没有 CI 工作流；PR 流程尚不能强制执行 | 在当前套餐支持时启用主分支保护、所有者审核和必需检查；先补 CI，再将其设为合并条件。未在本次检查中变更 GitHub 管理设置或邀请任何人 |
| 中：Telegram 被配置为群聊 | [回调与命令](../../src/job_apply_agent/telegram/review.py) 以及 [经历编辑](../../src/job_apply_agent/telegram/experiences.py) 仅检查 chat ID，不检查发起人的 user ID；群内其他成员会共享操作权限 | 当前只使用所有者私聊；若要协作群，需要明确用户允许列表和逐人授权测试 |

DNS 防护应同时考虑应用和网络出口，参见 [OWASP SSRF 指南](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)。
个人账号下私有仓库的 collaborator 权限是写权限，不能把邀请理解成只读分享；参见
[GitHub 权限说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/repository-access-and-collaboration/permission-levels-for-a-personal-account-repository)。
私有仓库的分支保护还受套餐限制，参见 [GitHub 分支保护说明](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches)。

此外，API 没有 TLS、限流或多租户隔离；浏览器可能加载跨域资源，当前未完成网络出口
审计。流式 HTML 检查也不等于对压缩炸弹、慢速响应和所有 ATS JSON 响应设置了完整资源
配额。继续保持 API 绑定 loopback，不共用真实运行实例。

## 协作方式

1. 只克隆这份干净源码仓库。不要传递原始工作区、旧 Git 历史、`private/`、`.env`、数据库、截图或浏览器 profile。
2. 每位协作者创建独立测试目录、虚构候选人资料和自己的 API token，保持 `ALLOW_LIVE_SUBMIT=false`、`CUA_ENABLED=false`，不配置生产 bot 或模型凭证。
3. 通过功能分支和 PR 交付。涉及投递开关、claim、事实与答案来源、上传文件、网络访问和权限的改动由所有者审核。
4. 不把生产凭证放进 CI，也不在带真实资料的环境执行未审核的分支。暂不把真实投递操作委托给共享实例。

## 验证记录与限制

- 对基线两个可达提交的 306 个文件版本执行导出白名单和隐私扫描，使用 11 个本地私有标记，未命中规则。扫描不证明不存在所有间接身份线索；GitHub 账号和提交署名仍是仓库元数据。
- GitHub 只读检查确认仓库为 private，repository Actions secrets 数量为 0，默认 workflow token 为 read，不能批准 PR。未检查组织级或环境级凭证、外部系统和原始私有仓库。
- `uv run pytest --tb=short --disable-warnings`：359 项通过，275 条弃用等警告；覆盖应用回归与新增安全边界。
- `uv export --frozen --no-dev --all-extras --no-emit-project --no-hashes` 后使用 `pip-audit --no-deps --disable-pip` 检查当前平台的 80 个锁定包，升级后无已知公告命中、无跳过。基线三个不同公告中有重复数据库记录，未重复计数。
- DNS 重绑定复现只访问自建的本地 HTTP fixture，没有访问外部或生产内网资源。
- 未进行完整生产部署、容器操作系统漏洞扫描、私有 CLI/LCU 运行时审计或真实模型对抗测试。
