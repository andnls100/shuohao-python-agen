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


# ---------------------------------------------------------------- 工作流读取与定位
def is_ui_format(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("nodes"), list)


def load_workflow(path: Path) -> Dict[str, Any]:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ComfyError(f"无法读取工作流 {path}：{exc}") from exc
    except ValueError as exc:
        raise ComfyError(f"工作流 {path} 不是合法 JSON：{exc}") from exc
    if is_ui_format(data):
        raise ComfyError(
            f"{path.name} 是 ComfyUI 界面格式（含 nodes/links），不能直接提交到 /prompt；"
            "请在 ComfyUI 里选「Save (API Format)」（导出 API）重新导出，替换该文件")
    if not isinstance(data, dict) or not data or not all(isinstance(v, dict) and "class_type" in v for v in data.values()):
        raise ComfyError(f"{path.name} 不是 API 格式工作流（应为 节点ID → {{class_type, inputs}}）")
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
        template = load_workflow(Path(cfg["workflow_i2v"]))
        uploaded = client.upload_image(previous_last)
        wf = build_i2v(template, prompt=prompt, seconds=seconds, uploaded=uploaded, seed=seed, prefix=prefix, nodes=nodes)
        mode, note = "i2v", "首帧=上一镜最后一帧"
    else:
        if not cfg.get("workflow_ref"):
            raise ComfyError("models.json 里没有配置 workflow_ref（Ref2VA 工作流路径）")
        template = load_workflow(Path(cfg["workflow_ref"]))
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
    log(f"[ComfyUI] 即将提交的完整工作流 JSON：\n{json.dumps(wf, ensure_ascii=False, indent=1)}\n[ComfyUI] 工作流 JSON 结束")
    try:  # 保存实际提交的工作流，方便对照 ComfyUI 界面或服务器报错排查
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / f"{item['id']}.workflow.json").write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    prompt_id = client.queue(wf)
    item["task_id"] = prompt_id
    outputs = client.wait(prompt_id, float(cfg.get("poll", 10)), float(cfg.get("max_wait", 3600)))
    save_id = find_node(wf, ("SaveVideo", "VHS_VideoCombine", "SaveAnimatedWEBP"), nodes.get("save"))
    info = pick_output(outputs, save_id)
    target = target_base.with_suffix(Path(info["filename"]).suffix or ".mp4")
    client.download(info, target)
    return target, f"{mode}：{note}"
