"""Screenshot resize/compress utilities for the copilot agent."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import time
import uuid
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import structlog
from PIL import Image

COPILOT_SCREENSHOT_MAX_WIDTH = 1024
COPILOT_SCREENSHOT_MAX_HEIGHT = 768
COPILOT_SHARP_MAX_WIDTH = 1600
COPILOT_SHARP_MAX_HEIGHT = 1200
COPILOT_JPEG_QUALITY = 60
_BROWSER_ABLATION_MAX_SCREENSHOT_FRAMES = 24

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
    viewport_css_px: tuple[int, int] | None = None

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
    image_px: tuple[int, int] | None = None
    # Sent on every model call that carries this frame while it fits the budget; session history keeps b64.
    sharp_b64: str | None = None
    sharp_px: tuple[int, int] | None = None


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
    viewport_css_px: tuple[int, int] | None = None


@dataclass(frozen=True)
class PendingFrameLease:
    capture_event_id: str
    capture_id: str
    input_fingerprint: str


@dataclass(frozen=True)
class ChatScreenshotFrame:
    image: bytes
    capture_id: str
    captured_at: datetime
    tool_call_id: str | None


_capturing_tool_call_id: ContextVar[str | None] = ContextVar("copilot_capturing_tool_call_id", default=None)


@contextlib.contextmanager
def capturing_tool_call(tool_call_id: str | None) -> Iterator[None]:
    """Name the tool call running in this task, so a frame it stages can be shown under that call."""
    token = _capturing_tool_call_id.set(tool_call_id)
    try:
        yield
    finally:
        _capturing_tool_call_id.reset(token)


def capturing_tool_call_id() -> str | None:
    return _capturing_tool_call_id.get()


@runtime_checkable
class CarriesChatScreenshots(Protocol):
    pending_chat_screenshots: list[ChatScreenshotFrame]
    chat_screenshot_capture_ids: set[str]


@runtime_checkable
class CarriesPendingScreenshots(Protocol):
    pending_screenshots: list[ScreenshotEntry]


class CarriesPendingFrames(CarriesPendingScreenshots, Protocol):
    pending_frame_lease: PendingFrameLease | None


class CarriesRunResultFrames(CarriesPendingFrames, Protocol):
    last_run_blocks_workflow_run_id: str | None


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


def screenshot_viewport_css_px(result: dict[str, Any]) -> tuple[int, int] | None:
    data = result.get("data")
    viewport = data.get("viewport") if isinstance(data, dict) else None
    if not isinstance(viewport, dict):
        return None
    width, height = viewport.get("width"), viewport.get("height")
    if type(width) is int and type(height) is int and width > 0 and height > 0:
        return width, height
    return None


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


def _fitted_jpeg_b64(img: Image.Image, box: tuple[int, int]) -> tuple[str, tuple[int, int]]:
    fitted = img.copy()
    fitted.thumbnail(box, Image.Resampling.LANCZOS)
    if fitted.mode != "RGB":
        fitted = fitted.convert("RGB")
    buf = io.BytesIO()
    fitted.save(buf, format="JPEG", quality=COPILOT_JPEG_QUALITY)
    return base64.b64encode(buf.getvalue()).decode("ascii"), fitted.size


def resize_screenshot_b64(
    b64_png: str,
    *,
    provenance: ScreenshotProvenance,
    captured_at: float | None = None,
) -> ScreenshotEntry:
    """Encode a frame as two JPEGs: a sharp copy fitted to 1600x1200 and a small copy fitted to 1024x768.

    thumbnail() preserves aspect ratio and never enlarges.
    """
    raw = base64.b64decode(b64_png)
    capture_id = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    with Image.open(io.BytesIO(raw)) as img:
        small_b64, small_px = _fitted_jpeg_b64(img, (COPILOT_SCREENSHOT_MAX_WIDTH, COPILOT_SCREENSHOT_MAX_HEIGHT))
        sharp_b64, sharp_px = _fitted_jpeg_b64(img, (COPILOT_SHARP_MAX_WIDTH, COPILOT_SHARP_MAX_HEIGHT))
    return ScreenshotEntry(
        b64=small_b64,
        mime="image/jpeg",
        capture_id=capture_id,
        provenance=provenance,
        captured_at=captured_at if captured_at is not None else time.monotonic(),
        image_px=small_px,
        sharp_b64=sharp_b64,
        sharp_px=sharp_px,
    )


def as_png(image: bytes) -> bytes:
    # A tool result may carry a JPEG, and the chat's artifact type is served as image/png.
    with Image.open(io.BytesIO(image)) as img:
        if img.format == "PNG":
            return image
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()


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
    if isinstance(ctx, CarriesChatScreenshots) and entry.capture_id not in ctx.chat_screenshot_capture_ids:
        ctx.chat_screenshot_capture_ids.add(entry.capture_id)
        ctx.pending_chat_screenshots.append(
            ChatScreenshotFrame(
                image=base64.b64decode(b64_png),
                capture_id=entry.capture_id,
                captured_at=datetime.now(UTC),
                tool_call_id=_capturing_tool_call_id.get(),
            )
        )
    if getattr(ctx, "eval_mode", None) == "browser_ablation":
        frames = getattr(ctx, "eval_screenshot_frames", None)
        if isinstance(frames, list) and not any(frame.get("capture_id") == entry.capture_id for frame in frames):
            frame = {
                "capture_id": entry.capture_id,
                "mime": entry.mime,
                "image_b64": entry.b64,
                "captured_url": entry.provenance.captured_url,
                "browser_session_id": entry.provenance.browser_session_id,
                "source_tool": entry.provenance.source_tool,
            }
            if len(frames) < _BROWSER_ABLATION_MAX_SCREENSHOT_FRAMES:
                frames.append(frame)
            else:
                # Keep starting-state evidence and roll the latter half so terminal evidence stays
                # current without allowing a single response frame to grow without bound.
                frames.pop(_BROWSER_ABLATION_MAX_SCREENSHOT_FRAMES // 2)
                frames.append(frame)
    return True


def withdraw_frame_captured_before(ctx: CarriesPendingFrames, cutoff: float) -> ScreenshotEntry | None:
    """Drop the pending frame if it was captured before `cutoff`, releasing a lease on it, and return it."""
    pending = ctx.pending_screenshots
    if not pending or pending[0].captured_at >= cutoff:
        return None
    entry = pending.pop(0)
    _release_lease_held_by(ctx, entry)
    return entry


def withdraw_superseded_run_result_frame(ctx: CarriesRunResultFrames, run_id: str) -> None:
    # Keyed on the latest committed run, not plain inequality: parallel run calls can store an older
    # run's evidence after a newer run queued its frame.
    if run_id != ctx.last_run_blocks_workflow_run_id:
        return
    pending = ctx.pending_screenshots
    if not pending:
        return
    provenance = pending[0].provenance
    if provenance.action_relation is not ScreenshotActionRelation.WORKFLOW_RUN_RESULT:
        return
    if provenance.workflow_run_id == run_id:
        return
    entry = pending.pop(0)
    _release_lease_held_by(ctx, entry)
    LOG.info(
        "copilot_superseded_run_frame_withdrawn",
        capture_event_id=entry.capture_event_id,
        frame_workflow_run_id=provenance.workflow_run_id,
        superseding_workflow_run_id=run_id,
    )


def _release_lease_held_by(ctx: CarriesPendingFrames, entry: ScreenshotEntry) -> None:
    if ctx.pending_frame_lease is not None and ctx.pending_frame_lease.capture_event_id == entry.capture_event_id:
        ctx.pending_frame_lease = None


def consume_screenshot_artifact(result: dict[str, Any]) -> bytes | None:
    data = result.get("data")
    path = data.get("path") if isinstance(data, dict) else None
    if not isinstance(path, str) or not path:
        return None
    artifact = Path(path)
    try:
        return artifact.read_bytes()
    except (OSError, ValueError):
        LOG.info("Copilot screenshot artifact could not be read")
        return None
    finally:
        with contextlib.suppress(OSError, ValueError):
            artifact.unlink(missing_ok=True)


VisibleEffect = Literal["changed", "unchanged", "unknown"]


@dataclass(frozen=True)
class ViewportFrame:
    png: bytes
    dispatch_session_id: str | None
    producer_url: str | None
    producer_session_id: str | None
    session_binding: ProvenanceBinding
    started_at: float
    viewport_css_px: tuple[int, int] | None = None


def viewport_visible_effect(pre: ViewportFrame | None, post: ViewportFrame | None) -> VisibleEffect:
    if pre is None or post is None:
        return "unknown"
    if ProvenanceBinding.DISAGREE in (pre.session_binding, post.session_binding):
        return "unknown"
    if pre.producer_session_id and post.producer_session_id and pre.producer_session_id != post.producer_session_id:
        return "unknown"
    if pre.dispatch_session_id and post.dispatch_session_id and pre.dispatch_session_id != post.dispatch_session_id:
        return "unknown"
    try:
        with Image.open(io.BytesIO(pre.png)) as pre_image, Image.open(io.BytesIO(post.png)) as post_image:
            pre_rgb = pre_image.convert("RGB")
            post_rgb = post_image.convert("RGB")
    except (OSError, ValueError):
        return "unknown"
    if pre_rgb.size == post_rgb.size and pre_rgb.tobytes() == post_rgb.tobytes():
        return "unchanged"
    return "changed"


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
