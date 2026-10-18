#!/usr/bin/env python3
"""ComfyUI 文生图冒烟测试：读取同目录 sdxl_api.json 并按节点标题替换提示词。"""
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

COMFY_URL = "http://127.0.0.1:8188"
CLIENT_ID = "short_drama_batch"

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
SEED = int(time.time()) % 2**32
TIMEOUT_SECONDS = 1200
POLL_SECONDS = 2

SCRIPT_DIR = Path(__file__).resolve().parent
WORKFLOW_PATH = SCRIPT_DIR / "sdxl_api.json"
OUTPUT_DIR = SCRIPT_DIR.parent / "output" / "comfyui-test"


def auth_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    user, password = os.getenv("COMFY_USER"), os.getenv("COMFY_PASS")
    if user:
        token = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    return headers


def request_json(path: str, method: str = "GET", body: dict | None = None) -> dict:
    url = COMFY_URL.rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=auth_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        raise RuntimeError(f"ComfyUI HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 ComfyUI：{url}；请确认服务正在运行。原因：{exc}") from exc


def load_workflow() -> dict:
    if not WORKFLOW_PATH.is_file():
        raise FileNotFoundError(f"找不到工作流文件：{WORKFLOW_PATH}")

    data = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8-sig"))
    workflow = data.get("prompt", data) if isinstance(data, dict) else data
    if not isinstance(workflow, dict) or not workflow:
        raise RuntimeError(f"不是有效的 ComfyUI API 格式工作流：{WORKFLOW_PATH}")

    found_positive = False
    found_negative = False
    for node_id, node in workflow.items():
        if not isinstance(node, dict) or node.get("class_type") != "CLIPTextEncode":
            continue
        inputs = node.get("inputs", {})
        if "text" not in inputs:
            continue
        title = str(node.get("_meta", {}).get("title", "")).strip()
        if title == "正面提示词":
            inputs["text"] = PROMPT
            found_positive = True
        elif title == "负面提示词":
            inputs["text"] = NEGATIVE_PROMPT
            found_negative = True

    if not found_positive or not found_negative:
        missing = []
        if not found_positive:
            missing.append("正面提示词")
        if not found_negative:
            missing.append("负面提示词")
        raise RuntimeError(
            "工作流缺少以下 CLIPTextEncode 节点标题：" + "、".join(missing)
            + "。请在 ComfyUI 工作流中设置对应标题并重新导出 API 格式 JSON。"
        )

    # 更新工作流中存在的常用生成参数，不依赖具体模型文件名。
    for node in workflow.values():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        node_type = node.get("class_type", "")
        inputs = node["inputs"]
        if node_type in ("EmptyLatentImage", "EmptySD3LatentImage"):
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

    print(f"已加载工作流：{WORKFLOW_PATH}")
    print(f"工作流节点数：{len(workflow)}")
    return workflow


def download_image(image_info: dict) -> Path:
    query = urllib.parse.urlencode({
        "filename": image_info["filename"],
        "subfolder": image_info.get("subfolder", ""),
        "type": image_info.get("type", "output"),
    })
    url = COMFY_URL.rstrip("/") + "/view?" + query
    req = urllib.request.Request(url, headers=auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            content = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1500]
        raise RuntimeError(f"下载图片失败，HTTP {exc.code}: {detail}") from exc

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(image_info["filename"]).suffix or ".png"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = OUTPUT_DIR / f"test_{timestamp}{suffix}"
    target.write_bytes(content)
    print(f"已保存：{target.resolve()}")
    return target


def main() -> None:
    print(f"ComfyUI: {COMFY_URL}")
    print(f"尺寸: {WIDTH}x{HEIGHT} | steps={STEPS} | seed={SEED}")
    print(f"提示词: {PROMPT}")

    workflow = load_workflow()
    submitted = request_json(
        "/prompt",
        "POST",
        {"prompt": workflow, "client_id": CLIENT_ID},
    )
    prompt_id = submitted.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI 未返回 prompt_id：{json.dumps(submitted, ensure_ascii=False)}")
    print(f"已提交任务：{prompt_id}")

    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        history = request_json("/history/" + urllib.parse.quote(prompt_id, safe=""))
        task = history.get(prompt_id)
        if not task:
            time.sleep(POLL_SECONDS)
            continue

        status = task.get("status", {})
        if status.get("status_str") == "error":
            raise RuntimeError(
                "ComfyUI 执行失败：" + json.dumps(status.get("messages", []), ensure_ascii=False)[:2000]
            )

        images = [
            image
            for node_output in task.get("outputs", {}).values()
            for image in node_output.get("images", [])
        ]
        if images:
            for image_info in images:
                download_image(image_info)
            return
        time.sleep(POLL_SECONDS)

    raise TimeoutError(f"等待 ComfyUI 超过 {TIMEOUT_SECONDS} 秒，任务 ID：{prompt_id}")


if __name__ == "__main__":
    main()
