"""ComfyUI local image generation backend (FLUX / SD / SDXL).

Routes Hermes ``image_generate`` to a local ComfyUI server
(``http://127.0.0.1:8188`` by default). Workflows adapt by model family:

- FLUX.1-dev fp8: UNETLoader + DualCLIPLoader(type="flux") + EmptySD3LatentImage
  + KSampler(cfg=1.0, euler/simple, 20 steps)
- SD/SDXL: CheckpointLoaderSimple + EmptyLatentImage + KSampler(cfg=7.0,
  euler/normal, 24/30 steps)

Base URL precedence: ``image_gen.base_url`` in config.yaml, else
``COMFYUI_URL`` env, else ``http://127.0.0.1:8188``.

Plugin registers only at Hermes process start — restart the app after
adding/editing this file.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_url_image,
    success_response,
)

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "http://127.0.0.1:8188"
_POLL_INTERVAL = 1.0
_POLL_TIMEOUT = 600.0


def _hermes_home() -> Path:
    # Canonical resolver first: the install home is D:\...\AI\hermes, not ~/.hermes, so a bare
    # env-or-home fallback silently points generation at the retired 8188 default.
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home()
    except Exception:  # pragma: no cover - resolution must never break gen
        pass
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path.home() / ".hermes"


def _comfy_base() -> str:
    cfg_path = _hermes_home() / "config.yaml"
    try:
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            ig = cfg.get("image_gen") or {}
            if ig.get("base_url"):
                return str(ig["base_url"]).rstrip("/")
    except Exception:  # pragma: no cover - config read must never break gen
        pass
    return os.environ.get("COMFYUI_URL", _DEFAULT_BASE).rstrip("/")


class ComfyUIImageGenProvider(ImageGenProvider):
    @property
    def name(self) -> str:
        return "comfyui"

    @property
    def display_name(self) -> str:
        return "ComfyUI (local)"

    def is_available(self) -> bool:
        base = _comfy_base()
        try:
            with urllib.request.urlopen(base + "/system_stats", timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    def list_models(self) -> list[dict[str, Any]]:
        base = _comfy_base()
        models: list[str] = []
        try:
            with urllib.request.urlopen(base + "/object_info/CheckpointLoaderSimple", timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
                info = data.get("CheckpointLoaderSimple", {}).get("input", {}).get("required", {})
                models.extend(info.get("ckpt_name", [{}])[0] if info.get("ckpt_name") else [])
        except Exception as exc:
            logger.debug("CheckpointLoaderSimple object_info failed: %s", exc)
        try:
            with urllib.request.urlopen(base + "/object_info/UNETLoader", timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
                info = data.get("UNETLoader", {}).get("input", {}).get("required", {})
                models.extend(info.get("unet_name", [{}])[0] if info.get("unet_name") else [])
        except Exception as exc:
            logger.debug("UNETLoader object_info failed: %s", exc)
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for m in models:
            if m not in seen:
                seen.add(m)
                out.append({"id": m, "display": m, "speed": "local GPU", "price": "free"})
        return out

    def default_model(self) -> Optional[str]:
        cfg_path = _hermes_home() / "config.yaml"
        try:
            if cfg_path.exists():
                cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                m = (cfg.get("image_gen") or {}).get("model")
                if m:
                    return str(m)
        except Exception:
            pass
        models = self.list_models()
        return models[0].get("id") if models else None

    def capabilities(self) -> dict[str, Any]:
        return {"modalities": ["text"], "max_reference_images": 0}

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        base = _comfy_base()
        aspect = resolve_aspect_ratio(aspect_ratio)
        model = kwargs.get("model") or self.default_model() or "flux1-dev-fp8.safetensors"
        if not prompt.strip():
            return error_response("Empty image prompt.", provider=self.name)

        if image_url or reference_image_urls:
            return error_response(
                "ComfyUI plugin does not support image-to-image in this build.",
                provider=self.name,
            )

        width, height, steps, cfg = self._dimensions_for(model, aspect)
        workflow = self._build_workflow(model, prompt, width, height, steps, cfg)
        try:
            prompt_id = self._submit(base, workflow)
            filename = self._wait_and_fetch(base, prompt_id)
            view_url = f"{base}/view?filename={filename}&subfolder=&type=output"
            path = save_url_image(view_url, prefix="comfyui")
            return success_response(
                image=str(path),
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
                provider=self.name,
                modality="text",
            )
        except Exception as exc:
            logger.exception("ComfyUI generation failed")
            return error_response(str(exc), provider=self.name)

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _dimensions_for(model: str, aspect: str) -> tuple[int, int, int, float]:
        lower = model.lower()
        if "flux" in lower:
            base_dim = 1024
            steps, cfg = 20, 1.0
        elif "xl" in lower or "sdxl" in lower:
            base_dim = 1024
            steps, cfg = 30, 7.0
        else:
            base_dim = 512
            steps, cfg = 24, 7.0
        if aspect == "square":
            w, h = base_dim, base_dim
        elif aspect == "portrait":
            w, h = base_dim, int(base_dim * 4 / 3)
        else:  # landscape / default
            w, h = int(base_dim * 4 / 3), base_dim
        return w, h, steps, cfg

    @staticmethod
    def _build_workflow(model: str, prompt: str, width: int, height: int,
                        steps: int, cfg: float) -> dict[str, Any]:
        lower = model.lower()
        if "flux" in lower:
            return {
                "3": {"class_type": "KSampler", "inputs": {
                    "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
                    "latent_image": ["5", 0], "seed": int(time.time() * 1000) % 2**32,
                    "steps": steps, "cfg": cfg, "sampler_name": "euler",
                    "scheduler": "simple", "denoise": 1.0}},
                "4": {"class_type": "UNETLoader", "inputs": {
                    "unet_name": model, "weight_dtype": "default"}},
                "5": {"class_type": "EmptySD3LatentImage", "inputs": {
                    "width": width, "height": height, "batch_size": 1}},
                "6": {"class_type": "CLIPTextEncode", "inputs": {
                    "clip": ["11", 0], "text": prompt}},
                "7": {"class_type": "CLIPTextEncode", "inputs": {
                    "clip": ["11", 0], "text": ""}},
                "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["10", 0]}},
                "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "hermes_comfyui"}},
                "10": {"class_type": "VAELoader", "inputs": {"vae_name": "flux-vae-bf16.safetensors"}},
                "11": {"class_type": "DualCLIPLoader", "inputs": {
                    "clip_name1": "clip_l.safetensors", "clip_name2": "t5xxl_fp8_e4m3fn.safetensors",
                    "type": "flux"}},
            }
        return {
            "3": {"class_type": "KSampler", "inputs": {
                "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
                "latent_image": ["5", 0], "seed": int(time.time() * 1000) % 2**32,
                "steps": steps, "cfg": cfg, "sampler_name": "euler",
                "scheduler": "normal", "denoise": 1.0}},
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model}},
            "5": {"class_type": "EmptyLatentImage", "inputs": {
                "width": width, "height": height, "batch_size": 1}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {
                "clip": ["4", 1], "text": prompt}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {
                "clip": ["4", 1], "text": ""}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
            "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "hermes_comfyui"}},
        }

    @staticmethod
    def _submit(base: str, workflow: dict[str, Any]) -> str:
        body = json.dumps({"prompt": workflow, "client_id": "hermes-image-gen"}).encode("utf-8")
        req = urllib.request.Request(
            base + "/prompt", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["prompt_id"]

    @staticmethod
    def _wait_and_fetch(base: str, prompt_id: str) -> str:
        deadline = time.time() + _POLL_TIMEOUT
        while time.time() < deadline:
            time.sleep(_POLL_INTERVAL)
            try:
                with urllib.request.urlopen(base + f"/history/{prompt_id}", timeout=10) as r:
                    hist = json.loads(r.read().decode("utf-8"))
            except Exception:
                continue
            entry = hist.get(prompt_id)
            if not entry:
                continue
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                msgs = status.get("messages") or []
                raise RuntimeError(f"ComfyUI execution error: {json.dumps(msgs)[:800]}")
            outputs = entry.get("outputs") or {}
            for node in outputs.values():
                images = node.get("images") or []
                if images:
                    return images[0]["filename"]
        raise TimeoutError(f"ComfyUI generation timed out after {_POLL_TIMEOUT:.0f}s")


def register(ctx) -> None:
    ctx.register_image_gen_provider(ComfyUIImageGenProvider())
