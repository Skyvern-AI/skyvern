"""Element handles and the actions performed on them.

The rule that governs this file: an action a user could perform with a mouse or keyboard is performed
through the CDP ``Input`` domain at real screen coordinates, never by mutating the DOM. Only state a
user cannot reach directly -- reading geometry, scrolling an element into view, choosing an
``<option>`` -- is done in JavaScript, and each of those is called out where it happens.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, TypeVar

import structlog

from skyvern.webeye.skycdp.errors import CdpError, CdpExecutionContextLost, CdpTimeoutError
from skyvern.webeye.skycdp.facade.evaluation import RemoteHandle, evaluate
from skyvern.webeye.skycdp.facade.timeouts import DEFAULT_ACTION_TIMEOUT_MS, seconds_from_ms

LOG = structlog.get_logger()

_T = TypeVar("_T")


class FilePayload(TypedDict):
    name: str
    mimeType: str
    buffer: bytes


InputFiles = str | Path | FilePayload | list[str | Path | FilePayload]

if TYPE_CHECKING:
    from skyvern.webeye.skycdp.facade.page import Frame

# Wraps a caller's function so it receives the element as its first argument while still being
# invoked against the element, which is how Playwright's handle-scoped evaluate behaves.
_AS_FIRST_ARGUMENT = """
function(arg) { return (__USER_FUNCTION__).call(null, this, arg); }
"""

_VISIBLE_JS = """
function() {
  const style = this.ownerDocument.defaultView.getComputedStyle(this);
  if (style.visibility === 'hidden' || style.display === 'none') return false;
  const rect = this.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
"""

# Measured misses do not separate one rAF from two; two is used because the first callback runs before its frame
# paints. The 500ms cap is not tuned: it only keeps a frame that never fires rAF (hidden, throttled) from hanging.
_PRESENTED_JS = """
function() {
  return new Promise(resolve => {
    setTimeout(resolve, 500);
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  });
}
"""


async def _wait_presented(wait: Awaitable[Any]) -> None:
    # Per frame, so one frame's failure does not skip the rest. It fails when a page replaces
    # requestAnimationFrame with a non-function; the click can still land, so this must not fail it.
    try:
        await wait
    except CdpError:
        LOG.warning("skycdp could not wait for a frame to render before clicking", exc_info=True)


# null when the element can never hold text, otherwise whether it is editable now. isContentEditable is
# false on every form control, and readonly is read as the attribute because <select> has no readOnly property.
_EDITABLE_JS = """
function(allowSelect) {
  const name = this.localName;
  const control = name === 'input' || name === 'textarea' || (allowSelect && name === 'select');
  if (!control && (name === 'select' || !this.isContentEditable)) return null;
  return !this.matches(':disabled') && !(control && this.hasAttribute('readonly'));
}
"""


# Playwright's fill rules by input type: text-like types take typed text, date/range-like types take an assigned
# value plus input/change events, and every other type (checkbox, file, button...) cannot be filled. With checkOnly
# it returns 'ok' after the refusals and touches nothing, so a refused fill never moves focus.
_PREPARE_FILL_JS = """
function([value, checkOnly]) {
  if (this.localName === 'input') {
    const type = this.type.toLowerCase();
    const assigned = ['color', 'date', 'time', 'datetime-local', 'month', 'range', 'week'].includes(type);
    const typed = ['', 'email', 'number', 'password', 'search', 'tel', 'text', 'url'].includes(type);
    if (!assigned && !typed) return 'type:' + type;
    if (type === 'number' && isNaN(Number(value.trim()))) return 'not-a-number';
    if (checkOnly) return 'ok';
    if (assigned) {
      value = value.trim();
      this.value = value;
      if (this.value !== value) return 'malformed';
      this.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
      this.dispatchEvent(new Event('change', {bubbles: true}));
      return 'done';
    }
    this.select();
    return type === 'number' ? 'trim' : 'typed';
  }
  if (checkOnly) return 'ok';
  if (this.select) { this.select(); return 'typed'; }
  const range = this.ownerDocument.createRange();
  range.selectNodeContents(this);
  const selection = this.ownerDocument.defaultView.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  return 'typed';
}
"""


def _raise_if_refused(outcome: str) -> None:
    if outcome == "malformed":
        raise CdpError("fill: Malformed value")
    if outcome == "not-a-number":
        raise CdpError("fill: Cannot type text into input[type=number]")
    if outcome.startswith("type:"):
        raise CdpError(f'fill: Input of type "{outcome[5:]}" cannot be filled')


def not_editable_error(name: str, timeout: float | None) -> CdpTimeoutError:
    budget = DEFAULT_ACTION_TIMEOUT_MS if timeout is None else timeout
    return CdpTimeoutError(f"{name}: element is not editable (read-only or disabled) within {budget}ms")


async def wait_until_editable(
    probe: Callable[[], Awaitable[_T | None]], *, timeout: float | None, on_deadline: Callable[[], CdpTimeoutError]
) -> _T:
    """Only the budget running out becomes ``on_deadline()``; a CdpTimeoutError the probe raises is an
    unanswered CDP call and propagates as itself."""
    probe_timeout: CdpTimeoutError | None = None

    async def guarded() -> _T | None:
        nonlocal probe_timeout
        try:
            return await probe()
        except CdpTimeoutError as exc:
            probe_timeout = exc
            raise

    try:
        return await wait_for(guarded, timeout=seconds_from_ms(timeout), description="element to be editable")
    except CdpTimeoutError as exc:
        if exc is probe_timeout:
            raise
        raise on_deadline() from exc


_CLICK_POINT_JS = """
function() {
  this.scrollIntoViewIfNeeded ? this.scrollIntoViewIfNeeded(false)
                              : this.scrollIntoView({block: 'center', inline: 'center'});
  const rect = this.getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0) return null;
  const style = getComputedStyle(this);
  return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2,
          width: rect.width, height: rect.height, left: rect.left, top: rect.top,
          borderLeft: parseInt(style.borderLeftWidth, 10) || 0, borderTop: parseInt(style.borderTopWidth, 10) || 0};
}
"""

# Playwright's hit-target check: hit-test each root from the document down to this element's own, which reaches closed
# shadow roots a walk down from the document cannot. Returns null when the topmost element is this one or inside it.
_HIT_TARGET_JS = """
function(p) {
  const roots = [];
  for (let n = this; ; n = roots[0].host) { roots.unshift(n.getRootNode()); if (!roots[0].host) break; }
  let hit = null;
  for (let i = 0; i < roots.length; i++) {
    const e = roots[i].elementFromPoint(p.x, p.y);
    if (!e) break;
    hit = e;
    if (i + 1 < roots.length && e !== roots[i + 1].host) break;
  }
  if (!hit) return 'outside';
  for (let n = hit; n; n = n.parentNode || n.host) if (n === this) return null;
  // Slotted light-DOM content is drawn inside the target; assignedSlot is null under a closed root, so ask the slots.
  for (const s of [this, ...this.querySelectorAll('slot')])
    if (s.localName === 'slot' && s.assignedElements({flatten: true}).some((a) => a.contains(hit))) return null;
  return '<' + hit.localName + '>';
}
"""

# Selecting an option is not something a user does through a synthesisable event stream -- the
# native popup is browser chrome. Playwright resolves this the same way: set the selection in JS and
# fire the two events the platform would have fired.
_SELECT_OPTION_JS = """
function(values) {
  if (this.nodeName.toLowerCase() !== 'select') throw new Error('not a <select> element');
  const wanted = Array.isArray(values) ? values : [values];
  const selected = [];
  for (const option of Array.from(this.options)) {
    option.selected = wanted.includes(option.value) || wanted.includes(option.label);
    if (option.selected) selected.push(option.value);
  }
  this.dispatchEvent(new Event('input', {bubbles: true}));
  this.dispatchEvent(new Event('change', {bubbles: true}));
  return selected;
}
"""


# Playwright's cap on in-memory upload payloads; anything larger has to be passed as a file path.
MAX_UPLOAD_PAYLOAD_BYTES = 50 * 1024 * 1024

# An in-memory payload has no file on disk for DOM.setFileInputFiles, which would also derive the type from a file
# name. Playwright builds the File in the page with its declared type and fires the events a real pick fires.
_SET_INPUT_FILE_PAYLOADS_JS = """
function(payloads) {
  if (this.nodeName.toLowerCase() !== 'input' || (this.getAttribute('type') || '').toLowerCase() !== 'file')
    throw new Error('not an input[type=file] element');
  if (payloads.length > 1 && !this.multiple) throw new Error('Non-multiple file input can only accept single file');
  const transfer = new DataTransfer();
  for (const payload of payloads) {
    const bytes = Uint8Array.from(atob(payload.buffer), (c) => c.charCodeAt(0));
    transfer.items.add(new File([bytes], payload.name, {type: payload.mimeType}));
  }
  this.files = transfer.files;
  this.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
  this.dispatchEvent(new Event('change', {bubbles: true}));
}
"""


class JSHandle:
    def __init__(self, frame: Frame, handle: RemoteHandle) -> None:
        self._frame = frame
        self._handle = handle

    @property
    def _object_id(self) -> str:
        object_id = self._handle.object_id
        if object_id is None:
            raise CdpError("handle no longer references a page object")
        return object_id

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        """Call ``expression`` with this handle as its FIRST ARGUMENT, as Playwright does.

        Playwright's handle-scoped evaluate passes the element as an argument, not as ``this`` -- a
        function written against ``this`` silently sees ``undefined`` there. Matching that is the
        difference between a drop-in replacement and one that returns None for every read.
        """
        return await evaluate(
            self._handle.session,
            _AS_FIRST_ARGUMENT.replace("__USER_FUNCTION__", expression),
            arg,
            object_id=self._object_id,
        )

    async def _bound(self, expression: str, arg: Any = None) -> Any:
        """Call ``expression`` with this handle as ``this``. Internal to the engine's own snippets."""
        return await evaluate(self._handle.session, expression, arg, object_id=self._object_id)

    async def json_value(self) -> Any:
        return await self._handle.json_value()

    async def dispose(self) -> None:
        await self._handle.dispose()

    def as_element(self) -> ElementHandle | None:
        return self if isinstance(self, ElementHandle) else None


class ElementHandle(JSHandle):
    @property
    def _session(self) -> Any:
        return self._handle.session

    # -- reads --------------------------------------------------------------

    async def text_content(self) -> str | None:
        return await self._bound("function() { return this.textContent; }")

    async def inner_text(self) -> str:
        return await self._bound("function() { return this.innerText; }")

    async def inner_html(self) -> str:
        return await self._bound("function() { return this.innerHTML; }")

    async def get_attribute(self, name: str) -> str | None:
        return await self._bound("function(name) { return this.getAttribute(name); }", name)

    async def input_value(self, *, timeout: float | None = None) -> str:
        return await self._bound("function() { return this.value; }")

    async def is_visible(self, *, timeout: float | None = None) -> bool:
        return bool(await self._bound(_VISIBLE_JS))

    async def is_hidden(self) -> bool:
        return not await self.is_visible()

    async def is_checked(self) -> bool:
        return bool(await self._bound("function() { return !!this.checked; }"))

    async def is_enabled(self) -> bool:
        return not await self.is_disabled()

    async def is_disabled(self) -> bool:
        # `:disabled` rather than the `disabled` property: a control inside a disabled <fieldset> is
        # disabled by the platform, but its own IDL property stays false.
        return bool(
            await self._bound("function() { return this.matches ? this.matches(':disabled') : !!this.disabled; }")
        )

    async def is_editable(self) -> bool:
        editable = await self._bound(_EDITABLE_JS, True)
        if editable is None:
            raise CdpError("Element is not an <input>, <textarea>, <select> or [contenteditable] element")
        return bool(editable)

    async def _fillable(self, name: str) -> bool:
        editable = await self._bound(_EDITABLE_JS, False)
        if editable is None:
            raise CdpError(f"{name}: element is not an <input>, <textarea> or [contenteditable] element")
        return bool(editable)

    async def bounding_box(self) -> dict[str, float] | None:
        box = await self._bound(
            """function() {
                const rect = this.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) return null;
                return {x: rect.left, y: rect.top, width: rect.width, height: rect.height};
            }"""
        )
        return box

    async def content_frame(self) -> Frame | None:
        return await self._frame.page.frame_for_element(self)

    async def owner_frame(self) -> Frame:
        return self._frame

    async def query_selector_all(self, selector: str) -> list[ElementHandle]:
        return await self._frame.query_selector_all(selector, root=self)

    # -- actions ------------------------------------------------------------

    async def scroll_into_view_if_needed(self, *, timeout: float | None = None) -> None:
        # The scroll is a single synchronous DOM call, so there is nothing to time out; the argument
        # exists because Playwright's signature has it and production passes it. Dropping it from the
        # signature made every input_text action raise TypeError before it ever touched the field.
        await self._bound(
            """function() {
                this.scrollIntoViewIfNeeded ? this.scrollIntoViewIfNeeded(false)
                                            : this.scrollIntoView({block: 'center', inline: 'center'});
            }"""
        )

    async def _click_point(self) -> tuple[float, float]:
        await self._wait_frames_presented()
        return await self._measure_point()

    async def _wait_frames_presented(self) -> None:
        if self._session is self._frame.page.session:
            return
        # Chrome routes input into an out-of-process frame by browser-side hit testing, which only
        # knows the frame once every embedder above it has rendered it in. Until then a correctly
        # addressed click is delivered to an embedding <iframe> element instead, and nothing raises.
        await self.scroll_into_view_if_needed()
        await _wait_presented(self._bound(_PRESENTED_JS))
        embedder = self._frame.parent_frame
        while embedder is not None:
            await _wait_presented(embedder.evaluate(_PRESENTED_JS))
            embedder = embedder.parent_frame

    async def _measure_point(self, position: dict[str, float] | None = None) -> tuple[float, float]:
        point = await self._bound(_CLICK_POINT_JS)
        if not point:
            raise CdpError("element has no layout box and cannot be clicked")
        # Viewport coordinates from the element's own frame need the frame's offset added before
        # they mean anything to the Input domain, which speaks in top-level viewport space.
        offset_x, offset_y = await self._frame.viewport_offset()
        if position is not None:
            # Playwright measures `position` from the top-left of the padding box.
            x = point["left"] + point["borderLeft"] + position["x"]
            y = point["top"] + point["borderTop"] + position["y"]
            return x + offset_x, y + offset_y
        return point["x"] + offset_x, point["y"] + offset_y

    async def _hit_elsewhere(self, x: float, y: float) -> str | None:
        """What a click at top-level (x, y) lands on instead of this element: checked in its own frame, then in each
        embedder against the <iframe> holding the frame below."""
        frame = self._frame
        element: ElementHandle = self
        try:
            while True:
                offset_x, offset_y = await frame.viewport_offset()
                found = await element._bound(_HIT_TARGET_JS, {"x": x - offset_x, "y": y - offset_y})
                parent = frame.parent_frame
                if found is not None or parent is None:
                    return found
                owner = await frame.frame_element()
                if element is not self:
                    await element.dispose()
                element, frame = owner, parent
        finally:
            if element is not self:
                with contextlib.suppress(CdpError):
                    await element.dispose()

    async def click(
        self,
        *,
        button: str = "left",
        click_count: int = 1,
        timeout: float | None = None,
        delay: float | None = None,
        force: bool = False,
        position: dict[str, float] | None = None,
        **_: Any,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds_from_ms(timeout)
        # The other way a caller triggers a request right after subscribing, and the one
        # production actually uses: ScopedXhrDownloadCapture subscribes, then clicks.
        await self._frame.page._network.settled()
        await self._wait_frames_presented()
        while True:
            # Re-measured each attempt: the cover can lift as the element moves, leaving something else at the old point.
            x, y = await self._measure_point(position)
            if force or (found := await self._hit_elsewhere(x, y)) is None:
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                budget = DEFAULT_ACTION_TIMEOUT_MS if timeout is None else timeout
                if found == "outside":
                    raise CdpTimeoutError(f"click: element is outside of the viewport within {budget}ms")
                raise CdpTimeoutError(f"click: {found} intercepts pointer events within {budget}ms")
            await asyncio.sleep(min(0.05, remaining))
        await self._frame.page.mouse.click(x, y, button=button, click_count=click_count)

    async def dblclick(self, **_: Any) -> None:
        x, y = await self._click_point()
        await self._frame.page.mouse.dblclick(x, y)

    async def hover(self, *, timeout: float | None = None, **_: Any) -> None:
        x, y = await self._click_point()
        await self._frame.page.mouse.move(x, y)

    async def focus(self) -> None:
        await self._session.send("DOM.focus", {"objectId": self._object_id})

    async def blur(self) -> None:
        await self._bound("function() { this.blur(); }")

    async def fill(self, value: str, *, timeout: float | None = None, **_: Any) -> None:
        async def fillable() -> bool:
            # wait_for retries a lost context, but a handle cannot follow a navigation, so fail now.
            try:
                return await self._fillable("fill")
            except CdpExecutionContextLost as exc:
                raise CdpError("fill: Element is not attached to the DOM") from exc

        await wait_until_editable(fillable, timeout=timeout, on_deadline=lambda: not_editable_error("fill", timeout))
        await self._commit_fill(value)

    # Commits with trusted Input.insertText, or a Delete for an empty value, so a framework's input handler
    # sees isTrusted and keeps the value instead of reverting it on the next render.
    async def _commit_fill(self, value: str) -> None:
        _raise_if_refused(await self._bound(_PREPARE_FILL_JS, [value, True]))
        await self.scroll_into_view_if_needed()
        await self.focus()
        outcome = await self._bound(_PREPARE_FILL_JS, [value, False])
        _raise_if_refused(outcome)
        if outcome == "done":
            return
        if outcome == "trim":
            value = value.strip()
        keyboard = self._frame.page.keyboard
        if value:
            await keyboard.insert_text(value)
        else:
            await keyboard.press("Delete")

    async def type(self, text: str, delay: float | None = None, *, timeout: float | None = None, **_: Any) -> None:
        await self.focus()
        await self._frame.page.keyboard.type(text, delay=delay)

    async def press(self, key: str, delay: float | None = None, *, timeout: float | None = None, **_: Any) -> None:
        await self.focus()
        await self._frame.page.keyboard.press(key, delay=delay)

    async def check(self, *, timeout: float | None = None, **_: Any) -> None:
        if not await self.is_checked():
            await self.click(timeout=timeout)

    async def uncheck(self, *, timeout: float | None = None, **_: Any) -> None:
        if await self.is_checked():
            await self.click(timeout=timeout)

    async def select_option(
        self,
        value: str | list[str] | None = None,
        *,
        index: int | None = None,
        label: str | list[str] | None = None,
        timeout: float | None = None,
        **_: Any,
    ) -> list[str]:
        return await self._bound(_SELECT_OPTION_JS, value)

    async def set_input_files(self, files: InputFiles, *, timeout: float | None = None, **_: Any) -> None:
        items = files if isinstance(files, (list, tuple)) else [files]
        payloads = [item for item in items if isinstance(item, dict)]
        if payloads:
            if len(payloads) != len(items):
                raise CdpError("File paths cannot be mixed with buffers")
            if sum(len(payload["buffer"]) for payload in payloads) > MAX_UPLOAD_PAYLOAD_BYTES:
                raise CdpError(
                    "Cannot set buffer larger than 50Mb, please write it to a file and pass its path instead."
                )
            encoded = [
                {
                    "name": payload["name"],
                    "mimeType": payload["mimeType"],
                    "buffer": base64.b64encode(payload["buffer"]).decode(),
                }
                for payload in payloads
            ]
            await self._bound(_SET_INPUT_FILE_PAYLOADS_JS, encoded)
            return
        paths = [str(item) for item in items]
        # Chrome accepts a path that does not exist and simply attaches nothing, so the upload fails
        # later as an empty submission rather than here as a bad argument.
        missing = [path for path in paths if not Path(path).is_file()]
        if missing:
            raise CdpError(f"cannot upload files that do not exist: {missing}")
        await self._session.send("DOM.setFileInputFiles", {"files": paths, "objectId": self._object_id})

    async def dispatch_event(self, event_type: str, event_init: dict | None = None) -> None:
        await self._bound(
            """function(spec) {
                this.dispatchEvent(new Event(spec.type, Object.assign({bubbles: true, cancelable: true}, spec.init)));
            }""",
            {"type": event_type, "init": event_init or {}},
        )

    async def screenshot(self, **kwargs: Any) -> bytes:
        return await self._frame.page.screenshot(element=self, **kwargs)


async def wait_for(
    resolve: Any,
    *,
    timeout: float,
    description: str,
    interval: float = 0.05,
) -> Any:
    """Poll ``resolve`` until it returns something truthy or the deadline passes."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            result = await resolve()
        except CdpExecutionContextLost:
            # A navigation swapped the document mid-poll. The page is alive and the next poll runs
            # against the new document, which is exactly what a caller waiting on a selector wants.
            result = None
        if result:
            return result
        if asyncio.get_running_loop().time() >= deadline:
            raise CdpTimeoutError(f"timed out after {timeout}s waiting for {description}")
        await asyncio.sleep(interval)
