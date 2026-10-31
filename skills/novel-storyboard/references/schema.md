# storyboard.json 结构

三层：**集 → 段（segment）→ 分镜（cut）**。

- **段** = 一次视频生成调用，总时长 ≤ `params.maxSegmentSeconds`（未设置时采用脚本默认值），不跨场次——换景必开新段
- **分镜** = 段内的一次剪切，`params.minCutSeconds`–`params.maxCutSeconds`（未设置时采用脚本默认值），各自认领剧本节拍、带景别运镜和一张分镜图
- **分镜图** = 每个分镜一张关键帧：第 1 切的是**主分镜图**（钉在 0.00 秒），其余是**子分镜图**（各钉在自己的切点时刻）。**每段一个文件夹**：`<段号>/f<切序>.png` + `prompt.md`（export 生成，内容就是 h3Prompt）

```json
{
  "source": "渡口",
  "aspectRatio": "16:9",
  "promptLang": "zh",
  "params": { "maxSegmentSeconds": 30, "minCutSeconds": 4, "maxCutSeconds": 10, "maxOnScreen": 6, "tolerance": 1.0 },
  "episodes": [ { "ep": 1, "segments": [ ... ] } ]
}
```

`aspectRatio` 是分镜与下游出图共同遵守的正式画幅合同，只接受 `"16:9"` 或 `"9:16"`；可省略，默认 `"16:9"`。报告的主分镜图与子分镜条按它排版，下游生成的每张分镜图也必须使用同一画幅。旧数据里的 `_meta.orientation` 和 `frame` 末尾画幅标签只作兼容，不要在新数据里继续写。

`promptLang` 可省略（**默认 `en`——官方规范口径**）：整条英文，台词在 `<d>[Chinese]` 里保留原文。设成 `zh` 可切整条中文（对齐指令、字段名、镜头标记都有中文版）——偏离官方推荐的备选项。**禁角色名不跟着语言变**：两种模式的 H3 正文都用通用身份。

**Seedance 视频提示词不存在 JSON 里**：它由程序从 `shot`、构图量化字段、`blocking`、`soundscape`、`music` 和剧本台词现拼（结构见 `references/seedance-prompt.md`），报告和 `export --protocol seedance` 都用同一个拼法。

## segment（段）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 段号 `E01-01`：集号 + 两位序号，**按顺序连号**。它就是素材文件名（`E01-01.mp4` / `E01-01-f1.png`） |
| `sceneIndex` | int | 这一段在剧本该集的第几场（1 起）。段内全部分镜同场 |
| `cuts` | cut[] | 段内分镜，按时间顺序。段总秒数 = 分镜秒数之和，**不单独存**——少一处会漂的冗余 |
| `h3Prompt` | string | **兼容字段名；内容是一段一条最终 Ref2V 视频提示词**，直接遵循所选模型 Skill（H3 Ref2VA 或 Wan 3.0 Ref2V），不使用旧 I2VA/T2V 中间结构 |
| `blocking` | string | **人物关系与构图逻辑**：开场那一瞬谁在画面左、谁在右、面朝哪、相距多远（厘米或步数）。一段一份，各镜从它出发。纯参考图出片时这是唯一说清「人在哪」的地方。**必填**（`composition` 门） |
| `soundscape` | string | **Seedance 用的音景**（中文）：环境声、动作声、非语言人声，不复述台词。程序套上 `<>`，字段里别自己写 |
| `music` | string | **Seedance 用的配乐**（中文），写配器与速度。没有就省略这个字段。程序套上 `（）` |
| `note` | string | 备注，可选 |

## 连续性字段（建议所有新分镜必填）

segment 可带 `continuityMode`：`continuous` 表示承接前段状态，`hard_cut` 表示剧情要求重置。cut 可带：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `continuity.mode` | enum | `inherit`（承接上一切/上一段末帧）或 `cut`（有意硬切） |
| `continuity.stateBefore` | string | 当前镜开始时角色位置、朝向、姿势、服装、道具、伤势与关键物件位置 |
| `continuity.stateAfter` | string | 当前镜结束时的可见状态，下一连续镜必须继承 |
| `continuity.referenceFrame` | string | 上一帧文件标识；同段通常为上一切帧，跨段连续时为前段末帧；硬切可为空 |

这组字段不替代 `frame`/设定图：它是显式的状态转移合同，避免模型每切重新发明人物站位、手持物和动作结果。当前镜 `frame` 管静态构图，`shot` 管动作过程与运镜。旧 storyboard 缺少这些字段仍可读取；新生成应提供。

## cut（分镜）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `beats` | [int, int] | 认领该场第几拍到第几拍（含两端）。**每个节拍必须被恰好一个分镜认领**，按顺序、连续 |
| `seconds` | number | 分镜时长必须在 `params.minCutSeconds`–`params.maxCutSeconds` 内。认领节拍的台词秒数必须装得下 |
| `size` | enum | 景别：`extreme-wide` 大远景 / `wide` 全景 / `medium` 中景 / `close` 特写 / `extreme-close` 大特写 |
| `camera` | enum | 运镜，**直接用 H3 官方词表**（原样字符串）：`Static Shot` `Push In` `Pull Out` `Zoom In/Out` `Pan Left/Right` `Truck Left/Right` `Tilt Up/Down` `Pedestal Up/Down` `Arc Shot` `Tracking Shot` `Shake Slightly/Strongly` `POV` `Roll Clockwise/Counterclockwise` |
| `characters` | string[] | 画内人物（C 编号），必须 ⊆ 剧本该场人物；空镜给空数组。> `maxOnScreen` 时必须带 `note` |
| `props` | string[] | 画内道具（P 编号），必须 ⊆ 剧本该场道具。可省略 |
| `frame` | string | **分镜图提示词**（中文）：这一格的主体、位置状态、动作瞬间、光线与氛围。景别中文词必须在里面；**直呼角色名**（它指的是挂上去的设定图） |
| `shot` | string | **镜头正文**（中文）：这几秒发生什么——运镜或切换方式 → 主体动作与表情 → 位置或空间变化，写法见 `shot-writing.md`。**用通用身份，不写角色名**；不写秒数、镜头编号、图片引用、台词和 `{}` `<>` `（）`——这些由程序加。Seedance 提示词的「画面：」行就是它 |
| `lens` | string | 焦距 + 景深：`50mm 标准，中浅景深` / `85mm 长焦，极浅景深`。这六个构图字段**每镜必填**（`composition` 门） |
| `cameraPosition` | string | 机位：对着谁 + 什么角度，`李四 + 平视正面` / `双人 + 俯视 30°` |
| `composition` | string | 构图法：`三分法` / `中心构图` / `对角线` / `对称` |
| `eyeline` | string | 视线落点：`对方面部` / `手中衬衫` / `闭眼` |
| `focus` | string | 焦点锁定什么：`锁定李四上半身` |
| `stability` | enum | `stable` 稳定 / `slight-shake` 微晃 / `handheld` 手持 |
| `sfx` | string | 这一镜自己的音效，段级音景之外，可选 |
| `lighting` | string | 这一镜特有的光影，场景级之外，可选 |
| `recipe` | string | 镜头配方卡 id，可选。挂了 `--shots <卡片目录>` 才查（`shot-recipe` 门）。**cut 级不是 segment 级**——一段可以跨多种配方；**多格配方靠连续同 id 的分镜表达**，不是数组 |
| `note` | string | 备注，可选 |

`recipe` 是**可选挂载**：不给 `--shots` 就整门跳过。给了就查三条——id 在卡库里、卡片的每条 `must_phrases` 出现在该切的 `frame` 里（两边小写化后 `includes`）、卡片 `cuts` 下限 ≥ 2 时连续同 id 的分镜数不得低于该下限。卡片的**建议景别与运镜不设门**，只在报告里提示偏离：配方是语汇不是法条。

## 最终 Ref2V 视频提示词

`h3Prompt` 是为兼容既有 JSON schema 保留的字段名，内容必须直接是所选模型可提交的最终提示词。模型选择由顶层 `videoModel` 指定；H3 使用 Ref2VA Skill，Wan 使用 Ref2V Skill。不要生成固定 I2VA 对齐行、`[Shot k]` 模板或其他中间格式；参考图标记与声音格式由模型对应的 Skill 决定。

## 时长约束链

台词秒数（按剧本语速折算）≤ 分镜 `seconds`；分镜时长由 `params.minCutSeconds` / `params.maxCutSeconds` 限制；段 Σ分镜 ≤ `params.maxSegmentSeconds`；集 Σ段 落在剧本 `targetSeconds` ±100%（默认；`params.tolerance` 可覆盖）。全部由 validate 逐级对账。
