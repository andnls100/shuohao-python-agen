# 来源

`SKILL.md` 与 `rules/` 原样取自 Black Forest Labs 的
[black-forest-labs/skills](https://github.com/black-forest-labs/skills) 仓库 `skills/flux-image-best-practices`（MIT，见同目录 LICENSE）。
`references/optimize-pass.md` 是本项目新增的：把上游规则收成"出图提示词优化"这一步给模型的任务指令，
由 `zhipu_agent/prompt_optimizer.py` 在角色 / 场景 / 道具的提示词写入 JSON 之前调用。
