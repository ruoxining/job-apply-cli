# 首次使用

[English](../onboarding.md) | 简体中文

从 README 的安装命令开始。`onboard init` 会复制代码内的示例到 `private/candidate/`，
已有文件不会被覆盖。直接编辑本地文件，不需要把简历上传给本项目的作者或某个托管平台。
本项目目前提供 CLI 和 API，没有文件上传网页或 PDF 自动事实提取器。

## 1. 放入完整简历

把自己审核过的 PDF 或 DOCX 复制到 `private/candidate/`。默认配置是：

```dotenv
RESUME_FILE=private/candidate/resume.pdf
```

可以使用其他本地路径。支持 PDF/DOCX，最大 20 MiB。准备申请时复制文件并锁定哈希；
不会修改原稿或自动润色其中的经历。格式检查不等于事实核验或排版审核。

LaTeX 源码、模板工程和个人 resume Git 仓库可以独立放在任意私有位置，只把导出文件路径
交给主项目。主项目不需要克隆或同步该仓库，也不会把它放入安装包或容器镜像。

## 2. 填写事实和求职范围

编辑 `profile.json`，替换所有 `REPLACE_` 标记，并逐项确认：

- 目标国家、职位类型、职位名称、远程偏好及可接受的工作模式。
- 实际工作年限、公司/行业经历年限、已掌握的技能。
- 有工作许可的国家，以及是否需要雇主担保。默认布尔值不是对个人资格的确认。
- `facts` 中每条经历的稳定 `id`、准确 `statement`、证据 `source`，以及适用的日期、技能和已核实数字。

`source` 可写自己的项目记录或本地资料引用。只添加你能确认的内容；没有证据的业绩、
雇佣经历、学历或技能不能由模型补齐。`allowed_rewrites` 是你已审核的等价表述列表。

可选的 `experiences.json` 是详细事实库，用于保存项目、工作、教育、论文、奖项和证书。
初始化生成的文件保持空白，避免把虚构示例误当成你的经历。可查看[可复制样例](../experience-library-template.json)，
但使用前必须替换其中所有虚构内容。

每条经历要能脱离简历其余部分独立看懂：

- 使用稳定的 `id`，并写清名称、你的实际角色，以及公司、团队或项目背景。
- `description` 使用简明要点，说明**你本人**负责什么、做了什么、采用什么技术或研究方法；把个人贡献与团队成果分开。
- 只有能提供依据的结果才写入。指标说明基线、对比值、单位和测量范围；在 `evidence_refs` 或本地笔记中记录证据来源。
- 按需填写日期、技能和证据引用，帮助检索。证据路径只指向本地文件；不要把雇主或研究中的机密材料放入此仓库。
- 未知信息直接省略。没有测量结果时，写清可核实的工作内容，不要估算数字；也不要因为职位描述要求某技能就把它加进经历。

顶层列表包括 `experience`、`projects`、`education`、`publications`、`awards` 和 `certificate`。
加载器会保留每条经历的其他字段作为辅助元数据。这是灵活的 JSON 结构；样例展示推荐字段，不代表严格必填 schema。

## 3. 填写可复用答案

编辑 `answer-bank.json`。至少填写 `first_name`、`last_name`、`email`；建议同时提供
`full_name`、`phone`、`linkedin_url`、`github_url` 等实际需要且已确认的字段。可删除不适用项。
每一项使用 `AnswerBankEntry` 结构：

```json
{
  "semantic_key": "work_authorization_canada",
  "answer": "REPLACE_WITH_YOUR_CONFIRMED_ANSWER",
  "regions": ["Canada"],
  "sensitivity": "sensitive",
  "valid_until": "2030-01-01"
}
```

上面的答案和有效期都必须按本人实际情况修改；不要直接导入。字段键参考
`services/answers.py`、`ats/answer_binding.py` 和 `ats/playwright_adapter.py` 的映射。
选项题仍须与当前页面选项匹配，不会仅凭相似措辞强行选择。

`regions` 为空意味着不限地区；工作许可和担保等应使用明确地区。`valid_until` 为空表示
没有设置到期日，不能因此假定事实永远不变。薪资、当前公司和可开始时间也应及时更新。
不提供密码、验证码、政府证件号或银行卡信息；面试录音、转录等授权应保留其公司和问题范围。

执行 `onboard import-answers` 才会写入配置的数据库。相同键和地区范围会更新；不同范围
分别保存。模板标记、过期项和重复范围会被拒绝，错误输出不打印个人答案。

## 4. 配置采集与筛选

`source-boards.json` 示例默认 `enabled: false`。从真实公司招聘页找到 ATS 类型和 board slug，
填写后启用。不要填招聘聚合站的搜索页。每个来源支持 `max_jobs`、`title_keywords` 和
`location_keywords`；过滤词为空代表不施加该过滤。

`search-policy.json` 设置公司和职位偏好、排除项、评分权重与人工审核阈值。文件属于用户
自己的配置。模型或来源网站的文字不能覆盖这些规则。

## 5. 本地验证并导入

```bash
uv run job-apply-agent onboard check
uv run job-apply-agent init-db
uv run job-apply-agent onboard import-answers
uv run job-apply-agent application-report
```

`check` 检查文件、类型、必要输入、模板残留、答案有效期和简历格式，不连接 ATS，也不
证明每份申请表都已经有答案。`init-db` 面向首次本地开发；已有部署使用 Alembic 迁移。

如需自定义数据目录，可运行 `onboard init --directory /your/private/candidate`，并在 `.env`
中同步 `PROFILE_PATH`、`ANSWER_BANK_PATH`、`SOURCE_BOARDS_PATH`、`SEARCH_POLICY_PATH`、
`EXPERIENCE_LIBRARY_PATH`、`RESUME_FILE`。`check` 和 `import-answers` 始终读取配置中的路径。

## 6. 按需启用服务

API 使用本地实例专用的 Bearer Token。运行下面命令生成随机值，把结果写入被忽略的
`.env` 中的 `API_TOKEN`，不要复制到公开示例或提交记录：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

业务接口要求 `Authorization: Bearer <token>`；未配置或短于 32 字符时返回 503，
凭证缺失或错误时返回 401。`/health` 不需要 token。此 token 提供该实例全部 API 权限，
没有逐人角色或候选人隔离；协作者应使用自己的测试实例和虚构资料。

先保持 `ALLOW_LIVE_SUBMIT=false`。只有需要队列时才启动 Redis；只有需要 Telegram 审核
或通知时才配置 bot token 和唯一允许的 chat ID；模型和浏览器也单独配置。
参见 [运行说明](operations.md) 和 [数据边界](data-privacy.md)。凭证始终放在被忽略的 `.env`
或部署环境中，不编辑公开示例来保存自己的值。
