# shuohao-python-agen

面向 AI 短剧制作的 Python Agent 流水线：小说 → 大纲 → 角色 → 美术 → 剧本 → 场面调度 → 分镜 → 可选视频/音频。

## 视频生成：现在的逻辑

视频阶段以**单个分镜头（cut）**为生成单位。静态 `output/frames/` 分镜图不再作为视频连续性的依据。

### 第一镜 / 新场景

- **固定场景**：场景图 + 角色参考图 + 道具参考图 + 当前镜头提示词
- **非固定场景**：角色参考图 + 道具参考图 + 当前镜头提示词

### 后续连续镜头

只有同时满足以下条件，才认定为“上一个连续镜头”：

1. 同一集
2. 同一 `sceneIndex`
3. 当前 cut 的 `continuity.mode == "inherit"`
4. `continuity.referenceFrame` 明确指向紧接着的上一镜
5. 跨 segment 时，当前 segment 的 `continuityMode == "continuous"`

满足后：

`上一段视频真实最后一帧 + 角色参考图 + 道具参考图 + 当前镜头提示词`

`hard_cut`、`cut`、换场、缺少/错误 `referenceFrame` 都不会继承上一镜最后一帧。

**同一个场景号不等于连续镜头。** 程序以 storyboard 的 continuity 字段为准。

视频下载成功后，程序自动用 FFmpeg 提取真实最后一帧：

```text
output/video/last-frames/E01-01-C01.png
```

这个文件才是下一连续镜头的首关键帧输入。

## 最重要的命令

### 1. 查看单个分镜头计划，不调用视频 API

```bash
python run_agent.py run --stages video --eps 1 --shots E01-01-C01
```

### 2. 提交第一个分镜头

```bash
python run_agent.py run --stages video --eps 1 --shots E01-01-C01 --video-submit
```

### 3. 提交下一个连续分镜头

```bash
python run_agent.py run --stages video --eps 1 --shots E01-01-C02 --video-submit
```

如果 C02 是连续镜头，程序会先确认 C01 已成功，并确认 C01 的真实最后一帧存在；否则拒绝提交 C02。

### 4. 整集按顺序生成

```bash
python run_agent.py run --stages video --eps 1 --video-submit
```

上一镜完成并提取最后一帧后，才提交下一镜。

### 5. 指定多个分镜头

```bash
python run_agent.py run --stages video --eps 1 --shots E01-01-C01,E01-01-C02 --video-submit
```

### 6. Seedance 风格提示词预览

```bash
python run_agent.py run --stages video --eps 1 --video-protocol seedance
```

默认协议是 `h3`，直接使用 storyboard 的 `h3Prompt`。

旧的 `--video-protocol i2v`、`--frames-dir`、`--video-flf` 已删除。

## Manifest

`output/video/video-manifest.json` 会记录：

- `input_mode`：`scene_or_assets` / `previous_last_frame`
- `continuity_decision`：连续性判断结果
- `previous_shot_id`：连续镜头依赖的上一镜
- `scene_fixed`：是否有可用场景图
- `initial_reference`：场景图 / 上一镜最后一帧 / 无
- `last_frame_path`：当前视频真实最后一帧

## 视频模型配置

在根目录 `models.json` 的 `video` 数组配置视频模型。新逻辑要求模型支持参考图/图生视频输入；当前最完整的是 Wan R2V 适配。

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

实际 URL、模型名、字段和鉴权方式必须按供应商官方接口填写。

FFmpeg 必须安装，并确保 `ffmpeg` 在 PATH 中。

## 其他阶段常用命令

```bash
python run_agent.py run --stages outline --book input/novel.txt --episodes 6 --minutes 2 --genre urban
python run_agent.py run --stages characters,art
python run_agent.py run --stages script,staging,storyboard --eps 1-2
python run_agent.py run --stages refs --refs-names Alice --refs-gen
python run_agent.py run --stages frames --frames-gen --frames-only E01-01-C01
python run_agent.py run --stages audio --eps 1 --audio-submit --yes
```

完整视频说明见 [docs/video-generation.md](docs/video-generation.md)。
