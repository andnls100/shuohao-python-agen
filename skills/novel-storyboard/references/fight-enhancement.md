# 打戏增强模式

Python Agent 的 --fight-enhancement 参数只影响 storyboard 阶段；不打戏的普通场景仍按现有规则切镜。

- off：关闭打戏专用提示词，不改变原有分镜生成逻辑。
- auto：默认。模型识别真实的肢体/兵器交锋，并判断其剧情重要程度；只强化决定胜负、揭示实力、改变剧情走向或构成高潮的打戏。短暂推搡、普通冲突以及重点在对白/情绪的场景不强化。
- enhanced：强化所有真实打戏，但不为非打戏场景凭空添加战斗。

## 增强打戏必须规划的内容

1. 动作因果：攻击/发起动作 → 防守、闪避或反制 → 接触/落空结果 → 重心与位置变化 → 下一动作承接。
2. 空间连续性：角色相对左右、距离、朝向、武器状态、站立/失衡状态应在相邻镜头间保持合理。
3. 可执行性：每个动作必须能在对应镜头时长内完成。动作序列按镜头切分，不能在一个短镜头里塞入过多复杂招式。
4. 剧情约束：遵守原剧本的胜负、伤势、人物能力和结局，不擅自增加关键人物、武器、致命攻击或剧情转折。
5. 视频提示词：动作计划必须落实到 frame、shot、sfx、blocking 和 h3Prompt，不能只生成一个计划字段而不改变镜头。

增强后的 segment 可以带 fightPlan 字段。每项动作包含 actionId、attacker、defender、attack、response、result、positionsBefore、positionsAfter、seconds、cutIndexes。cutIndexes 从 1 开始，对应该段 cuts 数组中的镜头序号。该字段是辅助规划资料，不取代现有 storyboard 校验；秒数、节拍认领、H3 对齐等仍以原有字段和校验器为准。

## 使用

```bash
# 默认：只强化重要打戏
python run_agent.py run --stages storyboard --fight-enhancement auto --eps 1

# 关闭打戏增强
python run_agent.py run --stages storyboard --fight-enhancement off --eps 1

# 强化所有打戏
python run_agent.py run --stages storyboard --fight-enhancement enhanced --eps 1

# 已有分镜需要按新模式重新生成时，显式使用 --force
python run_agent.py run --stages storyboard --fight-enhancement enhanced --eps 1 --force
```

注意：auto 的重要程度判断由文本模型完成，是生成时的判断而非确定性分类器。最终动作质量仍需检查生成的分镜和视频结果。
