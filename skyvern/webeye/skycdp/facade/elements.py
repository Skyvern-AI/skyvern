"""Element handles and the actions performed on them.

The rule that governs this file: an action a user could perform with a mouse or keyboard is performed
through the CDP ``Input`` domain at real screen coordinates, never by mutating the DOM. Only state a
user cannot reach directly -- reading geometry, scrolling an element into view, choosing an
``<option>`` -- is done in JavaScript, and each of those is called out where it happens.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from skyvern.webeye.skycdp.errors import CdpError, CdpExecutionContextLost, CdpTimeoutError
from skyvern.webeye.skycdp.facade.evaluation import RemoteHandle, evaluate

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

_CLICK_POINT_JS = """
function() {
  this.scrollIntoViewIfNeeded ? this.scrollIntoViewIfNeeded(false)
                              : this.scrollIntoView({block: 'center', inline: 'center'});
  const rect = this.getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0) return null;
  return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2,
          width: rect.width, height: rect.height, left: rect.left, top: rect.top};
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
        return bool(
            await self._bound(
                "function() { return !this.disabled && !this.readOnly && this.isContentEditable !== false; }"
            )
        )

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
        point = await self._bound(_CLICK_POINT_JS)
        if not point:
            raise CdpError("element has no layout box and cannot be clicked")
        # Viewport coordinates from the element's own frame need the frame's offset added before
        # they mean anything to the Input domain, which speaks in top-level viewport space.
        offset_x, offset_y = await self._frame.viewport_offset()
        return point["x"] + offset_x, point["y"] + offset_y

    async def click(
        self,
        *,
        button: str = "left",
        click_count: int = 1,
        timeout: float | None = None,
        delay: float | None = None,
        force: bool = False,
        **_: Any,
    ) -> None:
        # The other way a caller triggers a request right after subscribing, and the one
        # production actually uses: ScopedXhrDownloadCapture subscribes, then clicks.
        await self._frame.page._network.settled()
        x, y = await self._click_point()
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
        """Replace the element's content using only trusted events.

        Focus, select everything already there, then either commit the new text with
        ``Input.insertText`` or -- for an empty value -- delete the selection. Chrome raises the
        resulting ``input`` event itself, so a framework's change handler sees ``isTrusted`` true
        and keeps the value instead of reverting it on the next render.
        """
        await self.scroll_into_view_if_needed()
        await self.focus()
        await self._bound(
            """function() {
                if (this.select) { this.select(); return; }
                const range = this.ownerDocument.createRange();
                range.selectNodeContents(this);
                const selection = this.ownerDocument.defaultView.getSelection();
                selection.removeAllRanges();
                selection.addRange(range);
            }"""
        )
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
            await self.click()

    async def uncheck(self, *, timeout: float | None = None, **_: Any) -> None:
        if await self.is_checked():
            await self.click()

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

    async def set_input_files(self, files: str | list[str], *, timeout: float | None = None, **_: Any) -> None:
        paths = [files] if isinstance(files, str) else list(files)
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
