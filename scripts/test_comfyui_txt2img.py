#!/usr/bin/env python3
"""ComfyUI 本地文生图冒烟测试。

读取同目录的 image_sdxl_simple.json（ComfyUI API 格式工作流），替换提示词和常用生成参数后提交给远程 ComfyUI。
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


def load_workflow() -> dict:
    """读取与本脚本同目录的 ComfyUI API 格式工作流，并更新测试参数。"""
    workflow_path = Path(__file__).resolve().with_name("image_sdxl_simple.json")
    if not workflow_path.is_file():
        raise FileNotFoundError(f"找不到工作流文件：{workflow_path}")

    data = json.loads(workflow_path.read_text(encoding="utf-8-sig"))
    # 兼容文件直接保存 API prompt，或外层包含 prompt 字段的格式。
    workflow = data.get("prompt", data) if isinstance(data, dict) else data
    if not isinstance(workflow, dict) or not workflow:
        raise RuntimeError(f"工作流不是有效的 ComfyUI API 格式：{workflow_path}")

    text_nodes = []
    for node in workflow.values():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        node_type = node.get("class_type", "")
        inputs = node["inputs"]
        if node_type == "CLIPTextEncode" and "text" in inputs:
            text_nodes.append(inputs)
        elif node_type in ("EmptySD3LatentImage", "EmptyLatentImage"):
            if "width" in inputs:
                inputs["width"] = WIDTH
            if "height" in inputs:
                inputs["height"] = HEIGHT
        elif node_type == "KSampler":
            if "steps" in inputs:
                inputs["steps"] = STEPS
            if "cfg" in inputs:
                inputs["cfg"] = CFG
            if "seed" in inputs:
                inputs["seed"] = SEED

    if not text_nodes:
        raise RuntimeError(
            "工作流中没有找到 CLIPTextEncode 节点。请使用 ComfyUI 导出的 API 格式 JSON。"
        )
    # 常见 API 工作流中第一个 CLIPTextEncode 是正向提示词，第二个是负向提示词。
    text_nodes[0]["text"] = PROMPT
    if len(text_nodes) > 1:
        text_nodes[1]["text"] = NEGATIVE_PROMPT

    print(f"已加载工作流：{workflow_path}")
    print(f"工作流节点数：{len(workflow)}")
    return workflow


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
