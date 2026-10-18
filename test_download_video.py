"""独立查询并下载阿里云百炼 Wan R2V 视频任务；不导入项目内部模块，也不提交新任务。

运行：
    python test_download_video.py
    python test_download_video.py --task-id <任务ID>
    python test_download_video.py --output output/video/videos/task_test.mp4

请先把 API_KEY 替换为自己的阿里云百炼 API Key。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DEFAULT_TASK_ID = "41595cdd-9148-4fdd-ba4a-fde295073479"

# 独立写死的模型配置；将 api_key 的值替换成自己的 API Key。
VIDEO_CONFIG = {
    "key": "wan_r2v",
    "name": "阿里云百炼-Wan2.7-R2V",
    "adapter": "aliyun_wan",
    "enabled": True,
    "active": False,
    "submit_url": "https://llm-yygg5z4v26uyzh2y.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis",
    "status_url_template": "https://llm-yygg5z4v26uyzh2y.cn-beijing.maas.aliyuncs.com/api/v1/tasks/{task_id}",
    "workspace_id": "llm-yygg5z4v26uyzh2y",
    "api_key": "在这里填写你的API_KEY",
    "model": "wan3.0-video-prime",
    "resolution": "720P",
    "ratio": "16:9",
    "prompt_extend": False,
    "timeout": 120,
    "poll_interval": 15,
    "max_wait": 1800,
}

VIDEO_URL_KEYS = ("video_url", "videoUrl", "output_url", "download_url", "url")


def find_value(data, keys):
    """递归查找 JSON 中第一个非空匹配字段。"""
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if value not in (None, "") and isinstance(value, (str, int, float)):
                return str(value)
        for value in data.values():
            found = find_value(value, keys)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_value(value, keys)
            if found:
                return found
    return None


def request_json(url):
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + VIDEO_CONFIG["api_key"].strip(),
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=VIDEO_CONFIG["timeout"]) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"状态查询 HTTP {exc.code}: {detail[:1500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"状态查询网络失败：{exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"状态接口返回非 JSON：{raw[:1000]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("状态接口返回的 JSON 不是对象")
    return data


def download_video(url, target):
    if urlsplit(url).scheme not in ("http", "https"):
        raise RuntimeError("返回的视频 URL 不是 HTTP(S) 地址")
    target.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={"Accept": "video/mp4,video/*,application/octet-stream"},
        method="GET",
    )
    temp = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=VIDEO_CONFIG["timeout"]) as response:
            with temp.open("wb") as stream:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
        if not temp.is_file() or temp.stat().st_size <= 0:
            raise RuntimeError("下载结果为空文件")
        temp.replace(target)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"视频下载 HTTP {exc.code}: {detail[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"视频下载网络失败：{exc}") from exc
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="查询并下载阿里云百炼 Wan R2V 视频任务")
    parser.add_argument("--task-id", default=DEFAULT_TASK_ID, help="已有的视频任务 ID")
    parser.add_argument("--output", default="output/video/videos/task_test.mp4", help="下载目标文件")
    args = parser.parse_args()

    api_key = VIDEO_CONFIG["api_key"].strip()
    if not api_key or api_key == "在这里填写你的API_KEY":
        print('请先编辑 test_download_video.py，将 VIDEO_CONFIG["api_key"] 替换为自己的 API Key。')
        return 2

    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    task_id = urllib.parse.quote(args.task_id, safe="")
    status_url = VIDEO_CONFIG["status_url_template"].format(task_id=task_id)

    print(f"模型配置：{VIDEO_CONFIG['name']} ({VIDEO_CONFIG['model']})")
    print(f"任务 ID：{args.task_id}")
    print(f"正在查询任务状态：{status_url}")
    try:
        result = request_json(status_url)
        print("状态接口返回 JSON：")
        print(json.dumps(result, ensure_ascii=False, indent=2)[:8000])

        video_url = find_value(result, VIDEO_URL_KEYS)
        if not video_url:
            print("\n尚未在响应中找到视频 URL。请反馈上方完整状态 JSON 和任务状态字段。")
            return 1

        parsed = urlsplit(video_url)
        print(f"发现视频地址：{parsed.scheme}://{parsed.netloc}/...（已隐藏路径和签名参数）")
        download_video(video_url, output)
        print(f"下载成功：{output.resolve()}")
        print(f"文件大小：{output.stat().st_size} bytes")
        if output.stat().st_size < 1024:
            print("警告：文件小于 1 KB，可能不是有效视频文件。")
        return 0
    except Exception as exc:
        print(f"失败：{type(exc).__name__}: {exc}")
        print("请反馈以上错误，以及状态接口返回 JSON（隐藏 API Key 和 URL 签名参数）。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
