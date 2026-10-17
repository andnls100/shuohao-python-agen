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

## CogVideoX / 智谱

示例配置保持 `enabled: false`。目前未核实智谱开放平台提供名为 `CogVideoX-3` 的托管视频生成 API。CogVideoX 是模型系列/开源推理项目的名称，不能仅凭“智谱清言”产品名称推断可调用的 API 模型 ID 或接口地址。

请先确认智谱开放平台的具体模型产品页、模型 ID、提交与查询接口，再按其文档填入 `submit_url`、`status_url_template`、`request_template` 和鉴权头。若实际使用的是自部署 CogVideoX 推理服务，则需要按该服务暴露的 HTTP API 配置，而不是智谱清言网页地址。

## 已知限制

- 通用适配器能配置请求体和请求头，但任务 ID、状态和视频 URL 的解析目前使用常见字段名递归查找；若服务商使用特殊字段名，需要进一步扩展响应字段映射。
- 仅对成功响应中返回的 HTTP(S) 视频 URL 进行下载；需要签名或特殊鉴权的下载地址可能需要单独适配。
- 本次没有真实 API Key，不能进行付费端到端生成测试；请先用预览模式检查分镜，再选择单个镜头小额测试。
