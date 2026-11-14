# 项目结构与流程

[English](../project-structure.md) | 简体中文

```text
src/job_apply_agent/
  examples/       不含真实身份的初始化模板
  onboarding.py   本地模板复制、资料检查、答案导入
  collectors/     公开 ATS 与单链接采集、规范化
  domain/         数据 schema、状态与敏感性枚举
  resume/         通用事实约束 DOCX composer
  services/       筛选、配额、简历快照、答案、审核、结果报告
  ats/            Playwright 与可选模型/Computer Use 后端
  experience/     经历存储、版本、可选检索和推理
  telegram/       人工审核、经历编辑、结果通知
  tasks.py        持久状态与队列任务
  scheduler.py    定时采集入口
  api.py          本地运维和手动调度 API
  models.py       SQLAlchemy 模型
alembic/          数据库迁移，保留已有版本链
scripts/          来源检查、部署检查、状态查询、导出与实验辅助
private/          用户输入、简历、数据库、浏览器状态、输出；不分发
```

采集流程：来源配置 → ATS collector → 规范化与去重 → 资格和评分 → 准备材料 → 自动队列或
Telegram 审核。真实执行前检查状态、锁定简历和提交开关；最终提交前持久化 claim，只有
确认成功后才保存完成时间与回执。点击结果不明时保留 claim，先核对外部结果。

`job_postings` 保存岗位、当前材料、答案、状态和回执；`answer_bank` 保存已确认答案；
`experiences` 与 `experience_revisions` 保存可选经历库及修订。它们都是私有运行数据。
旧迁移曾使用其他表，不应因当前模型简化就删除历史迁移。

`RESUME_FILE` 是已经由本人审核的附件。服务只检查格式、复制、绑定哈希和有效期，不宣称
对预写 PDF 做过逐句事实验证。未配置此文件时，开发用 composer 从 ProfileFact 生成简版
DOCX，可在安装 LibreOffice 的环境转换为 PDF；哈希对应实际优先使用的文件。

Driver、Codex 和 LCU 是不同执行后端，不因接口相似就互相删去。测试 fixture 和调用边界
保留；依赖个人历史输入、固定主机或无法重建的旧镜像的启动脚本不属于公开入口。
