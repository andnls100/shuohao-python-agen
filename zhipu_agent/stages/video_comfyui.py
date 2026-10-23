"""ComfyUI 视频后端：把参考图和提示词填进 API 格式的工作流，发到远程 ComfyUI 服务器生成视频。

流程（ComfyUI 标准接口）：
  POST /upload/image   上传参考图／首帧，返回服务器上的文件名
  POST /prompt         提交填好的工作流，返回 prompt_id
  GET  /history/<id>   轮询，直到 SaveVideo 节点有输出
  GET  /view           下载输出视频

两个工作流（路径在 models.json 里配置）：
  workflow_ref   Ref2VA：场景图 + 角色图 + 道具图（最多 max_refs 张，超出的拼成一张拼图），用于新场景的第一镜
  workflow_i2v   I2V：上一镜最后一帧当首帧，用于连续镜头

注意：ComfyUI 的 /prompt 只接受「API 格式」工作流（节点 ID → class_type + inputs）。
界面格式（含 nodes / links）必须先在 ComfyUI 里用 Save (API Format) 导出。
"""
from __future__ import annotations

import copy
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

UA = "Mozilla/5.0 (compatible; shuohao-agent/1.0)"  # Cloudflare 隧道会拦 Python-urllib 默认 UA
VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv", ".gif")


class ComfyError(RuntimeError):
    pass


# Embedded canvas blueprint: runtime does not read 2.json.
_CANVAS_TEMPLATE_JSON = "{\n  \"id\": \"f55e764b-3b29-4619-b5f5-e9261e58d393\",\n  \"revision\": 0,\n  \"config\": {},\n  \"last_node_id\": 1,\n  \"last_link_id\": 48,\n  \"nodes\": [\n    {\n      \"id\": \"unet\",\n      \"type\": \"KaggleH3ShardedDiffusionLoader\",\n      \"pos\": [\n        100,\n        130\n      ],\n      \"size\": [\n        425.5845703125,\n        178\n      ],\n      \"flags\": {},\n      \"order\": 0,\n      \"mode\": 0,\n      \"inputs\": [],\n      \"outputs\": [\n        {\n          \"name\": \"MODEL\",\n          \"type\": \"MODEL\",\n          \"links\": [\n            28\n          ]\n        }\n      ],\n      \"properties\": {\n        \"Node name for S&R\": \"KaggleH3ShardedDiffusionLoader\"\n      },\n      \"widgets_values\": [\n        \"Ref2VA\",\n        \"singularity\",\n        \"int8_convrot\",\n        \"default\",\n        0,\n        1\n      ],\n      \"title\": \"Regular Video Workflow | Singularity Ref2VA Auto Loader\",\n      \"widgets_values_named\": {\n        \"model_variant\": \"Ref2VA\",\n        \"model_profile\": \"singularity\",\n        \"precision\": \"int8_convrot\",\n        \"weight_dtype\": \"default\",\n        \"gpu_0\": 0,\n        \"gpu_1\": 1\n      }\n    },\n    {\n      \"id\": \"clip\",\n      \"type\": \"KaggleH3TextEncoderLoader\",\n      \"pos\": [\n        100,\n        438\n      ],\n      \"size\": [\n        374.144140625,\n        106\n      ],\n      \"flags\": {},\n      \"order\": 1,\n      \"mode\": 0,\n      \"inputs\": [],\n      \"outputs\": [\n        {\n          \"name\": \"CLIP\",\n          \"type\": \"CLIP\",\n          \"links\": [\n            25\n          ]\n        }\n      ],\n      \"properties\": {\n        \"Node name for S&R\": \"KaggleH3TextEncoderLoader\"\n      },\n      \"widgets_values\": [\n        \"qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors\",\n        \"minimax\",\n        0\n      ],\n      \"title\": \"Regular Video Workflow | Text Encoder (GPU0)\",\n      \"widgets_values_named\": {\n        \"clip_name\": \"qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors\",\n        \"type\": \"minimax\",\n        \"device_id\": 0\n      }\n    },\n    {\n      \"id\": \"vae_video\",\n      \"type\": \"KaggleH3VAELoader\",\n      \"pos\": [\n        100,\n        674\n      ],\n      \"size\": [\n        330.2017578125,\n        106\n      ],\n      \"flags\": {},\n      \"order\": 2,\n      \"mode\": 0,\n      \"inputs\": [],\n      \"outputs\": [\n        {\n          \"name\": \"VAE\",\n          \"type\": \"VAE\",\n          \"links\": [\n            26,\n            41\n          ]\n        }\n      ],\n      \"properties\": {\n        \"Node name for S&R\": \"KaggleH3VAELoader\"\n      },\n      \"widgets_values\": [\n        \"minimax_h3_video_vae_fp16.safetensors\",\n        \"video\",\n        1\n      ],\n      \"title\": \"Regular Video Workflow | Video VAE (GPU1)\",\n      \"widgets_values_named\": {\n        \"vae_name\": \"minimax_h3_video_vae_fp16.safetensors\",\n        \"role\": \"video\",\n        \"device_id\": 1\n      }\n    },\n    {\n      \"id\": \"vae_audio\",\n      \"type\": \"KaggleH3VAELoader\",\n      \"pos\": [\n        100,\n        910\n      ],\n      \"size\": [\n        333.4556640625,\n        106\n      ],\n      \"flags\": {},\n      \"order\": 3,\n      \"mode\": 0,\n      \"inputs\": [],\n      \"outputs\": [\n        {\n          \"name\": \"VAE\",\n          \"type\": \"VAE\",\n          \"links\": [\n            27,\n            44\n          ]\n        }\n      ],\n      \"properties\": {\n        \"Node name for S&R\": \"KaggleH3VAELoader\"\n      },\n      \"widgets_values\": [\n        \"minimax_h3_audio_vae_fp32.safetensors\",\n        \"audio\",\n        0\n      ],\n      \"title\": \"Regular Video Workflow | Audio VAE (GPU0)\",\n      \"widgets_values_named\": {\n        \"vae_name\": \"minimax_h3_audio_vae_fp32.safetensors\",\n        \"role\": \"audio\",\n        \"device_id\": 0\n      }\n    },\n    {\n      \"id\": \"h3_adapter\",\n      \"type\": \"KaggleH3AdapterStack\",\n      \"pos\": [\n        625.5845703125,\n        130\n      ],\n      \"size\": [\n        481.9322265625,\n        294\n      ],\n      \"flags\": {},\n      \"order\": 5,\n      \"mode\": 0,\n      \"inputs\": [\n        {\n          \"name\": \"model\",\n          \"type\": \"MODEL\",\n          \"link\": 28\n        }\n      ],\n      \"outputs\": [\n        {\n          \"name\": \"MODEL\",\n          \"type\": \"MODEL\",\n          \"links\": [\n            29\n          ]\n        },\n        {\n          \"name\": \"H3_RUNTIME_CONFIG\",\n          \"type\": \"H3_RUNTIME_CONFIG\",\n          \"links\": [\n            30\n          ]\n        }\n      ],\n      \"properties\": {\n        \"Node name for S&R\": \"KaggleH3AdapterStack\"\n      },\n      \"widgets_values\": [\n        \"ref2va\",\n        true,\n        4,\n        \"auto_singularity_ref2va_turbo\",\n        1,\n        \"None\",\n        1,\n        \"None\",\n        1,\n        \"Ref2VA\"\n      ],\n      \"title\": \"Regular Video Workflow | Matching Singularity Ref2VA Turbo LoRA\",\n      \"widgets_values_named\": {\n        \"model_variant\": \"ref2va\",\n        \"turbo_mode\": true,\n        \"turbo_steps\": 4,\n        \"adapter_1\": \"auto_singularity_ref2va_turbo\",\n        \"strength_1\": 1,\n        \"adapter_2\": \"None\",\n        \"strength_2\": 1,\n        \"adapter_3\": \"None\",\n        \"strength_3\": 1,\n        \"conditioning_mode\": \"Ref2VA\"\n      }\n    },\n    {\n      \"id\": \"phase\",\n      \"type\": \"KaggleH3PhaseDispatch\",\n      \"pos\": [\n        1263.85859375,\n        130\n      ],\n      \"size\": [\n        411.269140625,\n        86\n      ],\n      \"flags\": {},\n      \"order\": 7,\n      \"mode\": 0,\n      \"inputs\": [\n        {\n          \"name\": \"model\",\n          \"type\": \"MODEL\",\n          \"link\": 29\n        },\n        {\n          \"name\": \"conditioning\",\n          \"type\": \"CONDITIONING\",\n          \"link\": 31\n        },\n        {\n          \"name\": \"latent_image\",\n          \"type\": \"LATENT\",\n          \"link\": 32\n        },\n        {\n          \"name\": \"runtime_config\",\n          \"type\": \"H3_RUNTIME_CONFIG\",\n          \"link\": 30,\n          \"shape\": 7\n        }\n      ],\n      \"outputs\": [\n        {\n          \"name\": \"MODEL\",\n          \"type\": \"MODEL\",\n          \"links\": [\n            33,\n            37\n          ]\n        },\n        {\n          \"name\": \"conditioning\",\n          \"type\": \"CONDITIONING\",\n          \"links\": [\n            35\n          ]\n        },\n        {\n          \"name\": \"latent_image\",\n          \"type\": \"LATENT\",\n          \"links\": [\n            36\n          ]\n        },\n        {\n          \"name\": \"H3_RUNTIME_CONFIG\",\n          \"type\": \"H3_RUNTIME_CONFIG\",\n          \"links\": [\n            34,\n            39\n          ]\n        }\n      ],\n      \"properties\": {\n        \"Node name for S&R\": \"KaggleH3PhaseDispatch\"\n      },\n      \"title\": \"Regular Video Workflow | Dual-T4 Transformer Dispatch\"\n    },\n    {\n      \"id\": \"sample\",\n      \"type\": \"KaggleH3TurboSampler\",\n      \"pos\": [\n        1677.4490234374998,\n        130\n      ],\n      \"size\": [\n        540.2291015625,\n        238\n      ],\n      \"flags\": {},\n      \"order\": 8,\n      \"mode\": 0,\n      \"inputs\": [\n        {\n          \"name\": \"model\",\n          \"type\": \"MODEL\",\n          \"link\": 33\n        },\n        {\n          \"name\": \"conditioning\",\n          \"type\": \"CONDITIONING\",\n          \"link\": 35\n        },\n        {\n          \"name\": \"latent_image\",\n          \"type\": \"LATENT\",\n          \"link\": 36\n        },\n        {\n          \"name\": \"runtime_config\",\n          \"type\": \"H3_RUNTIME_CONFIG\",\n          \"link\": 34,\n          \"shape\": 7\n        }\n      ],\n      \"outputs\": [\n        {\n          \"name\": \"video_latent\",\n          \"type\": \"LATENT\",\n          \"links\": [\n            38\n          ]\n        },\n        {\n          \"name\": \"audio_latent\",\n          \"type\": \"LATENT\",\n          \"links\": [\n            43\n          ]\n        },\n        {\n          \"name\": \"denoised_output\",\n          \"type\": \"LATENT\",\n          \"links\": null\n        }\n      ],\n      \"properties\": {\n        \"Node name for S&R\": \"KaggleH3TurboSampler\"\n      },\n      \"widgets_values\": [\n        292794338190016,\n        \"randomize\",\n        \"euler\",\n        20,\n        \"safe\",\n        false\n      ],\n      \"title\": \"Regular Video Workflow | Choose Euler or Euler Dual-clock + SageAttention\",\n      \"widgets_values_named\": {\n        \"noise_seed\": 292794338190016,\n        \"control_after_generate\": \"randomize\",\n        \"sampler_name\": \"euler\",\n        \"steps\": 20,\n        \"synchronize_mode\": \"safe\",\n        \"use_sage_attention\": false\n      }\n    },\n    {\n      \"id\": \"upscale\",\n      \"type\": \"KaggleH3LatentUpscale2x\",\n      \"pos\": [\n        2061.0837890625,\n        130\n      ],\n      \"size\": [\n        493.28671875,\n        170\n      ],\n      \"flags\": {},\n      \"order\": 9,\n      \"mode\": 0,\n      \"inputs\": [\n        {\n          \"name\": \"model\",\n          \"type\": \"MODEL\",\n          \"link\": 37\n        },\n        {\n          \"name\": \"samples\",\n          \"type\": \"LATENT\",\n          \"link\": 38\n        },\n        {\n          \"name\": \"runtime_config\",\n          \"type\": \"H3_RUNTIME_CONFIG\",\n          \"link\": 39,\n          \"shape\": 7\n        }\n      ],\n      \"outputs\": [\n        {\n          \"name\": \"upscaled_latent\",\n          \"type\": \"LATENT\",\n          \"links\": [\n            40\n          ]\n        },\n        {\n          \"name\": \"original_latent\",\n          \"type\": \"LATENT\",\n          \"links\": null\n        },\n        {\n          \"name\": \"H3_RUNTIME_CONFIG\",\n          \"type\": \"H3_RUNTIME_CONFIG\",\n          \"links\": [\n            42,\n            45\n          ]\n        }\n      ],\n      \"properties\": {\n        \"Node name for S&R\": \"KaggleH3LatentUpscale2x\"\n      },\n      \"widgets_values\": [\n        false,\n        \"360p\",\n        \"16:9\",\n        \"bicubic\"\n      ],\n      \"title\": \"Regular Video Workflow | Optional 2x Latent Upscale (360p to 720p)\",\n      \"widgets_values_named\": {\n        \"enabled\": false,\n        \"source_size_preset\": \"360p\",\n        \"aspect_ratio\": \"16:9\",\n        \"method\": \"bicubic\"\n      }\n    },\n    {\n      \"id\": \"decode\",\n      \"type\": \"KaggleH3VAEDecode\",\n      \"pos\": [\n        2520.8177734375,\n        130\n      ],\n      \"size\": [\n        355.2837890625,\n        98\n      ],\n      \"flags\": {},\n      \"order\": 10,\n      \"mode\": 0,\n      \"inputs\": [\n        {\n          \"name\": \"samples\",\n          \"type\": \"LATENT\",\n          \"link\": 40\n        },\n        {\n          \"name\": \"vae\",\n          \"type\": \"VAE\",\n          \"link\": 41\n        },\n        {\n          \"name\": \"runtime_config\",\n          \"type\": \"H3_RUNTIME_CONFIG\",\n          \"link\": 42,\n          \"shape\": 7\n        }\n      ],\n      \"outputs\": [\n        {\n          \"name\": \"IMAGE\",\n          \"type\": \"IMAGE\",\n          \"links\": [\n            46\n          ]\n        }\n      ],\n      \"properties\": {\n        \"Node name for S&R\": \"KaggleH3VAEDecode\"\n      },\n      \"widgets_values\": [\n        1\n      ],\n      \"title\": \"Regular Video Workflow | Video Decode (GPU1)\",\n      \"widgets_values_named\": {\n        \"device_id\": 1\n      }\n    },\n    {\n      \"id\": \"decode_audio\",\n      \"type\": \"KaggleH3AudioVAEDecode\",\n      \"pos\": [\n        2520.8177734375,\n        358\n      ],\n      \"size\": [\n        419.8140625,\n        122\n      ],\n      \"flags\": {},\n      \"order\": 11,\n      \"mode\": 0,\n      \"inputs\": [\n        {\n          \"name\": \"samples\",\n          \"type\": \"LATENT\",\n          \"link\": 43\n        },\n        {\n          \"name\": \"vae\",\n          \"type\": \"VAE\",\n          \"link\": 44\n        },\n        {\n          \"name\": \"runtime_config\",\n          \"type\": \"H3_RUNTIME_CONFIG\",\n          \"link\": 45,\n          \"shape\": 7\n        }\n      ],\n      \"outputs\": [\n        {\n          \"name\": \"AUDIO\",\n          \"type\": \"AUDIO\",\n          \"links\": [\n            47\n          ]\n        }\n      ],\n      \"properties\": {\n        \"Node name for S&R\": \"KaggleH3AudioVAEDecode\"\n      },\n      \"widgets_values\": [\n        0,\n        false\n      ],\n      \"title\": \"Regular Video Workflow | Optional Mute Generated Audio\",\n      \"widgets_values_named\": {\n        \"device_id\": 0,\n        \"mute_generated_audio\": false\n      }\n    },\n    {\n      \"id\": \"video\",\n      \"type\": \"CreateVideo\",\n      \"pos\": [\n        2979.35546875,\n        130\n      ],\n      \"size\": [\n        433.3013671875,\n        126\n      ],\n      \"flags\": {},\n      \"order\": 12,\n      \"mode\": 0,\n      \"inputs\": [\n        {\n          \"name\": \"images\",\n          \"type\": \"IMAGE\",\n          \"link\": 46\n        },\n        {\n          \"name\": \"audio\",\n          \"type\": \"AUDIO\",\n          \"link\": 47,\n          \"shape\": 7\n        }\n      ],\n      \"outputs\": [\n        {\n          \"name\": \"VIDEO\",\n          \"type\": \"VIDEO\",\n          \"links\": [\n            48\n          ]\n        }\n      ],\n      \"properties\": {\n        \"Node name for S&R\": \"CreateVideo\"\n      },\n      \"widgets_values\": [\n        24,\n        8,\n        \"sRGB\"\n      ],\n      \"title\": \"Regular Video Workflow | Assemble Video + Optional Audio\",\n      \"widgets_values_named\": {\n        \"fps\": 24,\n        \"bit_depth\": 8,\n        \"color_space\": \"sRGB\"\n      }\n    },\n    {\n      \"id\": \"save\",\n      \"type\": \"SaveVideo\",\n      \"pos\": [\n        3349.35546875,\n        130\n      ],\n      \"size\": [\n        278.624609375,\n        106\n      ],\n      \"flags\": {},\n      \"order\": 13,\n      \"mode\": 0,\n      \"inputs\": [\n        {\n          \"name\": \"video\",\n          \"type\": \"VIDEO\",\n          \"link\": 48\n        }\n      ],\n      \"outputs\": [\n        {\n          \"name\": \"video\",\n          \"type\": \"VIDEO\",\n          \"links\": null\n        }\n      ],\n      \"properties\": {},\n      \"widgets_values\": [\n        \"kaggle_h3/regular_video\",\n        \"mp4\",\n        \"auto\",\n        \"auto\"\n      ],\n      \"title\": \"Regular Video Workflow | Save MP4\",\n      \"widgets_values_named\": {\n        \"filename_prefix\": \"kaggle_h3/regular_video\",\n        \"format\": \"mp4\",\n        \"format.codec\": \"auto\",\n        \"codec\": \"auto\"\n      }\n    },\n    {\n      \"id\": \"step_count\",\n      \"type\": \"FirstInt (obvpm)\",\n      \"pos\": [\n        100,\n        1146\n      ],\n      \"size\": [\n        379.0171875,\n        98\n      ],\n      \"flags\": {},\n      \"order\": 4,\n      \"mode\": 0,\n      \"inputs\": [\n        {\n          \"name\": \"int1\",\n          \"type\": \"INT\",\n          \"link\": null,\n          \"shape\": 7\n        },\n        {\n          \"name\": \"int2\",\n          \"type\": \"INT\",\n          \"link\": null,\n          \"shape\": 7\n        },\n        {\n          \"name\": \"int3\",\n          \"type\": \"INT\",\n          \"link\": null,\n          \"shape\": 7\n        }\n      ],\n      \"outputs\": [\n        {\n          \"name\": \"int\",\n          \"type\": \"INT\",\n          \"links\": null\n        }\n      ],\n      \"properties\": {\n        \"Node name for S&R\": \"FirstInt (obvpm)\"\n      },\n      \"widgets_values\": [\n        4\n      ],\n      \"title\": \"Regular Video Workflow | Shared Turbo Step Count\",\n      \"widgets_values_named\": {\n        \"fallback\": 4\n      }\n    },\n    {\n      \"id\": \"h3\",\n      \"type\": \"KaggleH3Conditioning\",\n      \"pos\": [\n        623.3845703124999,\n        682.6999999999999\n      ],\n      \"size\": [\n        538.2740234375,\n        968\n      ],\n      \"flags\": {},\n      \"order\": 6,\n      \"mode\": 0,\n      \"inputs\": [\n        {\n          \"name\": \"clip\",\n          \"type\": \"CLIP\",\n          \"link\": 25\n        },\n        {\n          \"name\": \"vae\",\n          \"type\": \"VAE\",\n          \"link\": 26,\n          \"shape\": 7\n        },\n        {\n          \"name\": \"audio_vae\",\n          \"type\": \"VAE\",\n          \"link\": 27,\n          \"shape\": 7\n        },\n        {\n          \"name\": \"start_frame\",\n          \"type\": \"IMAGE\",\n          \"link\": null,\n          \"shape\": 7\n        },\n        {\n          \"name\": \"end_frame\",\n          \"type\": \"IMAGE\",\n          \"link\": null,\n          \"shape\": 7\n        },\n        {\n          \"name\": \"ref_images.ref_image_0\",\n          \"type\": \"IMAGE\",\n          \"link\": null,\n          \"label\": \"ref_image_0\",\n          \"shape\": 7\n        },\n        {\n          \"name\": \"ref_videos.ref_video_0\",\n          \"type\": \"IMAGE\",\n          \"link\": null,\n          \"label\": \"ref_video_0\",\n          \"shape\": 7\n        },\n        {\n          \"name\": \"ref_video_audios.ref_video_audio_0\",\n          \"type\": \"AUDIO\",\n          \"link\": null,\n          \"label\": \"ref_video_audio_0\",\n          \"shape\": 7\n        },\n        {\n          \"name\": \"ref_audios.ref_audio_0\",\n          \"type\": \"AUDIO\",\n          \"link\": null,\n          \"label\": \"ref_audio_0\",\n          \"shape\": 7\n        }\n      ],\n      \"outputs\": [\n        {\n          \"name\": \"positive\",\n          \"type\": \"CONDITIONING\",\n          \"links\": [\n            31\n          ]\n        },\n        {\n          \"name\": \"LATENT\",\n          \"type\": \"LATENT\",\n          \"links\": [\n            32\n          ]\n        }\n      ],\n      \"properties\": {\n        \"Node name for S&R\": \"KaggleH3Conditioning\"\n      },\n      \"widgets_values\": [\n        \"镜头小幅缓推向<Picture 5>。<Picture 1>与<Picture 2>身着<Picture 3>，被粗麻绳反绑双臂，在两名<Picture 4>推搡下踉跄跌向滑道。<Picture 1>双脚踩空，身体前倾失去平衡，<Picture 2>紧随其后。以画外音说（唇形完全闭合）远处传来<Picture 4>的呵斥声。\\\\n[镜头 2] 于 00:05.000，镜头切换至近景特写。一名红衣年轻女子（<Picture 2>）死死攥住师兄的袖口，指甲掐进布料。她抬起头，眼神从慌乱转为决绝，看向师兄。她低声回应：<d>[Chinese] 师兄别喊了，他们听不懂人话。</d>\\\\n\\\\n整体音景：火焰燃烧的噼啪声，衣物摩擦声，沉重的脚步声，粗重的喘息声。\\\\n\\\\n非叙事配乐：低沉的大提琴弦乐，节奏紧张缓慢。\\\"  优化下这个提示词  真人感\",\n        \"Ref2VA\",\n        5,\n        \"360p\",\n        \"16:9\",\n        \"match\",\n        \"None\",\n        \"None\",\n        \"image\",\n        \"3e813aa9_燕溪山-C02-sheet.png\",\n        \"image\",\n        \"97d7ed0d_徐今朝-C03-sheet.png\",\n        \"image\",\n        \"25f75664_婚服-P01-sheet.png\",\n        \"image\",\n        \"2741df8e_江边村民-C09-sheet.png\",\n        \"image\",\n        \"1766cdda_江边祭坛-sheet.png\",\n        \"none\",\n        \"none\",\n        \"none\",\n        \"none\",\n        \"none\",\n        \"none\",\n        \"none\",\n        \"none\",\n        \"none\",\n        \"none\",\n        0,\n        0,\n        0\n      ],\n      \"title\": \"Regular Video Workflow | Choose Prompt, References, Resolution, Duration\",\n      \"widgets_values_named\": {\n        \"prompt\": \"镜头小幅缓推向<Picture 5>。<Picture 1>与<Picture 2>身着<Picture 3>，被粗麻绳反绑双臂，在两名<Picture 4>推搡下踉跄跌向滑道。<Picture 1>双脚踩空，身体前倾失去平衡，<Picture 2>紧随其后。以画外音说（唇形完全闭合）远处传来<Picture 4>的呵斥声。\\\\n[镜头 2] 于 00:05.000，镜头切换至近景特写。一名红衣年轻女子（<Picture 2>）死死攥住师兄的袖口，指甲掐进布料。她抬起头，眼神从慌乱转为决绝，看向师兄。她低声回应：<d>[Chinese] 师兄别喊了，他们听不懂人话。</d>\\\\n\\\\n整体音景：火焰燃烧的噼啪声，衣物摩擦声，沉重的脚步声，粗重的喘息声。\\\\n\\\\n非叙事配乐：低沉的大提琴弦乐，节奏紧张缓慢。\\\"  优化下这个提示词  真人感\",\n        \"mode\": \"Ref2VA\",\n        \"seconds\": 5,\n        \"size_preset\": \"360p\",\n        \"aspect_ratio\": \"16:9\",\n        \"ref_image_size\": \"match\",\n        \"start_frame_file\": \"None\",\n        \"end_frame_file\": \"None\",\n        \"reference_0\": \"image\",\n        \"reference_0.file\": \"3e813aa9_燕溪山-C02-sheet.png\",\n        \"reference_1\": \"image\",\n        \"reference_1.file\": \"97d7ed0d_徐今朝-C03-sheet.png\",\n        \"reference_2\": \"image\",\n        \"reference_2.file\": \"25f75664_婚服-P01-sheet.png\",\n        \"reference_3\": \"image\",\n        \"reference_3.file\": \"2741df8e_江边村民-C09-sheet.png\",\n        \"reference_4\": \"image\",\n        \"reference_4.file\": \"1766cdda_江边祭坛-sheet.png\",\n        \"reference_5\": \"none\",\n        \"reference_6\": \"none\",\n        \"reference_7\": \"none\",\n        \"reference_8\": \"none\",\n        \"reference_9\": \"none\",\n        \"reference_10\": \"none\",\n        \"reference_11\": \"none\",\n        \"reference_12\": \"none\",\n        \"reference_13\": \"none\",\n        \"reference_14\": \"none\",\n        \"width\": 0,\n        \"height\": 0,\n        \"length\": 0\n      }\n    }\n  ],\n  \"links\": [\n    [\n      25,\n      \"clip\",\n      0,\n      \"h3\",\n      0,\n      \"CLIP\"\n    ],\n    [\n      26,\n      \"vae_video\",\n      0,\n      \"h3\",\n      1,\n      \"VAE\"\n    ],\n    [\n      27,\n      \"vae_audio\",\n      0,\n      \"h3\",\n      2,\n      \"VAE\"\n    ],\n    [\n      28,\n      \"unet\",\n      0,\n      \"h3_adapter\",\n      0,\n      \"MODEL\"\n    ],\n    [\n      29,\n      \"h3_adapter\",\n      0,\n      \"phase\",\n      0,\n      \"MODEL\"\n    ],\n    [\n      30,\n      \"h3_adapter\",\n      1,\n      \"phase\",\n      3,\n      \"H3_RUNTIME_CONFIG\"\n    ],\n    [\n      31,\n      \"h3\",\n      0,\n      \"phase\",\n      1,\n      \"CONDITIONING\"\n    ],\n    [\n      32,\n      \"h3\",\n      1,\n      \"phase\",\n      2,\n      \"LATENT\"\n    ],\n    [\n      33,\n      \"phase\",\n      0,\n      \"sample\",\n      0,\n      \"MODEL\"\n    ],\n    [\n      34,\n      \"phase\",\n      3,\n      \"sample\",\n      3,\n      \"H3_RUNTIME_CONFIG\"\n    ],\n    [\n      35,\n      \"phase\",\n      1,\n      \"sample\",\n      1,\n      \"CONDITIONING\"\n    ],\n    [\n      36,\n      \"phase\",\n      2,\n      \"sample\",\n      2,\n      \"LATENT\"\n    ],\n    [\n      37,\n      \"phase\",\n      0,\n      \"upscale\",\n      0,\n      \"MODEL\"\n    ],\n    [\n      38,\n      \"sample\",\n      0,\n      \"upscale\",\n      1,\n      \"LATENT\"\n    ],\n    [\n      39,\n      \"phase\",\n      3,\n      \"upscale\",\n      2,\n      \"H3_RUNTIME_CONFIG\"\n    ],\n    [\n      40,\n      \"upscale\",\n      0,\n      \"decode\",\n      0,\n      \"LATENT\"\n    ],\n    [\n      41,\n      \"vae_video\",\n      0,\n      \"decode\",\n      1,\n      \"VAE\"\n    ],\n    [\n      42,\n      \"upscale\",\n      2,\n      \"decode\",\n      2,\n      \"H3_RUNTIME_CONFIG\"\n    ],\n    [\n      43,\n      \"sample\",\n      1,\n      \"decode_audio\",\n      0,\n      \"LATENT\"\n    ],\n    [\n      44,\n      \"vae_audio\",\n      0,\n      \"decode_audio\",\n      1,\n      \"VAE\"\n    ],\n    [\n      45,\n      \"upscale\",\n      2,\n      \"decode_audio\",\n      2,\n      \"H3_RUNTIME_CONFIG\"\n    ],\n    [\n      46,\n      \"decode\",\n      0,\n      \"video\",\n      0,\n      \"IMAGE\"\n    ],\n    [\n      47,\n      \"decode_audio\",\n      0,\n      \"video\",\n      1,\n      \"AUDIO\"\n    ],\n    [\n      48,\n      \"video\",\n      0,\n      \"save\",\n      0,\n      \"VIDEO\"\n    ]\n  ],\n  \"groups\": [],\n  \"extra\": {\n    \"ds\": {\n      \"scale\": 0.8264462809917354,\n      \"offset\": [\n        318.6216155616296,\n        -371.0274285724452\n      ]\n    },\n    \"frontendVersion\": \"1.49.6\"\n  },\n  \"version\": 0.4\n}"

def canvas_from_api(api_wf: Dict[str, Any]) -> Dict[str, Any]:
    """Build canvas/UI-format output and populate widget values from actual parameters."""
    canvas = json.loads(_CANVAS_TEMPLATE_JSON)
    for node in canvas.get("nodes", []):
        api_node = api_wf.get(str(node.get("id", "")))
        if not api_node:
            continue
        inputs = api_node.get("inputs") or {}
        named = node.get("widgets_values_named")
        if not isinstance(named, dict):
            continue
        for key in list(named):
            if key in inputs and not isinstance(inputs[key], list):
                named[key] = inputs[key]
        values = node.get("widgets_values")
        if isinstance(values, list):
            for index, key in enumerate(named):
                if index < len(values):
                    values[index] = named[key]
    return canvas


# ---------------------------------------------------------------- 工作流读取与定位
def is_ui_format(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("nodes"), list)


def _ui_to_api(data: Dict[str, Any], object_info: Dict[str, Any], source: str) -> Dict[str, Any]:
    """Convert a ComfyUI canvas workflow (nodes/links) to /prompt API format."""
    nodes = data.get("nodes")
    if not isinstance(nodes, list) or not isinstance(data.get("links"), list):
        raise ComfyError(f"{source} 缺少 nodes/links，无法转换为 ComfyUI API 工作流")

    link_map = {}
    for link in data["links"]:
        if isinstance(link, list) and len(link) >= 6:
            link_id, origin_id, origin_slot, target_id, target_slot = link[:5]
            link_map[str(link_id)] = (str(origin_id), int(origin_slot), str(target_id), int(target_slot))

    result: Dict[str, Any] = {}
    for node in nodes:
        if not isinstance(node, dict) or node.get("id") is None or not node.get("type"):
            continue
        node_id = str(node["id"])
        node_type = str(node["type"])
        resolved_type = node_type
        if resolved_type not in object_info:
            # Some custom nodes expose a display suffix in the canvas type.
            candidates = [name for name in object_info if name == node_type.split(" (", 1)[0]]
            if len(candidates) == 1:
                resolved_type = candidates[0]
            else:
                raise ComfyError(
                    f"{source} 节点 {node_id} 类型 {node_type!r} 不存在于服务器 /object_info；"
                    "请确认远程 ComfyUI 已安装对应自定义节点"
                )
        schema = object_info[resolved_type]
        allowed = set()
        for section in ("required", "optional"):
            values = (schema.get("input") or {}).get(section) or {}
            allowed.update(values.keys())

        inputs: Dict[str, Any] = {}
        # Use the server's registered input schema to avoid submitting canvas-only metadata.
        named = node.get("widgets_values_named")
        if isinstance(named, dict):
            for key, value in named.items():
                if key in allowed:
                    inputs[key] = value
        # Connections are authoritative and override any stale widget values.
        for port in node.get("inputs") or []:
            if not isinstance(port, dict) or port.get("link") is None:
                continue
            link = link_map.get(str(port["link"]))
            if not link:
                raise ComfyError(f"{source} 节点 {node_id} 的输入 {port.get('name')} 引用了不存在的 link {port['link']}")
            origin_id, origin_slot, target_id, _target_slot = link
            if target_id != node_id:
                raise ComfyError(f"{source} link {port['link']} 的目标节点不匹配")
            inputs[str(port.get("name"))] = [origin_id, origin_slot]

        missing = []
        required = (schema.get("input") or {}).get("required") or {}
        for key, spec in required.items():
            if key not in inputs:
                # Inputs with a connected socket are filled above; a required unconnected
                # input must have a named widget value in the canvas JSON.
                missing.append(key)
        if missing:
            raise ComfyError(
                f"{source} 节点 {node_id}（{resolved_type}）缺少 API 必需输入：{', '.join(missing)}；"
                "该节点可能没有 widgets_values_named，需导出包含完整控件值的工作流"
            )
        result[node_id] = {"class_type": resolved_type, "inputs": inputs}
        title = node.get("title") or (node.get("properties") or {}).get("Node name for S&R")
        if title:
            result[node_id]["_meta"] = {"title": str(title)}
    if not result:
        raise ComfyError(f"{source} 没有可转换的工作流节点")
    return result


def load_workflow(path: Path, object_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ComfyError(f"无法读取工作流 {path}：{exc}") from exc
    except ValueError as exc:
        raise ComfyError(f"工作流 {path} 不是合法 JSON：{exc}") from exc
    if is_ui_format(data):
        if object_info is None:
            raise ComfyError(f"{path.name} 是 ComfyUI 界面格式；转换为 /prompt API 格式需要先读取服务器 /object_info")
        data = _ui_to_api(data, object_info, path.name)
    if not isinstance(data, dict) or not data or not all(isinstance(v, dict) and "class_type" in v and isinstance(v.get("inputs"), dict) for v in data.values()):
        raise ComfyError(f"{path.name} 不是有效的 ComfyUI 工作流（应为 API 格式，或含 nodes/links 的界面格式）")
    return data


def find_node(wf: Dict[str, Any], class_types: Sequence[str], node_id: Any = None, title_hint: str = "") -> Optional[str]:
    """优先用配置的节点 ID；否则按 class_type 找，多个时用标题关键字挑。"""
    if node_id not in (None, "") and str(node_id) in wf:
        return str(node_id)
    cands = [k for k, v in wf.items() if v.get("class_type") in class_types]
    hint = title_hint.lower()
    if hint:
        titled = [k for k in cands if hint in str((wf[k].get("_meta") or {}).get("title", "")).lower()]
        if titled:
            return titled[0]
    return cands[0] if cands else None


def _need(wf: Dict[str, Any], class_types: Sequence[str], node_id: Any, title_hint: str, what: str) -> str:
    found = find_node(wf, class_types, node_id, title_hint)
    if not found:
        raise ComfyError(f"工作流里找不到{what}节点（class_type 应为 {' / '.join(class_types)}）；可在 models.json 的 nodes 里指定节点 ID")
    return found


# ---------------------------------------------------------------- 参考图规划（含拼图）
def tile_images(paths: Sequence[Path], target: Path, cell: int = 512) -> Path:
    """把多张图拼成一张（不调用 AI）：网格排布，每格等比缩放居中，白底。"""
    from PIL import Image  # Pillow 已是依赖（压缩大图时也用）

    n = len(paths)
    if n == 0:
        raise ComfyError("拼图需要至少一张图")
    cols = 1 if n == 1 else 2 if n <= 4 else 3
    rows = (n + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell, rows * cell), (255, 255, 255))
    for i, p in enumerate(paths):
        with Image.open(p) as im:
            im = im.convert("RGB")
            im.thumbnail((cell, cell))
            x = (i % cols) * cell + (cell - im.width) // 2
            y = (i // cols) * cell + (cell - im.height) // 2
            sheet.paste(im, (x, y))
    target.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(target)
    return target


def plan_references(refs: List[Tuple[str, Path]], max_refs: int, collage_path: Path) -> List[Tuple[str, Path]]:
    """refs 超过 max_refs 时，把最后几张（优先级最低）拼成一张拼图，总数正好 max_refs。"""
    if max_refs < 1:
        raise ComfyError("max_refs 必须 >= 1")
    if len(refs) <= max_refs:
        return list(refs)
    keep = refs[: max_refs - 1]
    rest = refs[max_refs - 1:]
    if max_refs == 1:
        keep, rest = [], refs
    sheet = tile_images([p for _k, p in rest], collage_path)
    return keep + [("collage", sheet)]


def reference_header(kinds: Sequence[str]) -> str:
    """用 <Picture N> 说明每张参考图的作用（与工作流模板里的写法一致）。"""
    subj = [i for i, k in enumerate(kinds, 1) if k in ("character", "previous")]
    scene = [i for i, k in enumerate(kinds, 1) if k == "scene"]
    props = [i for i, k in enumerate(kinds, 1) if k == "prop"]
    coll = [i for i, k in enumerate(kinds, 1) if k == "collage"]
    pic = lambda ids: ", ".join(f"<Picture {i}>" for i in ids)  # noqa: E731
    parts = []
    if subj:
        parts.append(f"Preserve the subject from {pic(subj)}")
    if scene:
        parts.append(f"{'and ' if parts else ''}the scene from {pic(scene)}")
    if props:
        parts.append(f"keep the props consistent with {pic(props)}")
    if coll:
        parts.append(f"{pic(coll)} is a collage of additional references; keep those characters and props consistent")
    return ("; ".join(parts) + ".") if parts else ""


# ---------------------------------------------------------------- 填工作流
def _set_seed_and_prefix(wf: Dict[str, Any], seed: int, prefix: str, nodes: Dict[str, Any]) -> None:
    seed_id = find_node(wf, ("KaggleH3TurboSampler", "RandomNoise", "KSampler", "KSamplerAdvanced"), nodes.get("seed"))
    if seed_id:
        ins = wf[seed_id]["inputs"]
        for key in ("noise_seed", "seed"):
            if key in ins and not isinstance(ins[key], list):
                ins[key] = seed
    save_id = _need(wf, ("SaveVideo", "VHS_VideoCombine", "SaveAnimatedWEBP"), nodes.get("save"), "", "保存视频（SaveVideo）")
    wf[save_id]["inputs"]["filename_prefix"] = prefix


def build_ref2va(template: Dict[str, Any], *, prompt: str, seconds: float, aspect: Optional[str], size_preset: Optional[str],
                 uploaded: Sequence[str], seed: int, prefix: str, nodes: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    nodes = nodes or {}
    wf = copy.deepcopy(template)
    cond = _need(wf, ("KaggleH3Conditioning",), nodes.get("conditioning"), "", "条件（KaggleH3Conditioning）")
    ins = wf[cond]["inputs"]
    ref_kind = ins.get("reference_0", "image")
    for key in [k for k in ins if k.startswith("reference_")]:
        del ins[key]
    for i, name in enumerate(uploaded):
        ins[f"reference_{i}"] = ref_kind
        ins[f"reference_{i}.file"] = name
    ins["prompt"] = prompt
    ins["seconds"] = float(seconds)
    if aspect:
        ins["aspect_ratio"] = aspect
    if size_preset:
        ins["size_preset"] = size_preset
    _set_seed_and_prefix(wf, seed, prefix, nodes)
    return wf


def build_i2v(template: Dict[str, Any], *, prompt: str, seconds: float, uploaded: str, seed: int, prefix: str,
              nodes: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    nodes = nodes or {}
    wf = copy.deepcopy(template)
    img = _need(wf, ("LoadImage",), nodes.get("start_image"), "start", "首帧图片（LoadImage，标题含 START FRAME）")
    wf[img]["inputs"]["image"] = uploaded
    gen = _need(wf, ("MiniMaxH3ImageToVideo",), nodes.get("i2v"), "", "图生视频（MiniMaxH3ImageToVideo）")
    gins = wf[gen]["inputs"]
    pkey = "prompt" if "prompt" in gins and not isinstance(gins["prompt"], list) else next(
        (k for k, v in gins.items() if isinstance(v, str) and not isinstance(v, list)), None)
    if not pkey:
        raise ComfyError("图生视频节点里找不到提示词输入")
    gins[pkey] = prompt
    dur = find_node(wf, ("PrimitiveFloat", "PrimitiveInt"), nodes.get("duration"), "duration")
    if dur and "value" in wf[dur]["inputs"]:
        wf[dur]["inputs"]["value"] = float(seconds)
    _set_seed_and_prefix(wf, seed, prefix, nodes)
    return wf


# ---------------------------------------------------------------- HTTP 客户端
class Client:
    def __init__(self, base_url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 120):
        self.base = base_url.rstrip("/")
        self.headers = {"User-Agent": UA, **(headers or {})}
        self.timeout = timeout
        self.client_id = uuid.uuid4().hex
        self.log = print
        self._object_info_cache: Optional[Dict[str, Any]] = None

    def _open(self, req: urllib.request.Request):
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:8000]
            self.log(f"[ComfyUI] HTTP {exc.code} {req.get_method()} {req.full_url}\n[ComfyUI] 响应头：{dict(exc.headers)}\n[ComfyUI] 响应体（完整）：\n{detail}")
            hint = ""
            if exc.code >= 500:
                hint = "\n（500 表示 ComfyUI 服务器内部报错，真正原因在服务器控制台/Kaggle 输出的 Traceback 里；本次提交的工作流已存到 output/video/work/<镜头ID>.workflow.json）"
            raise ComfyError(f"ComfyUI HTTP {exc.code}（{req.full_url}）：{detail}{hint}") from exc
        except urllib.error.URLError as exc:
            raise ComfyError(f"连接 ComfyUI 失败（{self.base}）：{exc}；隧道地址是否已生成/仍在运行？") from exc

    def _json(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        with self._open(urllib.request.Request(self.base + path, data=data, headers=headers, method=method)) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ComfyError(f"ComfyUI 返回非 JSON（{path}）：{raw[:300]}") from exc

    def object_info(self) -> Dict[str, Any]:
        """Fetch node schemas only when needed to convert a canvas-format workflow."""
        if self._object_info_cache is None:
            data = self._json("GET", "/object_info")
            if not isinstance(data, dict) or not data:
                raise ComfyError("ComfyUI /object_info 没有返回有效节点定义")
            self._object_info_cache = data
        return self._object_info_cache

    def load_workflow(self, path: Path) -> Dict[str, Any]:
        path = Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return load_workflow(path)
        return load_workflow(path, self.object_info() if is_ui_format(raw) else None)

    def upload_image(self, path: Path) -> str:
        path = Path(path)
        boundary = "----shuohao" + uuid.uuid4().hex
        filename = f"{uuid.uuid4().hex[:8]}_{path.name}"
        chunks: List[bytes] = []

        def field(name: str, value: str) -> None:
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())

        field("type", "input")
        field("overwrite", "true")
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "application/octet-stream"
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
                       f"Content-Type: {mime}\r\n\r\n").encode())
        chunks.append(path.read_bytes())
        chunks.append(f"\r\n--{boundary}--\r\n".encode())
        headers = {**self.headers, "Content-Type": f"multipart/form-data; boundary={boundary}"}
        req = urllib.request.Request(self.base + "/upload/image", data=b"".join(chunks), headers=headers, method="POST")
        with self._open(req) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            info = json.loads(raw)
        except ValueError as exc:
            raise ComfyError(f"上传图片返回非 JSON：{raw[:300]}") from exc
        self.log(f"[ComfyUI] 上传 {path.name}（{path.stat().st_size} 字节）→ 服务器返回 {json.dumps(info, ensure_ascii=False)}")
        name = info.get("name")
        if not name:
            raise ComfyError(f"上传图片响应里没有 name：{raw[:300]}")
        sub = str(info.get("subfolder") or "").strip("/")
        return f"{sub}/{name}" if sub else str(name)

    def queue(self, workflow: Dict[str, Any]) -> str:
        self.log(f"[ComfyUI] POST {self.base}/prompt（client_id={self.client_id}，节点数={len(workflow)}）")
        data = self._json("POST", "/prompt", {"prompt": workflow, "client_id": self.client_id})
        self.log(f"[ComfyUI] /prompt 响应：{json.dumps(data, ensure_ascii=False)[:2000]}")
        pid = data.get("prompt_id") if isinstance(data, dict) else None
        if not pid:
            raise ComfyError(f"提交工作流失败，响应里没有 prompt_id：{json.dumps(data, ensure_ascii=False)[:800]}")
        return str(pid)

    def wait(self, prompt_id: str, poll: float = 10, max_wait: float = 3600) -> Dict[str, Any]:
        deadline = time.time() + max_wait
        while time.time() < deadline:
            hist = self._json("GET", f"/history/{urllib.parse.quote(prompt_id)}")
            entry = hist.get(prompt_id) if isinstance(hist, dict) else None
            self.log(f"[ComfyUI] 轮询 {prompt_id}：" + (f"status={json.dumps(entry.get('status', {}), ensure_ascii=False)[:300]}" if entry else "尚未完成（排队/执行中）"))
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    msgs = [m for m in status.get("messages", []) if isinstance(m, list) and m and m[0] == "execution_error"]
                    detail = json.dumps(msgs[-1][1], ensure_ascii=False)[:1200] if msgs else json.dumps(status, ensure_ascii=False)[:800]
                    raise ComfyError(f"ComfyUI 执行失败：{detail}")
                if entry.get("outputs"):
                    return entry["outputs"]
                if status.get("completed"):
                    raise ComfyError("工作流已完成但没有任何输出节点结果（检查 SaveVideo 节点是否被连上）")
            time.sleep(poll)
        raise ComfyError(f"等待 ComfyUI 任务 {prompt_id} 超时（{int(max_wait)} 秒）")

    def download(self, info: Dict[str, Any], target: Path) -> None:
        q = urllib.parse.urlencode({"filename": info["filename"], "subfolder": info.get("subfolder", ""), "type": info.get("type", "output")})
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            with self._open(urllib.request.Request(self.base + "/view?" + q, headers=self.headers)) as resp, tmp.open("wb") as fh:
                while True:
                    block = resp.read(1024 * 1024)
                    if not block:
                        break
                    fh.write(block)
            if tmp.stat().st_size <= 0:
                raise ComfyError("视频下载完成但文件为空")
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)


def pick_output(outputs: Dict[str, Any], save_node: Optional[str] = None) -> Dict[str, Any]:
    """从 history 的 outputs 里挑出视频文件（SaveVideo 通常放在 images 数组里）。"""
    order = ([save_node] if save_node and save_node in outputs else []) + [k for k in outputs if k != save_node]
    for node in order:
        block = outputs.get(node)
        if not isinstance(block, dict):
            continue
        for value in block.values():
            if isinstance(value, list):
                for entry in value:
                    if isinstance(entry, dict) and str(entry.get("filename", "")).lower().endswith(VIDEO_EXTS):
                        return entry
    raise ComfyError("ComfyUI 输出里找不到视频文件：" + json.dumps(outputs, ensure_ascii=False)[:600])


# ---------------------------------------------------------------- 单个镜头
def generate(item: Dict[str, Any], manifest: Dict[str, Any], cfg: Dict[str, Any], out_root: Path, work_dir: Path,
             previous_last: Optional[Path], target_base: Path, log=print) -> Tuple[Path, str]:
    """生成一个镜头，返回（下载好的视频路径, 说明）。cfg 是 models.json 里 comfyui 条目整理后的字典。"""
    client = Client(cfg["base_url"], cfg.get("headers"), cfg.get("timeout", 120))
    client.log = log
    nodes = cfg.get("nodes") or {}
    seconds = float(item["duration_seconds"])
    cap = cfg.get("max_seconds")
    if cap:
        seconds = min(seconds, float(cap))
    aspect = manifest.get("aspect_ratio") if manifest.get("aspect_ratio") in ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9") else None
    seed = random.randint(1, 2**31 - 1)
    prefix = f"shuohao/{item['id']}"
    prompt = str(item["prompt"])

    use_i2v = bool(previous_last) and bool(cfg.get("workflow_i2v"))
    if use_i2v:
        template = client.load_workflow(Path(cfg["workflow_i2v"]))
        uploaded = client.upload_image(previous_last)
        wf = build_i2v(template, prompt=prompt, seconds=seconds, uploaded=uploaded, seed=seed, prefix=prefix, nodes=nodes)
        mode, note = "i2v", "首帧=上一镜最后一帧"
    else:
        if not cfg.get("workflow_ref"):
            raise ComfyError("models.json 里没有配置 workflow_ref（Ref2VA 工作流路径）")
        template = client.load_workflow(Path(cfg["workflow_ref"]))
        refs: List[Tuple[str, Path]] = []
        if previous_last:  # 没配 i2v 工作流时，上一镜最后一帧作为第一张参考图
            refs.append(("previous", Path(previous_last)))
        for asset in item.get("reference_assets_all") or item.get("reference_assets") or []:
            if asset.get("kind") == "previous":
                continue
            p = Path(str(asset.get("path") or ""))
            p = p if p.is_absolute() else out_root / p
            if not p.is_file():
                raise FileNotFoundError(f"参考图不存在：{asset.get('path')}")
            refs.append((str(asset.get("kind") or "reference"), p))
        if not refs:
            raise ComfyError(f"镜头 {item['id']} 没有参考图（Ref2VA 至少需要一张场景图或角色图）")
        max_refs = int(cfg.get("max_refs", 6))
        refs = plan_references(refs, max_refs, work_dir / f"collage-{item['id']}.png")
        uploaded_names = [client.upload_image(p) for _k, p in refs]
        # 图片编号按上传顺序生成；只要提示词没有完整引用所有参考图，就补上角色/场景/道具对应关系。
        ref_kinds = [k for k, _p in refs]
        header = reference_header(ref_kinds)
        required_picture_refs = [f"<Picture {i}>" for i in range(1, len(refs) + 1)]
        if header and not all(tag in prompt for tag in required_picture_refs):
            prompt = header + "\n" + prompt

        wf = build_ref2va(template, prompt=prompt, seconds=seconds, aspect=aspect, size_preset=cfg.get("size_preset"),
                          uploaded=uploaded_names, seed=seed, prefix=prefix, nodes=nodes)
        mode, note = "ref2va", f"{len(refs)} 张参考图" + ("（含拼图）" if any(k == "collage" for k, _p in refs) else "")
    log(f"\n===== ComfyUI 镜头 {item['id']} | 模式={mode}（{note}）| 种子={seed} =====\n{prompt}\n===== 提示词结束 =====\n")
    log(f"[ComfyUI] 服务器：{cfg['base_url']} | 工作流：{cfg.get('workflow_i2v') if mode == 'i2v' else cfg.get('workflow_ref')} | 时长={seconds}s | 画幅={aspect} | 清晰度={cfg.get('size_preset')}")
    canvas_wf = canvas_from_api(wf)
    log(f"[ComfyUI] 生成的画布格式工作流 JSON：\n{json.dumps(canvas_wf, ensure_ascii=False, indent=1)}\n[ComfyUI] 画布工作流 JSON 结束")
    try:  # 保存画布/UI 格式工作流，参数按当前镜头动态填充
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / f"{item['id']}.workflow.json").write_text(json.dumps(canvas_wf, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    # /prompt 只接收 API 格式；画布格式作为生成记录保存。
    prompt_id = client.queue(wf)
    item["task_id"] = prompt_id
    outputs = client.wait(prompt_id, float(cfg.get("poll", 10)), float(cfg.get("max_wait", 3600)))
    save_id = find_node(wf, ("SaveVideo", "VHS_VideoCombine", "SaveAnimatedWEBP"), nodes.get("save"))
    info = pick_output(outputs, save_id)
    target = target_base.with_suffix(Path(info["filename"]).suffix or ".mp4")
    client.download(info, target)
    return target, f"{mode}：{note}"
