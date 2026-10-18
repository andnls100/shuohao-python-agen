#!/usr/bin/env python3
"""ComfyUI 文生图冒烟测试：读取同目录 sdxl.json，转换画布工作流并提交。"""
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
WORKFLOW_PATH = SCRIPT_DIR / "sdxl.json"
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


def canvas_to_api(data: dict) -> dict:
    """将 ComfyUI 前端画布导出的 JSON 转为 /prompt 接受的 API prompt 格式。"""
    nodes = {
        str(node["id"]): node
        for node in data.get("nodes", [])
        if isinstance(node, dict) and "id" in node and node.get("type") != "MarkdownNote"
    }
    links = {
        link[0]: link
        for link in data.get("links", [])
        if isinstance(link, list) and len(link) >= 6
    }
    workflow = {}

    for node_id, node in nodes.items():
        api_inputs = {}

        # 先恢复节点连线；/prompt 使用 [上游节点 ID, 输出端口序号]。
        for port in node.get("inputs", []):
            if not isinstance(port, dict):
                continue
            name, link_id = port.get("name"), port.get("link")
            if name and link_id in links:
                link = links[link_id]
                api_inputs[name] = [str(link[1]), int(link[2])]

        # 画布 JSON 的 widgets_values_named 比 widgets_values 更可靠，
        # 因为某些节点的 widgets_values 还包含 control_after_generate 等额外值。
        named = node.get("widgets_values_named")
        if isinstance(named, dict):
            api_inputs.update(named)
        else:
            widget_names = [
                port.get("widget", {}).get("name")
                for port in node.get("inputs", [])
                if isinstance(port, dict)
                and isinstance(port.get("widget"), dict)
                and port["widget"].get("name")
            ]
            values = node.get("widgets_values", [])
            for index, name in enumerate(widget_names):
                if index < len(values):
                    api_inputs[name] = values[index]

        # ComfyUI 画布的节点标题转换成 API 格式的 _meta.title，便于识别提示词节点。
        api_node = {"class_type": node["type"], "inputs": api_inputs}
        if node.get("title"):
            api_node["_meta"] = {"title": str(node["title"])}
        workflow[node_id] = api_node

    return workflow


def load_workflow() -> dict:
    if not WORKFLOW_PATH.is_file():
        raise FileNotFoundError(f"找不到工作流文件：{WORKFLOW_PATH}")

    data = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and isinstance(data.get("nodes"), list):
        workflow = canvas_to_api(data)
    else:
        # 兼容原生 API prompt JSON，或外层包含 prompt 字段的 JSON。
        workflow = data.get("prompt", data) if isinstance(data, dict) else data

    if not isinstance(workflow, dict) or not workflow:
        raise RuntimeError(f"不是有效的 ComfyUI 工作流 JSON：{WORKFLOW_PATH}")

    text_nodes = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        node_type = node.get("class_type", "")
        inputs = node["inputs"]
        title = str(node.get("_meta", {}).get("title", "")).strip().lower()

        if node_type == "CLIPTextEncode" and "text" in inputs:
            text_nodes.append((node_id, title, inputs))
        elif node_type in ("EmptyLatentImage", "EmptySD3LatentImage"):
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
            "工作流中没有 CLIPTextEncode 节点；请确认 sdxl.json 是 ComfyUI 工作流导出文件。"
        )

    # 兼容英文/中文标题；若标题缺失，则按工作流中 CLIPTextEncode 节点的顺序兜底。
    positive = next(
        (item for item in text_nodes if any(k in item[1] for k in ("positive", "正面", "正向"))),
        None,
    )
    negative = next(
        (item for item in text_nodes if any(k in item[1] for k in ("negative", "负面", "负向"))),
        None,
    )
    if positive is None:
        positive = text_nodes[0]
    positive[2]["text"] = PROMPT

    if negative is None:
        negative = next((item for item in text_nodes if item[0] != positive[0]), None)
    if negative is not None:
        negative[2]["text"] = NEGATIVE_PROMPT

    print(f"已加载工作流：{WORKFLOW_PATH}")
    print(f"工作流节点数：{len(workflow)}")
    print(f"正向提示词节点：{positive[0]}（{positive[1] or '未命名'}）")
    if negative is not None:
        print(f"负向提示词节点：{negative[0]}（{negative[1] or '未命名'}）")
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
