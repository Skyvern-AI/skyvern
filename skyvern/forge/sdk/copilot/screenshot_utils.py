"""Screenshot resize/compress utilities for the copilot agent."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import structlog
from PIL import Image

COPILOT_SCREENSHOT_MAX_WIDTH = 1024
COPILOT_SCREENSHOT_MAX_HEIGHT = 768
COPILOT_JPEG_QUALITY = 60

LOG = structlog.get_logger()


class ScreenshotActionRelation(str, Enum):
    SAME_PAGE_OBSERVATION = "same_page_observation"
    AFTER_SOURCE_ACTION = "after_source_action"
    WORKFLOW_RUN_RESULT = "workflow_run_result"
    TOOL_RESULT = "tool_result"


class ProvenanceBinding(str, Enum):
    AGREE = "agree"
    DISAGREE = "disagree"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ScreenshotProvenance:
    source_tool: str
    captured_url: str | None
    observation_step: int | None
    browser_session_id: str | None
    workflow_run_id: str | None
    action_relation: ScreenshotActionRelation
    dispatch_url: str | None = None
    dispatch_browser_session_id: str | None = None
    producer_browser_session_id: str | None = None
    session_binding: ProvenanceBinding = ProvenanceBinding.UNAVAILABLE

    @classmethod
    def unknown(cls, *, source_tool: str) -> ScreenshotProvenance:
        return cls(
            source_tool=source_tool,
            captured_url=None,
            observation_step=None,
            browser_session_id=None,
            workflow_run_id=None,
            action_relation=ScreenshotActionRelation.TOOL_RESULT,
        )


@dataclass(frozen=True)
class ScreenshotEntry:
    b64: str
    mime: str  # "image/jpeg" or "image/png"
    capture_id: str
    provenance: ScreenshotProvenance
    captured_at: float = field(default_factory=time.monotonic)
    capture_event_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class CapturedFrame:
    b64: str
    captured_at: float
    captured_url: str | None = None
    browser_session_id: str | None = None
    dispatch_url: str | None = None
    dispatch_browser_session_id: str | None = None
    producer_browser_session_id: str | None = None
    session_binding: ProvenanceBinding = ProvenanceBinding.UNAVAILABLE


@dataclass(frozen=True)
class PendingFrameLease:
    capture_event_id: str
    capture_id: str
    input_fingerprint: str


def screenshot_result_facts(
    result: dict[str, Any],
    *,
    dispatch_url: str | None,
    dispatch_browser_session_id: str | None,
) -> tuple[str | None, str | None, ProvenanceBinding]:
    """Return only facts reported by the screenshot producer, plus their dispatch binding.

    Internal MCP responses may run in concise mode, where ``browser_context`` is deliberately
    omitted. In that case the binding is unavailable; the caller's dispatch target remains
    separately labelled and is never promoted to a producer fact.
    """
    data = result.get("data")
    producer_url: str | None = None
    if isinstance(data, dict):
        value = data.get("current_url") or data.get("url")
        producer_url = value if isinstance(value, str) and value else None
    browser_context = result.get("browser_context")
    producer_session_id: str | None = None
    if isinstance(browser_context, dict):
        value = browser_context.get("session_id")
        producer_session_id = value if isinstance(value, str) and value else None
    if dispatch_browser_session_id is None or producer_session_id is None:
        binding = ProvenanceBinding.UNAVAILABLE
    elif dispatch_browser_session_id == producer_session_id:
        binding = ProvenanceBinding.AGREE
    else:
        binding = ProvenanceBinding.DISAGREE
    return producer_url, producer_session_id, binding


def _fingerprint_projection(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        if value.get("type") in {"input_image", "image_url"}:
            return {"type": value.get("type")}
        return {
            str(key): _fingerprint_projection(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_fingerprint_projection(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def model_input_fingerprint(items: list[Any]) -> str:
    """Stable, order-sensitive identity for one logical model request.

    Image payloads project to their block type so pixels never enter the serialized
    fingerprint material. The digest distinguishes a provider retry from a later
    request whose model-visible conversation has advanced.
    """
    projected = _fingerprint_projection(items)
    encoded = json.dumps(projected, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def resize_screenshot_b64(
    b64_png: str,
    *,
    provenance: ScreenshotProvenance,
    captured_at: float | None = None,
) -> ScreenshotEntry:
    """Resize a base64 PNG to 1024x768 max and compress to JPEG.

    Uses thumbnail() to preserve aspect ratio — only shrinks, never enlarges.
    Converts RGBA/P modes to RGB for JPEG compatibility.
    """
    raw = base64.b64decode(b64_png)
    capture_id = f"sha256:{hashlib.sha256(raw).hexdigest()}"
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
        capture_id=capture_id,
        provenance=provenance,
        captured_at=captured_at if captured_at is not None else time.monotonic(),
    )


def enqueue_screenshot(
    ctx: Any,
    b64_png: str,
    *,
    provenance: ScreenshotProvenance,
    captured_at: float | None = None,
) -> bool:
    """Resize, compress, and store a screenshot for later LLM injection, reporting whether this call
    left an entry on the queue. Replaces any previously pending screenshot (cap to 1).
    """
    if not getattr(ctx, "supports_vision", False):
        return False
    try:
        entry = resize_screenshot_b64(b64_png, provenance=provenance, captured_at=captured_at)
    except Exception:
        LOG.warning("Failed to resize copilot screenshot", exc_info=True)
        return False
    pending = getattr(ctx, "pending_screenshots", None)
    if not isinstance(pending, list):
        return False
    if pending and isinstance(pending[0], ScreenshotEntry) and pending[0].captured_at > entry.captured_at:
        LOG.info(
            "copilot_older_screenshot_rejected",
            source_tool=entry.provenance.source_tool,
            pending_source_tool=pending[0].provenance.source_tool,
        )
        return False
    pending.clear()
    pending.append(entry)
    return True


def stage_screenshot_from_artifact(
    ctx: Any,
    result: dict[str, Any],
    *,
    provenance: ScreenshotProvenance,
    captured_at: float | None = None,
) -> bool:
    """Stage the frame a non-inline screenshot tool call wrote to disk, reporting whether this call
    left an entry on the queue rather than whether the queue is merely non-empty.
    """
    data = result.get("data")
    path = data.get("path") if isinstance(data, dict) else None
    if not isinstance(path, str) or not path:
        return False
    try:
        raw = Path(path).read_bytes()
    except (OSError, ValueError):
        LOG.info("Copilot screenshot artifact could not be read", path=path)
        return False
    return enqueue_screenshot(
        ctx,
        base64.b64encode(raw).decode("ascii"),
        provenance=provenance,
        captured_at=captured_at if captured_at is not None else time.monotonic(),
    )


def enqueue_screenshot_from_result(
    ctx: Any,
    result: dict[str, Any],
    *,
    provenance: ScreenshotProvenance,
) -> None:
    """Extract, validate, and enqueue a screenshot from a copilot result dict."""
    from skyvern.forge.sdk.copilot.output_utils import extract_screenshot_b64, is_valid_image_base64

    captured_at = time.monotonic()
    screenshot_b64 = extract_screenshot_b64(result)
    if screenshot_b64 and is_valid_image_base64(screenshot_b64):
        enqueue_screenshot(ctx, screenshot_b64, provenance=provenance, captured_at=captured_at)
