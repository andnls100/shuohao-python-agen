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
WORKFLOW_PATH = SCRIPT_DIR / "image_sdxl_simple.json"
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
    """读取工作流；自动把 ComfyUI 画布 JSON 转成 /prompt 所需的 API 格式。"""
    if not WORKFLOW_PATH.is_file():
        raise FileNotFoundError(f"找不到工作流文件：{WORKFLOW_PATH}")

    data = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and isinstance(data.get("nodes"), list):
        nodes = {
            str(node["id"]): node
            for node in data["nodes"]
            if "id" in node and node.get("type") != "MarkdownNote"
        }
        links = {
            link[0]: link
            for link in data.get("links", [])
            if isinstance(link, list) and len(link) >= 6
        }
        workflow = {}
        for node_id, node in nodes.items():
            api_inputs = {}
            for port in node.get("inputs", []):
                name, link_id = port.get("name"), port.get("link")
                if name and link_id in links:
                    link = links[link_id]
                    api_inputs[name] = [str(link[1]), int(link[2])]

            named = node.get("widgets_values_named") or {}
            widget_names = [
                port.get("widget", {}).get("name")
                for port in node.get("inputs", [])
                if port.get("widget", {}).get("name")
            ]
            values = node.get("widgets_values", [])
            for index, name in enumerate(widget_names):
                if name in named:
                    api_inputs[name] = named[name]
                elif index < len(values):
                    api_inputs[name] = values[index]

            # KSampler 画布数据含 control_after_generate，API prompt 不接受此字段。
            if node.get("type") == "KSampler":
                for key in ("seed", "steps", "cfg", "sampler_name", "scheduler", "denoise"):
                    if key in named:
                        api_inputs[key] = named[key]
                api_inputs.pop("control_after_generate", None)

            api_node = {"class_type": node["type"], "inputs": api_inputs}
            if node.get("title"):
                api_node["_meta"] = {"title": node["title"]}
            workflow[node_id] = api_node
    else:
        # 也兼容原生 API prompt JSON。
        workflow = data.get("prompt", data) if isinstance(data, dict) else data

    if not isinstance(workflow, dict) or not workflow:
        raise RuntimeError(f"不是有效的 ComfyUI 工作流 JSON：{WORKFLOW_PATH}")

    text_nodes = []
    for node in workflow.values():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        node_type, inputs = node.get("class_type", ""), node["inputs"]
        title = str(node.get("_meta", {}).get("title", "")).strip().lower()
        if node_type == "CLIPTextEncode" and "text" in inputs:
            text_nodes.append((title, inputs))
        elif node_type in ("EmptyLatentImage", "EmptySD3LatentImage"):
            inputs.update({"width": WIDTH, "height": HEIGHT})
        elif node_type == "KSampler":
            inputs.update({"steps": STEPS, "cfg": CFG, "seed": SEED})

    if not text_nodes:
        raise RuntimeError("工作流中没有找到 CLIPTextEncode 节点。")

    positive = next((inputs for title, inputs in text_nodes if "positive" in title or "正面" in title or "正向" in title), None)
    negative = next((inputs for title, inputs in text_nodes if "negative" in title or "负面" in title or "负向" in title), None)
    if positive is None:
        positive = text_nodes[0][1]
    positive["text"] = PROMPT
    if negative is None and len(text_nodes) > 1:
        negative = text_nodes[1][1]
    if negative is not None:
        negative["text"] = NEGATIVE_PROMPT

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
