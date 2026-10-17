"""Offline tests for the optional video stage."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zhipu_agent.stages import video


def _ctx(tmp_path: Path):
    out = tmp_path / "output"
    board = out / "storyboard" / "Demo-storyboard.json"
    board.parent.mkdir(parents=True)
    board.write_text(json.dumps({
        "source": "Demo", "aspectRatio": "9:16", "episodes": [
            {"ep": 1, "segments": [
                {"id": "E01-01", "sceneIndex": 1, "h3Prompt": "A person opens a door.",
                 "blocking": "Person left, door right.", "soundscape": "Rain",
                 "cuts": [{"seconds": 4, "shot": "Open the door.", "camera": "Push In"}]}
            ]}
        ]
    }), encoding="utf-8")
    return SimpleNamespace(
        out=out, title="Demo", force=False,
        artifact=lambda name: board if name == "storyboard" else out / f"{name}.json",
        log=lambda message: None,
    )


def test_video_preview_does_not_submit(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.delenv("VIDEO_API_SUBMIT_URL", raising=False)
    monkeypatch.delenv("VIDEO_API_KEY", raising=False)
    monkeypatch.delenv("VIDEO_API_MODEL", raising=False)
    path = video.run(ctx, eps=[1], protocol="h3", submit=False)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["aspect_ratio"] == "9:16"
    assert manifest["items"][0]["state"] == "planned"
    assert manifest["items"][0]["duration_seconds"] == 4.0
    assert (ctx.out / "video" / "prompts" / "E01-01.txt").is_file()
    assert (ctx.out / "video" / "qc-checklist.md").is_file()
    assert not video.check(ctx).ok


def test_seedance_prompt_uses_structured_shots(tmp_path):
    ctx = _ctx(tmp_path)
    path = video.run(ctx, protocol="seedance", submit=False)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    prompt = manifest["items"][0]["prompt"]
    assert "【人物关系与构图逻辑】" in prompt
    assert "画面：Open the door." in prompt
    assert "【镜头1】" in prompt
