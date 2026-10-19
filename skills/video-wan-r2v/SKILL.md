---
name: video-wan-r2v
version: 1.0
---
# Wan R2V 视频提示词技能

适用于阿里云 Wan R2V。

规则：
- 参考图负责角色、场景和道具的一致性；prompt 重点描述动作、镜头、时序和声音。
- 明确当前镜头的主体、动作、镜头运动和环境变化。
- 保持角色外观、服装、场景和空间关系连续。
- 避免字幕、画面文字、水印和无关人物。
- 中间层负责统一空白和换行，API 适配器负责 reference media 与请求结构。

## 出图提示词优化（Wan 写法）

`references/optimize-pass.md` 供 `run --stages optimize --optimize-mode wan` 使用：把设定集里的角色 / 场景 / 道具出图提示词按 Wan 习惯重写，结果写到单独的文件，不改 cast.json / art.json。写法依据通义万相公开的提示词公式（主体 + 场景 + 风格 + 镜头 + 光影），不是上游仓库的原文。
