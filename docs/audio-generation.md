# 角色对白音频生成

音频阶段读取现有的 `characters/*-cast.json` 和 `script/*-script.json`，将每句对白单独生成音频，不重新写剧本，也不重新生成分镜。

## 1. 先预览，不产生 API 费用

```powershell
python run_agent.py run --stages audio --eps 1
```

产物：

- `output/audio/audio-manifest.json`：说话人、对白、角色音色指令、状态。
- `output/audio/prompts/`：逐句配音提示词，便于人工检查。
- `output/audio/qc-checklist.md`：配音质检清单。
- `output/audio/lines/`：提交 TTS 后保存的逐句音频文件。

每句对白会把角色卡的英文 `voice.prompt` 作为音色设计指令，并附加剧本中的 `delivery`（本句语气、情绪、动作）。如果没有 `voice.prompt`，则回退到 `timbre/pitch/pace/accent/emotion/referenceHint`。

## 2. 配置 TTS 服务

在项目根目录的 `models.json` 增加 `audio` 配置。接口需要接受类似 OpenAI Speech API 的 JSON 请求，并**直接返回音频二进制**（不是 JSON 包装的 URL 或 base64）。不同供应商字段不一致时，需要按该供应商文档调整适配器。

```json
{
  "audio": [
    {
      "name": "TTS 主用",
      "speech_url": "https://你的TTS平台/v1/audio/speech",
      "api_key_env": "AUDIO_PRIMARY_API_KEY",
      "model": "支持语音合成的模型名",
      "voice": "alloy",
      "voice_map": {
        "C01": "voice_id_1",
        "C02": "voice_id_2",
        "沈知微": "voice_id_1"
      },
      "response_format": "mp3",
      "instructions_field": "instructions",
      "timeout": 120
    }
  ]
}
```

保留现有 `models.json` 里的其他字段，只添加或合并 `audio`。不要把真实 API Key 提交到 Git。

- `voice` 是没有专属映射时的默认音色。
- `voice_map` 可按角色 ID（如 `C01`）或角色名映射供应商音色 ID。若希望角色之间有稳定、明显的音色差异，优先配置供应商支持的不同音色 ID。
- `instructions_field` 默认是 `instructions`。若供应商不支持这个字段，设为 `""`；但这样就不能保证自然语言音色提示被模型使用。
- 供应商必须支持配置的模型、音色和响应格式。不能仅凭接口路径相似就认定兼容。

Windows CMD 设置密钥示例：

```bat
set AUDIO_PRIMARY_API_KEY=你的TTS密钥
```

## 3. 正式生成

确认模型、价格、角色音色映射无误后：

```powershell
python run_agent.py run --stages audio --eps 1 --audio-submit --yes
```

`--audio-submit` 才会调用 TTS API；不加该参数只生成预览清单。生成结果是一句一个文件，便于重做单句和后期对口型、混音。重跑时会复用文本与音色指令未变化的既有状态；加 `--force` 会忽略已有产物，避免在没必要时使用。

## 4. 当前边界

- 本阶段生成的是**角色对白 TTS**，并非完整混音成片。
- 角色音色字段提供设计指令；是否能按自然语言重塑音色取决于 TTS 模型。使用固定预设音色时，应通过 `voice_map` 给不同角色分配不同 voice ID。
- 分镜里的环境音、音效和配乐目前仍是提示词内容；要实际生成这些音轨，需要接入支持音效/音乐生成的专用 API，再由 FFmpeg 按时间线混音。
- 本阶段不会自动把逐句音频拼接成整集，也不会自动把音频合进视频。
