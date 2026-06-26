from __future__ import annotations

import math
from io import BytesIO
from typing import Any

import litellm
import structlog
from PIL import Image

LOG = structlog.get_logger()

# OpenAI has two regimes. Source:
# https://developers.openai.com/api/docs/guides/images-vision#patch-based-image-tokenization
# - Patch-based (GPT-5 family, gpt-4.1-mini/nano, o4-mini): 32px patches, capped per model,
#   then scaled by a per-model multiplier.
# - Tile-based legacy (GPT-4o, GPT-4.1, o1/o3): fit 2048 box -> shortest side 768 -> 512px tiles.
_OPENAI_PATCH_SIZE = 32
_OPENAI_PATCH_BUDGET_SMALL = 1536  # mini / nano / o4-mini
_OPENAI_PATCH_BUDGET_LARGE = 2500  # full gpt-5.x at detail=high
_OPENAI_FIT_BOX = 2048
_OPENAI_SHORT_SIDE = 768
_OPENAI_TILE = 512
_OPENAI_BASE_TOKENS = 85
_OPENAI_TOKENS_PER_TILE = 170

# Gemini: images with both sides <= 384px cost a flat 258 tokens; larger images are
# split into 768x768 tiles at 258 tokens each. Source:
# https://ai.google.dev/gemini-api/docs/tokens#multimodal-tokens
# (Estimator is a fallback only — Gemini reports actual image tokens; see provider_image_tokens.)
_GEMINI_SMALL_SIDE = 384
_GEMINI_TILE = 768
_GEMINI_TOKENS_PER_TILE = 258

# Anthropic: tokens ~= width * height / 750, capped where the provider downsizes
# large images to ~1.15 megapixels. Source:
# https://platform.claude.com/docs/en/build-with-claude/vision#calculate-image-costs
_ANTHROPIC_PIXELS_PER_TOKEN = 750
_ANTHROPIC_MAX_TOKENS = 1600


def _image_dimensions(image: bytes) -> tuple[int, int] | None:
    try:
        with Image.open(BytesIO(image)) as img:
            return img.width, img.height
    except Exception:
        return None


def _normalize_model(model: str) -> str:
    return (model or "").split("/")[-1].lower()


def _openai_patch_spec(model: str) -> tuple[int, float] | None:
    """(patch_budget, multiplier) for patch-based OpenAI models, else None (tile-based)."""
    is_patch = "gpt-5" in model or "gpt-4.1-mini" in model or "gpt-4.1-nano" in model or "o4-mini" in model
    if not is_patch:
        return None
    if "nano" in model:
        return _OPENAI_PATCH_BUDGET_SMALL, 2.46
    if "o4-mini" in model:
        return _OPENAI_PATCH_BUDGET_SMALL, 1.72
    if "mini" in model:
        return _OPENAI_PATCH_BUDGET_SMALL, 1.62
    return _OPENAI_PATCH_BUDGET_LARGE, 1.0


def _openai_patch_tokens(width: int, height: int, patch_budget: int, multiplier: float) -> int:
    patches = math.ceil(width / _OPENAI_PATCH_SIZE) * math.ceil(height / _OPENAI_PATCH_SIZE)
    if patches > patch_budget:
        shrink = math.sqrt((_OPENAI_PATCH_SIZE**2 * patch_budget) / (width * height))
        scaled_w, scaled_h = width * shrink, height * shrink
        # snap the shrink factor so both sides land on whole 32px patches
        adjust = min(
            math.floor(scaled_w / _OPENAI_PATCH_SIZE) / (scaled_w / _OPENAI_PATCH_SIZE),
            math.floor(scaled_h / _OPENAI_PATCH_SIZE) / (scaled_h / _OPENAI_PATCH_SIZE),
        )
        shrink *= adjust
        patches = math.ceil(width * shrink / _OPENAI_PATCH_SIZE) * math.ceil(height * shrink / _OPENAI_PATCH_SIZE)
    return round(min(patches, patch_budget) * multiplier)


def _openai_tile_tokens(width: int, height: int) -> int:
    if max(width, height) > _OPENAI_FIT_BOX:
        scale = _OPENAI_FIT_BOX / max(width, height)
        width, height = round(width * scale), round(height * scale)
    if min(width, height) > _OPENAI_SHORT_SIDE:
        scale = _OPENAI_SHORT_SIDE / min(width, height)
        width, height = round(width * scale), round(height * scale)
    tiles = math.ceil(width / _OPENAI_TILE) * math.ceil(height / _OPENAI_TILE)
    return _OPENAI_BASE_TOKENS + _OPENAI_TOKENS_PER_TILE * tiles


def _openai_image_tokens(width: int, height: int, model: str) -> int:
    spec = _openai_patch_spec(model)
    if spec is not None:
        return _openai_patch_tokens(width, height, spec[0], spec[1])
    return _openai_tile_tokens(width, height)


def _gemini_image_tokens(width: int, height: int) -> int:
    if width <= _GEMINI_SMALL_SIDE and height <= _GEMINI_SMALL_SIDE:
        return _GEMINI_TOKENS_PER_TILE
    tiles = math.ceil(width / _GEMINI_TILE) * math.ceil(height / _GEMINI_TILE)
    return tiles * _GEMINI_TOKENS_PER_TILE


def _anthropic_image_tokens(width: int, height: int) -> int:
    return min(round(width * height / _ANTHROPIC_PIXELS_PER_TOKEN), _ANTHROPIC_MAX_TOKENS)


def _tokens_for_dimensions(width: int, height: int, normalized_model: str) -> int:
    if "gemini" in normalized_model:
        return _gemini_image_tokens(width, height)
    if "claude" in normalized_model or "anthropic" in normalized_model:
        return _anthropic_image_tokens(width, height)
    return _openai_image_tokens(width, height, normalized_model)


def estimate_image_tokens(screenshots: list[bytes] | None, model: str) -> int:
    """Estimate provider-billed image tokens for the screenshots sent in one LLM call.

    Deterministic from image dimensions + model family, so it is stable across an A/B
    and independent of the provider's blended ``prompt_tokens``. Used only when the
    provider does not report image tokens directly (see ``provider_image_tokens``).
    """
    if not screenshots:
        return 0
    normalized = _normalize_model(model)
    total = 0
    for image in screenshots:
        dims = _image_dimensions(image)
        if dims is None:
            continue
        total += _tokens_for_dimensions(dims[0], dims[1], normalized)
    return total


def _input_cost_per_token(model: str) -> float:
    for key in (model, _normalize_model(model)):
        try:
            info = litellm.model_cost.get(key)
        except Exception:
            info = None
        if info and info.get("input_cost_per_token"):
            return float(info["input_cost_per_token"])
    return 0.0


def estimate_image_cost(image_tokens: int, model: str) -> float:
    """Dollar cost of ``image_tokens`` at the model's input-token price.

    Returns 0.0 when litellm has no price for the model (e.g. custom Azure deployment
    names); the token count is still meaningful and the dashboard can fall back to
    ``image_tokens * (llm_cost / input_tokens)``.
    """
    if image_tokens <= 0:
        return 0.0
    return image_tokens * _input_cost_per_token(model)


def provider_image_tokens(response: Any) -> int | None:
    """Prompt image tokens the provider actually billed, when it reports them.

    litellm populates ``usage.prompt_tokens_details.image_tokens`` from Gemini/Vertex's
    per-modality breakdown; OpenAI and Anthropic leave it unset, so this returns None and
    the caller falls back to ``estimate_image_tokens``.
    """
    usage = getattr(response, "usage", None)
    details = getattr(usage, "prompt_tokens_details", None)
    image_tokens = getattr(details, "image_tokens", None)
    if image_tokens is None:
        return None
    try:
        value = int(image_tokens)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None
