# 视频生成阶段

本阶段接在 storyboard 之后，不改变 outline、characters、art、script、staging、storyboard 和 refs 的既有流程。

## 1. 先生成提示词，不花视频生成费用

```bash
python run_agent.py run --stages video --eps 1
```

默认读取 `output/storyboard/<书名>-storyboard.json`，按分段写入：

- `output/video/video-manifest.json`：每段的提示词、时长、任务 ID、状态、视频 URL 和本地路径。
- `output/video/prompts/<分段ID>.txt`：逐段提示词。
- `output/video/qc-checklist.md`：人工质检清单。
- `output/video/videos/<分段ID>.mp4`：API 返回可下载地址且下载成功时保存的视频文件。

默认协议为 `h3`，直接使用分镜中的 `h3Prompt`。若要从结构化分镜字段拼接 Seedance 风格提示词：

```bash
python run_agent.py run --stages video --eps 1 --video-protocol seedance
```

## 2. 配置视频 API

在项目根目录的 `.env` 中填写：

```dotenv
VIDEO_API_SUBMIT_URL=https://your-provider.example/v1/videos/generations
VIDEO_API_STATUS_URL_TEMPLATE=https://your-provider.example/v1/videos/{task_id}
VIDEO_API_KEY=your_api_key
VIDEO_API_MODEL=your-video-model
VIDEO_API_TIMEOUT=120
VIDEO_API_POLL_INTERVAL=5
VIDEO_API_MAX_WAIT=900
```

此适配器使用通用 REST 约定：提交时 POST JSON `model`、`prompt`、`duration`、`aspect_ratio`，以 Bearer Token 鉴权；响应应直接提供 `video_url` / `output_url`，或提供 `task_id` 并允许通过状态 URL 查询。若平台字段、鉴权或请求路径不同，需要按平台文档修改 `zhipu_agent/stages/video.py`；不要假定任意 OpenAI 兼容接口都支持视频生成。

## 3. 明确提交云端任务

检查提示词、模型、价格和平台限制后，才运行：

```bash
python run_agent.py run --stages video --eps 1 --video-submit
```

未指定 `--video-submit` 时绝不调用视频 API。提交模式会逐段保存任务状态；已成功生成的分段会跳过，已保存任务 ID 的分段在配置状态 URL 后可续查。生成失败会记录到 manifest，不会悄悄当作成功。

## 4. 当前边界

- 这是通用 API 适配器，不是对 Seedance、MiniMax 或其他特定平台的官方 SDK。请以实际供应商 API 文档为准。
- `seedance` 提示词会尽量复用结构化分镜字段，并附带现有 H3 提示词作为台词参考；正式投产前应人工检查。
- 视频下载失败时仍保留云端 URL，并在 manifest 写入 `download_error`。
- 本阶段生成的视频片段尚未自动拼接成整集。人工审核通过后，可再接入 FFmpeg 剪辑阶段。
