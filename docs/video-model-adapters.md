# 视频模型适配器配置

视频阶段统一负责分镜清单、镜头 ID、任务状态、轮询、下载和断点续跑；不同服务商的请求体和请求头由 `zhipu_agent/stages/video_adapters.py` 负责构造。

## 选择模型

复制/合并 `models.example.json` 的 `video` 配置到本地 `models.json`。通过 `key` 选择模型：

```powershell
# 默认使用 active=true 的模型（示例配置中是 Wan Prime）
python run_agent.py run --stages video --eps 1 --video-protocol seedance --video-submit --yes

# 明确选择 Wan Prime
python run_agent.py run --stages video --eps 1 --video-model wan_prime --video-submit --yes
```

显式使用 `--video-model` 时，只会使用匹配的模型配置，不会因为 API 错误而自动改用另一个模型，以免生成质量变化或发生意外计费。旧配置若没有 `active` 字段，则仍按原来的多平台故障转移方式运行。

## Wan 3.0 Video Prime（阿里云百炼）

已实现官方异步 API 格式：提交到 `/api/v1/services/aigc/video-generation/video-synthesis`，使用 `X-DashScope-Async: enable`，请求体使用 `input.prompt` 与 `parameters`；查询使用 `/api/v1/tasks/{task_id}`。

需要在 `models.json` 或环境变量中设置同一地域的 Workspace ID 和 API Key：

```powershell
$env:DASHSCOPE_WORKSPACE_ID = "你的业务空间ID"
$env:DASHSCOPE_API_KEY = "你的百炼API Key"
```

示例默认使用 480P 控制费用；需要更高分辨率时，修改该模型的 `resolution`。Wan 的生成时长为整数秒，当前适配器会把分镜时长四舍五入到整数并限制在 2–30 秒；如要求每镜头严格保持原时长，需在剪辑阶段裁切/补齐。

## Cheap.dev Seedance

已预留通用请求模板、附加请求头和独立模型选择，但示例配置保持 `enabled: false`。当前可访问的公开资料确认其有模型目录和 OpenAPI，但无法在此处可靠读取完整端点/模式定义。因此没有猜测提交 URL、状态查询 URL 或 model ID。

启用前，从 Cheap.dev 模型目录/OpenAPI 获取并填写：准确的模型 ID、创建任务 URL、状态查询 URL 模板、请求体模板，以及必要的附加请求头。模板中的 `{{model}}`、`{{prompt}}`、`{{duration_seconds}}`、`{{aspect_ratio}}`、`{{resolution}}` 会被替换；单独占据字符串值的占位符会保留原数据类型。不同 API 若需要嵌套请求体，直接在 `request_template` 中配置对应结构。API Token 通过 `CHEAPDEV_API_TOKEN` 环境变量提供。

建议先使用 Seedance 2.0 Mini 480P 做低成本验证，并在控制台确认当前模型 ID、实际计费档位和失败任务是否退款。

## CogVideoX-3（智谱开放平台）

使用官方异步 API：提交 `POST https://open.bigmodel.cn/api/paas/v4/videos/generations`，查询 `GET .../api/paas/v4/async-result/{task_id}`，视频地址取 `video_result[0].url`，失败状态为 `FAIL`。适配器 `cogvideox_zhipu` 的行为：

- 时长只支持 5 或 10 秒：不超过 5 秒取 5，其余取 10。
- `size` 不填时按分镜画幅选择（16:9 为 1920x1080，9:16 为 1080x1920，其余 1024x1024）；填写时必须是官方支持的尺寸，否则报错。
- `prompt` 最多 512 字符：适配器会去掉图片对齐指令行、压缩空白，超长时按句子边界截断。完整提示词仍保存在 `video-manifest.json` 与 `prompts/` 里。
- `with_audio` 只生成 AI 音效，不保证台词和口型；本适配器暂不传入首帧图片。
- Key 通过 `ZHIPUAI_API_KEY` 环境变量提供。

## 已知限制

- 通用适配器能配置请求体和请求头，但任务 ID、状态和视频 URL 的解析目前使用常见字段名递归查找；若服务商使用特殊字段名，需要进一步扩展响应字段映射。
- 仅对成功响应中返回的 HTTP(S) 视频 URL 进行下载；需要签名或特殊鉴权的下载地址可能需要单独适配。
- 本次没有真实 API Key，不能进行付费端到端生成测试；请先用预览模式检查分镜，再选择单个镜头小额测试。
