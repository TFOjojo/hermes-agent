"""PianyiToken image generation (nano-banana) via OpenAI-compatible chat completions.

The 生图-group token lists ``nano-banana-2`` / ``nano-banana-pro`` only.
Those models return an embedded ``data:image/...;base64,...`` markdown image
from ``POST /v1/chat/completions``. ``POST /v1/images/generations`` 500s
with ``convert_request_failed``; ``gpt-image-2`` / 2.5 flare/sunburst 503
``model_not_found`` on this key.

Selection: ``model`` kwarg → ``PIANYITOKEN_IMAGE_MODEL`` →
``image_gen.pianyitoken.model`` → :data:`DEFAULT_MODEL`.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from agent.secret_scope import get_secret
from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO, resolve_aspect_ratio, save_b64_image, success_response)
from plugins.image_gen._common import (
    StaticImageGenProvider, error_factory, prompt_required_error, resolve_static_model)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://pianyitoken.gay/v1"
API_KEY_ENV = "PIANYITOKEN_IMAGE_API_KEY"
FALLBACK_KEY_ENV = "PIANYITOKEN_TAVERN_API_KEY"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_B64_RE = re.compile(r"data:image/(\w+);base64,([A-Za-z0-9+/=]+)")
_TIMEOUT = 180

_MODELS: Dict[str, Dict[str, Any]] = {
    "nano-banana-2": {
        "display": "Nano Banana 2",
        "speed": "~15s",
        "strengths": "PianyiToken 生图分组；chat/completions 内嵌 PNG",
        "price": "¥0.0072/次",
    },
    "nano-banana-pro": {
        "display": "Nano Banana Pro",
        "speed": "~20s",
        "strengths": "更高质量香蕉线",
        "price": "¥0.0117/次",
    },
}
DEFAULT_MODEL = "nano-banana-2"


def _resolve_api_key() -> Optional[str]:
    return get_secret(API_KEY_ENV) or get_secret(FALLBACK_KEY_ENV)


def _resolve_model(caller_model: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    return resolve_static_model(
        _MODELS, DEFAULT_MODEL, env_var="PIANYITOKEN_IMAGE_MODEL",
        config_key="pianyitoken", explicit=caller_model,
    )


class PianyitokenImageGenProvider(StaticImageGenProvider):
    """PianyiToken 生图分组 ``nano-banana`` backend."""

    provider_id = "pianyitoken"
    label = "便宜Token·生图"
    models = _MODELS
    default_model_id = DEFAULT_MODEL
    setup = dict(
        name="便宜Token·生图", badge="paid",
        tag="nano-banana-2 / nano-banana-pro via pianyitoken.gay 生图分组",
        key=API_KEY_ENV, prompt="PianyiToken 生图分组 API key",
        url="https://pianyitoken.gay")

    def is_available(self) -> bool:
        return bool(_resolve_api_key())

    def capabilities(self) -> Dict[str, Any]:
        return {"modalities": ["text"], "max_reference_images": 0}

    def generate(
        self, prompt: str, aspect_ratio: str = DEFAULT_ASPECT_RATIO, *,
        image_url: Optional[str] = None, reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        if not prompt:
            return prompt_required_error("pianyitoken", aspect)
        api_key = _resolve_api_key()
        if not api_key:
            return error_factory("pianyitoken", aspect)(
                f"{API_KEY_ENV} not set.", "auth_required")
        model_id, _meta = _resolve_model(kwargs.get("model"))
        fail = error_factory("pianyitoken", aspect, model=model_id, prompt=prompt)
        body = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4000,
        }
        req = urllib.request.Request(
            f"{DEFAULT_BASE_URL}/chat/completions",
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": UA,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                data = json.loads(resp.read().decode() or "null")
        except urllib.error.HTTPError as exc:
            logger.debug("PianyiToken image HTTP %s", exc.code, exc_info=True)
            return fail(f"PianyiToken image generation HTTP {exc.code}", "api_error")
        except Exception as exc:
            logger.debug("PianyiToken image generation failed", exc_info=True)
            return fail(f"PianyiToken image generation failed: {type(exc).__name__}", "api_error")

        try:
            content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
        except (AttributeError, IndexError, TypeError):
            return fail("PianyiToken response contained no choices", "empty_response")
        if "quota" in content.lower() or "exceeded" in content.lower():
            return fail("PianyiToken quota gate (200 with exhaustion notice)", "quota")
        m = _B64_RE.search(content)
        if not m:
            return fail("PianyiToken response contained no embedded image", "empty_response")
        ext = m.group(1) if m.group(1) in ("png", "jpeg", "jpg", "webp") else "png"
        try:
            image_ref = str(save_b64_image(m.group(2), prefix="pianyitoken", extension=ext))
        except Exception as exc:
            return fail(f"Failed to save PianyiToken image: {exc}", "io_error")
        return success_response(
            image=image_ref, model=model_id, prompt=prompt, aspect_ratio=aspect,
            provider="pianyitoken", modality="text", extra={"format": ext})


def register(ctx) -> None:
    ctx.register_image_gen_provider(PianyitokenImageGenProvider())
