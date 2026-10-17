# 角色对白音频生成

音频阶段读取现有的角色卡与剧本，按句生成对白音频，不重新写剧本或分镜。角色音色 ID 存在输出目录中，不需要把每个角色的 ID 写进 `models.json`。

## 1. 先预览，不产生 API 费用

```powershell
python run_agent.py run --stages audio --eps 1
```

产物：
- `output/audio/character-voices.json`：角色音色注册表，保存稳定的 `character_id`、`voice_id`、音色描述、模型和状态。
- `output/audio/audio-manifest.json`：逐句对白清单；每句都包含角色 ID、当前 voice ID、表演指令、生成状态与音频路径。
- `output/audio/prompts/`：逐句配音提示词。
- `output/audio/qc-checklist.md`：配音质检清单。
- `output/audio/voice-previews/`：音色设计接口返回试听音频时保存的文件。
- `output/audio/lines/`：提交 TTS 后保存的逐句音频文件。

预览模式只整理数据，不调用百炼 API。实际创建音色与合成音频需要显式传入 `--audio-submit`。

## 2. 配置 CosyVoice

在项目根目录的 `models.json` 的 `audio` 列表中配置接口地址、模型和 API Key 环境变量。当前模型使用 `cosyvoice-v3.5-flash`，声音设计与合成均需使用北京地域的百炼服务和对应 API Key。

```json
{
  "audio": [
    {
      "name": "百炼-CosyVoice",
      "provider": "cosyvoice_tts",
      "speech_url": "https://你的WorkspaceId.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer",
      "voice_design_url": "https://你的WorkspaceId.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/customization",
      "api_key_env": "DASHSCOPE_API_KEY",
      "model": "cosyvoice-v3.5-flash",
      "response_format": "mp3",
      "sample_rate": 24000,
      "voice_language_hints": ["zh"],
      "language_hints": ["zh"],
      "voice_preview_text": "你好，我是这个故事中的角色，很高兴认识你。",
      "timeout": 120
    }
  ]
}
```

将 `你的WorkspaceId` 替换为百炼控制台中的实际 Workspace ID。保留 `models.json` 里的其他字段，只添加或合并 `audio`；不要提交真实 API Key。

Windows CMD 设置密钥：
```bat
set DASHSCOPE_API_KEY=你的百炼API密钥
```

## 3. 固定角色音色

`character-voices.json` 示例：
```json
{
  "version": 1,
  "provider": "aliyun_bailian",
  "characters": {
    "C01": {
      "character_id": "C01",
      "name": "主角",
      "model": "cosyvoice-v3.5-flash",
      "voice_id": "由百炼接口返回的实际 ID",
      "voice_description": "成年男性，中低音，略带沙哑",
      "status": "ready"
    }
  }
}
```

示例中的 ID 是占位文本。正式运行时，程序会从角色卡的 `voice.prompt`（或音色字段）读取描述，通过百炼声音设计接口创建音色，并将返回的 `output.voice_id` 写入注册表。后续集数和重跑会复用该 ID，不会按每句对白重复创建。也可以手动把真实 `voice_id` 写入 JSON。

- 以稳定的 `character_id` 作为主键；角色改名但 ID 不变时会继续复用音色。
- 缺少音色描述时会报错，不会静默切换成随机默认音色。
- `voice_id` 与模型绑定。切换到 CosyVoice 后，之前为其他模型创建的 ID 不能直接复用；需要为 CosyVoice 创建新的音色 ID。
- CosyVoice v3.5 Flash 声音设计仅支持北京地域；请使用匹配地域的 Workspace、API Key 和接口地址。

## 4. 正式生成

确认模型、地域、API 地址、音色描述和费用后：
```powershell
python run_agent.py run --stages audio --eps 1 --audio-submit --yes
```

不加 `--audio-submit` 不会调用 API。生成结果是一句一个音频文件；重跑会复用已成功的结果和已登记的音色。

## 5. 当前边界

- 本阶段生成角色对白 TTS，不是完整混音成片。
- 每个角色的 voice ID 固定，单句的情绪和表演要求通过 `instruction` 传递。
- 环境音、音效、配乐还需要独立音频模型，再用 FFmpeg 按时间线混音。
- 本阶段不会自动把逐句音频拼接成整集，也不会自动把音频合进视频。

官方文档：[CosyVoice 声音设计](https://help.aliyun.com/zh/model-studio/voice-design-user-guide)；[CosyVoice HTTP API](https://help.aliyun.com/zh/model-studio/cosyvoice-tts-http-api)。
