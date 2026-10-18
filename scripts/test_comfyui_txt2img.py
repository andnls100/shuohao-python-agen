#!/usr/bin/env python3
"""ComfyUI 本地文生图冒烟测试。

通过远程 ComfyUI HTTP API 提交工作流。模型文件由 ComfyUI 服务端管理，脚本会从服务端自动发现可用模型。
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# -------------------- 可直接修改的测试参数 --------------------
COMFY_URL = "http://127.0.0.1:8188"
PROMPT = (
    "电影级写实风格，古代中国客栈内部，夜晚，暖色油灯照明，"
    "一名身穿深蓝色长袍的年轻男子坐在木桌旁，神情警觉，"
    "中景构图，真实皮肤纹理，细节丰富，专业电影摄影，无文字，无水印"
)
NEGATIVE_PROMPT = "低质量，模糊，畸形手指，多余肢体，文字，水印，过曝"
WIDTH = 1152
HEIGHT = 896
STEPS = 20
CFG = 2.5
SHIFT = 3.1
SEED = int(time.time()) % 2**32
TIMEOUT_SECONDS = 1200
POLL_SECONDS = 2


OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "comfyui-test"
FILENAME_PREFIX = "comfyui-test"
# ------------------------------------------------------------


def request_json(path: str, method: str = "GET", body: dict | None = None) -> dict:
    url = COMFY_URL.rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    user, password = os.getenv("COMFY_USER"), os.getenv("COMFY_PASS")
    if user:
        token = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1500]
        raise RuntimeError(f"ComfyUI HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 ComfyUI：{url}；请确认 ComfyUI 正在运行。原因：{exc}") from exc


def discover_models() -> tuple[str, str, str]:
    """从远程 ComfyUI 的 object_info 中发现模型文件名；模型由服务端加载。"""
    info = request_json("/object_info")
    try:
        unets = info["UNETLoader"]["input"]["required"]["unet_name"][0]
        clips = info["CLIPLoader"]["input"]["required"]["clip_name"][0]
        vaes = info["VAELoader"]["input"]["required"]["vae_name"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("远程 ComfyUI 未提供标准模型列表，请确认服务端工作流节点可用") from exc

    def choose(options, keywords, label):
        matches = [name for name in options if all(k in name.lower() for k in keywords)]
        if not matches:
            raise RuntimeError(f"远程 ComfyUI 找不到匹配的 {label} 模型；请检查服务端安装的模型类型。")
        return matches[0]

    unet = choose(unets, ("qwen", "image"), "Qwen Image UNET")
    clip = choose(clips, ("qwen",), "Qwen CLIP")
    vae = choose(vaes, ("qwen", "image"), "Qwen Image VAE")
    print(f"远程服务端模型：UNET={unet} | CLIP={clip} | VAE={vae}")
    return unet, clip, vae


def build_workflow(unet_name: str, clip_name: str, vae_name: str) -> dict:
    return {
        "unet": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": unet_name, "weight_dtype": "default"},
        },
        "clip": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": clip_name, "type": "qwen_image", "device": "default"},
        },
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}},
        "sampling": {
            "class_type": "ModelSamplingAuraFlow",
            "inputs": {"model": ["unet", 0], "shift": SHIFT, "sampling": "flow"},
        },
        "positive": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["clip", 0], "text": PROMPT},
        },
        "negative": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["clip", 0], "text": NEGATIVE_PROMPT},
        },
        "latent": {
            "class_type": "EmptySD3LatentImage",
            "inputs": {"width": WIDTH, "height": HEIGHT, "batch_size": 1},
        },
        "sampler": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["sampling", 0],
                "positive": ["positive", 0],
                "negative": ["negative", 0],
                "latent_image": ["latent", 0],
                "seed": SEED,
                "steps": STEPS,
                "cfg": CFG,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
            },
        },
        "decode": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["sampler", 0], "vae": ["vae", 0]},
        },
        "save": {
            "class_type": "SaveImage",
            "inputs": {"images": ["decode", 0], "filename_prefix": FILENAME_PREFIX},
        },
    }


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"ComfyUI: {COMFY_URL}")
    print(f"尺寸: {WIDTH}x{HEIGHT} | steps={STEPS} | seed={SEED}")
    print(f"提示词: {PROMPT}")

    unet_name, clip_name, vae_name = discover_models()
    workflow = build_workflow(unet_name, clip_name, vae_name)
    submitted = request_json("/prompt", "POST", {"prompt": workflow})
    prompt_id = submitted.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI 未返回 prompt_id：{submitted}")
    print(f"任务已提交：{prompt_id}")

    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        history = request_json("/history/" + urllib.parse.quote(prompt_id, safe=""))
        task = history.get(prompt_id)
        if not task:
            time.sleep(POLL_SECONDS)
            continue

        status = task.get("status", {})
        if status.get("status_str") == "error" or status.get("completed") is False and status.get("messages"):
            messages = status.get("messages", [])
            raise RuntimeError("ComfyUI 执行失败：" + json.dumps(messages, ensure_ascii=False)[:2000])

        images = [
            image
            for node_output in task.get("outputs", {}).values()
            for image in node_output.get("images", [])
        ]
        if images:
            image = images[0]
            query = urllib.parse.urlencode({
                "filename": image["filename"],
                "subfolder": image.get("subfolder", ""),
                "type": image.get("type", "output"),
            })
            url = COMFY_URL.rstrip("/") + "/view?" + query
            req = urllib.request.Request(url)
            user, password = os.getenv("COMFY_USER"), os.getenv("COMFY_PASS")
            if user:
                token = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
                req.add_header("Authorization", f"Basic {token}")
            with urllib.request.urlopen(req, timeout=300) as response:
                data = response.read()
            suffix = Path(image["filename"]).suffix or ".png"
            target = OUTPUT_DIR / f"test_{datetime.now():%Y%m%d_%H%M%S}{suffix}"
            target.write_bytes(data)
            print(f"OK：图片已保存到 {target.resolve()}")
            return
        time.sleep(POLL_SECONDS)

    raise TimeoutError(f"等待 ComfyUI 超过 {TIMEOUT_SECONDS} 秒，任务 ID：{prompt_id}")


if __name__ == "__main__":
    main()
