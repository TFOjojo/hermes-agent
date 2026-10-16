"""ComfyUI MiniMax H3 本地视频生成后端。

对接本地 ComfyUI（默认 http://127.0.0.1:8188）：文生视频（T2V）/ 图生视频
（I2V）通过 MiniMaxH3ImageToVideo + KSampler + VAEDecode + CreateVideo +
SaveVideo 工作流实现。支持 duration（秒→H3 帧网格）/ aspect_ratio / resolution
/ seed / negative_prompt 全参数。模型：minimax_h3_fl2va_pruned_int8_convrot
（T2V/I2V 一体，first_frame 可选）。ref2va（参考视频）暂未开放。

注意：H3 的 AV latent 含音频通道，但 ComfyUI 当前无公开的 H3 音频 VAE 解码
节点，本版输出无声视频（audio 参数透传但被忽略，extra 注明）。
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.video_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    DEFAULT_RESOLUTION,
    VideoGenProvider,
    error_response,
    save_url_video,
    success_response,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8188"


def _resolve_base() -> str:
    """Base URL precedence: ``video_gen.base_url`` in config.yaml, else
    ``COMFYUI_URL`` env, else ``http://127.0.0.1:8188`` (same as image plugin)."""
    try:
        from hermes_constants import get_hermes_home
        import yaml

        cfg = yaml.safe_load((get_hermes_home() / "config.yaml").read_text(encoding="utf-8")) or {}
        vg = cfg.get("video_gen") or {}
        if vg.get("base_url"):
            return str(vg["base_url"]).rstrip("/")
    except Exception:
        pass
    return os.environ.get("COMFYUI_URL", DEFAULT_BASE_URL).rstrip("/")
H3_T2V_UNET = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
H3_REF_UNET = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
H3_CLIP = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
H3_VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"

POLL_INTERVAL = 3.0
POLL_TIMEOUT = 1800.0

# aspect -> 宽高比（以高度档为基准计算宽度）
_RATIOS: Dict[str, float] = {
    "16:9": 16 / 9,
    "9:16": 9 / 16,
    "1:1": 1.0,
    "4:3": 4 / 3,
    "3:4": 3 / 4,
    "3:2": 3 / 2,
    "2:3": 2 / 3,
}
_HEIGHTS: Dict[str, int] = {"480p": 480, "540p": 540, "720p": 720, "1080p": 1080}


def _align32(v: int) -> int:
    return max(32, int(v // 32 * 32))


def _snap_length(duration: int) -> int:
    """秒 -> H3 帧数（17k+5 网格，向上取整；124 帧 ≈ 5s）。"""
    n = max(1, int(round(duration * 24)))
    return max(5, ((n - 5 + 16) // 17) * 17 + 5)


def _http_json(method: str, url: str, payload: Optional[dict] = None, timeout: float = 30.0) -> Any:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")


def _upload_image(base_url: str, image_url: str) -> Optional[str]:
    """把源图（URL 或本地路径）上传到 ComfyUI input 目录，返回文件名。"""
    try:
        if image_url.startswith(("http://", "https://")):
            import tempfile
            fd, tmp_path = tempfile.mkstemp(suffix=".png")
            with urllib.request.urlopen(image_url, timeout=60) as resp, open(tmp_path, "wb") as fh:
                fh.write(resp.read())
            src = Path(tmp_path)
        else:
            src = Path(image_url)
            if not src.exists():
                return None
        boundary = "----hermescomfyvideo"
        name = f"hermes_ref_{int(time.time() * 1000)}.png"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
            f"Content-Type: image/png\r\n\r\n"
        ).encode() + src.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{base_url}/upload/image",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        return result.get("name") or name
    except Exception as exc:
        logger.warning("upload source image failed: %s", exc)
        return None


def _workflow_h3(
    prompt: str,
    negative: str,
    unet: str,
    width: int,
    height: int,
    length: int,
    seed: int,
    steps: int,
    cfg: float,
    first_frame_name: Optional[str],
) -> Dict[str, Any]:
    wf: Dict[str, Any] = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": H3_CLIP, "type": "minimax"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": H3_VIDEO_VAE}},
        "4": {
            "class_type": "MiniMaxH3ImageToVideo",
            "inputs": {
                "clip": ["2", 0],
                "vae": ["3", 0],
                "prompt": prompt,
                "width": width,
                "height": height,
                "length": length,
            },
        },
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["2", 0]}},
        "6": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["4", 0],
                "negative": ["5", 0],
                "latent_image": ["4", 1],
            },
        },
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "8": {"class_type": "CreateVideo", "inputs": {"images": ["7", 0], "fps": 24}},
        "9": {
            "class_type": "SaveVideo",
            "inputs": {"video": ["8", 0], "filename_prefix": "hermes_h3", "format": "auto", "codec": "auto"},
        },
    }
    if first_frame_name:
        wf["10"] = {"class_type": "LoadImage", "inputs": {"image": first_frame_name}}
        wf["4"]["inputs"]["first_frame"] = ["10", 0]
    return wf


class ComfyUIVideoGenProvider(VideoGenProvider):
    """本地 ComfyUI MiniMax H3 T2V / I2V 后端。"""

    def __init__(self, base_url: str = DEFAULT_BASE_URL) -> None:
        self._base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        return "comfyui"

    @property
    def display_name(self) -> str:
        return "ComfyUI MiniMax H3 (local)"

    def is_available(self) -> bool:
        try:
            _http_json("GET", f"{self._base_url}/system_stats", timeout=5.0)
            return True
        except Exception:
            return False

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": H3_T2V_UNET,
                "display": "MiniMax H3 (T2V / I2V)",
                "speed": "~3-6 min/5s clip (5070 Ti)",
                "strengths": "本地生成，零 API 费用",
                "price": "free",
            },
            {
                "id": H3_REF_UNET,
                "display": "MiniMax H3 ref2va (reference)",
                "speed": "not yet enabled",
                "strengths": "参考视频生成（开发中）",
                "price": "free",
            },
        ]

    def default_model(self) -> Optional[str]:
        try:
            import yaml
            from hermes_constants import get_hermes_home
            cfg_path = get_hermes_home() / "config.yaml"
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            m = (cfg.get("video_gen") or {}).get("model")
            if m:
                return str(m)
        except Exception:
            pass
        return H3_T2V_UNET

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "ComfyUI MiniMax H3 (local)",
            "badge": "local",
            "tag": "本地 H3 视频生成，无 API 费用；需 ComfyUI 运行在 127.0.0.1:8188",
            "env_vars": [],
        }

    def capabilities(self) -> Dict[str, Any]:
        return {"modalities": ["text", "image"], "max_reference_images": 0}

    def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        duration: Optional[int] = None,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        resolution: str = DEFAULT_RESOLUTION,
        negative_prompt: Optional[str] = None,
        audio: Optional[bool] = None,
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = aspect_ratio.strip().lower() if aspect_ratio else DEFAULT_ASPECT_RATIO
        if aspect not in _RATIOS:
            aspect = DEFAULT_ASPECT_RATIO
        res = resolution.strip().lower() if resolution else DEFAULT_RESOLUTION
        if res not in _HEIGHTS:
            res = "720p"
        dur = int(duration) if duration and duration > 0 else 5
        length = _snap_length(dur)

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument", provider=self.name, aspect_ratio=aspect,
            )
        if not self.is_available():
            return error_response(
                error="ComfyUI 服务不可达（127.0.0.1:8188）。请先启动 ComfyUI。",
                error_type="provider_unavailable", provider=self.name, aspect_ratio=aspect,
            )

        model_ids = [m["id"] for m in self.list_models()]
        ckpt = (model or "").strip() or (self.default_model() or H3_T2V_UNET)
        if ckpt not in model_ids:
            ckpt = H3_T2V_UNET
        if ckpt == H3_REF_UNET:
            return error_response(
                error="ref2va（参考视频）暂未开放，请使用 fl2va 模型（T2V/I2V）",
                error_type="not_supported", provider=self.name, model=ckpt, prompt=prompt, aspect_ratio=aspect,
            )
        if reference_image_urls:
            return error_response(
                error="参考图/视频生成暂未开放（第一版仅 T2V + I2V）",
                error_type="not_supported", provider=self.name, model=ckpt, prompt=prompt, aspect_ratio=aspect,
            )

        ratio = _RATIOS[aspect]
        height = _align32(_HEIGHTS[res] * ratio if ratio < 1 else _HEIGHTS[res])
        width = _align32(height * ratio)
        if ratio < 1:
            height = _align32(_HEIGHTS[res])
        seed_v = int(seed) if seed is not None else int(time.time() * 1000) % (2**31)
        neg = (negative_prompt or "").strip()

        # 上传首帧（I2V）
        first_name = None
        modality = "text"
        if image_url:
            first_name = _upload_image(self._base_url, image_url)
            if first_name is None:
                return error_response(
                    error=f"无法上传源图 {image_url}",
                    error_type="provider_error", provider=self.name, model=ckpt, prompt=prompt, aspect_ratio=aspect,
                )
            modality = "image"

        workflow = _workflow_h3(prompt, neg, ckpt, width, height, length, seed_v, 40, 4.0, first_name)

        try:
            resp = _http_json(
                "POST", f"{self._base_url}/prompt",
                {"prompt": workflow, "client_id": f"hermes-video-{seed_v}"}, timeout=30.0,
            )
            prompt_id = resp["prompt_id"]
        except Exception as exc:
            return error_response(
                error=f"提交 ComfyUI 工作流失败: {exc}",
                error_type="provider_error", provider=self.name, model=ckpt, prompt=prompt, aspect_ratio=aspect,
            )

        try:
            deadline = time.monotonic() + POLL_TIMEOUT
            while time.monotonic() < deadline:
                time.sleep(POLL_INTERVAL)
                hist = _http_json("GET", f"{self._base_url}/history/{prompt_id}", timeout=10.0)
                if hist and prompt_id in hist:
                    entry = hist[prompt_id]
                    st = entry.get("status", {})
                    if st.get("completed"):
                        outputs = entry.get("outputs") or {}
                        for node_out in outputs.values():
                            for v in (node_out.get("videos") or node_out.get("images") or []):
                                view_url = (
                                    f"{self._base_url}/view?filename={urllib.parse.quote(v['filename'])}"
                                    f"&subfolder={urllib.parse.quote(v.get('subfolder', ''))}&type={v.get('type', 'output')}"
                                )
                                try:
                                    path = save_url_video(view_url, prefix="comfyui_h3")
                                except Exception as exc2:
                                    return error_response(
                                        error=f"下载视频失败: {exc2}",
                                        error_type="provider_error", provider=self.name, model=ckpt, prompt=prompt, aspect_ratio=aspect,
                                    )
                                extra: Dict[str, Any] = {"seed": seed_v, "frames": length}
                                if audio:
                                    extra["note"] = "当前版本输出无声视频（H3 音频 VAE 解码节点未开放）"
                                return success_response(
                                    video=str(path), model=ckpt, prompt=prompt, modality=modality,
                                    aspect_ratio=aspect, duration=max(1, round(length / 24)),
                                    provider=self.name, extra=extra,
                                )
                        return error_response(
                            error="ComfyUI 任务完成但无输出视频",
                            error_type="provider_error", provider=self.name, model=ckpt, prompt=prompt, aspect_ratio=aspect,
                        )
                    if st.get("status_str") in ("error",):
                        msgs = st.get("messages") or []
                        err = "; ".join(
                            str(m[1]) for m in msgs if len(m) > 1 and isinstance(m[1], dict) and m[1].get("message")
                        ) or "unknown comfyui error"
                        return error_response(
                            error=f"ComfyUI 执行错误: {err}",
                            error_type="provider_error", provider=self.name, model=ckpt, prompt=prompt, aspect_ratio=aspect,
                        )
        except Exception as exc:
            return error_response(
                error=f"轮询 ComfyUI 失败: {exc}",
                error_type="provider_error", provider=self.name, model=ckpt, prompt=prompt, aspect_ratio=aspect,
            )

        return error_response(
            error=f"ComfyUI 超时（{int(POLL_TIMEOUT)}s）",
            error_type="timeout", provider=self.name, model=ckpt, prompt=prompt, aspect_ratio=aspect,
        )


def register(ctx) -> None:
    """Plugin entry point — register the local ComfyUI H3 video provider."""
    ctx.register_video_gen_provider(ComfyUIVideoGenProvider(base_url=_resolve_base()))
