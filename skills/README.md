# 自定义技能目录

放在这里的 `.yaml` / `.py` 文件会被自动加载成工具，助手（无论 LLM 模式还是离线规则模式）
都能调用它们。改完在网页控制台点「重新加载技能」，或者重启即可生效。

- 完整格式说明：`python -m voice_agent skills new demo` 会生成一份带注释的模板
- 查看加载状态：`python -m voice_agent skills check`
- 用户级技能目录（对所有项目生效）：`~/.voice-agent/skills`

本目录里的示例：

| 文件 | 演示了什么 |
| --- | --- |
| `greet.yaml` | 最简单的 `say` 技能，带参数占位符 |
| `daily_brief.yaml` | `sequence` 组合多个已有工具 |
| `network_info.py` | Python 技能：主机名与局域网 IP |
