"""Screenshot resize/compress utilities for the copilot agent."""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from typing import Any

import structlog
from PIL import Image

COPILOT_SCREENSHOT_MAX_WIDTH = 1024
COPILOT_SCREENSHOT_MAX_HEIGHT = 768
COPILOT_JPEG_QUALITY = 60

LOG = structlog.get_logger()


@dataclass
class ScreenshotEntry:
    b64: str
    mime: str  # "image/jpeg" or "image/png"


def resize_screenshot_b64(b64_png: str) -> ScreenshotEntry:
    """Resize a base64 PNG to 1024x768 max and compress to JPEG.

    Uses thumbnail() to preserve aspect ratio — only shrinks, never enlarges.
    Converts RGBA/P modes to RGB for JPEG compatibility.
    """
    raw = base64.b64decode(b64_png)
    img = Image.open(io.BytesIO(raw))
    img.thumbnail(
        (COPILOT_SCREENSHOT_MAX_WIDTH, COPILOT_SCREENSHOT_MAX_HEIGHT),
        Image.Resampling.LANCZOS,
    )
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=COPILOT_JPEG_QUALITY)
    return ScreenshotEntry(
        b64=base64.b64encode(buf.getvalue()).decode("ascii"),
        mime="image/jpeg",
    )


def enqueue_screenshot(ctx: Any, b64_png: str) -> None:
    """Resize, compress, and store a screenshot for later LLM injection.

    Replaces any previously pending screenshot (cap to 1).
    No-op if the context does not support vision.
    """
    if not getattr(ctx, "supports_vision", False):
        return
    try:
        entry = resize_screenshot_b64(b64_png)
    except Exception:
        LOG.warning("Failed to resize copilot screenshot", exc_info=True)
        return
    pending = getattr(ctx, "pending_screenshots", None)
    if isinstance(pending, list):
        pending.clear()
        pending.append(entry)


def enqueue_screenshot_from_result(ctx: Any, result: dict[str, Any]) -> None:
    """Extract, validate, and enqueue a screenshot from a copilot result dict."""
    from skyvern.forge.sdk.copilot.output_utils import extract_screenshot_b64, is_valid_image_base64

    screenshot_b64 = extract_screenshot_b64(result)
    if screenshot_b64 and is_valid_image_base64(screenshot_b64):
        enqueue_screenshot(ctx, screenshot_b64)
