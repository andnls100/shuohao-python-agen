# 来源

本技能的写法方法论学习自 [penposs/minimax-h3-video-prompt](https://github.com/penposs/minimax-h3-video-prompt)
（MIT，见同目录 LICENSE），涉及其中的 `SKILL.md`、`references/prompt-framework.md`、`references/official-rules.md`
和 `scripts/validate_h3_prompt.py`。

没有原样复制上游文件：`SKILL.md` 是按本项目的流水线重新整理的版本，主要差异：

- 输入是分镜阶段已经写好的 h3Prompt，任务是「改写成英文」，不是从零创作。
- `<Picture N>`、`<d>` 台词块、`[Shot k]` 标记由程序保护，不允许改动。
- 官方限制（提示词不超过 7000 字符、时长 4—15 整数秒、多模态参考最多 9 张图）取自上游 `official-rules.md`（更新时间 2026-08-01）。

由 `zhipu_agent/video_prompt_strategies.py` 在 ComfyUI（MiniMax H3）视频提示词优化时加载；
机械校验（字符数、时间轴）由 `zhipu_agent/video_prompt_optimizer.py` 完成。
