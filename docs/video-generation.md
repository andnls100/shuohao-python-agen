# 视频生成阶段

视频阶段现在按**单个分镜头（cut）顺序生成**，不再把静态 `frames/` 分镜图当作视频首帧，也不再使用旧的 `--video-protocol i2v` / `--video-flf` 逻辑。

## 1. 核心规则

每个分镜头先判断它和上一个分镜头的关系：

1. **第一镜 / 新场景 / 明确切断**
   - 固定场景：`场景图 + 角色参考图 + 道具参考图 + 当前镜头提示词`
   - 非固定场景：`角色参考图 + 道具参考图 + 当前镜头提示词`
2. **连续镜头**
   - 必须同时满足：同一集、同一 `sceneIndex`、当前 cut 的 `continuity.mode == "inherit"`、`referenceFrame` 明确指向上一个镜头、跨 segment 时 `continuityMode == "continuous"`。
   - 输入：`上一段视频真实最后一帧 + 角色参考图 + 道具参考图 + 当前镜头提示词`
3. `hard_cut`、`cut`、换场、缺少/错误 `referenceFrame` 都不会继承上一段视频最后一帧。

**同一场景并不自动等于连续。** 连续性以 storyboard 明确写入的 continuity 字段为准。

## 2. 最重要的命令

### 先只生成一个镜头的计划，不调用视频 API

```bash
python run_agent.py run --stages video --eps 1 --shots E01-01-C01
```

### 提交第一个镜头

```bash
python run_agent.py run --stages video --eps 1 --shots E01-01-C01 --video-submit
```

### 提交下一个连续镜头

```bash
python run_agent.py run --stages video --eps 1 --shots E01-01-C02 --video-submit
```

如果 `E01-01-C02` 被 storyboard 标记为连续镜头，程序会先检查 `E01-01-C01` 是否已经成功，并读取 `E01-01-C01` 生成视频的**真实最后一帧**。没有最后一帧就拒绝提交 C02，避免错误地退回文字生视频。

### 按整集顺序生成

```bash
python run_agent.py run --stages video --eps 1 --video-submit
```

程序严格按 storyboard 顺序逐镜生成。上一镜完成并提取最后一帧后，才允许下一连续镜头提交。

### 只生成多个指定镜头

```bash
python run_agent.py run --stages video --eps 1 --shots E01-01-C01,E01-01-C02 --video-submit
```

如果指定的后一个镜头依赖前一个连续镜头，而前一个镜头没有可用的成功结果，程序会直接停止并说明依赖关系。

### 只看 Seedance 风格提示词

```bash
python run_agent.py run --stages video --eps 1 --video-protocol seedance
```

默认 `h3` 直接使用 storyboard 的 `h3Prompt`。

## 3. 视频输入判断写入 manifest

`output/video/video-manifest.json` 的每个镜头包含：

- `input_mode`: `scene_or_assets` 或 `previous_last_frame`
- `continuity_decision`: 程序实际采用的判断原因
- `previous_shot_id`: 连续镜头依赖的上一镜 ID
- `scene_fixed`: 是否找到可用场景设定图
- `initial_reference`: `scene_reference`、`previous_last_frame` 或 `none`
- `last_frame_path`: 当前视频生成后由 FFmpeg 提取的真实最后一帧

程序不会仅凭 `sceneIndex` 判断连续性。

## 4. 最后一帧

视频下载成功后，程序自动用 FFmpeg 提取最后一帧到：

```text
output/video/last-frames/E01-01-C01.png
```

这个文件是下一连续镜头的首关键帧输入，而不是 `output/frames/` 中的静态分镜图。

需要安装 FFmpeg，并确保 `ffmpeg` 在 PATH 中。

## 5. 视频模型配置

视频模型配置仍放在根目录 `models.json` 的 `video` 数组中。要实现上述参考图链路，所选模型必须支持参考图/图生视频输入。当前最完整的适配是 Wan R2V：程序会把“上一镜最后一帧”放在参考媒体列表的第一位，并继续传入角色、场景、道具参考图。

示意：

```json
{
  "video": [
    {
      "key": "wan-r2v",
      "name": "Wan R2V",
      "adapter": "aliyun_wan",
      "mode": "r2v",
      "submit_url": "https://your-provider.example/v1/videos/generations",
      "status_url_template": "https://your-provider.example/v1/tasks/{task_id}",
      "api_key_env": "VIDEO_API_KEY",
      "model": "your-r2v-model"
    }
  ]
}
```

实际 URL、模型名和字段必须按供应商官方接口填写。

## 6. 产物

```text
output/video/
├── video-manifest.json
├── prompts/
├── videos/
├── last-frames/
└── qc-checklist.md
```

`video-manifest.json` 是续跑的状态来源。重新运行同一命令不会重复提交已经成功且本地视频仍存在的镜头。
