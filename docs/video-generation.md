# 视频生成阶段

本阶段接在 storyboard 之后，不改变 outline、characters、art、script、staging、storyboard 和 refs 的既有流程。

## 1. 先生成提示词，不花视频生成费用

```bash
python run_agent.py run --stages video --eps 1
```

默认读取 `output/storyboard/<书名>-storyboard.json`，按分段写入：

- `output/video/video-manifest.json`：每段的提示词、时长、任务 ID、状态、使用的平台、视频 URL 和本地路径。
- `output/video/prompts/<分段ID>.txt`：逐段提示词。
- `output/video/qc-checklist.md`：人工质检清单。
- `output/video/videos/<分段ID>.mp4`：API 返回可下载地址且下载成功时保存的视频文件。

默认协议为 `h3`，直接使用分镜中的 `h3Prompt`。若要从结构化分镜字段拼接 Seedance 风格提示词：

```bash
python run_agent.py run --stages video --eps 1 --video-protocol seedance
```

## 2. 在 models.json 配置视频 API 号池

模型配置统一放在项目根目录的 `models.json`。首次配置时复制 `models.example.json` 为 `models.json`，保留需要的 `text` / `vision` 列表，并填写 `video` 列表：

```json
{
  "video": [
    {
      "name": "视频平台主用",
      "submit_url": "https://provider-a.example/v1/videos/generations",
      "status_url_template": "https://provider-a.example/v1/videos/{task_id}",
      "api_key_env": "VIDEO_PRIMARY_API_KEY",
      "model": "video-model-a",
      "timeout": 120,
      "poll_interval": 5,
      "max_wait": 900
    },
    {
      "name": "视频平台备用",
      "submit_url": "https://provider-b.example/v1/videos/generations",
      "status_url_template": "https://provider-b.example/v1/videos/{task_id}",
      "api_key_env": "VIDEO_BACKUP_API_KEY",
      "model": "video-model-b"
    }
  ]
}
```

- `video` 是视频生成 API 号池，按数组顺序尝试；提交请求失败时自动切换下一个备用平台。
- 每项必填 `name`、`submit_url`、`model`，以及 `api_key` 或 `api_key_env`。建议用 `api_key_env` 引用系统环境变量，避免把真实密钥写进配置文件。
- `status_url_template` 可选；如果平台返回 `task_id` 而不是视频 URL，就必须填写，并包含 `{task_id}` 占位符。
- `timeout`、`poll_interval`、`max_wait` 分别控制请求超时、轮询间隔和最长等待时间。
- 为避免同一分段重复提交并产生费用，任务一旦返回 `task_id`，后续续查固定使用创建该任务的平台，不会因轮询失败而转投其他平台。
- `models.json` 已被 `.gitignore` 忽略。视频 API 的字段、鉴权或请求路径因平台而异，必要时需按供应商文档调整 `zhipu_agent/stages/video.py`。

使用 `api_key_env` 时，可通过系统环境变量设置 Key（Windows CMD 示例）：

```bat
set VIDEO_PRIMARY_API_KEY=你的主用Key
set VIDEO_BACKUP_API_KEY=你的备用Key
```

通用适配器提交时 POST JSON `model`、`prompt`、`duration`、`aspect_ratio`，使用 Bearer Token 鉴权。不能假定任意 OpenAI 兼容接口都支持视频生成。

## 3. 明确提交云端任务

检查提示词、模型、价格和平台限制后，才运行：

```bash
python run_agent.py run --stages video --eps 1 --video-submit
```

未指定 `--video-submit` 时绝不调用视频 API。首次提交会异步创建云端任务并立即保存 `task_id`，不会逐段阻塞等待视频渲染；再次运行相同命令时，会查询已保存的任务并下载完成的视频。已成功生成的分段会跳过。生成失败会记录到 manifest，不会悄悄当作成功。

## 4. 当前边界

- 这是通用 API 适配器，不是对 Seedance、MiniMax 或其他特定平台的官方 SDK。请以实际供应商 API 文档为准。
- `seedance` 提示词会尽量复用结构化分镜字段，并附带现有 H3 提示词作为台词参考；正式投产前应人工检查。
- 视频下载失败时仍保留云端 URL，并在 manifest 写入 `download_error`。
- 本阶段生成的视频片段尚未自动拼接成整集。人工审核通过后，可再接入 FFmpeg 剪辑阶段。
