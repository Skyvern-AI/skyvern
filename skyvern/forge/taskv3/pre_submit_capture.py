"""The last pre-submit page frames of a Task V3 run, persisted as artifacts when the run ends.

A submit destroys the page that held the filled form, so the frame taken immediately before a
submit-shaped action is the only artifact that shows every typed value next to its label.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

LOG = structlog.get_logger(__name__)

DEFAULT_RING_SIZE = 8
MAX_HTML_BYTES = 4 * 1024 * 1024
# Per step, on the hot path before a submit: a wedged page must cost the action a bounded wait.
CAPTURE_STEP_TIMEOUT_SECONDS = 5.0

# Serialisation carries attributes, not live state, so the live values are copied onto an INERT
# re-parse of the document (DOMParser: no browsing context, so no custom-element upgrade, no
# loads, no script) and the page the model acts on is never touched. Authored content attributes
# (hidden tokens, server-rendered values) survive as in HTML_ACTION unless the control is
# password-shaped, whose authored value is stripped too. The secret match deliberately
# over-includes (`passport`, `bypass` lose their value) because under-inclusion writes a plaintext
# password into a stored artifact; `autocomplete` survives a show-password toggle.
SERIALIZE_FORM_STATE_JS = """(maxBytes) => {
  const live = document.querySelectorAll('input,textarea,select');
  const doc = new DOMParser().parseFromString(document.documentElement.outerHTML, 'text/html');
  const root = doc.documentElement;
  const copy = root.querySelectorAll('input,textarea,select');
  const out = { html: null, bytes: 0, filled: 0, shadowHosts: 0,
                iframes: document.querySelectorAll('iframe,frame').length, error: null };
  if (live.length !== copy.length) { out.error = 'reparse mismatch: ' + live.length + ' live vs ' + copy.length; return out; }
  const secret = (el) => el.type === 'password'
    || /password/i.test(el.getAttribute('autocomplete') || '') || /pass|pwd/i.test(el.name + ' ' + el.id);
  const inert = (el) => secret(el) || el.type === 'file' || el.type === 'hidden';
  for (let i = 0; i < live.length; i++) {
    const el = live[i], c = copy[i];
    if (el.tagName === 'TEXTAREA') {
      if (secret(el)) { c.textContent = ''; continue; }
      if (el.value) out.filled++;
      c.textContent = el.value;
    } else if (el.tagName === 'SELECT') {
      if (el.value) out.filled++;
      for (let j = 0; j < el.options.length; j++) {
        const co = c.options[j];
        if (co) el.options[j].selected ? co.setAttribute('selected', '') : co.removeAttribute('selected');
      }
    } else if (el.type === 'checkbox' || el.type === 'radio') {
      if (el.checked) out.filled++;
      el.checked ? c.setAttribute('checked', '') : c.removeAttribute('checked');
    } else if (secret(el)) {
      // The authored `value` attribute is already in the serialisation; a controlled-input
      // framework mirrors the typed password into it.
      c.removeAttribute('value');
    } else if (!inert(el)) {
      if (el.value) out.filled++;
      c.setAttribute('value', el.value);
    }
  }
  for (const el of document.querySelectorAll('*')) if (el.shadowRoot) out.shadowHosts++;
  for (const el of root.querySelectorAll('*')) {
    for (const a of Array.from(el.attributes)) if (a.name.startsWith('data-tv3')) el.removeAttribute(a.name);
  }
  const html = '<!DOCTYPE html>\\n' + root.outerHTML;
  out.bytes = new TextEncoder().encode(html).length;
  if (out.bytes <= maxBytes) out.html = html;
  return out;
}"""


@dataclass
class PreSubmitFrame:
    ordinal: int
    tool_name: str
    url: str
    captured_at: float
    html: bytes | None
    screenshot: bytes | None
    filled: int = 0
    iframes: int = 0
    shadow_hosts: int = 0
    html_skipped_bytes: int = 0
    error: str | None = None
    # A `type` that presses Enter is captured before its text lands; the flag (never the text, nor
    # its length) keeps the frame from reading as an empty form.
    pending_text: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def html_document(self) -> bytes:
        """A skipped or failed DOM still yields a header-only document so the reason is recorded
        in-band; the ordinal, not the artifact position, pairs it with its screenshot."""
        # Both `-->` and `--!>` close an HTML comment, and url/error carry page-influenced text.
        url = self.url.replace("--", "-&#45;")
        error = self.error.replace("--", "-&#45;") if self.error is not None else None
        header = (
            f"<!-- skyvern pre_submit_frame ordinal={self.ordinal} tool={self.tool_name} "
            f"captured_at={self.captured_at:.3f} filled={self.filled} iframes={self.iframes} "
            f"shadow_hosts={self.shadow_hosts} dom_skipped_bytes={self.html_skipped_bytes} "
            f"pending_text={int(self.pending_text)} error={error!r} url={url!r} -->\n"
        )
        return header.encode("utf-8") + (self.html or b"")


def is_run_sampled(run_key: str, sample_rate: float) -> bool:
    """Deterministic per run, so a run's arm can be derived offline from its id alone."""
    if sample_rate >= 1.0:
        return True
    if sample_rate <= 0.0:
        return False
    bucket = int(hashlib.sha256(run_key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return bucket < sample_rate


class CapturePage(Protocol):
    @property
    def url(self) -> str: ...

    async def evaluate(self, expression: str, arg: Any = None) -> Any: ...

    async def screenshot(self, **kwargs: Any) -> bytes: ...


async def pre_submit_screenshot(page: CapturePage) -> bytes:
    """A forensic frame needs no determinism: the defaults fast-forward in-flight transitions
    (firing `transitionend`) and write an inline caret style onto focused inputs."""
    return await page.screenshot(
        animations="allow", caret="initial", full_page=False, timeout=CAPTURE_STEP_TIMEOUT_SECONDS * 1000
    )


class PreSubmitCaptureRing:
    def __init__(
        self,
        page_provider: Callable[[], Awaitable[CapturePage | None]],
        screenshot: Callable[[CapturePage], Awaitable[bytes]] | None,
        *,
        size: int = DEFAULT_RING_SIZE,
        max_html_bytes: int = MAX_HTML_BYTES,
        step_timeout_seconds: float = CAPTURE_STEP_TIMEOUT_SECONDS,
    ) -> None:
        """`screenshot(page)` shoots the SAME page object the DOM came from, so the pair is one frame."""
        self._page_provider = page_provider
        self._screenshot = screenshot
        # No in-loop selection: every kept frame persists and the offline query picks the pre-submit
        # one, so the terminal frame survives at most `size - 1` later submit-shaped actions.
        self._frames: deque[PreSubmitFrame] = deque(maxlen=size)
        self._max_html_bytes = max_html_bytes
        self._step_timeout = step_timeout_seconds
        self._captured = 0
        self._persisted = False

    @property
    def frames(self) -> list[PreSubmitFrame]:
        """The last `size` frames in capture order."""
        return list(self._frames)

    @property
    def captured(self) -> int:
        return self._captured

    async def capture(self, tool_name: str, args: dict[str, Any]) -> None:
        if self._persisted:
            return
        page = await self._page_provider()
        if page is None:
            return
        started = time.monotonic()
        url = ""
        try:
            url = page.url
        except Exception:
            pass
        html: bytes | None = None
        skipped = 0
        filled = 0
        iframes = 0
        shadow_hosts = 0
        error: str | None = None
        try:
            async with asyncio.timeout(self._step_timeout):
                serialized = await page.evaluate(SERIALIZE_FORM_STATE_JS, self._max_html_bytes)
            if not isinstance(serialized, dict) or serialized.get("error"):
                raise RuntimeError(f"serialisation failed: {serialized!r}")
            filled = int(serialized.get("filled") or 0)
            iframes = int(serialized.get("iframes") or 0)
            shadow_hosts = int(serialized.get("shadowHosts") or 0)
            if serialized.get("html") is None:
                # Oversized: the DOM never crossed the browser boundary, only its size did.
                skipped = int(serialized.get("bytes") or 0)
            else:
                html = str(serialized["html"]).encode("utf-8")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            LOG.warning("taskv3 pre-submit DOM capture failed", tool=tool_name, exc_info=True)
        screenshot: bytes | None = None
        if self._screenshot is not None:
            try:
                async with asyncio.timeout(self._step_timeout):
                    screenshot = await self._screenshot(page)
            except Exception:
                LOG.warning("taskv3 pre-submit screenshot failed", tool=tool_name, exc_info=True)
        if html is None and screenshot is None:
            return
        self._captured += 1
        frame = PreSubmitFrame(
            ordinal=self._captured,
            tool_name=tool_name,
            url=url,
            captured_at=time.time(),
            html=html,
            screenshot=screenshot,
            filled=filled,
            iframes=iframes,
            shadow_hosts=shadow_hosts,
            html_skipped_bytes=skipped,
            error=error,
            pending_text=tool_name == "type" and bool(args.get("text")),
            meta={"selector": args.get("selector")},
        )
        self._frames.append(frame)
        LOG.info(
            "taskv3 pre-submit frame captured",
            tool=tool_name,
            ordinal=self._captured,
            html_bytes=len(html) if html is not None else 0,
            html_skipped_bytes=skipped,
            error=error,
            filled=filled,
            iframes=iframes,
            screenshot_bytes=len(screenshot) if screenshot is not None else 0,
            capture_seconds=round(time.monotonic() - started, 3),
        )

    async def persist(
        self,
        write: Callable[[str, bytes], Awaitable[str | None]],
    ) -> int:
        """`write(kind, data)` stores one artifact (`kind` is "html" or "screenshot") and returns its id.
        Each frame writes its HTML then its screenshot, NEWEST frame first so a write budget that runs
        out drops the frames farthest from the submit; never raises."""
        if self._persisted:
            return 0
        self._persisted = True
        written = 0
        frames = self.frames
        for frame in reversed(frames):
            try:
                if await write("html", frame.html_document()) is not None:
                    written += 1
            except Exception:
                LOG.warning("taskv3 pre-submit HTML persist failed", ordinal=frame.ordinal, exc_info=True)
            if frame.screenshot is not None:
                try:
                    if await write("screenshot", frame.screenshot) is not None:
                        written += 1
                except Exception:
                    LOG.warning("taskv3 pre-submit screenshot persist failed", ordinal=frame.ordinal, exc_info=True)
        LOG.info(
            "taskv3 pre-submit frames persisted",
            frames=len(frames),
            captured=self._captured,
            artifacts=written,
            dom_skipped=sum(1 for f in frames if f.html is None),
        )
        return written
