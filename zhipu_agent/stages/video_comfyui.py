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
_CANVAS_TEMPLATE_JSON = "{\"id\":\"f55e764b-3b29-4619-b5f5-e9261e58d393\",\"revision\":0,\"config\":{},\"last_node_id\":1,\"last_link_id\":48,\"nodes\":[{\"id\":\"unet\",\"type\":\"KaggleH3ShardedDiffusionLoader\",\"pos\":[100,130],\"size\":[425.5845703125,178],\"flags\":{},\"order\":0,\"mode\":0,\"inputs\":[],\"outputs\":[{\"name\":\"MODEL\",\"type\":\"MODEL\",\"links\":[28]}],\"properties\":{\"Node name for S&R\":\"KaggleH3ShardedDiffusionLoader\"},\"widgets_values\":[\"Ref2VA\",\"singularity\",\"int8_convrot\",\"default\",0,1],\"title\":\"Regular Video Workflow | Singularity Ref2VA Auto Loader\",\"widgets_values_named\":{\"model_variant\":\"Ref2VA\",\"model_profile\":\"singularity\",\"precision\":\"int8_convrot\",\"weight_dtype\":\"default\",\"gpu_0\":0,\"gpu_1\":1}},{\"id\":\"clip\",\"type\":\"KaggleH3TextEncoderLoader\",\"pos\":[100,438],\"size\":[374.144140625,106],\"flags\":{},\"order\":1,\"mode\":0,\"inputs\":[],\"outputs\":[{\"name\":\"CLIP\",\"type\":\"CLIP\",\"links\":[25]}],\"properties\":{\"Node name for S&R\":\"KaggleH3TextEncoderLoader\"},\"widgets_values\":[\"qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors\",\"minimax\",0],\"title\":\"Regular Video Workflow | Text Encoder (GPU0)\",\"widgets_values_named\":{\"clip_name\":\"qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors\",\"type\":\"minimax\",\"device_id\":0}},{\"id\":\"vae_video\",\"type\":\"KaggleH3VAELoader\",\"pos\":[100,674],\"size\":[330.2017578125,106],\"flags\":{},\"order\":2,\"mode\":0,\"inputs\":[],\"outputs\":[{\"name\":\"VAE\",\"type\":\"VAE\",\"links\":[26,41]}],\"properties\":{\"Node name for S&R\":\"KaggleH3VAELoader\"},\"widgets_values\":[\"minimax_h3_video_vae_fp16.safetensors\",\"video\",1],\"title\":\"Regular Video Workflow | Video VAE (GPU1)\",\"widgets_values_named\":{\"vae_name\":\"minimax_h3_video_vae_fp16.safetensors\",\"role\":\"video\",\"device_id\":1}},{\"id\":\"vae_audio\",\"type\":\"KaggleH3VAELoader\",\"pos\":[100,910],\"size\":[333.4556640625,106],\"flags\":{},\"order\":3,\"mode\":0,\"inputs\":[],\"outputs\":[{\"name\":\"VAE\",\"type\":\"VAE\",\"links\":[27,44]}],\"properties\":{\"Node name for S&R\":\"KaggleH3VAELoader\"},\"widgets_values\":[\"minimax_h3_audio_vae_fp32.safetensors\",\"audio\",0],\"title\":\"Regular Video Workflow | Audio VAE (GPU0)\",\"widgets_values_named\":{\"vae_name\":\"minimax_h3_audio_vae_fp32.safetensors\",\"role\":\"audio\",\"device_id\":0}},{\"id\":\"h3_adapter\",\"type\":\"KaggleH3AdapterStack\",\"pos\":[625.5845703125,130],\"size\":[481.9322265625,294],\"flags\":{},\"order\":5,\"mode\":0,\"inputs\":[{\"name\":\"model\",\"type\":\"MODEL\",\"link\":28}],\"outputs\":[{\"name\":\"MODEL\",\"type\":\"MODEL\",\"links\":[29]},{\"name\":\"H3_RUNTIME_CONFIG\",\"type\":\"H3_RUNTIME_CONFIG\",\"links\":[30]}],\"properties\":{\"Node name for S&R\":\"KaggleH3AdapterStack\"},\"widgets_values\":[\"ref2va\",true,4,\"auto_singularity_ref2va_turbo\",1,\"None\",1,\"None\",1,\"Ref2VA\"],\"title\":\"Regular Video Workflow | Matching Singularity Ref2VA Turbo LoRA\",\"widgets_values_named\":{\"model_variant\":\"ref2va\",\"turbo_mode\":true,\"turbo_steps\":4,\"adapter_1\":\"auto_singularity_ref2va_turbo\",\"strength_1\":1,\"adapter_2\":\"None\",\"strength_2\":1,\"adapter_3\":\"None\",\"strength_3\":1,\"conditioning_mode\":\"Ref2VA\"}},{\"id\":\"phase\",\"type\":\"KaggleH3PhaseDispatch\",\"pos\":[1263.85859375,130],\"size\":[411.269140625,86],\"flags\":{},\"order\":7,\"mode\":0,\"inputs\":[{\"name\":\"model\",\"type\":\"MODEL\",\"link\":29},{\"name\":\"conditioning\",\"type\":\"CONDITIONING\",\"link\":31},{\"name\":\"latent_image\",\"type\":\"LATENT\",\"link\":32},{\"name\":\"runtime_config\",\"type\":\"H3_RUNTIME_CONFIG\",\"link\":30,\"shape\":7}],\"outputs\":[{\"name\":\"MODEL\",\"type\":\"MODEL\",\"links\":[33,37]},{\"name\":\"conditioning\",\"type\":\"CONDITIONING\",\"links\":[35]},{\"name\":\"latent_image\",\"type\":\"LATENT\",\"links\":[36]},{\"name\":\"H3_RUNTIME_CONFIG\",\"type\":\"H3_RUNTIME_CONFIG\",\"links\":[34,39]}],\"properties\":{\"Node name for S&R\":\"KaggleH3PhaseDispatch\"},\"title\":\"Regular Video Workflow | Dual-T4 Transformer Dispatch\"},{\"id\":\"sample\",\"type\":\"KaggleH3TurboSampler\",\"pos\":[1677.4490234374998,130],\"size\":[540.2291015625,238],\"flags\":{},\"order\":8,\"mode\":0,\"inputs\":[{\"name\":\"model\",\"type\":\"MODEL\",\"link\":33},{\"name\":\"conditioning\",\"type\":\"CONDITIONING\",\"link\":35},{\"name\":\"latent_image\",\"type\":\"LATENT\",\"link\":36},{\"name\":\"runtime_config\",\"type\":\"H3_RUNTIME_CONFIG\",\"link\":34,\"shape\":7}],\"outputs\":[{\"name\":\"video_latent\",\"type\":\"LATENT\",\"links\":[38]},{\"name\":\"audio_latent\",\"type\":\"LATENT\",\"links\":[43]},{\"name\":\"denoised_output\",\"type\":\"LATENT\",\"links\":null}],\"properties\":{\"Node name for S&R\":\"KaggleH3TurboSampler\"},\"widgets_values\":[292794338190016,\"randomize\",\"euler\",20,\"safe\",false],\"title\":\"Regular Video Workflow | Choose Euler or Euler Dual-clock + SageAttention\",\"widgets_values_named\":{\"noise_seed\":292794338190016,\"control_after_generate\":\"randomize\",\"sampler_name\":\"euler\",\"steps\":20,\"synchronize_mode\":\"safe\",\"use_sage_attention\":false}},{\"id\":\"upscale\",\"type\":\"KaggleH3LatentUpscale2x\",\"pos\":[2061.0837890625,130],\"size\":[493.28671875,170],\"flags\":{},\"order\":9,\"mode\":0,\"inputs\":[{\"name\":\"model\",\"type\":\"MODEL\",\"link\":37},{\"name\":\"samples\",\"type\":\"LATENT\",\"link\":38},{\"name\":\"runtime_config\",\"type\":\"H3_RUNTIME_CONFIG\",\"link\":39,\"shape\":7}],\"outputs\":[{\"name\":\"upscaled_latent\",\"type\":\"LATENT\",\"links\":[40]},{\"name\":\"original_latent\",\"type\":\"LATENT\",\"links\":null},{\"name\":\"H3_RUNTIME_CONFIG\",\"type\":\"H3_RUNTIME_CONFIG\",\"links\":[42,45]}],\"properties\":{\"Node name for S&R\":\"KaggleH3LatentUpscale2x\"},\"widgets_values\":[true,\"360p\",\"16:9\",\"bicubic\"],\"title\":\"Regular Video Workflow | Optional 2x Latent Upscale (360p to 720p)\",\"widgets_values_named\":{\"enabled\":true,\"source_size_preset\":\"360p\",\"aspect_ratio\":\"16:9\",\"method\":\"bicubic\"}},{\"id\":\"decode\",\"type\":\"KaggleH3VAEDecode\",\"pos\":[2520.8177734375,130],\"size\":[355.2837890625,98],\"flags\":{},\"order\":10,\"mode\":0,\"inputs\":[{\"name\":\"samples\",\"type\":\"LATENT\",\"link\":40},{\"name\":\"vae\",\"type\":\"VAE\",\"link\":41},{\"name\":\"runtime_config\",\"type\":\"H3_RUNTIME_CONFIG\",\"link\":42,\"shape\":7}],\"outputs\":[{\"name\":\"IMAGE\",\"type\":\"IMAGE\",\"links\":[46]}],\"properties\":{\"Node name for S&R\":\"KaggleH3VAEDecode\"},\"widgets_values\":[1],\"title\":\"Regular Video Workflow | Video Decode (GPU1)\",\"widgets_values_named\":{\"device_id\":1}},{\"id\":\"decode_audio\",\"type\":\"KaggleH3AudioVAEDecode\",\"pos\":[2520.8177734375,358],\"size\":[419.8140625,122],\"flags\":{},\"order\":11,\"mode\":0,\"inputs\":[{\"name\":\"samples\",\"type\":\"LATENT\",\"link\":43},{\"name\":\"vae\",\"type\":\"VAE\",\"link\":44},{\"name\":\"runtime_config\",\"type\":\"H3_RUNTIME_CONFIG\",\"link\":45,\"shape\":7}],\"outputs\":[{\"name\":\"AUDIO\",\"type\":\"AUDIO\",\"links\":[47]}],\"properties\":{\"Node name for S&R\":\"KaggleH3AudioVAEDecode\"},\"widgets_values\":[0,false],\"title\":\"Regular Video Workflow | Optional Mute Generated Audio\",\"widgets_values_named\":{\"device_id\":0,\"mute_generated_audio\":false}},{\"id\":\"video\",\"type\":\"CreateVideo\",\"pos\":[2979.35546875,130],\"size\":[433.3013671875,126],\"flags\":{},\"order\":12,\"mode\":0,\"inputs\":[{\"name\":\"images\",\"type\":\"IMAGE\",\"link\":46},{\"name\":\"audio\",\"type\":\"AUDIO\",\"link\":47,\"shape\":7}],\"outputs\":[{\"name\":\"VIDEO\",\"type\":\"VIDEO\",\"links\":[48]}],\"properties\":{\"Node name for S&R\":\"CreateVideo\"},\"widgets_values\":[24,8,\"sRGB\"],\"title\":\"Regular Video Workflow | Assemble Video + Optional Audio\",\"widgets_values_named\":{\"fps\":24,\"bit_depth\":8,\"color_space\":\"sRGB\"}},{\"id\":\"save\",\"type\":\"SaveVideo\",\"pos\":[3349.35546875,130],\"size\":[278.624609375,106],\"flags\":{},\"order\":13,\"mode\":0,\"inputs\":[{\"name\":\"video\",\"type\":\"VIDEO\",\"link\":48}],\"outputs\":[{\"name\":\"video\",\"type\":\"VIDEO\",\"links\":null}],\"properties\":{},\"widgets_values\":[\"kaggle_h3/regular_video\",\"mp4\",\"auto\",\"auto\"],\"title\":\"Regular Video Workflow | Save MP4\",\"widgets_values_named\":{\"filename_prefix\":\"kaggle_h3/regular_video\",\"format\":\"mp4\",\"format.codec\":\"auto\",\"codec\":\"auto\"}},{\"id\":\"step_count\",\"type\":\"FirstInt (obvpm)\",\"pos\":[100,1146],\"size\":[379.0171875,98],\"flags\":{},\"order\":4,\"mode\":0,\"inputs\":[{\"name\":\"int1\",\"type\":\"INT\",\"link\":null,\"shape\":7},{\"name\":\"int2\",\"type\":\"INT\",\"link\":null,\"shape\":7},{\"name\":\"int3\",\"type\":\"INT\",\"link\":null,\"shape\":7}],\"outputs\":[{\"name\":\"int\",\"type\":\"INT\",\"links\":null}],\"properties\":{\"Node name for S&R\":\"FirstInt (obvpm)\"},\"widgets_values\":[4],\"title\":\"Regular Video Workflow | Shared Turbo Step Count\",\"widgets_values_named\":{\"fallback\":4}},{\"id\":\"h3\",\"type\":\"KaggleH3Conditioning\",\"pos\":[623.3845703124999,682.6999999999999],\"size\":[538.2740234375,968],\"flags\":{},\"order\":6,\"mode\":0,\"inputs\":[{\"name\":\"clip\",\"type\":\"CLIP\",\"link\":25},{\"name\":\"vae\",\"type\":\"VAE\",\"link\":26,\"shape\":7},{\"name\":\"audio_vae\",\"type\":\"VAE\",\"link\":27,\"shape\":7},{\"name\":\"start_frame\",\"type\":\"IMAGE\",\"link\":null,\"shape\":7},{\"name\":\"end_frame\",\"type\":\"IMAGE\",\"link\":null,\"shape\":7},{\"name\":\"ref_images.ref_image_0\",\"type\":\"IMAGE\",\"link\":null,\"label\":\"ref_image_0\",\"shape\":7},{\"name\":\"ref_videos.ref_video_0\",\"type\":\"IMAGE\",\"link\":null,\"label\":\"ref_video_0\",\"shape\":7},{\"name\":\"ref_video_audios.ref_video_audio_0\",\"type\":\"AUDIO\",\"link\":null,\"label\":\"ref_video_audio_0\",\"shape\":7},{\"name\":\"ref_audios.ref_audio_0\",\"type\":\"AUDIO\",\"link\":null,\"label\":\"ref_audio_0\",\"shape\":7}],\"outputs\":[{\"name\":\"positive\",\"type\":\"CONDITIONING\",\"links\":[31]},{\"name\":\"LATENT\",\"type\":\"LATENT\",\"links\":[32]}],\"properties\":{\"Node name for S&R\":\"KaggleH3Conditioning\"},\"widgets_values\":[\"镜头小幅缓推向<Picture 5>。<Picture 1>与<Picture 2>身着<Picture 3>，被粗麻绳反绑双臂，在两名<Picture 4>推搡下踉跄跌向滑道。<Picture 1>双脚踩空，身体前倾失去平衡，<Picture 2>紧随其后。以画外音说（唇形完全闭合）远处传来<Picture 4>的呵斥声。\\\\n[镜头 2] 于 00:05.000，镜头切换至近景特写。一名红衣年轻女子（<Picture 2>）死死攥住师兄的袖口，指甲掐进布料。她抬起头，眼神从慌乱转为决绝，看向师兄。她低声回应：<d>[Chinese] 师兄别喊了，他们听不懂人话。</d>\\\\n\\\\n整体音景：火焰燃烧的噼啪声，衣物摩擦声，沉重的脚步声，粗重的喘息声。\\\\n\\\\n非叙事配乐：低沉的大提琴弦乐，节奏紧张缓慢。\\\"  优化下这个提示词  真人感\",\"Ref2VA\",5,\"360p\",\"16:9\",\"match\",\"None\",\"None\",\"image\",\"3e813aa9_燕溪山-C02-sheet.png\",\"image\",\"97d7ed0d_徐今朝-C03-sheet.png\",\"image\",\"25f75664_婚服-P01-sheet.png\",\"image\",\"2741df8e_江边村民-C09-sheet.png\",\"image\",\"1766cdda_江边祭坛-sheet.png\",\"none\",\"none\",\"none\",\"none\",\"none\",\"none\",\"none\",\"none\",\"none\",\"none\",0,0,0],\"title\":\"Regular Video Workflow | Choose Prompt, References, Resolution, Duration\",\"widgets_values_named\":{\"prompt\":\"镜头小幅缓推向<Picture 5>。<Picture 1>与<Picture 2>身着<Picture 3>，被粗麻绳反绑双臂，在两名<Picture 4>推搡下踉跄跌向滑道。<Picture 1>双脚踩空，身体前倾失去平衡，<Picture 2>紧随其后。以画外音说（唇形完全闭合）远处传来<Picture 4>的呵斥声。\\\\n[镜头 2] 于 00:05.000，镜头切换至近景特写。一名红衣年轻女子（<Picture 2>）死死攥住师兄的袖口，指甲掐进布料。她抬起头，眼神从慌乱转为决绝，看向师兄。她低声回应：<d>[Chinese] 师兄别喊了，他们听不懂人话。</d>\\\\n\\\\n整体音景：火焰燃烧的噼啪声，衣物摩擦声，沉重的脚步声，粗重的喘息声。\\\\n\\\\n非叙事配乐：低沉的大提琴弦乐，节奏紧张缓慢。\\\"  优化下这个提示词  真人感\",\"mode\":\"Ref2VA\",\"seconds\":5,\"size_preset\":\"360p\",\"aspect_ratio\":\"16:9\",\"ref_image_size\":\"match\",\"start_frame_file\":\"None\",\"end_frame_file\":\"None\",\"reference_0\":\"image\",\"reference_0.file\":\"3e813aa9_燕溪山-C02-sheet.png\",\"reference_1\":\"image\",\"reference_1.file\":\"97d7ed0d_徐今朝-C03-sheet.png\",\"reference_2\":\"image\",\"reference_2.file\":\"25f75664_婚服-P01-sheet.png\",\"reference_3\":\"image\",\"reference_3.file\":\"2741df8e_江边村民-C09-sheet.png\",\"reference_4\":\"image\",\"reference_4.file\":\"1766cdda_江边祭坛-sheet.png\",\"reference_5\":\"none\",\"reference_6\":\"none\",\"reference_7\":\"none\",\"reference_8\":\"none\",\"reference_9\":\"none\",\"reference_10\":\"none\",\"reference_11\":\"none\",\"reference_12\":\"none\",\"reference_13\":\"none\",\"reference_14\":\"none\",\"width\":0,\"height\":0,\"length\":0}}],\"links\":[[25,\"clip\",0,\"h3\",0,\"CLIP\"],[26,\"vae_video\",0,\"h3\",1,\"VAE\"],[27,\"vae_audio\",0,\"h3\",2,\"VAE\"],[28,\"unet\",0,\"h3_adapter\",0,\"MODEL\"],[29,\"h3_adapter\",0,\"phase\",0,\"MODEL\"],[30,\"h3_adapter\",1,\"phase\",3,\"H3_RUNTIME_CONFIG\"],[31,\"h3\",0,\"phase\",1,\"CONDITIONING\"],[32,\"h3\",1,\"phase\",2,\"LATENT\"],[33,\"phase\",0,\"sample\",0,\"MODEL\"],[34,\"phase\",3,\"sample\",3,\"H3_RUNTIME_CONFIG\"],[35,\"phase\",1,\"sample\",1,\"CONDITIONING\"],[36,\"phase\",2,\"sample\",2,\"LATENT\"],[37,\"phase\",0,\"upscale\",0,\"MODEL\"],[38,\"sample\",0,\"upscale\",1,\"LATENT\"],[39,\"phase\",3,\"upscale\",2,\"H3_RUNTIME_CONFIG\"],[40,\"upscale\",0,\"decode\",0,\"LATENT\"],[41,\"vae_video\",0,\"decode\",1,\"VAE\"],[42,\"upscale\",2,\"decode\",2,\"H3_RUNTIME_CONFIG\"],[43,\"sample\",1,\"decode_audio\",0,\"LATENT\"],[44,\"vae_audio\",0,\"decode_audio\",1,\"VAE\"],[45,\"upscale\",2,\"decode_audio\",2,\"H3_RUNTIME_CONFIG\"],[46,\"decode\",0,\"video\",0,\"IMAGE\"],[47,\"decode_audio\",0,\"video\",1,\"AUDIO\"],[48,\"video\",0,\"save\",0,\"VIDEO\"]],\"groups\":[],\"extra\":{\"ds\":{\"scale\":0.8264462809917354,\"offset\":[318.6216155616296,-371.0274285724452]},\"frontendVersion\":\"1.49.6\"},\"version\":0.4}"

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
    # Produce a 720p result through the workflow’s 2x latent upscaler.
    upscale_id = find_node(wf, ("KaggleH3LatentUpscale2x",), nodes.get("upscale"))
    if upscale_id:
        upscale_inputs = wf[upscale_id]["inputs"]
        upscale_inputs["enabled"] = True
        upscale_inputs["source_size_preset"] = "360p"
        if aspect:
            upscale_inputs["aspect_ratio"] = aspect
        # The upscaler expects 360p latent input and doubles it to 720p.
        ins["size_preset"] = "360p"
    elif size_preset:
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
