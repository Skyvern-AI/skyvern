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


# Selector resolution mirrors Playwright's engine for the shapes Skyvern's producers emit.
# Engine dispatch: `xpath=` / `css=` / `id=` / `text=` prefixes, bare XPath auto-detection
# (`//`, `..`, and parenthesised `(//x)[n]`), and the css engine's `:has-text()` / `:visible`
# tail pseudo-extensions. Anything else still fails loudly as invalid CSS. Copied Playwright
# behaviors that matter for correctness: a leading `/` is rewritten relative when the root is an
# element (chained absolute paths stay inside their scope); XPath results keep only element nodes
# (a `..` hop onto an attribute/text/shadow-root node vanishes); CSS pierces open shadow roots,
# including when the root itself is the host, with a root's light-DOM matches emitted before its
# shadow matches; `text=` matches the smallest containing element, case-insensitively when
# unquoted, exactly when quoted. `:has-text()`/`:visible` resolve
# anywhere Playwright allows them -- comma lists, composed with native pseudos, intermediate
# compounds (via a :scope recursion per candidate). Text containment across nested shadow
# boundaries follows textContent, which does not cross shadow roots -- same as per-root matching.
_QUERY_ALL_JS = """
const __queryAll = (root, selector) => {
  const normText = (s) => s.replace(/\\s+/g, ' ').trim();
  const elementText = (el) => {
    if (el.tagName === 'INPUT' && (el.type === 'button' || el.type === 'submit')) return el.value || '';
    if (/^(SCRIPT|STYLE|NOSCRIPT)$/.test(el.tagName)) return '';
    let text = '';
    for (const child of el.childNodes) {
      if (child.nodeType === 3) text += child.nodeValue;
      else if (child.nodeType === 1) text += elementText(child);
    }
    return text;
  };
  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  };
  const queryCss = (from, css) => {
    const out = [];
    const visit = (r) => {
      for (const el of r.querySelectorAll(css)) out.push(el);
      if (r.nodeType === 1 && r.shadowRoot) visit(r.shadowRoot);
      for (const el of r.querySelectorAll('*')) {
        if (el.shadowRoot) visit(el.shadowRoot);
      }
    };
    visit(from);
    return out;
  };
  const queryXPath = (xp) => {
    if (xp.startsWith('/') && root.nodeType !== 9) xp = '.' + xp;
    const doc = root.nodeType === 9 ? root : root.ownerDocument;
    const it = doc.evaluate(xp, root, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
    const out = [];
    for (let i = 0; i < it.snapshotLength; i++) {
      const n = it.snapshotItem(i);
      if (n && n.nodeType === 1) out.push(n);
    }
    return out;
  };

  let engine = 'css';
  let body = selector;
  const prefixed = /^([a-zA-Z][a-zA-Z0-9-]*)=([\\s\\S]*)$/.exec(selector);
  if (prefixed && ['css', 'xpath', 'id', 'text'].includes(prefixed[1])) {
    engine = prefixed[1];
    body = prefixed[2];
  } else if (selector.startsWith('//') || selector.startsWith('..') || /^\\(+\\s*\\.?\\//.test(selector)) {
    engine = 'xpath';
  }

  if (engine === 'xpath') return queryXPath(body);
  if (engine === 'id') return queryCss(root, '[id=' + JSON.stringify(body) + ']');
  if (engine === 'text') {
    let matchesText;
    const re = /^\\/(.*)\\/([a-z]*)$/s.exec(body);
    const quoted =
      body.length > 1 &&
      ((body.startsWith('"') && body.endsWith('"')) || (body.startsWith("'") && body.endsWith("'")));
    if (re) {
      matchesText = (t) => new RegExp(re[1], re[2]).test(t);
    } else if (quoted) {
      const exact = normText(body.slice(1, -1));
      matchesText = (t) => t === exact;
    } else {
      const needle = normText(body).toLowerCase();
      matchesText = (t) => t.toLowerCase().includes(needle);
    }
    // Playwright's text engine never matches inside head/script/style content.
    const matches = queryCss(root, '*').filter(
      (el) => !/^(HEAD|SCRIPT|STYLE|NOSCRIPT)$/.test(el.tagName) && matchesText(normText(elementText(el)))
    );
    return matches.filter(
      (el) => !matches.some((m) => m !== el && (el.contains(m) || (el.shadowRoot && el.shadowRoot.contains(m))))
    );
  }

  // CSS with the :visible / :has-text() extensions anywhere Playwright allows them: in
  // comma-separated lists, composed with native pseudos, and on intermediate compounds.
  // scanCss marks indices outside quotes, brackets, and parens so splitting and pseudo
  // detection never trip on selector-internal punctuation.
  const scanCss = (s) => {
    const top = new Array(s.length).fill(false);
    let quote = null;
    let depth = 0;
    for (let i = 0; i < s.length; i++) {
      const ch = s[i];
      if (quote) {
        if (ch === '\\\\') { i++; continue; }
        if (ch === quote) quote = null;
        continue;
      }
      if (ch === '"' || ch === "'") { quote = ch; continue; }
      if (ch === '[' || ch === '(') { depth++; continue; }
      if (ch === ']' || ch === ')') { depth--; continue; }
      if (depth === 0) top[i] = true;
    }
    return top;
  };
  const findPseudo = (s) => {
    const top = scanCss(s);
    for (let i = 0; i < s.length; i++) {
      if (!top[i] || s[i] !== ':') continue;
      if (s.startsWith(':visible', i) && !/[a-zA-Z-]/.test(s[i + 8] || '')) {
        return { start: i, end: i + 8, kind: 'visible' };
      }
      if (s.startsWith(':has-text(', i)) {
        let j = i + 10;
        let quote = null;
        for (; j < s.length; j++) {
          const ch = s[j];
          if (quote) {
            if (ch === '\\\\') { j++; continue; }
            if (ch === quote) quote = null;
            continue;
          }
          if (ch === '"' || ch === "'") { quote = ch; continue; }
          if (ch === ')') break;
        }
        return { start: i, end: j + 1, kind: 'has-text', raw: s.slice(i + 10, j).trim() };
      }
    }
    return null;
  };
  const hasTextPredicate = (raw) => {
    if (raw.length > 1 && (raw[0] === '"' || raw[0] === "'") && raw[raw.length - 1] === raw[0]) {
      raw = raw.slice(1, -1).replace(/\\\\(.)/g, '$1');
    }
    const needle = normText(raw).toLowerCase();
    return (el) => normText(elementText(el)).toLowerCase().includes(needle);
  };
  const resolveComplex = (r, sel) => {
    const found = findPseudo(sel);
    if (!found) return queryCss(r, sel);
    const top = scanCss(sel);
    let cStart = 0;
    for (let i = found.start - 1; i >= 0; i--) {
      if (top[i] && ' >+~'.includes(sel[i])) { cStart = i + 1; break; }
    }
    let cEnd = sel.length;
    for (let i = found.end; i < sel.length; i++) {
      if (top[i] && ' >+~'.includes(sel[i])) { cEnd = i; break; }
    }
    let compound = sel.slice(cStart, cEnd);
    const predicates = [];
    for (;;) {
      const p = findPseudo(compound);
      if (!p) break;
      predicates.push(p.kind === 'visible' ? isVisible : hasTextPredicate(p.raw));
      compound = compound.slice(0, p.start) + compound.slice(p.end);
    }
    const base = sel.slice(0, cStart) + (compound.trim() === '' ? '*' : compound);
    let candidates = queryCss(r, base);
    for (const pred of predicates) candidates = candidates.filter(pred);
    const rest = sel.slice(cEnd);
    if (rest.trim() === '') return candidates;
    if (rest.trimStart()[0] === '+' || rest.trimStart()[0] === '~') {
      // Loud, not silently empty: :scope queries only reach descendants, so a sibling
      // combinator after a custom pseudo would always resolve to nothing.
      throw new Error('sibling combinator after :visible/:has-text() is not supported: ' + sel);
    }
    const seen = new Set();
    const out = [];
    for (const c of candidates) {
      for (const el of resolveSelector(c, ':scope' + rest)) {
        if (!seen.has(el)) { seen.add(el); out.push(el); }
      }
    }
    return out;
  };
  const resolveSelector = (r, sel) => {
    const top = scanCss(sel);
    const parts = [];
    let start = 0;
    for (let i = 0; i < sel.length; i++) {
      if (top[i] && sel[i] === ',') { parts.push(sel.slice(start, i)); start = i + 1; }
    }
    parts.push(sel.slice(start));
    const trimmed = parts.map((p) => p.trim()).filter(Boolean);
    if (trimmed.length <= 1) return resolveComplex(r, trimmed[0] || sel);
    // A pseudo-free comma list is native CSS: one query keeps document order exactly.
    if (trimmed.every((part) => !findPseudo(part))) return queryCss(r, sel);
    const seen = new Set();
    const out = [];
    for (const part of trimmed) {
      for (const el of resolveComplex(r, part)) {
        if (!seen.has(el)) { seen.add(el); out.push(el); }
      }
    }
    // Merged branches must come back in document order -- .first/.nth() and capped consumers
    // depend on it. Stable sort leaves cross-shadow (disconnected) pairs in insertion order.
    out.sort((a, b) => {
      const pos = a.compareDocumentPosition(b);
      if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
      if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
      return 0;
    });
    return out;
  };
  return resolveSelector(root, body);
};
"""


def _split_selector_chain(selector: str) -> list[str]:
    """Split a Playwright `a >> b` chain into steps at the top level, leaving quoted `>>` alone."""
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(selector):
        ch = selector[i]
        if quote is not None:
            if ch == quote:
                quote = None
            current.append(ch)
        elif ch in "\"'":
            quote = ch
            current.append(ch)
        elif selector.startswith(">>", i):
            parts.append("".join(current).strip())
            current = []
            i += 2
            continue
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current).strip())
    return [part for part in parts if part] or [selector.strip()]


# Walks a locator chain inside the page: each step queries within the previous step's matches, and a
# step carrying an index narrows to exactly that match (negative counts from the end) before the next
# step runs. Returning either the count or one element from the same walk keeps the two consistent.
_RESOLVE_JS = f"""
(spec) => {{
  {_QUERY_ALL_JS}
  let current = [document];
  for (const step of spec.steps) {{
    let next = [];
    for (const root of current) {{
      next = next.concat(__queryAll(root, step.selector));
    }}
    next = Array.from(new Set(next));
    if (step.index !== null && step.index !== undefined) {{
      const at = step.index < 0 ? next.length + step.index : step.index;
      next = at >= 0 && at < next.length ? [next[at]] : [];
    }}
    current = next;
  }}
  if (spec.mode === 'count') return current.length;
  const at = spec.index < 0 ? current.length + spec.index : spec.index;
  return current[at] || null;
}}
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
            chain = _split_selector_chain(selector)
            self._steps: list[tuple[str, int | None]] = [(part, None) for part in chain[:-1]] + [(chain[-1], index)]
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
        chained = [(part, None) for part in _split_selector_chain(selector)]
        return Locator(self._frame_source, [*self._steps, *chained])

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
