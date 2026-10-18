"""独立测试视频任务查询与下载逻辑，不提交新的生成任务。

用法：
    python test_download_video.py
    python test_download_video.py --task-id <任务ID> --model wan_prime
配置读取项目根目录的 models.json，并使用其中配置的环境变量。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path

from zhipu_agent.config import models_file_path
from zhipu_agent.stages.video import _config, _download, _find, _request_get

DEFAULT_TASK_ID = "41595cdd-9148-4fdd-ba4a-fde295073479"
ROOT = Path(__file__).resolve().parent


def configured_model_keys() -> list[str]:
    path = models_file_path()
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    entries = data.get("video", [])
    keys = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("enabled", True) is False:
            continue
        key = str(entry.get("key") or entry.get("name") or entry.get("model") or "").strip()
        if key and key not in keys:
            keys.append(key)
    if not keys:
        raise RuntimeError(f"{path} 中没有启用的视频模型配置")
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(description="使用 models.json 查询并下载指定视频任务")
    parser.add_argument("--task-id", default=DEFAULT_TASK_ID, help="视频任务 ID")
    parser.add_argument("--model", default="", help="models.json 中的视频模型 key；默认逐个尝试已启用配置")
    parser.add_argument("--output", default="output/video/videos/task_test.mp4", help="下载目标文件")
    args = parser.parse_args()

    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output

    try:
        keys = [args.model] if args.model else configured_model_keys()
    except Exception as exc:
        print(f"读取 models.json 失败：{exc}")
        return 2

    failures = []
    for key in keys:
        try:
            providers = _config(key)
            if not providers:
                failures.append(f"{key}: 未得到有效模型配置")
                continue
            provider = providers[0]
            template = provider.get("status_template")
            if not template:
                failures.append(f"{key}: 缺少 status_url_template")
                continue
            status_url = template.format(task_id=urllib.parse.quote(args.task_id, safe=""))
            print(f"尝试模型配置：{key}（{provider['name']}）")
            print(f"查询任务状态：{status_url}")
            result = _request_get(status_url, provider)
            print("状态接口返回：")
            print(json.dumps(result, ensure_ascii=False, indent=2)[:6000])

            video_url = _find(
                result,
                tuple(provider.get("video_url_keys") or
                      ["video_url", "videoUrl", "output_url", "download_url", "url"]),
            )
            if not video_url:
                failures.append(f"{key}: 接口可访问，但响应中未找到视频 URL；请反馈上方状态接口返回")
                continue

            print(f"发现视频 URL（已隐藏）：{urllib.parse.urlsplit(video_url).scheme}://{urllib.parse.urlsplit(video_url).netloc}/...")
            _download(video_url, output, provider)
            size = output.stat().st_size
            print(f"下载成功：{output.resolve()}")
            print(f"文件大小：{size} bytes")
            if size < 1024:
                print("警告：文件小于 1 KB，可能下载到错误响应而非视频内容。")
            return 0
        except Exception as exc:
            failures.append(f"{key}: {type(exc).__name__}: {exc}")
            print(f"该配置测试失败：{type(exc).__name__}: {exc}")

    print("\n未能下载。请反馈以下信息：")
    print("\n".join(f"- {item}" for item in failures))
    print("同时提供对应模型的状态接口返回 JSON（隐藏 API Key 和 URL 中的签名参数）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
