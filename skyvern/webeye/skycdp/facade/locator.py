"""Locators: a lazy, re-resolving reference to elements matching a selector.

A Locator holds no element. Every action re-runs the query, which is what makes it survive a
re-render between the moment it is created and the moment it is used -- the same contract Playwright
offers, and the reason callers use locators instead of handles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from skyvern.webeye.skycdp.errors import CdpError, CdpTimeoutError
from skyvern.webeye.skycdp.facade.elements import ElementHandle, wait_for
from skyvern.webeye.skycdp.facade.timeouts import DEFAULT_ACTION_TIMEOUT_MS, seconds_from_ms

if TYPE_CHECKING:
    from skyvern.webeye.skycdp.facade.page import Frame


class FrameLocator:
    """A lazily-resolved reference to a frame, reached through one or more iframe selectors.

    Playwright builds this chain synchronously -- ``page.frame_locator(a).frame_locator(b)`` -- long
    before the frames are needed, and ``skyvern/webeye/utils/dom.py`` relies on that to describe a
    target one link per iframe. So the chain is recorded here and walked only when something is
    finally read or acted on.
    """

    def __init__(self, source: Frame | FrameLocator, selector: str) -> None:
        self._source = source
        self._selector = selector

    def __repr__(self) -> str:
        return f"<FrameLocator {self._selector}>"

    @property
    def page(self) -> Any:
        return self._source.page

    def frame_locator(self, selector: str) -> FrameLocator:
        return FrameLocator(self, selector)

    def locator(self, selector: str) -> Locator:
        return Locator(self, selector)

    async def resolve_frame(self) -> Frame:
        parent = self._source
        parent_frame = await parent.resolve_frame() if isinstance(parent, FrameLocator) else parent

        handle = await parent_frame.query_selector(self._selector)
        if handle is None:
            raise CdpError(f"frame_locator found no element matching {self._selector!r}")
        frame = await handle.content_frame()
        if frame is None:
            raise CdpError(f"element matching {self._selector!r} hosts no frame")
        return frame


# Walks a locator chain inside the page: each step queries within the previous step's matches, and a
# step carrying an index narrows to exactly that match (negative counts from the end) before the next
# step runs. Returning either the count or one element from the same walk keeps the two consistent.
_RESOLVE_JS = """
(spec) => {
  let current = [document];
  for (const step of spec.steps) {
    let next = [];
    for (const root of current) {
      next = next.concat(Array.from(root.querySelectorAll(step.selector)));
    }
    next = Array.from(new Set(next));
    if (step.index !== null && step.index !== undefined) {
      const at = step.index < 0 ? next.length + step.index : step.index;
      next = at >= 0 && at < next.length ? [next[at]] : [];
    }
    current = next;
  }
  if (spec.mode === 'count') return current.length;
  const at = spec.index < 0 ? current.length + spec.index : spec.index;
  return current[at] || null;
}
"""


class Locator:
    """A selector chain plus an optional index at the tail.

    The chain is kept as a list of (selector, index) steps rather than one concatenated string. Two
    reasons: `a, b` is a selector list whose meaning changes entirely under concatenation
    (`"a, b div"` is not `"(a, b) div"`), and an index taken partway along the chain has to narrow
    *that* step rather than the whole thing -- `nth(1).locator("span")` means the spans inside the
    second match, not the second span overall.
    """

    def __init__(
        self,
        frame: Frame | FrameLocator,
        selector: str | list[tuple[str, int | None]],
        *,
        index: int | None = None,
    ) -> None:
        # A locator built from a FrameLocator does not know its frame yet: the iframe chain has to be
        # walked at action time, not at construction, because the frames may not exist yet and the
        # caller builds the chain synchronously.
        self._frame_source = frame
        if isinstance(selector, str):
            self._steps: list[tuple[str, int | None]] = [(selector, index)]
        else:
            self._steps = list(selector)
            if index is not None:
                head, _ = self._steps[-1]
                self._steps[-1] = (head, index)

    def __repr__(self) -> str:
        return "<Locator " + " >> ".join(f"{sel}" + ("" if i is None else f"[{i}]") for sel, i in self._steps) + ">"

    @property
    def _selector(self) -> str:
        """A human-readable rendering of the chain, for error messages only."""
        return " >> ".join(sel + ("" if i is None else f"[{i}]") for sel, i in self._steps)

    @property
    def _index(self) -> int | None:
        return self._steps[-1][1]

    # -- narrowing ----------------------------------------------------------

    def locator(self, selector: str) -> Locator:
        return Locator(self._frame_source, [*self._steps, (selector, None)])

    def nth(self, index: int) -> Locator:
        return Locator(self._frame_source, self._steps, index=index)

    @property
    def first(self) -> Locator:
        return self.nth(0)

    @property
    def last(self) -> Locator:
        return self.nth(-1)

    @property
    def page(self) -> Any:
        return self._frame_source.page

    async def _frame(self) -> Frame:
        """The frame this locator resolves in, walking any iframe chain the first time it is needed."""
        source = self._frame_source
        return source if not hasattr(source, "resolve_frame") else await source.resolve_frame()

    # -- resolution ---------------------------------------------------------

    async def count(self) -> int:
        frame = await self._frame()
        return int(await frame.evaluate(_RESOLVE_JS, {"steps": self._encoded_steps(), "mode": "count"}))

    def _encoded_steps(self) -> list[dict[str, Any]]:
        return [{"selector": sel, "index": idx} for sel, idx in self._steps]

    async def element_handle(self, timeout: float | None = None) -> ElementHandle:
        """`timeout` is milliseconds, as Playwright's is."""
        return await wait_for(
            self._resolve,
            timeout=seconds_from_ms(timeout),
            description=f"selector {self._selector!r} to match an element",
        )

    async def element_handles(self) -> list[ElementHandle]:
        total = await self.count()
        handles = []
        for index in range(total):
            handle = await self._resolve_at(index)
            if handle is not None:
                handles.append(handle)
        return handles

    async def _resolve(self) -> ElementHandle | None:
        # The tail step's own index is applied by the chain walk, so the final pick is always the
        # first of whatever survived. Applying it again here would index into a one-element set.
        return await self._resolve_at(0)

    async def _resolve_at(self, index: int) -> ElementHandle | None:
        frame = await self._frame()
        return await frame.resolve_locator_chain(self._encoded_steps(), index)

    async def _act(self, name: str, timeout: float | None) -> ElementHandle:
        try:
            return await self.element_handle(timeout=timeout)
        except CdpTimeoutError as exc:
            budget = DEFAULT_ACTION_TIMEOUT_MS if timeout is None else timeout
            raise CdpTimeoutError(f"{name}: no element matched {self._selector!r} within {budget}ms") from exc

    # -- reads --------------------------------------------------------------

    async def text_content(self, timeout: float | None = None) -> str | None:
        return await (await self._act("text_content", timeout)).text_content()

    async def inner_text(self, timeout: float | None = None) -> str:
        return await (await self._act("inner_text", timeout)).inner_text()

    async def inner_html(self, timeout: float | None = None) -> str:
        return await (await self._act("inner_html", timeout)).inner_html()

    async def get_attribute(self, name: str, timeout: float | None = None) -> str | None:
        return await (await self._act("get_attribute", timeout)).get_attribute(name)

    async def input_value(self, timeout: float | None = None) -> str:
        return await (await self._act("input_value", timeout)).input_value()

    async def bounding_box(self, timeout: float | None = None) -> dict[str, float] | None:
        return await (await self._act("bounding_box", timeout)).bounding_box()

    # Deliberately absent: `content_frame`. On a Locator, Playwright exposes it as a *property*
    # returning a FrameLocator, not as an awaitable returning a Frame -- a shape divergence a caller
    # could not see until it failed at runtime. Production reaches frames through
    # ElementHandle.content_frame() instead (skyvern/webeye/utils/dom.py:140), which this engine does
    # implement with matching semantics. An AttributeError here is the loud failure; a lookalike
    # would be the silent one.

    async def evaluate(self, expression: str, arg: Any = None, timeout: float | None = None) -> Any:
        return await (await self._act("evaluate", timeout)).evaluate(expression, arg)

    async def _presence(self, name: str, absent: bool) -> bool:
        """Visibility questions answer for an absent element; an absent thing is simply not visible."""
        handle = await self._resolve()
        if handle is None:
            return absent
        return bool(await getattr(handle, name)())

    async def _state(self, name: str, timeout: float | None) -> bool:
        """State questions require an element. Playwright waits and then times out rather than
        inventing an answer, because False would be indistinguishable from a real False."""
        return bool(await getattr(await self._act(name, timeout), name)())

    async def is_visible(self, *, timeout: float | None = None) -> bool:
        return await self._presence("is_visible", absent=False)

    async def is_hidden(self) -> bool:
        return not await self.is_visible()

    async def is_checked(self, timeout: float | None = None) -> bool:
        return await self._state("is_checked", timeout)

    async def is_enabled(self, timeout: float | None = None) -> bool:
        return await self._state("is_enabled", timeout)

    async def is_disabled(self, timeout: float | None = None) -> bool:
        return await self._state("is_disabled", timeout)

    async def is_editable(self, timeout: float | None = None) -> bool:
        return await self._state("is_editable", timeout)

    # -- actions ------------------------------------------------------------

    async def click(self, timeout: float | None = None, **kwargs: Any) -> None:
        await (await self._act("click", timeout)).click(**kwargs)

    async def dblclick(self, timeout: float | None = None, **kwargs: Any) -> None:
        await (await self._act("dblclick", timeout)).dblclick(**kwargs)

    async def hover(self, timeout: float | None = None) -> None:
        await (await self._act("hover", timeout)).hover()

    async def focus(self, timeout: float | None = None) -> None:
        await (await self._act("focus", timeout)).focus()

    async def blur(self, timeout: float | None = None) -> None:
        await (await self._act("blur", timeout)).blur()

    async def fill(self, value: str, timeout: float | None = None) -> None:
        await (await self._act("fill", timeout)).fill(value)

    async def clear(self, timeout: float | None = None) -> None:
        await (await self._act("clear", timeout)).fill("")

    async def type(self, text: str, delay: float | None = None, timeout: float | None = None) -> None:
        await (await self._act("type", timeout)).type(text, delay=delay)

    async def press_sequentially(self, text: str, delay: float | None = None, timeout: float | None = None) -> None:
        await self.type(text, delay=delay, timeout=timeout)

    async def press(self, key: str, delay: float | None = None, timeout: float | None = None) -> None:
        await (await self._act("press", timeout)).press(key, delay=delay)

    async def check(self, timeout: float | None = None) -> None:
        await (await self._act("check", timeout)).check()

    async def uncheck(self, timeout: float | None = None) -> None:
        await (await self._act("uncheck", timeout)).uncheck()

    async def select_option(
        self,
        value: str | list[str] | None = None,
        timeout: float | None = None,
        *,
        index: int | None = None,
        label: str | list[str] | None = None,
        **_: Any,
    ) -> list[str]:
        return await (await self._act("select_option", timeout)).select_option(value)

    async def set_input_files(self, files: str | list[str], timeout: float | None = None) -> None:
        await (await self._act("set_input_files", timeout)).set_input_files(files)

    async def scroll_into_view_if_needed(self, *, timeout: float | None = None) -> None:
        await (await self._act("scroll_into_view_if_needed", timeout)).scroll_into_view_if_needed()

    async def dispatch_event(
        self, event_type: str, event_init: dict | None = None, timeout: float | None = None
    ) -> None:
        await (await self._act("dispatch_event", timeout)).dispatch_event(event_type, event_init)

    async def screenshot(self, timeout: float | None = None, **kwargs: Any) -> bytes:
        return await (await self._act("screenshot", timeout)).screenshot(timeout=timeout, **kwargs)

    async def wait_for(self, state: str = "visible", timeout: float | None = None) -> None:
        checks = {
            "attached": lambda: self._resolve(),
            "visible": lambda: self.is_visible(),
            "hidden": lambda: self.is_hidden(),
        }
        check = checks.get(state)
        if check is None:
            raise CdpError(f"unsupported wait state {state!r}")
        await wait_for(check, timeout=seconds_from_ms(timeout), description=f"{self._selector!r} to be {state}")
