# 开发与验证

[English](en/development.md) | 简体中文

```bash
uv sync --frozen --extra test --extra browser
uv run playwright install chromium
uv run pytest
```

浏览器 fixture 测试需要 browser extra 与 Playwright Chromium。没有这些依赖时，应明确
区分跳过、环境失败和逻辑失败。测试使用虚构身份与本地 fixture；不要把真实申请数据
复制到测试目录。

新增功能先检查现有模块，保留提交 claim、未知结果不重投、答案地区/有效期范围和事实
来源约束。更改迁移时保留已有版本链。个人化模型提示或实验数据留在私有文件中。

公开范围由 `public-files.txt` 管理。增加文件后更新清单，再运行：

```bash
python3 scripts/export_public.py --check
python3 scripts/export_public.py --output ../job-apply-agent-public
```

导出目录不得预先存在。检查脚本不执行 Git push，也不审计原始仓库的全部历史。
构建分发包时检查归档内容，确认只包含清单内源码和正常生成的包元数据。

保留的实验辅助包括合成浏览器 fixture 和接收显式输入的经历回放器。它们不是生产部署
前提；固定某台私人主机、历史输入路径或旧镜像的脚本已从公开入口移除。

尚未完成的产品工作：未知问题的完整用户交互、长期无人值守可靠性、队列故障恢复、
更完整的来源健康统计、邮箱接入、通用 HTML 解析和独立的匹配质量评估。不要用本地
单元测试替代这些尚未完成的集成验证。
