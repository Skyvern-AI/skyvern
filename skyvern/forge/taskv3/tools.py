"""Raw-browser tools for the Task V3 native harness.

These drive the run's live Playwright page **directly** (raw DOM / CDP) — no calls into
the task/prompt ecosystem (no LLM-backed observe/act/extract). That is the whole point:
the agent perceives via a raw DOM snapshot and acts by selector, so the only LLM in the
loop is the agent's own persistent conversation.

`build_browser_tools(page_provider, ...)` returns `ToolSpec`s that resolve their page via
`page_provider` on every call (not a page bound once), ready to hand to `run_agent_tool_loop`
alongside `make_finish_tool()`.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import random
import re
import secrets
import time
import unicodedata
import weakref
from collections import deque
from enum import Enum
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Awaitable, Callable, NamedTuple

import structlog
from PIL import Image, ImageDraw

from skyvern.config import settings
from skyvern.constants import BROWSER_DOWNLOADING_SUFFIX
from skyvern.core.script_generations.fuzzy_matcher import (
    match_option_exact_or_stem,
    match_option_exact_or_stem_with_tier,
    normalize_option_label,
)
from skyvern.forge.sdk.core.skyvern_context import URL_IN_TEXT, canonical_url, opaque_url_echo_window
from skyvern.forge.taskv3.loop import (
    NAVIGATION_DEAD_END_STATUSES,
    PAGE_UNAVAILABLE_ERROR,
    SemanticCommitStats,
    ToolHandler,
    ToolResult,
    ToolSpec,
)
from skyvern.forge.taskv3.preflight import PREFLIGHT_TOOL_NAMES, preflight_tool_action

if TYPE_CHECKING:
    # opaque_refs imports auth_tools which imports this module, so it can only be referenced for
    # typing; the OpaqueUrlRefs instance is passed in at runtime, never imported here.
    from skyvern.forge.taskv3.opaque_refs import OpaqueUrlRefs

LOG = structlog.get_logger()

# Resolved fresh per tool call rather than a page bound once, so a click that opens a new
# tab/popup is followed on the next call instead of leaving the loop stuck on a stale page.
PageProvider = Callable[[], Awaitable[Any]]

# Answers one question for the selector guard: did this action fail because its target is markup the
# page never renders? None means "not that", so the original failure keeps its own reporting.
InertTargetDiagnosis = Callable[[str, Exception], Awaitable["ToolResult | None"]]

# Cap on the page URL observe() echoes. Callers that register a secret URL for exact-match redaction
# must register this prefix too, or the truncated echo survives the scrub.
OBSERVE_URL_MAX_CHARS = 300

# Cap on what get_html returns, for markup and for rendered text alike.
HTML_MAX_CHARS = 20000

# Nothing in these may vary between two reads of an unchanged page: get_html's content is hashed
# into the loop's perception digests, which decide whether the run has returned to known ground.
# No quote character may appear in either notice: loop._TV3_MARKER_CUT_RE recognizes a marker the cut
# left open only while no quote follows it, and the notice is what follows. Only the whole-page read
# is steered toward text — a cut element read already has the element it asked about.
_MARKUP_CUT = f"…[truncated at {HTML_MAX_CHARS} chars]"
_PAGE_MARKUP_CUT = (
    f"…[truncated at {HTML_MAX_CHARS} chars - for the visible text instead of markup, call get_html with format=text]"
)
# ponytail: text past the cap stays unreachable; add an offset argument if a real page needs it.
# Component text is appended after the light DOM (_PAGE_TEXT_JS walks document first), so it is what
# a cut drops first — hence naming the component read here rather than only a generic selector.
_RENDERED_TEXT_CUT = (
    f"…[rendered text truncated at {HTML_MAX_CHARS} chars - text inside components is appended last "
    "and is cut first; read one region with get_html and a selector, or call observe]"
)


def _escape_tags_in_text(text: str) -> str:
    """Neutralize start tags in a rendered-text result.

    The alias layer's tag scanner runs over every page-content result and a page controls every byte
    of its text, so a page printing `<input id="...">` as visible text would be handed the alias meant
    for a real element. Only `<` is escaped: masking matches a selector by its literal spelling, so
    escaping `"` or `&` would hide a quoted one from the pass that owns it.
    """
    return text.replace("<", "&lt;")


# The exact selector shapes our own enrichment mints: data-tv3 by observe(), data-tv3-menu by the
# click menu probe, data-tv3-act by act-by-mark (written on the look-resolved element at act time and
# kept for the life of the document, so the submit watch can still resolve it turns later). Each
# exists only where we set it, so one that matches nothing now cannot reappear without a fresh
# observe / menu-opening click / look.
_TV3_MARKER_SELECTOR_RE = re.compile(r'^\[data-tv3(?:-menu|-act|-sugg)?="[^"\\]+"\]$')
# An opaque identifier (a uuid, or a run of 12+ hex digits) does not survive a model's copy: one
# transposed pair sends every later call to a selector that matches nothing. observe hands such a
# selector out under a short alias instead, resolved back before any handler sees it.
_OPAQUE_ID_RUN_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|(?=[0-9a-f]*[a-f])[0-9a-f]{12,}", re.I
)
# Lenient on purpose: the model may tag-qualify or unquote the handle; the number is what names it.
_ALIAS_SELECTOR_RE = re.compile(r'^\s*[a-z]*\[data-tv3-ref=["\']?(\d+|\?)["\']?\]\s*$', re.I)
# The attribute a raw value shared by more than one alias renders as: relabeling it to either alias
# would hand the model a handle for the other instance, and "?" never resolves.
_REDACTED_REF_ATTR = 'data-tv3-ref="?"'
# Written only by this masking layer, never by a page: any pre-existing copy in fetched markup or an
# exception message is stripped before it can be mistaken for one this layer minted. Captures the
# value so a dedupe pass can tell a redacted "?" apart from a usable ref without a second regex.
_DATA_TV3_REF_ATTR_RE = re.compile(r'\s+data-tv3-ref="([^"]*)"')
# The same attribute left unterminated by a truncation: dropping it would eat the text the cut
# appended after it, so it is defused in place instead (see `_strip_page_refs`).
_CUT_TV3_REF_ATTR_RE = re.compile(r'\s+data-tv3-ref="(?=[^"]*\Z)')


def _strip_page_refs(tag: str) -> str:
    """Remove every data-tv3-ref a page wrote in ONE start tag. A cut one keeps its bytes but gains a
    leading `?`, so the handle it spoofs resolves to no alias."""
    return _CUT_TV3_REF_ATTR_RE.sub(r"\g<0>?", _DATA_TV3_REF_ATTR_RE.sub("", tag))


# Precompiled so `_first_start_tag_span` can resume with `Pattern.search(text, pos)` (absolute
# indices, no copy) instead of re.search on a freshly sliced `text[pos:]` each call.
_START_TAG_OPEN_RE = re.compile(r"<[A-Za-z]")


def _first_start_tag_span(text: str, pos: int = 0) -> tuple[int, int] | None:
    # A start tag begins at `<` immediately followed by a letter: a closing tag or comment never
    # anchors it, but prose naming a real tag (Playwright's "not a <select> element") does. Use
    # `_owned_start_tag_span` when the span must actually carry an owned identity attribute; `>` is
    # legal unescaped inside a quoted attribute value, so the tag ends at the first `>` outside quotes.
    match = _START_TAG_OPEN_RE.search(text, pos)
    if match is None:
        return None
    start = match.start()
    quote: str | None = None
    for i in range(start + 1, len(text)):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == ">":
            return start, i
    # No `>` at all: a tag a truncation or Playwright's `…` elision left open still spans to the end
    # of the text, so a cut can never carry an identity attribute past the span-scoped masking passes.
    return start, len(text)


# A `<` inside a comment, CDATA section or raw-text element is page content, not markup: rewriting
# there would corrupt the source `get_html` returns verbatim, so every element below (each of which
# serializes its text children unescaped) is jumped over whole. `plaintext` has no end tag while
# parsing, but the fragment serialization `get_html` reads still emits a `</plaintext>` closer.
_RAW_TEXT_TAGS = (
    "script",
    "style",
    "textarea",
    "title",
    "iframe",
    "noscript",
    "xmp",
    "noembed",
    "noframes",
    "plaintext",
)
_SKIP_REGION_OPEN_RE = re.compile(r"<!--|<!\[CDATA\[|<[A-Za-z]")
_TAG_NAME_RE = re.compile(r"<([A-Za-z][^\s/>]*)")
_RAW_TEXT_CLOSE_RES = {name: re.compile(r"</" + name + r"\s*>", re.IGNORECASE) for name in _RAW_TEXT_TAGS}


def _start_tag_spans(text: str) -> list[tuple[int, int]]:
    """Every start-tag span in `text`, left to right; each search resumes from the previous span's end
    via `pos`, so no suffix of `text` is ever copied — O(n) total, not O(n) per tag. Comment, CDATA and
    raw-text regions are jumped over whole, so their contents are never mistaken for tags."""
    spans: list[tuple[int, int]] = []
    pos = 0
    while pos < len(text):
        opener = _SKIP_REGION_OPEN_RE.search(text, pos)
        if opener is None:
            break
        if opener.group(0) in ("<!--", "<![CDATA["):
            closer = "-->" if opener.group(0) == "<!--" else "]]>"
            closed_at = text.find(closer, opener.end())
            pos = len(text) if closed_at < 0 else closed_at + len(closer)
            continue
        span = _first_start_tag_span(text, opener.start())
        if span is None:
            break
        start, end = span
        spans.append((start, end))
        pos = end
        name = _TAG_NAME_RE.match(text, start)
        tag_name = name.group(1).lower() if name is not None else ""
        if tag_name in _RAW_TEXT_TAGS and not text[start:end].endswith("/"):
            close_re = _RAW_TEXT_CLOSE_RES.get(tag_name)
            close = close_re.search(text, end) if close_re is not None else None
            pos = len(text) if close is None else close.end()
    return spans


def _map_start_tags(text: str, fn: Callable[[str, int], str]) -> str:
    """Apply `fn(tag, start)` to each start-tag span in `text` only; everything between/outside spans
    (prose, page text, an error message with no markup at all) passes through untouched. `start` is the
    span's absolute offset, so a caller can tell one particular tag apart from every other one."""
    out: list[str] = []
    pos = 0
    for start, end in _start_tag_spans(text):
        out.append(text[pos:start])
        out.append(fn(text[start:end], start))
        pos = end
    out.append(text[pos:])
    return "".join(out)


# The identity attributes observe's naturalSelector names, and the only ones whose value is a
# selector a model can copy: a raw sitting in any of them has to be masked, whichever one the emitted
# selector happened to use.
_IDENTITY_ATTRS = ("id", "name", "data-testid")
# CSS string escapes, as observe's `attr()` writes them (`\` and `"`) and as CSS.escape would: a hex
# escape may swallow one following whitespace character, which is part of the escape, not the value.
_CSS_ESCAPE_RE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n\f\r]?|(.))", re.S)


def _decode_css_escapes(value: str) -> str:
    """The DOM attribute value a selector's quoted component spells: `[id="a\\"b"]` names `a"b`."""

    def _decoded(match: re.Match[str]) -> str:
        if match.group(1) is None:
            return match.group(2)
        code = int(match.group(1), 16)
        return "\ufffd" if code == 0 or code > 0x10FFFF or 0xD800 <= code <= 0xDFFF else chr(code)

    return _CSS_ESCAPE_RE.sub(_decoded, value)


def _css_escape_attr_value(value: str) -> str:
    """The spelling observe's `attr()` renders, which is also what Playwright's call log quotes."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _serialize_attr_value(value: str) -> str:
    """The spelling markup carries. Measured against real Chromium: an attribute value escapes these
    three and nothing else — `<` and `>` are escaped in TEXT nodes, and stay literal in a value."""
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("\u00a0", "&nbsp;")


@lru_cache(maxsize=1024)
def _markup_spellings(raw: str) -> tuple[str, ...]:
    """The only spelling a start tag can carry. Context-specific on purpose: the DOM ids `q&<uuid>`
    and the literal `q&amp;<uuid>` share a spelling once the spellings are pooled, and an owner
    matching markup by that pool would claim the other one's tag."""
    return (_serialize_attr_value(raw),)


@lru_cache(maxsize=1024)
def _selector_spellings(raw: str) -> tuple[str, ...]:
    """The spellings a selector quoted in an error message carries: CSS-escaped, as observe's `attr()`
    writes it, and escaped a second time, which is what the call log's `locator("…")` line renders."""
    once = _css_escape_attr_value(raw)
    spellings = {once, _css_escape_attr_value(once)}
    return tuple(sorted(spellings, key=lambda spelling: (-len(spelling), spelling)))


@lru_cache(maxsize=1024)
def _token_spellings(real: str) -> tuple[str, ...]:
    """Every spelling of an emitted selector a tool's own text can carry: the selector itself, and the
    one `{selector!r}` writes -- repr doubles a backslash and escapes the quote it wraps with, so a
    selector holding `"` or `\\` is a substring of neither the raw one nor a CSS-escaped one. Taken
    from repr itself, not rebuilt: a hand-built variant also spells the call log's nested escaping,
    whose own pass writes the alias escaped to match, and would win the substitution from it."""
    spellings = {real, repr(real)[1:-1]}
    return tuple(sorted(spellings, key=lambda spelling: (-len(spelling), spelling)))


@lru_cache(maxsize=1024)
def _raw_spellings(raw: str) -> tuple[str, ...]:
    """Every spelling any context can carry, for the two passes that are deliberately context-free:
    the last-resort scrub and the leak check's no-run fallback. Longest first, so a substring pass
    never lets a shorter spelling eat a longer one."""
    spellings = {raw, *_markup_spellings(raw), *_selector_spellings(raw)}
    return tuple(sorted(spellings, key=lambda spelling: (-len(spelling), spelling)))


def _text_holds_raw(text: str, raw: str) -> bool:
    return any(spelling in text for spelling in _raw_spellings(raw))


def _text_holds_markup(text: str, raw: str) -> bool:
    return any(spelling in text for spelling in _markup_spellings(raw))


def _text_holds_selector(text: str, raw: str) -> bool:
    return any(spelling in text for spelling in _selector_spellings(raw))


@lru_cache(maxsize=1024)
def _raw_opaque_runs(raw: str) -> tuple[str, ...]:
    """The uuid/hex runs that made the value worth aliasing: they hold no character any escaping
    layer rewrites, so every spelling of the value — modeled here or not — still contains them."""
    return tuple(_OPAQUE_ID_RUN_RE.findall(raw))


@lru_cache(maxsize=1024)
def _bare_value_spellings(raw: str) -> tuple[str, ...]:
    """What a tagless mention in an error message carries: the value itself, and its opaque runs —
    the part that survives an escaping no pass models."""
    spellings = {raw, *_raw_opaque_runs(raw)}
    return tuple(sorted(spellings, key=lambda spelling: (-len(spelling), spelling)))


def _text_holds_opaque_run(text: str, raw: str) -> bool:
    """Spelling-independent presence test, for deciding whether masking actually got everything."""
    runs = _raw_opaque_runs(raw)
    return any(run in text for run in runs) if runs else _text_holds_raw(text, raw)


# One start-tag attribute, quote-aware: a value ends at its own quote, or at whitespace when unquoted.
_START_TAG_ATTR_RE = re.compile(r"""(?<=\s)([^\s=/<>"']+)\s*=\s*("[^"]*"|'[^']*'|[^\s"'<>=]+)""")


def _blank_page_attr_values(tag: str) -> str:
    def _blanked(match: re.Match[str]) -> str:
        if match.group(1).lower() in _IDENTITY_ATTRS:
            return match.group(0)
        return match.group(1) + '=""'

    return _START_TAG_ATTR_RE.sub(_blanked, tag)


def _leak_check_text(text: str) -> str:
    """`text` reduced to the places masking owns -- prose, selector text, bare mentions and the
    identity attributes (matched case-insensitively) -- with every other start-tag attribute value
    blanked. A raw id in a `for=`, `href=` or `aria-*` value is a page value masking deliberately
    keeps, so its presence there is not evidence masking missed one."""
    return _map_start_tags(text, lambda tag, _start: _blank_page_attr_values(tag))


@lru_cache(maxsize=4096)
def _identity_attr_re(attr: str, raw: str, with_space: bool = False) -> re.Pattern[str]:
    """`attr="<raw>"` as markup spells it, left-boundary anchored so `id="R"` never matches inside
    `data-testid="R"`. `with_space` consumes the attribute's own leading whitespace, for a drop."""
    values = "|".join(re.escape(spelling) for spelling in _markup_spellings(raw))
    return re.compile((r"\s+" if with_space else r"(?<=\s)") + re.escape(attr) + '="(?:' + values + ')"')


def _tag_carries_raw(tag: str, attr: str, raw: str) -> bool:
    return _identity_attr_re(attr, raw).search(tag) is not None


def _owned_start_tag_span(text: str, owners: dict[tuple[str, str], set[str]]) -> tuple[int, int] | None:
    # The requested element's own tag: the first start tag that actually carries one of the owned
    # identity attributes, not merely the first `<letter` — prose like "not a <select> element" never
    # qualifies, since it names no owned attribute. Left-boundary anchored like `plain_pattern` below:
    # a bare substring test would let `id="R"` match inside `data-testid="R"` on an earlier tag.
    for start, end in _start_tag_spans(text):
        tag = text[start:end]
        if any(_tag_carries_raw(tag, attr, raw) for attr, raw in owners):
            return start, end
    return None


# Playwright's call log renders the element the locator actually resolved to on this line and only
# there; an outerHTML anywhere else in a message is some other element, whatever the call asked for.
_RESOLVED_TARGET_RE = re.compile(r"resolved to\s+(?:[a-z]+\s+)*$")


def _names_resolved_target(text: str, owners: dict[tuple[str, str], set[str]]) -> bool:
    span = _owned_start_tag_span(text, owners)
    return span is not None and _RESOLVED_TARGET_RE.search(text[: span[0]]) is not None


def _dedupe_single_tag_refs(tag: str, own_ref: str | None = None) -> str:
    # Position-first-wins would let a redacted "?" (written for a raw value shared by several aliases)
    # evict a real, usable handle that happens to sit later in the same tag; keep `own_ref` (the handle
    # the caller queried with) if the tag carries it, else the first non-"?" ref, else the first "?",
    # and drop every other data-tv3-ref in the tag.
    matches = list(_DATA_TV3_REF_ATTR_RE.finditer(tag))
    if len(matches) <= 1:
        return tag
    keeper_start = next(
        (m.start() for m in matches if own_ref is not None and m.group(1) == own_ref),
        next((m.start() for m in matches if m.group(1) != "?"), matches[0].start()),
    )

    def _drop_non_keeper(match: re.Match[str]) -> str:
        return match.group(0) if match.start() == keeper_start else ""

    return _DATA_TV3_REF_ATTR_RE.sub(_drop_non_keeper, tag)


# An identity attribute a truncation cut mid-value has no closing quote, so the whole-attribute
# rewrite below can never match it; only a tag left unterminated can hold one, since a span that
# ended at `>` has balanced quotes.
_CUT_IDENTITY_ATTR_RE = re.compile(r'\s(id|name|data-testid)="([^"]*)\Z')
# Shortest raw head that names its owner: a shorter fragment identifies no element, and matching on
# it would rewrite unrelated ids that merely open the same way.
_CUT_RAW_PREFIX_MIN = 8


def _shared_prefix_len(text: str, other: str) -> int:
    limit = min(len(text), len(other))
    length = 0
    while length < limit and text[length] == other[length]:
        length += 1
    return length


def _shared_raw_prefix(value: str, raw: str) -> tuple[int, int]:
    """The longest prefix `value` shares with the markup spelling of `raw`, and that spelling's
    length. A cut value is markup, so only that spelling can be a prefix of it."""
    best = (0, 0)
    for spelling in _markup_spellings(raw):
        shared = _shared_prefix_len(value, spelling)
        if shared > best[0]:
            best = (shared, len(spelling))
    return best


def _prefix_run_start(raw: str) -> int:
    """Index of the first opaque run in `raw`'s markup spelling, or 0 when it holds none — the offset
    a shared prefix must clear before any of it counts toward `_CUT_RAW_PREFIX_MIN`."""
    match = _OPAQUE_ID_RUN_RE.search(_markup_spellings(raw)[0])
    return match.start() if match is not None else 0


def _prefix_owners(value: str, owners: dict[tuple[str, str], set[str]]) -> set[tuple[str, str]]:
    """The owner(s) a cut-mid-value attribute head plausibly names: only the raw(s) sharing the
    LONGEST prefix with `value` at or above `_CUT_RAW_PREFIX_MIN`, and only when whatever follows
    that shared prefix in `value` is empty or opens with the elision marker "…" — get_html's
    truncation notice and Playwright's own elision both start with it, so anything else there is
    real page content proving `value` is a different id that merely opens the same way. When the raw
    holds an opaque run, the shared prefix must reach `_CUT_RAW_PREFIX_MIN` chars into that run, not
    merely share the raw's constant lead-in (`question_`), which names no owner on its own."""
    head = 0
    matched: set[tuple[str, str]] = set()
    for key in owners:
        shared, _raw_len = _shared_raw_prefix(value, key[1])
        if shared < _prefix_run_start(key[1]) + _CUT_RAW_PREFIX_MIN:
            continue
        suffix = value[shared:]
        if suffix and not suffix.startswith("…"):
            continue
        if shared < head:
            continue
        if shared > head:
            head, matched = shared, {key}
        else:
            matched.add(key)
    return matched


def _cut_value_owners(attr: str, value: str, owners: dict[tuple[str, str], set[str]]) -> set[tuple[str, str]]:
    """Candidates restricted to owners minted for the SAME attribute the cut left open: a shared id
    prefix is ordinary, so a cut inside `name="…"` matched against an `id` owner would stamp a clean,
    resolvable handle for a different element."""
    return _prefix_owners(value, {key: aliases for key, aliases in owners.items() if key[0] == attr})


def _cut_value_foreign_owners(attr: str, value: str, owners: dict[tuple[str, str], set[str]]) -> set[tuple[str, str]]:
    return _prefix_owners(value, {key: aliases for key, aliases in owners.items() if key[0] != attr})


def _mask_cut_identity_attr(
    tag: str,
    owners: dict[tuple[str, str], set[str]],
    own_alias: str | None,
    ambiguous: set[tuple[str, str]],
) -> str:
    """Rewrite the head of a raw value a cut left unterminated to the open marker shape loop.py
    canonicalizes (`data-tv3-ref="<n>` with no closing quote), keeping whatever the cut appended
    after it (the truncation notice) byte-exact."""
    match = _CUT_IDENTITY_ATTR_RE.search(tag)
    if match is None:
        return tag
    attr, value = match.group(1), match.group(2)
    matched = _cut_value_owners(attr, value, owners)
    # The head names an owned raw, but only under a DIFFERENT attribute: no alias here would resolve
    # to the element this fragment belongs to, so it is redacted rather than relabeled or left bare.
    foreign = _cut_value_foreign_owners(attr, value, owners) if not matched else set()
    if not matched and not foreign:
        return tag
    head = _shared_raw_prefix(value, next(iter(matched or foreign))[1])[0]
    if not matched:
        return f"{tag[: match.start()]} {_REDACTED_REF_ATTR[:-1]}{value[head:]}"
    aliases = {alias for key in matched for alias in owners[key]}
    ref = next(iter(aliases))[1:-1] if len(aliases) == 1 and not matched & ambiguous else _REDACTED_REF_ATTR
    if own_alias is not None and own_alias in aliases:
        ref = own_alias[1:-1]
    return f"{tag[: match.start()]} {ref[:-1]}{value[head:]}"


def _ambiguous_owners(
    text: str,
    owners: dict[tuple[str, str], set[str]],
    absent_alias: str | None,
    distinct_tags: bool = False,
) -> set[tuple[str, str]]:
    """Owner keys no single tag of `text` can claim: a raw more than one start tag carries names no one
    element, so its alias is rendered only on the tag proven to be the requested one. `absent_alias`
    counts as a carrier — its element's own tag exists (get_html returned its inner HTML) but is not
    shown, so a tag here holding that raw is some other element. `distinct_tags` collapses repeated
    identical tag text to one carrier, for a call log that reprints the same resolved-to element on
    every retry; real markup leaves it False, since two identical tags there are duplicate elements."""
    counts: dict[tuple[str, str], int] = {}
    for key, aliases in owners.items():
        if absent_alias is not None and absent_alias in aliases:
            counts[key] = 1
    seen_tags: set[str] = set()
    for start, end in _start_tag_spans(text):
        tag = text[start:end]
        if distinct_tags:
            if tag in seen_tags:
                continue
            seen_tags.add(tag)
        cut = _CUT_IDENTITY_ATTR_RE.search(tag)
        # A cut left the value unterminated: `_mask_cut_identity_attr` still rewrites its head, using
        # the same longest-match arbitration, so the tag carries at most one owner here too.
        cut_owners = _cut_value_owners(cut.group(1), cut.group(2), owners) if cut is not None else set()
        # Counted per (attribute, raw): a tag is a carrier of an owner only when it holds that
        # owner's OWN attribute, so a radio group sharing one `name` does not make every sibling a
        # carrier of the first option's `id`. The mirrored attribute is still dropped below.
        for key in owners:
            if _tag_carries_raw(tag, key[0], key[1]) or key in cut_owners:
                counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}


def _mask_identity_attrs(
    tag: str,
    owners: dict[tuple[str, str], set[str]],
    own_alias: str | None,
    ambiguous: set[tuple[str, str]],
) -> str:
    """Rewrite a whole `id="<raw>"` (name, data-testid) attribute in ONE start tag to the alias
    attribute. A raw value that more than one alias names, or that more than one tag of the answer
    carries (`ambiguous`), is redacted instead, except in the requested element's own tag
    (`own_alias` set), whose first occurrence renders the requested alias."""
    for (attr, raw), aliases in owners.items():
        if not _text_holds_markup(tag, raw):
            continue
        plain_pattern = _identity_attr_re(attr, raw)
        if len(aliases) == 1 and (attr, raw) not in ambiguous:
            tag = plain_pattern.sub(next(iter(aliases))[1:-1], tag)
            continue
        first = plain_pattern.search(tag)
        if first is not None and own_alias is not None and own_alias in aliases:
            tag = tag[: first.start()] + own_alias[1:-1] + plain_pattern.sub(_REDACTED_REF_ATTR, tag[first.end() :])
        else:
            tag = plain_pattern.sub(_REDACTED_REF_ATTR, tag)
    # id/name mirroring is ordinary in form markup, and the attribute the emitted selector did NOT
    # name is just as copyable a selector; it is dropped whole, leaving the one ref written above.
    for raw in {raw for _attr, raw in owners}:
        if not _text_holds_markup(tag, raw):
            continue
        for attr in _IDENTITY_ATTRS:
            if (attr, raw) not in owners:
                tag = _identity_attr_re(attr, raw, True).sub("", tag)
    return _mask_cut_identity_attr(tag, owners, own_alias, ambiguous)


# Every identity attribute an emitted selector names (id, name, data-testid — the attributes
# observe's naturalSelector minds), wherever it sits in the compound: each one is masked out of
# results and markup, so the value that triggered the alias never reaches the transcript.
# The `#id` capture accepts exactly what `CSS.escape` leaves untouched — ASCII word characters, the
# hyphen, and anything non-ASCII — since observe emits the bare `#` form only when that escape is a
# no-op. A `\s` cutoff would stop at U+00A0 and mint no owner for an id holding one.
_SELECTOR_ID_COMPONENTS_RE = re.compile(
    r'\[(id|name|data-testid)="((?:[^"\\]|\\.)*)"\]|(#)((?:[A-Za-z0-9_-]|[^\x00-\x7f])+)'
)
# Whitespace outside a quoted attribute value is a combinator: only hostAnchored composes selectors
# that way, while a natural `[name="first name"]` keeps its single round trip.
_TV3_QUOTED_VALUE_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
_TV3_ANCHORED_SELECTOR_RE = re.compile(r"^\S+\s+\S.*$")


# Relies on observe emitting a combinator only from hostAnchored; every natural selector is one
# compound, with any whitespace inside a quoted value.
def _is_host_anchored_selector(selector: str) -> bool:
    return bool(_TV3_ANCHORED_SELECTOR_RE.match(_TV3_QUOTED_VALUE_RE.sub('""', selector.strip())))


# A plain bare `#<id>`: no combinator/pseudo/attribute part, and no char that would need escaping
# inside `[id="<id>"]` (quotes, backslash, and whitespace are excluded, so the rewrite is always safe).
_BARE_ID_SELECTOR_RE = re.compile(r"""^#([^\s#.>+~\[\]()=,:*|^$'"\\]+)$""")


def _bare_id_is_invalid_css(ident: str) -> bool:
    # Invalid as a bare `#id` when the first char can't start a CSS identifier: a digit, a hyphen
    # followed by a digit, or a lone hyphen. `--`-leading is valid and is deliberately not flagged.
    if not ident:
        return True
    if ident[0].isdigit():
        return True
    return ident[0] == "-" and (len(ident) == 1 or ident[1].isdigit())


def _normalize_selector(selector: str) -> str:
    """Rewrite a bare `#<id>` that is invalid as written (digit/UUID/hyphen-digit leading, common on ATS
    forms) into the equivalent `[id="<id>"]`. `#id` ≡ `[id="id"]` for every id, and a bare id that already
    parses is returned untouched, so a valid selector's target is never altered."""
    match = _BARE_ID_SELECTOR_RE.match(selector.strip())
    if match is None or not _bare_id_is_invalid_css(match.group(1)):
        return selector
    return f'[id="{match.group(1)}"]'


# patchright/playwright report an invalid CSS selector with one of these message markers; matching the
# message (not the exception type) survives the patchright/playwright fork boundary. Version-coupled
# strings: a unit test RED-proofs the exact wording so a library upgrade that reworded them fails loudly.
_INVALID_SELECTOR_MARKERS = ("is not a valid selector", "while parsing selector", "while parsing css selector")


def _invalid_selector_result(selector: Any, exc: Exception) -> ToolResult | None:
    """An actionable error when `exc` is an invalid-CSS-selector parse failure; otherwise None so the
    caller re-raises (timeouts, teardown, and unrelated failures must not be swallowed)."""
    if not any(marker in str(exc) for marker in _INVALID_SELECTOR_MARKERS):
        return None
    return ToolResult.error(
        f"{selector!r} is not a valid CSS selector. Use a selector from the latest observe(), or an "
        '[id="..."] / [name="..."] attribute form (ids that start with a digit are not valid as a bare #id).'
    )


# Every tool that acts on a model-supplied CSS selector. file_upload's naked query_selector was the one
# that crashed on an invalid selector; the guard is shared so all of these inherit the same behavior.
_SELECTOR_GUARD_TOOL_NAMES = frozenset(
    {
        "get_html",
        "click",
        "hover",
        "type",
        "select_option",
        "select_combobox",
        "press_key",
        "scroll",
        "wait",
        "file_upload",
    }
)

# The tools whose driver timeout actually propagates to the guard AND whose action needs its target
# rendered. Measured per ANCHOR SHAPE, not per tool -- one shape answering does not mean the tool
# does. On a display:none template: select_option on a native <select> and a click on a native
# checkbox are refused by a visibility pre-gate before anything raises (they reach the same diagnosis
# through _unreachable_error); select_option on a custom listbox and select_combobox on a div anchor
# return their own could-not-open error; but select_combobox on a TYPEABLE anchor (the searchable
# autocomplete) takes the typeahead branch and raises the driver's call log verbatim, which is the
# evidence chain this exists to break. press_key returns ok without failing on every shape tried.
# file_upload is absent for a different reason: set_input_files drives a hidden <input type=file>,
# which is how essentially every site ships one, so a hidden target is the normal case there.
_INERT_DIAGNOSIS_TOOL_NAMES = frozenset({"click", "hover", "type", "select_combobox"})


def _inert_target_error(selector: str) -> ToolResult:
    # Distinct from the unreachable error, which sends the model to whatever reveals a collapsed
    # section: a template is not a section, has no opener of its own, and the live control (if there
    # is one) is a different element with a different selector. Distinct from the covered error for
    # the reason the run this exists for terminated — the model was handed a driver log saying its
    # target resolved and was not visible, read that as a layer over the page, and gave up on a page
    # that was ready to accept the action (SKY-15662).
    # Scoped to the target, and to NOW. The probe establishes that THIS element has no box at this
    # moment: not that the page is unobstructed (a real modal can be open at the same time, and a
    # categorical "nothing is blocking you" would turn this into a way to dismiss a genuine blocker),
    # and not that it will stay hidden -- a pending fetch can reveal it with no trigger at all, so the
    # shapes are named as the usual ones rather than as an exhaustive pair.
    return ToolResult.error(
        f"{selector} is not rendered — it matched inside a display:none subtree. An element with no box "
        "cannot be obstructed, so THIS failure is not evidence of a layer over it; whether something "
        "else on the page is blocked is a separate question this does not answer. Hidden markup is "
        "usually a template the page clones (the live control is then a DIFFERENT element) or a panel "
        "some trigger opens (act on the trigger first), and something still loading may yet reveal "
        "this one. Re-observe and act on what the page actually renders."
    )


def _with_selector_guard(handler: ToolHandler, diagnose_inert: InertTargetDiagnosis | None = None) -> ToolHandler:
    """Shared seam for selector tools: normalize a bare invalid `#id` before the handler resolves it, and
    convert a residual invalid-selector crash into an actionable error instead of a batch-aborting raise."""

    async def wrapped(args: dict[str, Any]) -> ToolResult:
        selector = args.get("selector")
        if isinstance(selector, str):
            args = {**args, "selector": _normalize_selector(selector)}
        try:
            return await handler(args)
        except Exception as exc:
            guarded = _invalid_selector_result(args.get("selector"), exc)
            if guarded is not None:
                return guarded
            # Diagnosed here rather than per tool because the act paths reach the driver the same
            # way: each waits for a target that is display:none until the timeout, then re-raises the
            # driver's own call log ("resolved to <button ...>", "element is not visible") for the
            # loop to hand the model verbatim.
            acted_on = args.get("selector")
            if diagnose_inert is not None and isinstance(acted_on, str):
                inert = await diagnose_inert(acted_on, exc)
                if inert is not None:
                    return inert
            raise

    return wrapped


# The observable-state vocabulary a readback compares — the same fields observe reports per element.
# None means "not read"; the classifier treats absence as no-committable-state, never as a value.
_COMMIT_STATE_KEYS = ("value", "checked", "selected", "pressed")


class CommitStatus(str, Enum):
    OK = "ok"  # state moved in the committing direction, read off exactly one element
    DID_NOT_COMMIT = "did_not_commit"  # target readable, and it did NOT commit
    UNVERIFIED = "unverified"  # no readable committable state, or committed but re-resolved to n != 1


def _has_committable_state(state: dict[str, Any] | None) -> bool:
    return isinstance(state, dict) and any(state.get(k) is not None for k in _COMMIT_STATE_KEYS)


def _classify_commit(
    pre: dict[str, Any] | None, post_matches: int, post: dict[str, Any] | None, *, committed_value: bool | None = None
) -> CommitStatus:
    """Classify a value-must-change action from a before/after observable-state readback.

    Ranked fail-closed: a readable did-not-commit is reported whatever the target re-resolved to, because
    an error halts the rest of a batched turn only when it moved the page -- otherwise the field is
    reported unfilled and only its same-selector dependents and any later click or Enter are skipped
    (INV-1 guards the confident ok, not the refusal). A commit read off
    a target that re-resolved to n != 1 is `unverified` (INV-1); no readable committable state is
    `unverified` (INV-2). `committed_value` hands in a caller's own value-dimension truth in place of the
    generic any-field-changed rule.
    """
    if post is None or not _has_committable_state(post):
        return CommitStatus.UNVERIFIED
    if committed_value is None:
        if pre is None or not _has_committable_state(pre):
            return CommitStatus.UNVERIFIED
        committed_value = any(pre.get(k) != post.get(k) for k in _COMMIT_STATE_KEYS)
    if not committed_value:
        return CommitStatus.DID_NOT_COMMIT
    return CommitStatus.OK if post_matches == 1 else CommitStatus.UNVERIFIED


_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")


def _canon_label(text: str) -> str:
    """One canonical form for option text: two labels that render alike compare equal (NFKC, zero-width
    characters dropped, NBSP and runs of whitespace collapsed, casefolded)."""
    folded = unicodedata.normalize("NFKC", _ZERO_WIDTH_RE.sub("", str(text or "")))
    return " ".join(folded.replace("\u00a0", " ").split()).casefold()


class _TypeaheadPick(NamedTuple):
    """What one type-and-pick attempt at a typeahead established.

    `clicked` False means a row WAS matched but the click never landed — the widget re-rendered the row
    out from under it. That is a selection never delivered, not one the field refused, so a caller may
    still ask the same field a coarser question instead of reporting a dead end. `declared` says the
    rows came from a list the widget declared; where nothing did, the caller keeps the older path.
    """

    committed: str | None
    suggestion: str | None
    readable: bool
    candidates: list[dict[str, Any]] | None
    clicked: bool
    declared: bool
    note: str | None = None
    # Whether the commit surface already vouched for the chosen label BEFORE the pick click — such a
    # surface proves nothing about the commit and must not vouch for it downstream either.
    pre_surface_hit: bool = False


# The smallest query many closed-vocabulary pickers need before they render candidates. Read by the
# reduced-query ladder as its last rung, where it may only reveal a vocabulary, never commit one.
_SHORT_PREFIX_RUNG_CHARS = 2


def _match_option_exact(value: str, options: list[dict[str, Any]]) -> int | None:
    """Pick the row whose WHOLE label IS `value` after canonical cleanup (case/whitespace/apostrophes/
    Unicode forms/zero-width). No stem, prefix, or other inferred tier is ever accepted here — anything
    short of that exact match returns None so the caller hands the rows back instead of guessing.
    """
    rows = [(o.get("n"), str(o.get("text") or "")) for o in options if isinstance(o.get("n"), int)]
    if not value or not rows:
        return None
    idx, tier = match_option_exact_or_stem_with_tier(_canon_label(value), [_canon_label(label) for _, label in rows])
    return rows[idx][0] if idx is not None and tier == "exact" else None


def _exact_tier_key(text: str) -> str:
    """The exact tier's own equality key — `_canon_label` plus the shared matcher's case/apostrophe fold —
    for pre-filters asking "which rows did that tier see as this value". A plain `_canon_label` filter
    folds less than the tier does and would disagree with it, so pre-filters must use this key instead.
    """
    return normalize_option_label(_canon_label(text))


def _lone_duplicate_candidate(rows: list[dict[str, Any]]) -> int | None:
    """Whether ≥2 matched rows are the SAME candidate rendered more than once: canonical TEXT agreement
    across every row is the load-bearing check, and a present aria-label, `val`, or other declared value
    is a VETO on top of it — one disagreeing across otherwise-text-identical rows marks them distinct, an
    absent one never does. Returns the FIRST row's `n` when the whole set collapses to one candidate, else
    None so the caller keeps refusing.
    """
    if len(rows) < 2:
        return None
    texts = {_canon_label(str(o.get("text") or "")) for o in rows}
    if len(texts) != 1:
        return None
    # The tagged leaf and its option ancestor are independent name surfaces: a shared leaf label
    # ("Choose") must not mask ancestors that disagree, so each position vetoes on its own.
    for surface in range(2):
        names = {
            _canon_label(str((o.get("labels") or [None, None])[surface]))
            for o in rows
            if (o.get("labels") or [None, None])[surface]
        }
        if len(names) >= 2:
            return None
    present_labels = {_canon_label(str(o.get("label"))) for o in rows if o.get("label")}
    if len(present_labels) >= 2:
        return None
    # Vals are machine identifiers, not display text: compared byte-exact, never case/Unicode-folded,
    # so "ID-A" and "id-a" stay two candidates. `vals` is the collapse's OWN read of the other value
    # surfaces (data-code, data-key, name, title, ...) — unfiltered by the commit verifier's numeric/
    # length drops, so "101" vs "202" is a real disagreement here. Non-empty sets must agree byte-exact;
    # an empty set says nothing and cannot contradict.
    present_vals = {str(o.get("val")) for o in rows if o.get("val") is not None}
    if len(present_vals) >= 2:
        return None
    # Two-rule agreement over the surface+attribute-keyed entries. (1) A key present on BOTH rows
    # must carry one value — crossed values across surfaces refuse, while a value carried on one
    # row's surface only says nothing against the other row. (2) The depth-blind floor: one row's
    # flattened attr=value pairs must nest inside the other's. Rows declaring identity on disjoint
    # attributes cannot be confirmed the same, and an agreeing generic attribute (a shared title)
    # beside disjoint identifiers is ordinary markup, not identity evidence — the pairs don't nest,
    # so it refuses. A row declaring NOTHING still constrains nothing (its empty set nests, so an
    # attribute-less a11y copy collapses).
    val_maps: list[dict[str, str]] = []
    for o in rows:
        entries: dict[str, str] = {}
        for raw in o.get("vals") or []:
            key, _, val_part = str(raw).partition("=")
            entries[key] = val_part
        val_maps.append(entries)
    for i, first in enumerate(val_maps):
        for second in val_maps[i + 1 :]:
            for shared in first.keys() & second.keys():
                if first[shared] != second[shared]:
                    return None
            flat_first = {f"{k.partition(':')[2]}={v}" for k, v in first.items()}
            flat_second = {f"{k.partition(':')[2]}={v}" for k, v in second.items()}
            if not (flat_first <= flat_second or flat_second <= flat_first):
                return None
    n = rows[0].get("n")
    return n if isinstance(n, int) else None


def _match_menu_option(value: str, options: list[dict[str, Any]], *, collapse_duplicates: bool = False) -> int | None:
    """Pick the enumerated menu row (its data-tv3-menu index) whose label matches the wanted value.

    Deterministic and site-agnostic, precision-first. Exact/singular-plural-stem matching (apostrophe
    folding, unique-or-None) is delegated to the shared `match_option_exact_or_stem` so this is not a
    third copy of that logic. Failing that, a UNIQUE FORWARD token-prefix — the observed value is a whole-
    token prefix of a fuller option label ("Decline" → "Decline to self-identify") — is accepted. The
    REVERSE direction is deliberately NOT matched: committing a shorter, more-general option for a longer
    value ("New York" → "New") is a silent wrong success, and on a virtualised window the fuller row may
    simply be unrendered. A value that is only an incidental SUBSTRING of an option is never matched ("No"
    inside "Prefer not to answer"). Ambiguity or no match returns None so the caller hands the options
    back to the model. Uniqueness is only meaningful over the COMPLETE list — the caller must not pass a
    truncated slice.

    `collapse_duplicates` is for the one call site that reads the whole, non-overflowed menu: when the
    exact/stem tier or the prefix tier finds more than one hit, it hands those rows to
    `_lone_duplicate_candidate` before giving up — a value that matches several DOM rows wearing the
    same candidate still resolves, while several genuinely distinct rows still refuse. Off by default so
    a caller working from a partial or reconstructed row set (a virtualised scroll-search window) never
    collapses on incomplete evidence.
    """
    rows = [(o.get("n"), str(o.get("text") or "")) for o in options if isinstance(o.get("n"), int)]
    if not value or not rows:
        return None

    # Canonicalize before the exact/stem tier — the shared normalizer folds case and apostrophes but
    # not internal spacing, Unicode forms or zero-width characters.
    want_canon = _canon_label(value)
    hit = match_option_exact_or_stem(want_canon, [_canon_label(label) for _, label in rows])
    if hit is not None:
        return rows[hit][0]
    if collapse_duplicates:
        want_key = _exact_tier_key(value)
        exact_matched = [
            o for o in options if isinstance(o.get("n"), int) and _exact_tier_key(str(o.get("text") or "")) == want_key
        ]
        if len(exact_matched) >= 2:
            collapsed = _lone_duplicate_candidate(exact_matched)
            if collapsed is not None:
                return collapsed
        elif not exact_matched:
            # Mirror the shared matcher's stem tier (trailing-s stem, 3-char floor) so a duplicated row
            # that would stem-commit as a lone row still collapses. Only when the exact tier saw
            # NOTHING — the matcher never consults stems once exact matches exist.
            want_stem = want_key.rstrip("s")
            if len(want_stem) >= 3:
                stem_matched = [
                    o
                    for o in options
                    if isinstance(o.get("n"), int)
                    and len(k := _exact_tier_key(str(o.get("text") or "")).rstrip("s")) >= 3
                    and k == want_stem
                ]
                if len(stem_matched) >= 2:
                    collapsed = _lone_duplicate_candidate(stem_matched)
                    if collapsed is not None:
                        return collapsed

    def toks(s: str) -> list[str]:
        # Fold commas and apostrophes so a short value token-prefix-matches a punctuated label ("Yes" →
        # "Yes, I consent"). A slash is left intact so a combined "Yes/No" option is not prefix-matched by
        # "Yes".
        return re.sub(r"[,'’]", " ", s).lower().split()

    want = toks(value)
    if not want:
        return None
    prefixed = [n for n, label in rows if (t := toks(label)) and len(want) < len(t) and t[: len(want)] == want]
    if len(prefixed) == 1:
        return prefixed[0]
    if collapse_duplicates and len(prefixed) > 1:
        prefixed_ns = set(prefixed)
        prefixed_rows = [o for o in options if isinstance(o.get("n"), int) and o.get("n") in prefixed_ns]
        return _lone_duplicate_candidate(prefixed_rows)
    return None


def _ambiguous_rows_error(
    selector: str, value: str, rows: list[dict[str, Any]], *, next_step: str, note: str | None = None
) -> ToolResult:
    """The refusal owed a caller when rows reacted and none of them IS the requested value.

    Geometry must never break a tie, so the rows are named in list order (≤15) and the pick stays the
    caller's. One wording for both entry points, so a refusal reported by type() and by select_combobox
    cannot drift into telling a model two different stories about the same page.
    """
    shown = rows[:15]
    listing = "; ".join(repr(str(o.get("text") or "")[:60]) for o in shown)
    more = len(rows) - len(shown)
    lead = (
        f"{value!r} matches several rows in {selector}: "
        if len(rows) > 1
        else f"{value!r} is not the one row showing in {selector}: "
    )
    tail = f" ({note})" if note else ""
    return ToolResult.error(
        f"{lead}{listing}{f'; +{more} more' if more > 0 else ''}{tail} — {next_step}; the field is NOT filled"
    )


def _row_value_suffix(o: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    """Every present distinguishing surface for one row — value, accessible label, then any other
    declared value (data-code, data-key, ...) — each truncated to 60 chars like the row text already is,
    so a page-controlled attribute can never blow a refusal message up wholesale. Surfaces are additive
    (not first-match), since the veto may have come from a surface other than the first one present.
    `rows` is the same-text row set the refusal lists: a surface that agrees across every row
    distinguishes nothing, so the row-label clause prints only when the names disagree, and the
    capped vals render entries that differ across rows first.
    """
    parts = ""
    if o.get("val") is not None:
        parts += f" (value {str(o.get('val'))[:60]!r})"
    if o.get("label"):
        parts += f" (label {str(o.get('label'))[:60]!r})"
    # The ancestor's name is its own surface: when it differs from the preferred display label (a
    # shared leaf label masking distinct row names) AND disagrees across the rows, the disagreeing
    # name is the one that converts — gated like the veto itself, on cross-row disagreement.
    ancestor_name = (o.get("labels") or [None, None])[1]
    ancestor_names = {str((p.get("labels") or [None, None])[1]) for p in rows if (p.get("labels") or [None, None])[1]}
    if ancestor_name and str(ancestor_name) != str(o.get("label") or "") and len(ancestor_names) >= 2:
        parts += f" (row label {str(ancestor_name)[:60]!r})"
    # `vals` entries are attribute-keyed for comparison; render only the value part, and subtract
    # what the value surface already showed so a row never prints one value twice.
    shown_already = {str(o.get("val")).strip()} if o.get("val") is not None else set()
    raw_vals = [str(v) for v in (o.get("vals") or [])]
    # The caller is being asked to pick between these rows: entries every row carries identically
    # cannot be what tells them apart, so the ones that differ print first (the cap must not hide
    # the distinguishing value behind agreeing generic ones).
    common_to_all = set(raw_vals)
    for p in rows:
        if p is not o:
            common_to_all &= {str(v) for v in (p.get("vals") or [])}
    vals = [
        bare[:60]
        for v in sorted(raw_vals, key=lambda v: v in common_to_all)
        if (bare := v.split("=", 1)[-1]) not in shown_already
    ]
    if vals:
        shown_vals = vals[:3]
        more = "; ..." if len(vals) > 3 else ""
        parts += f" (values {'; '.join(repr(v) for v in shown_vals)}{more})"
    return parts


def _identical_text_rows_error(
    selector: str, value: str, rows: list[dict[str, Any]], *, tags_live: bool = True, note: str | None = None
) -> ToolResult:
    """The refusal owed when ≥2 rows match the value at the exact tier and are not one duplicate-rendered
    candidate: the exact tier folds case/apostrophes, so every row already IS the requested text and only
    a direct click on a named row can choose between them. With `tags_live` the query stays typed and the
    rows' [data-tv3-sugg="N"] tags stay clickable; otherwise the field's prior value is restored and the
    refusal directs a re-open instead.
    """
    shown = rows[:15]

    def _sel(o: dict[str, Any]) -> str:
        # A selector is only named while it can be honored: after a restore the list may have closed
        # and the stale tags may re-land on different rows at the next scan.
        return f'[data-tv3-sugg="{o.get("n")}"] ' if tags_live else ""

    listing = "; ".join(f"{_sel(o)}{str(o.get('text') or '')[:60]!r}{_row_value_suffix(o, rows)}" for o in shown)
    more = len(rows) - len(shown)
    next_step = (
        'click the intended row directly by its [data-tv3-sugg="N"] selector; the typed query was left '
        "in the field to keep the list open for that click"
        if tags_live
        else "type the value to reopen the list, then click the intended row directly; the field's "
        "prior value was put back"
    )
    tail = f" ({note})" if note else ""
    return ToolResult.error(
        f"{value!r} matches {len(rows)} rows in {selector} whose labels the exact matcher cannot tell "
        f"apart by text: {listing}{f'; +{more} more' if more > 0 else ''}{tail} — {next_step} — the field "
        "is NOT filled"
    )


# ARIA combobox signals — used by observe() only to add a hint that a field is a typeahead. This is a
# nudge for the model, not load-bearing: type() handles typeaheads behaviorally (see _FIND_SUGGESTION_JS),
# so a field with no ARIA (a plain <input> backed by a custom dropdown) is still handled correctly.
_IS_AUTOCOMPLETE_JS = r"""(el) => {
  if (!el) return false;
  if (el.tagName !== 'INPUT') {
    // A non-INPUT anchor (button/div) that declares list semantics is a click-to-open combobox, not a
    // typeahead — but it still routes through select_combobox, so it earns the same hint. A wrapper
    // around a real input is that input's widget, not a click-to-open one: the input gets its own line.
    if (el.querySelector('input:not([type=hidden]),textarea,[contenteditable=""],[contenteditable=true]')) return false;
    return /(^|\s)combobox(\s|$)/i.test(el.getAttribute('role') || '') || (el.getAttribute('aria-haspopup') || '').toLowerCase() === 'listbox';
  }
  const ac = el.getAttribute('aria-autocomplete');
  // Only definitive combobox semantics — NOT bare aria-controls, which a search/filter input pointing
  // at a results table also carries and would over-flag.
  return /(^|\s)combobox(\s|$)/i.test(el.getAttribute('role') || '') || (ac && ac !== 'none') || el.getAttribute('aria-haspopup') === 'listbox';
}"""

# Function words to ignore when matching the typed value against a candidate's text — otherwise a stray
# "the"/"for"/"and" shared with some page chrome could score a hit. Only content words count. NOTE: not
# "new" — it is load-bearing in proper names ("New York" vs "York"), so it stays a matchable token.
_STOPWORDS_JS = (
    "new Set(['the','and','for','you','our','are','was','add','all','not','but','can','will',"
    "'one','get','job','your','this','that','with','from','has','have','may','use','any','per','via',"
    "'inc','llc','ltd','corp'])"
)

# The roles this engine treats as controls. observe enumerates exactly these (its `q` selector is
# this list expanded) and reports them on each record, so it is the single answer to "is this a
# control?" rather than each probe keeping its own.
_WIDGET_ROLES_JS = (
    "['button','checkbox','radio','combobox','option','menuitem',"
    "'menuitemcheckbox','menuitemradio','listbox','switch','spinbutton','tab']"
)

# The subset of those that can be a ROW in an opened menu. Derived rather than restated so the two
# cannot drift. Excluded: combobox/listbox/spinbutton, which are the control or its container and
# never one of its rows; and tab, because a probe that called a tab strip a menu of options would
# invite a wrong move.
_MENU_ROW_ROLES_JS = (
    "new Set(" + _WIDGET_ROLES_JS + ".filter((r) => ['combobox','listbox','spinbutton','tab'].indexOf(r) === -1))"
)

# Counts the VISIBLE menu-row descendants of a node, using the SAME row definition _FIND_MENU_JS
# reports on (its MENU_ROW_ROLES plus native <button>/<a>), so the growth signal and the finder cannot
# disagree about what a row is. Assumes an enclosing `vis(el)` helper. Shared by the two click probes.
_VIS_ROWS_JS = (
    r"""
  const MENU_ROW_ROLES = """
    + _MENU_ROW_ROLES_JS
    + r""";
  const _visRows = (el) => {
    let n = 0;
    try {
      for (const d of el.querySelectorAll('[role], button, a')) {
        const t = d.tagName;
        if ((MENU_ROW_ROLES.has(d.getAttribute('role')) || t === 'BUTTON' || t === 'A') && vis(d)) n++;
      }
    } catch (e) {}
    return n;
  };
"""
)


# Every open shadow root on the page, document first, then each root in depth-first order. Web-component libraries put the
# real input/button inside a shadow root, and `document.querySelector*` does not cross that boundary
# while Playwright's selector engine does — so any probe that must agree with what an action tool
# will resolve has to search these roots too, not just `document`.
_SHADOW_ROOTS_JS = r"""(from_root) => {
  const roots = [];
  const seen = new Set();
  // An explicit stack, not recursion: the traversal is unbounded in depth because Playwright's
  // selector engine is, and a root we stop short of is a root the callers' probes silently miss.
  const stack = [from_root];
  while (stack.length) {
    const root = stack.pop();
    roots.push(root);
    // Per root, not per walk: one root whose querySelectorAll throws would otherwise propagate out
    // of the whole traversal, and every caller reads that as "there are no shadow roots here".
    let all;
    try { all = root.querySelectorAll('*'); } catch (e) { continue; }
    const kids = [];
    for (const el of all) {
      let sr = null;
      // A form's named getter can make el.shadowRoot a foreign element; nodeType 11 is what makes
      // this a real shadow root rather than an <input name="shadowRoot">.
      try { sr = el.shadowRoot; } catch (e) { continue; }
      if (!sr || sr.nodeType !== 11 || seen.has(sr)) continue;
      seen.add(sr);
      kids.push(sr);
    }
    // Reversed, so popping walks the children in document order and the list stays pre-order DFS.
    for (let k = kids.length - 1; k >= 0; k--) stack.push(kids[k]);
  }
  return roots;
}"""


# Document-plus-shadow equivalents of the DOM query APIs. Every reaction/commit probe below judges
# what a Playwright action just did, and Playwright's selector engine pierces open shadow roots — so
# a document-only probe reports "not an option" / "menu closed" / "did not commit" about elements
# that are visible and were acted on successfully, which is a fabricated answer rather than a gap.
_PIERCED_QUERY_JS = (
    r"""
  const _shadowRoots = """
    + _SHADOW_ROOTS_JS
    + r""";
  // Walked once per invocation: a probe calls these helpers several times, and the roots cannot
  // change between those calls.
  const _rootList = _shadowRoots(document);
  // A throw here propagates, exactly as document.querySelector did: an unparseable selector is not
  // the same fact as "no such element", and callers that gate on the result disarm themselves if the
  // two are conflated.
  const pQS = (sel) => {
    for (const root of _rootList) {
      const el = root.querySelector(sel);
      if (el) return el;
    }
    return null;
  };
  const pQSA = (sel) => {
    const acc = [];
    for (const root of _rootList) {
      for (const el of root.querySelectorAll(sel)) acc.push(el);
    }
    return acc;
  };
  // The composed-tree step, and the only one any probe below takes: parentNode, or the host at a
  // shadow boundary (a ShadowRoot has no parentNode, and parentElement is null there). A walk that
  // stops at the boundary answers about the component's inside, which is not the page's structure.
  const composedParent = (n) => (n && (n.parentNode || n.host)) || null;
  const composedParentElement = (n) => {
    let p = composedParent(n);
    while (p && p.nodeType !== 1) p = composedParent(p);
    return p;
  };
  // The nearest ancestor matching `sel`, across shadow boundaries: closest() covers the whole chain
  // within one root, then the walk hops to that root's host and repeats. Bounded by shadow depth --
  // and a boundary it cannot cross (a closed root, a detached node) reads as "no such ancestor", so
  // callers fall back to refusing rather than to a guess.
  const composedClosest = (el, sel) => {
    for (let n = el, hops = 0; n && hops < 32; hops++) {
      if (n.nodeType === 1) {
        let hit = null;
        try { hit = n.closest(sel); } catch (e) { hit = null; }
        if (hit) return hit;
      }
      const root = n.getRootNode ? n.getRootNode() : null;
      n = root && root.nodeType === 11 ? root.host : null;
    }
    return null;
  };
  // Node.contains walks the light tree only, so a host does not contain its own shadow content.
  // Every caller below compares elements drawn from the pierced scope, where a cross-tree pair is
  // ordinary.
  const pContains = (a, b) => {
    if (!a || !b) return false;
    for (let n = b; n; n = composedParent(n)) if (n === a) return true;
    return false;
  };
  // The pre-snapshot carries element identity across a click or a keystroke, so the carrier has to
  // survive whatever the page did in between -- and the two halves of the page need different ones.
  // In the light DOM an attribute is the only carrier that survives cloneNode/innerHTML, so a
  // container the page re-creates by cloning still reads as "existed before" rather than "appeared
  // in reaction". Inside a shadow root we write nothing at all: stamping there makes a component
  // watching its own root re-render, destroying the marks just made and leaving the finders reading
  // a static list as a reaction. The WeakSet is the best carrier that costs no mutation, at one
  // disclosed price -- a component that re-creates its own content by cloning reads as all-new.
  // Absent (a navigation cleared window) means "no snapshot", never "everything is new".
  const preMark = (el, inShadow) => {
    if (inShadow) window.__tv3_pre.add(el);
    else el.setAttribute('data-tv3-pre', '1');
  };
  // instanceof, not truthiness: a page that pre-defines __tv3_pre as an accessor keeps its own
  // object through preReset, and a `has: () => false` impostor would make every element read as a
  // reaction -- defeating the one distinction this guard exists to draw.
  const preReady = () => window.__tv3_pre instanceof WeakSet;
  const preHas = (el) => {
    try { if (el.hasAttribute('data-tv3-pre')) return true; } catch (e) { /* clobbered getter */ }
    return preReady() && window.__tv3_pre.has(el);
  };
  const preReset = () => {
    pQSA('[data-tv3-pre]').forEach((e) => e.removeAttribute('data-tv3-pre'));
    window.__tv3_pre = new WeakSet();
    focusReset();
  };
  // A third class beside pre-existing and typing-revealed: rows a list rendered in reaction to the
  // FOCUS click. They are options the widget offered, not a filter it applied to the typed value, so
  // a match among them is picked under the open->observe->pick contract, not the typeahead's.
  const focusMark = (el, inShadow) => {
    if (inShadow) { if (window.__tv3_focus instanceof WeakSet) window.__tv3_focus.add(el); }
    else el.setAttribute('data-tv3-focus', '1');
  };
  const focusHas = (el) => {
    try { if (el.hasAttribute('data-tv3-focus')) return true; } catch (e) { /* clobbered getter */ }
    return window.__tv3_focus instanceof WeakSet && window.__tv3_focus.has(el);
  };
  const focusReset = () => {
    pQSA('[data-tv3-focus]').forEach((e) => e.removeAttribute('data-tv3-focus'));
    window.__tv3_focus = new WeakSet();
    window.__tv3_focus_offered = null;
    window.__tv3_open_own = null;
  };
  // 'body *' has no meaning inside a shadow root, whose own descendants are the equivalent scope.
  const pScopeEach = (fn) => {
    for (const root of _rootList) {
      const inShadow = root !== document;
      for (const el of root.querySelectorAll(inShadow ? '*' : 'body *')) fn(el, inShadow);
    }
  };
  const pScopeAll = () => {
    const acc = [];
    pScopeEach((el) => acc.push(el));
    return acc;
  };
"""
)

# Snapshot of everything visible BEFORE typing. The finder ignores anything marked here, so only DOM
# that appeared (or became visible) IN REACTION to typing can be treated as a suggestion — static page
# text that merely happens to share a word with the value (a nearby card, nav item, prior answer) is
# never eligible. This is what makes "detect by the page's reaction" rigorous rather than a claim.
# The clause a committed-selection label may carry after its value ("…, press delete to clear
# value.") — recognized before any comma-clause strip, so a bare comma-bearing label never loses its
# tail to the matcher.
# Actual instruction SYNTAX, not a keyword alone: a proper-name suffix that merely contains an
# action word ("Austin, Clear Lake") must never read as a clearing instruction.
_INSTRUCTION_CLAUSE_RE = re.compile(
    r"\b(?:press|tap|click|hit)\s+(?:delete|backspace|enter|escape)\b"
    r"|\bto\s+(?:delete|remove|clear|dismiss|deselect)\b"
    r"|\b(?:delete|remove|clear|dismiss|deselect|backspace)\s+(?:the\s+)?"
    r"(?:value|values|selection|selections|item|items|option|options|entry|entries|choice|choices|tag|tags|this|it|all)\b",
    re.IGNORECASE,
)

_PRESNAPSHOT_JS = (
    r"""() => {"""
    + _PIERCED_QUERY_JS
    + r"""
  preReset();
  pScopeEach((el, inShadow) => {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) preMark(el, inShadow);
  });
}"""
)

# Behavioral, site-agnostic suggestion finder. After the caller types a value (with a pre-snapshot taken
# first), this looks for the suggestion list the typeahead rendered IN REACTION: a small, visible,
# leaf-ish row that did NOT exist/show before typing (not `data-tv3-pre`), sits in the dropdown region
# near the field, and shares a CONTENT word with the typed value. It keys off reaction + geometry + token
# overlap — NOT any site's CSS classes, ARIA, or field vocabulary — so a bespoke widget (plain <input> +
# custom dropdown) is handled like an ARIA combobox and it stays durable as sites restyle. Navigational
# controls (links/buttons) are excluded unless explicitly role=option. A candidate that CONTAINS another
# match is a container (its text is the union of all rows), so it's dropped.
# Two outcomes, split on whether the widget DECLARES its rows. When a surviving leaf sits inside a row
# the field declares (rowSelFor: role=option, or a gridcell of a grid popup the field itself declares),
# EVERY such row is promoted, tagged data-tv3-sugg="1..N" top-to-bottom and returned as
# {count, options, declared: true} — geometry must never break a tie between real options, so the
# caller's own precision matcher (_match_menu_option) picks, over the full text _MENU_OPTION_TEXTS_JS
# reads back. Where nothing declares a row, reconstructing one from geometry does not converge, so the
# finder keeps the single-winner contract instead: highest score, innermost, refusing an ambiguous
# leading-clause tie and a multi-row container, as {count: 1, options: [the row], declared: false}.
# Returns null when nothing reacted at all.
# The ONE definition of "may this row be auto-clicked". Every finder that decides that embeds this
# snippet: two hand-copied predicates drifted once (menuitem listed as both option and nav, so
# `<a role=menuitem href>` read as an option and was clicked off the form). Semantics resolve from the
# closest declaring ANCESTOR, not the reduced leaf (`<a href><span>` reduces to the span).
_ROW_SEMANTICS_JS = r"""
  const OPT_SEL = '[role="option"],[role="menuitemradio"],[role="menuitemcheckbox"],[role="treeitem"],[role="radio"]';
  const NAV_SEL = 'a[href],button,[role="button"],[role="link"],[role="menuitem"],[role="tab"]';
  const LIST_SEL = '[role="listbox"],[role="menu"],[role="tree"],[role="grid"],[role="radiogroup"],datalist';
  // Composed, not closest(): an option component declares the row's role on its HOST, outside the
  // root the pointer leaf lives in, so a same-root lookup reads that row as a bare button.
  const isNavRow = (el) => {
    try { return !composedClosest(el, OPT_SEL) && !!composedClosest(el, NAV_SEL); } catch (e) { return true; }
  };
  // A control the row only WRAPS reaches the click just as the row's own box does -- but only while
  // it is rendered. A display:none or visibility:hidden action never receives that click, so it says
  // nothing about where the row leads.
  const isShown = (n) => {
    try {
      const r = n.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) return false;
      const view = (n.ownerDocument && n.ownerDocument.defaultView) || window;
      const cs = view.getComputedStyle(n);
      return cs.visibility !== 'hidden' && cs.display !== 'none';
    } catch (e) { return true; }
  };
  // Clicking a row activates whatever control it WRAPS, so a row holding a link or a submit is as
  // navigational as one that IS a link. Declaring a row role does not change where the click lands, so
  // this holds for an option row too. Only the DECLARED-row pick path judges rows this way; every other
  // finder keeps isNavRow.
  // A <button> with no type attribute defaults to type=submit, same as an explicit one.
  const DEPARTURE_SEL =
    'a[href],[role="link"],[role="menuitem"],[role="tab"],' +
    'button:not([type="button" i]):not([type="reset" i]),input[type="submit" i],input[type="image" i]';
  const wrapsDeparture = (el) => {
    try {
      if (el.matches(DEPARTURE_SEL) && isShown(el)) return true;
      for (const n of el.querySelectorAll(DEPARTURE_SEL)) if (isShown(n)) return true;
      return false;
    } catch (e) { return true; }
  };
  // What the field points at with aria-controls/aria-owns, resolved in its own root and its ancestor
  // roots (an id lives in exactly one root).
  const declaredTargets = (field) => {
    const out = [];
    if (!field || !field.getAttribute) return out;
    const ids = [];
    for (const a of ['aria-controls', 'aria-owns']) {
      const v = field.getAttribute(a);
      if (v) for (const id of v.split(/\s+/)) if (id) ids.push(id);
    }
    if (!ids.length) return out;
    for (let root = field.getRootNode(), hops = 0; root && hops < 8; hops++, root = root.host ? root.host.getRootNode() : null) {
      for (const id of ids) {
        let target = null;
        try { target = root.getElementById ? root.getElementById(id) : null; } catch (e) { target = null; }
        if (target && out.indexOf(target) === -1) out.push(target);
      }
      if (!root.host) break;
    }
    return out;
  };
  // Which rows count as this FIELD's option rows. A role=gridcell is one only when the field declares a
  // GRID popup (aria-haspopup="grid", or an aria-controls/aria-owns target that is or holds a
  // role="grid") -- the ARIA 1.2 grid-combobox pattern. Everywhere else a gridcell is a cell of tabular
  // content, and a link or button inside it stays navigational (isNavRow keeps OPT_SEL, so a
  // search-results grid is never auto-clicked).
  const rowSelFor = (field) => {
    if (!field) return OPT_SEL;
    let grid = false;
    try { grid = String(field.getAttribute('aria-haspopup') || '').toLowerCase() === 'grid'; } catch (e) { grid = false; }
    if (!grid) {
      for (const t of declaredTargets(field)) {
        let hit = false;
        try { hit = t.matches('[role="grid"]') || !!t.querySelector('[role="grid"]'); } catch (e) { hit = false; }
        if (hit) { grid = true; break; }
      }
    }
    return grid ? OPT_SEL + ',[role="gridcell"]' : OPT_SEL;
  };
  // The popup this field DECLARES (aria-controls/aria-owns) AND that is a list of its own: the target
  // carries a list role and holds no other form control (a declared target wrapping inputs is a panel
  // or a results region, not this field's menu). `rendered` asks for one that is on screen NOW -- a
  // declaration still stands while the list sits empty, but a geometry rule may only bend for a box
  // that actually has one.
  const fieldOwnPopup = (field, rendered) => {
    if (!field) return null;
    for (const t of declaredTargets(field)) {
      if (pContains(t, field)) continue;
      let listish = false;
      try { listish = t.matches('[role="listbox"],[role="grid"],[role="menu"],[role="tree"]'); } catch (e) { listish = false; }
      if (!listish) continue;
      let holdsControls = false;
      try {
        for (const c of t.querySelectorAll('input,select,textarea')) { if (c !== field) { holdsControls = true; break; } }
      } catch (e) { holdsControls = true; }
      if (holdsControls) continue;
      if (!rendered) return t;
      let tr = null;
      try { tr = t.getBoundingClientRect(); } catch (e) { tr = null; }
      if (tr && tr.width > 0 && tr.height > 0) return t;
    }
    return null;
  };
  // Which of several lists is THIS field's menu: the one it declares (aria-controls/aria-owns), else
  // the single list within the dropdown window under or above the field. Size alone would name a
  // sibling list's options as its own.
  const fieldOwnList = (field, lists) => {
    if (!lists.length) return null;
    if (field) {
      for (const target of declaredTargets(field)) {
        const hit = lists.find((l) => l === target || (target.contains && target.contains(l)) || pContains(target, l));
        if (hit) return hit;
      }
      let fr = null;
      try { fr = field.getBoundingClientRect(); } catch (e) { fr = null; }
      if (fr) {
        const near = lists.filter((l) => {
          let r = null;
          try { r = l.getBoundingClientRect(); } catch (e) { return false; }
          if (!r || (r.width === 0 && r.height === 0)) return false;
          return r.top >= fr.top - 400 && r.top <= fr.bottom + 500 && r.right >= fr.left && r.left <= fr.right;
        });
        // A known field with no declared and no nearby list has no menu among these candidates;
        // saying so lets the caller fall back to what was recorded when the field opened.
        if (!near.length) return null;
        // Two undeclared lists in the window are indistinguishable by geometry (a sibling's decoy
        // sits exactly where a menu opens); naming one of them would report another widget's rows.
        return near.length === 1 ? near[0] : null;
      }
    }
    return lists[0];
  };
"""

# The values a row declares for itself: the attributes on the row or on its option ancestor that NAME
# a value. A widget that commits a code ("CA" for "California") commits one of these, and nothing else
# short. A positional or boolean attribute (data-index="1", data-selected="true") is not one.
_DECLARED_VALUES_JS = r"""
  const declaredValues = (row, rowSel) => {
    const out = [];
    try {
      const VALUE_ATTR = /^(value|data-value|data-val|data-v|data-code|data-key|data-option-value|name|title)$/;
      // An explicit option value may legitimately be "1" or "true"; any other attribute must not
      // contribute one (a positional data-index="1" is not a value the widget commits).
      const EXPLICIT = /^(value|data-value)$/;
      const opt = composedClosest(row, rowSel || OPT_SEL);
      for (const node of new Set([row, opt || row])) {
        for (const a of node.attributes) {
          if (!VALUE_ATTR.test(a.name)) continue;
          const v = String(a.value).trim();
          if (!v || v.length > 40) continue;
          if (!EXPLICIT.test(a.name) && (/^\d+$/.test(v) || /^(true|false|null|undefined)$/i.test(v))) continue;
          out.push(v);
        }
      }
    } catch (e) { /* attributes unreadable: no declared values */ }
    return out.slice(0, 12);
  };
"""


_FIND_SUGGESTION_JS = (
    r"""(args) => {"""
    + _PIERCED_QUERY_JS
    + _ROW_SEMANTICS_JS
    + r"""
  const STOP = """
    + _STOPWORDS_JS
    + r""";
  const toks = (s) => new Set(String(s).toLowerCase().replace(/[\/,]/g, ' ').split(/\s+/).filter((w) => w.length >= 3 && !STOP.has(w)));
  const want = toks(args.value || '');
  const wantNorm = String(args.value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  // A value with no >=3-char word ("No", "UK") has nothing to overlap; it matches a row only by exact text.
  const exact = want.size ? null : String(args.value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  pQSA('[data-tv3-sugg]').forEach((e) => e.removeAttribute('data-tv3-sugg'));
  if ((!want.size && !exact) || !preReady()) return null;
  const field = pQS(args.field) || (args.el && args.el.isConnected ? args.el : null);
  // No field means no geometry gate, and without it the scan below is page-wide and will happily
  // tag -- and then click -- a row far from the control the caller typed into. Refuse instead:
  // "cannot judge" and "nothing reacted" are both safe, and a confident wrong click is not.
  if (!field) return null;
  const fr = field.getBoundingClientRect();
  // Only for a popup the field owns is the VERTICAL half of the dropdown-window gate relaxed — a long
  // list runs well past the window, and applying it would truncate the field's own options to whichever
  // ones happen to render near it. The horizontal half still applies: a column of the page that merely
  // sits under the field is not its menu.
  const ownPopup = fieldOwnPopup(field, true);
  const rowSel = rowSelFor(field);
  // A list this field already had open when the call arrived (see _FOCUS_SNAPSHOT_JS) reacts by
  // NARROWING rather than by appearing: once it has, the rows it kept are the ones typing selected.
  let openNarrowed = false;
  try {
    const rec = window.__tv3_open_own;
    if (rec && rec.sel === args.field && rec.list && rec.list.isConnected) {
      openNarrowed = rec.list.querySelectorAll(rowSel).length < rec.rows;
    }
  } catch (e) { openNarrowed = false; }
  const cands = [];
  for (const el of pScopeAll()) {
    // Pre-existing → not a reaction, unless it is a surviving row of that narrowed list.
    if (preHas(el) && !(openNarrowed && focusHas(el))) continue;
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LABEL' || tag === 'FORM') continue;
    if (el.children.length > 8) continue;                             // a suggestion row, not a big container
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || r.height > 120) continue;  // visible, row-sized (allows a 2-line row)
    if (fr) {                                                          // in the dropdown region: below, or above if it flipped up
      if (r.right < fr.left || r.left > fr.right) continue;
      if (!(ownPopup && pContains(ownPopup, el)) && (r.top < fr.top - 400 || r.top > fr.bottom + 500)) continue;
    }
    const txt = (el.innerText || '').trim();
    if (!txt || txt.length > 80) continue;
    // never click something navigational (would leave the form) unless it's explicitly an option;
    // last of the gates because it is the only one that walks a subtree
    if (isNavRow(el)) continue;
    let score = 0;
    const norm = txt.replace(/\s+/g, ' ').trim().toLowerCase();
    // A row whose whole text IS the value outranks a partial match ("New York" over "New York City";
    // "No" over "No, I have not ..."). This is only the CANDIDATE gate, not a cross-row ranking: every
    // row that clears score > 0 is tagged below and handed to the caller's own precision matcher.
    const isExact = norm === (exact !== null ? exact : wantNorm);
    if (exact !== null) {
      if (isExact) score = 2;
      else if (norm.split(/\s*[,;:(]\s*|\s+[-\u2013\u2014]\s+/)[0] === exact) score = 1;
    } else {
      const have = toks(txt);
      for (const w of want) if (have.has(w)) score++;
      if (isExact && score > 0) score += 100;
    }
    if (score > 0) cands.push({ el, score, h: r.height, exactRow: isExact, r });
  }
  if (!cands.length) return null;
  // Drop any candidate that CONTAINS another candidate (a dropdown container over its own rows).
  const leaves = cands.filter((c) => !cands.some((o) => o.el !== c.el && pContains(c.el, o.el)));
  const pool = leaves.length ? leaves : cands;
  if (pool.some((c) => !!composedClosest(c.el, rowSel))) {
    // The ROW the widget DECLARED is the unit, not the fragment that happens to hold the matched
    // substring: a widget that wraps only the match in its own highlight span leaves that span as the
    // innermost candidate, and then every row reads as the same bare query text. Promote each leaf to
    // its declared row and dedupe by that row, so the text read back is the row's own full label.
    const byRow = new Map();
    for (const c of pool) {
      const row = composedClosest(c.el, rowSel);
      if (!row || byRow.has(row)) continue;
      // The promotion can only widen what a click lands on, so the row it produced has to clear the
      // navigational gate too -- declaring a row does not make one holding a link a pick.
      if (isNavRow(row) || wrapsDeparture(row)) continue;
      let r = c.r;
      try { r = row.getBoundingClientRect(); } catch (e) { r = c.r; }
      byRow.set(row, { el: row, r });
    }
    const promoted = Array.from(byRow.values());
    // Two leaves can promote onto nested rows; keep the innermost so no tagged row holds another.
    const rows = promoted.filter((c) => !promoted.some((o) => o.el !== c.el && pContains(c.el, o.el)));
    if (!rows.length) return null;
    // Top-to-bottom order, same as _FIND_MENU_JS. This finder only says which rows reacted; the caller
    // (via _match_menu_option, over the full untruncated text _MENU_OPTION_TEXTS_JS reads back) picks
    // which one is the wanted value -- geometry never breaks a tie between them.
    rows.sort((a, b) => a.r.top - b.r.top || a.r.left - b.r.left);
    const options = [];
    let n = 0;
    for (const c of rows) {
      n++;
      c.el.setAttribute('data-tv3-sugg', String(n));
      if (options.length < 15) options.push({ n, text: c.el.innerText.trim().slice(0, 60) });
    }
    return { count: n, options, declared: true };
  }
  // Nothing here declares a row, so there is no unit to name: take the highest score, breaking ties
  // toward the smallest (innermost) row.
  pool.sort((a, b) => b.score - a.score || a.h - b.h);
  const best = pool[0];
  // Two leading-clause matches for a short value ("No, ..." and "No - ...") with no exact row are
  // ambiguous: geometry must not decide an answer, so refuse and let the caller report the options.
  if (exact !== null && !best.exactRow && pool.length > 1 && pool[1].score === best.score) return null;
  // Refuse to tag a multi-row CONTAINER even when it is the only match (its score came from different
  // rows' text combined, and clicking it would land on an arbitrary middle row). A real suggestion is a
  // single row: its visible child elements, if any, sit on one line (inline sub-parts), not stacked rows.
  const childRows = new Set();
  for (const ch of best.el.children) {
    const cr = ch.getBoundingClientRect();
    if (cr.width > 0 && cr.height > 0 && (ch.innerText || '').trim()) childRows.add(Math.round(cr.top));
  }
  if (childRows.size >= 2) return null;
  best.el.setAttribute('data-tv3-sugg', '1');
  return { count: 1, options: [{ n: 1, text: (best.el.innerText || '').trim() }], declared: false };
}"""
)

# Read back the row _FIND_SUGGESTION_JS tagged data-tv3-sugg="N" once the caller has picked `n` (via
# _match_menu_option): its declared value/data-* attributes -- a widget that commits a code ("CA" for a
# row displaying "California") commits one of these -- and whether it came from the FOCUS-opened list
# rather than reacting to a keystroke (that pick is verified under the open->observe->pick contract, not
# the typeahead's change-based one; see _VERIFY_COMMIT_JS's `noSuggestionList`).
_SUGG_ROW_INFO_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + _ROW_SEMANTICS_JS
    + _DECLARED_VALUES_JS
    + r"""
  const el = pQS('[data-tv3-sugg="' + arg.n + '"]');
  if (!el) return null;
  let field = null;
  try { field = pQS(arg.sel); } catch (e) { field = null; }
  if (!field && arg.el && arg.el.isConnected) field = arg.el;
  return {
    text: (el.innerText || '').trim(),
    // Revealed BY this call's focus click. A row already open before it is one this same field left
    // up, and the typing that filtered down to it is the reaction the change-based contract asks for.
    fromFocus: focusHas(el) && !preHas(el),
    declared: declaredValues(el, rowSelFor(field)),
  };
}"""
)

# Second pass after the focus click: everything now visible that is not a list row is marked as
# pre-existing, so focus-revealed help/validation text cannot read as a suggestion while a menu the
# focus opened keeps its rows eligible.
_FOCUS_SNAPSHOT_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + _ROW_SEMANTICS_JS
    + r"""
  if (!preReady()) return;
  focusReset();
  let field = null;
  try { field = pQS(arg.sel); } catch (e) { field = null; }
  if (!field && arg.el && arg.el.isConnected) field = arg.el;
  // Menu/option semantics, not any list: a plain <ul> that focus revealed is page text, an ARIA
  // list or an option row is the widget's own menu. A list that CONTAINS the field is layout.
  const LIST = LIST_SEL;
  const rowSel = rowSelFor(field);
  const inOptionList = (el) => {
    for (let n = el; n; n = n.parentNode || n.host || null) {
      if (n.nodeType !== 1) continue;
      let isList = false;
      try { isList = n.matches(LIST) || n.matches(rowSel); } catch (e) { isList = false; }
      if (isList) return !(field && pContains(n, field));
    }
    return false;
  };
  // A list the field ALREADY declares open was opened by an earlier call on this same field, not by
  // the page: its rows are options the widget is offering, however long they have been on screen.
  const declaresOpen = () => {
    if (!field) return false;
    if (fieldOwnPopup(field, true)) return true;
    // The control's own declaration, not any expanded ancestor: a surrounding open accordion says
    // nothing about whether THIS widget has a list up.
    let exp = null;
    try { exp = field.matches('[aria-expanded]') ? field : composedClosest(field, '[role="combobox"][aria-expanded]'); }
    catch (e) { exp = null; }
    try { return !!(exp && exp.getAttribute('aria-expanded') === 'true'); } catch (e) { return false; }
  };
  // Only the field's OWN list qualifies, resolved the same way the offered-labels read resolves it:
  // fieldOwnList refuses rather than guess, so an undeclared decoy sharing the window promotes nothing.
  let openOwn = null;
  if (declaresOpen()) {
    const rendered = [];
    for (const el of pScopeAll()) {
      let isList = false;
      try { isList = el.matches(LIST); } catch (e) { isList = false; }
      if (!isList || (field && pContains(el, field))) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      let hasRow = false;
      try { hasRow = !!el.querySelector(rowSel); } catch (e) { hasRow = false; }
      if (hasRow) rendered.push(el);
    }
    openOwn = fieldOwnList(field, rendered);
  }
  // How many rows that list held BEFORE a single keystroke. The finder treats them as this field's
  // options only once typing has narrowed the list -- an open list that ignores typing is furniture.
  if (openOwn) {
    let held = 0;
    try { held = openOwn.querySelectorAll(rowSel).length; } catch (e) { held = 0; }
    if (held) window.__tv3_open_own = { sel: arg.sel, list: openOwn, rows: held };
  }
  pScopeEach((el, inShadow) => {
    const inOpenOwn = !!openOwn && pContains(openOwn, el);
    if (preHas(el) && !inOpenOwn) return;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return;
    if (inOptionList(el)) focusMark(el, inShadow); else preMark(el, inShadow);
  });
  // Record what the list offered NOW: a widget that filters by re-rendering unmounts the rows the
  // typed value does not match, so a later read would find nothing to name on an honest no-match.
  const byList = new Map();
  const seen = new Set();
  pScopeEach((el) => {
    if (!focusHas(el)) return;
    const row = composedClosest(el, rowSel);
    if (!row || seen.has(row)) return;
    seen.add(row);
    const txt = (row.textContent || '').replace(/\s+/g, ' ').trim();
    if (!txt || txt.length > 80) return;
    const key = composedClosest(row, LIST_SEL) || row.parentNode;
    if (!byList.has(key)) byList.set(key, []);
    byList.get(key).push(txt);
  });
  const lists = Array.from(byList.keys()).sort((a, b) => byList.get(b).length - byList.get(a).length);
  const own = fieldOwnList(field, lists);
  const best = own ? byList.get(own) : [];
  // Stamped with the field it was recorded for: a record left by an earlier field is never read
  // for this one, even when this call's own focus pass did not run.
  window.__tv3_focus_offered = { sel: arg.sel, total: best.length, labels: best.slice(0, 15) };
}"""
)

# The labels the widget OFFERED when the field opened — its focus-revealed rows (see _FOCUS_SNAPSHOT_JS),
# read even after the typed filter hid them, so an honest no-match can name the real choices instead of
# leaving the model to guess a label again. Reads only; tags nothing.
_FOCUS_OFFERED_LABELS_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + _ROW_SEMANTICS_JS
    + r"""
  let field = null;
  try { field = pQS(arg.sel); } catch (e) { field = null; }
  if (!field && arg.el && arg.el.isConnected) field = arg.el;
  // Rows grouped by their list; fieldOwnList resolves the field's own menu the same way as the
  // snapshot pass, refusing (empty result) rather than guessing when it can't tell which list is whose.
  const rowSel = rowSelFor(field);
  const byList = new Map();
  const seen = new Set();
  for (const el of pScopeAll()) {
    if (!focusHas(el)) continue;
    if (field && pContains(el, field)) continue;
    const row = composedClosest(el, rowSel);
    if (!row || seen.has(row)) continue;
    seen.add(row);
    const txt = (row.textContent || '').replace(/\s+/g, ' ').trim();
    if (!txt || txt.length > 80) continue;
    const list = composedClosest(row, LIST_SEL) || row.parentNode;
    if (!byList.has(list)) byList.set(list, []);
    byList.get(list).push(txt);
  }
  const lists = Array.from(byList.keys()).sort((a, b) => byList.get(b).length - byList.get(a).length);
  const own = fieldOwnList(field, lists);
  const best = own ? byList.get(own) : [];
  if (!best.length) {
    const rec = window.__tv3_focus_offered;
    if (rec && rec.sel === arg.sel && Array.isArray(rec.labels) && rec.labels.length) return { total: rec.total || rec.labels.length, labels: rec.labels.slice(0, 15) };
  }
  return { total: best.length, labels: best.slice(0, 15) };
}"""
)

# Whether this field DECLARES a list popup of its own, or opened one whose rows it declares. Only such a
# field gets the reduced-query/empty-query recovery; on anything else those questions would be asked of a
# widget whose rows no rule can name, and the caller keeps the plain no-match path.
_FIELD_DECLARES_LIST_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + _ROW_SEMANTICS_JS
    + r"""
  let field = null;
  try { field = pQS(arg.sel); } catch (e) { field = null; }
  if (!field && arg.el && arg.el.isConnected) field = arg.el;
  if (!field) return false;
  if (fieldOwnPopup(field, false)) return true;
  // A widget that swaps its empty popup to role=status still declares the popup on the control itself.
  let haspopup = '';
  try { haspopup = String(field.getAttribute('aria-haspopup') || '').toLowerCase(); } catch (e) { haspopup = ''; }
  if (haspopup === 'listbox' || haspopup === 'grid' || haspopup === 'menu' || haspopup === 'tree') return true;
  const rowSel = rowSelFor(field);
  for (const el of pScopeAll()) {
    if (!focusHas(el)) continue;
    if (pContains(el, field)) continue;
    if (composedClosest(el, rowSel)) return true;
  }
  return false;
}"""
)

# Classifies the currently-open list's rows as EXPANDABLE CATEGORIES rather than leaves — for the
# no-match error path only, when _FIND_SUGGESTION_JS found nothing (a drilldown menu's leaves are
# often hidden a level down until their category is clicked, so text-matching never sees them). Unlike
# the reaction-gated finders above, category rows commonly PRE-EXIST the typing, so this does not gate
# on preHas/preReady — only on geometry (the same field-rect window) and a positive expand signal:
# aria-haspopup, aria-expanded, or (for a clickable option/menuitem/treeitem row) >=2 nested option
# rows. Tags qualifying rows data-tv3-menu="1..N" so the model can click one via the menu-click channel;
# leaves data-tv3-menu untouched when it tags nothing, so it never clobbers a prior menu's tags.
_FIND_CATEGORIES_JS = (
    r"""(args) => {"""
    + _PIERCED_QUERY_JS
    + r"""
  const field = pQS(args.field);
  if (!field) return null;
  const fr = field.getBoundingClientRect();
  const ROW_ROLES = new Set(['option', 'menuitem', 'treeitem', 'row', 'group']);
  const CHILD_ROLES = new Set(['option', 'menuitem', 'treeitem']);
  const cats = [];
  for (const el of pScopeAll()) {
    if (el === field || cats.length >= 8) continue;
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LABEL' || tag === 'FORM') continue;
    const role = el.getAttribute('role');
    if (!ROW_ROLES.has(role)) continue;
    // Never offer a navigational row: clicking an <a href>/<button> leaves the form (mirrors the same
    // exclusion in _FIND_SUGGESTION_JS). An already-open row (aria-expanded="true") would collapse on
    // click, not reveal, so it is not a category worth clicking either.
    if ((tag === 'A' && el.hasAttribute('href')) || tag === 'BUTTON') continue;
    if (el.getAttribute('aria-expanded') === 'true') continue;
    if (el.getAttribute('aria-disabled') === 'true' || el.hasAttribute('disabled')) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || r.height > 120) continue;
    if (r.top < fr.top - 400 || r.top > fr.bottom + 500) continue;
    if (r.right < fr.left || r.left > fr.right) continue;
    const hp = el.getAttribute('aria-haspopup');
    const hasPopup = hp !== null && hp !== 'false';
    // Only a COLLAPSED row is worth clicking to reveal options; aria-expanded="true" is already open,
    // so clicking it would toggle it closed.
    const hasExpanded = el.getAttribute('aria-expanded') === 'false';
    // A container role (group/row) is a static section wrapper unless it carries an explicit expand
    // affordance; only an option/menuitem/treeitem row may qualify on nested-option count alone, else
    // a grouped listbox (role=group over already-visible option leaves) is misread as a drilldown.
    let childCount = 0;
    if (CHILD_ROLES.has(role)) {
      for (const kid of el.querySelectorAll('[role]')) {
        if (CHILD_ROLES.has(kid.getAttribute('role'))) childCount++;
      }
    }
    // A drill-down prompt with NO popup ARIA at all marks its category rows with a TRAILING icon (a
    // chevron at the row's far edge) — the one affordance a person drills by. Trailing only: a leading
    // icon is a radio/checkbox/avatar and marks a selectable leaf, and an aria-selected row's icon is
    // its checkmark. (LTR assumption: RTL pages mirror the chevron and are not caught here.)
    let sideCharm = false;
    if (!hasPopup && !hasExpanded && childCount < 2 && CHILD_ROLES.has(role)
        && el.getAttribute('aria-selected') !== 'true') {
      for (const ic of el.querySelectorAll('svg,[class*="icon" i]')) {
        // An icon inside a button is the row's own affordance (a clear/remove control), not a drill marker.
        if (ic.closest('button,[role="button"]')) continue;
        const ir = ic.getBoundingClientRect();
        if (ir.width > 0 && ir.height > 0 && ir.left >= r.left + r.width * 0.6) { sideCharm = true; break; }
      }
    }
    if (!hasPopup && !hasExpanded && childCount < 2 && !sideCharm) continue;
    const label = el.getAttribute('aria-label') || (el.innerText || '').trim().split('\n')[0];
    const text = label.trim().slice(0, 80);
    if (!text) continue;
    cats.push({ el, text });
  }
  if (!cats.length) return null;
  pQSA('[data-tv3-menu]').forEach((e) => e.removeAttribute('data-tv3-menu'));
  cats.forEach((c, i) => c.el.setAttribute('data-tv3-menu', String(i + 1)));
  return { count: cats.length, categories: cats.map((c, i) => ({ n: i + 1, text: c.text })) };
}"""
)

# Tier-1 semantic commit read (SKY-15322): ONE shape-invariant probe consulted before the shape
# heuristics below. Decisive-ACCEPT-only — it answers {committed: true} or {committed: false}
# ("unknown"), never a decisive negative, so an unresolvable widget always falls to the heuristics
# unchanged. Every accept rests on a signal the tool did NOT author: raw value equality is NOT one
# (the tool typed the intended string itself, so it holds on every dead click — the equal-value
# impostor pair proves no black-box read can split that case).
# Tier-1 semantic commit read (SKY-15322): the narrowed solid core. ONE decisive-accept rule —
# the value TRANSFORM on a real form control: the widget rewrote what the tool typed into the
# intended value (raw equality is never evidence; the tool authored the typed string). Everything
# else is unknown by contract and falls to the shape heuristics unchanged: native selects (their own
# tool verifies by value; selection state alone carries no click causality), contenteditable anchors
# (three review rounds showed their text state cannot carry commit causality — completion, previews
# and blur-expansion are all indistinguishable from selection at the DOM level), and ARIA selection
# state (four distinct false-accept shapes across temporal, wiring, polarity and cross-root
# dimensions — retired to a later phase rather than guarded a fifth time).
_SEMANTIC_COMMIT_STATE_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + r"""
  const nrm = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim().toLowerCase();
  const want = nrm(arg.intended);
  if (!want) return { committed: false };
  let el = null;
  try { el = pQS(arg.sel); } catch (e) { el = null; }
  if (!el && arg.el && arg.el.isConnected) el = arg.el;
  if (!el) return { committed: false };
  if (el.tagName === 'SELECT') return { committed: false };
  if (el.isContentEditable) return { committed: false };
  // The typed baseline must have been READ from the element by the caller and is opt-in
  // (typedTrusted !== true reads as untrusted), so a caller that forgets the key fails SAFE.
  if (arg.typedTrusted !== true) return { committed: false };
  const cur = el.value;
  if (nrm(cur) === want && nrm(arg.typed) !== want) {
    return { committed: true, via: 'value-transform', value: String(cur || '').trim() };
  }
  return { committed: false };
}"""
)

# Read back whether the field committed a real selection CAUSED BY the suggestion click — not just that
# the field holds text (the caller typed into it before clicking, so a bare value check would call any
# no-op click a success). Committed iff the visible value (a) reflects the row we clicked (shares a word
# with the chosen suggestion, or the typed value) and (b) shows the click took effect — it changed from
# the raw typed text OR the suggestion list closed. Failing that, a hidden input in the nearest
# div/li/fieldset (never the whole <form>) whose value overlaps. Otherwise "" — nothing committed.
_VERIFY_COMMIT_JS = (
    r"""(args) => {"""
    + _PIERCED_QUERY_JS
    + r"""
  const toks = (s) => new Set(String(s).toLowerCase().replace(/[\/,]/g, ' ').split(/\s+/).filter((w) => w.length >= 3));
  const overlaps = (a, b) => { const B = toks(b); for (const w of toks(a)) if (B.has(w)) return true; return false; };
  // Token overlap drops words shorter than 3 chars, so a short committed label ("No", "MA") has no token
  // to overlap. Case/space-normalized EXACT equality rescues it — a hidden value that IS the chosen label
  // is a real commit, and an exact match cannot be an incidental partial overlap.
  const eqi = (a, b) => !!a && !!b && a.replace(/\s+/g, ' ').trim().toLowerCase() === b.replace(/\s+/g, ' ').trim().toLowerCase();
  const el = pQS(args.field) || (args.el && args.el.isConnected ? args.el : null);
  // null (not '') when there is nothing to read: the caller must tell "read it, no commit" from
  // "could not read it", and a later second probe would answer about a different instant.
  if (!el) return null;
  const typed = String(args.typed || '').trim();
  const chosen = String(args.chosen || '').trim() || typed;
  const cur = (el.value || '').trim();
  const tagged = pQS('[data-tv3-sugg]');
  // The open->observe->pick path tags no suggestion, so `listClosed` would be unconditionally true and
  // defeat the change check — the caller sets noSuggestionList so the el.value branch rests on an actual
  // change from the pre-click value (passed as `typed`), never leftover text the tool itself put there.
  const tagsGone = !tagged || tagged.getBoundingClientRect().height === 0;
  // Row tags vanish on ANY re-render; the stamped list container (suggListOpen, read by the caller)
  // survives one. Closure normally needs both: tags gone AND the stamped container gone/hidden — a
  // dead click whose re-render strips tags must not read as a commit. The one exemption is a widget
  // whose FIELD declares its list (fieldDeclared) with declared rows AND whose click fired an input
  // event on the field (commitEvt, armed just before the click): its commit legitimately leaves a
  // re-searching list open, and the declared close-and-verify (Escape + value survival) that runs
  // downstream can see and manage that popup. A dead click fires no input event, so a re-render
  // alone never qualifies.
  const listClosed = args.noSuggestionList
    ? false
    : (tagsGone && ((args.declaredRows && args.fieldDeclared && args.commitEvt) || !args.suggListOpen));
  // A short normalized value ("New York" -> "NY", "United States" -> "US") has no >=3-char token to
  // overlap, so accept it on causality alone (it changed / the list closed). Longer values must still
  // relate to the chosen suggestion so an unrelated change can't read as a successful commit.
  if (args.noSuggestionList) {
    // On the open->observe->pick path the CHOSEN label is known, so require the new value to BE it
    // (short: exact; long: token overlap), not merely "some short value changed" — a dead row that
    // resets the input to "N/A" changes cur but does not commit the chosen option.
    const declared = Array.isArray(args.chosenValues) ? args.chosenValues : [];
    if (cur && cur !== typed && (eqi(cur, chosen) || overlaps(cur, chosen) || declared.some((d) => eqi(d, cur)))) return cur;
    // typed == chosen == cur is unreadable by value alone (a dead row click leaves the same string in
    // the field). The click's own reaction discriminates: the caller tagged real suggestion rows
    // (suggTagged) and the LIST ITSELF is gone/hidden now (read off the stamped list
    // container — a re-render that merely replaces row nodes strips the row tags but keeps the
    // container, and must not read as a commit; suggListOpen). Exact equality with the CHOSEN label only.
    if (args.suggTagged && !args.suggListOpen && cur && eqi(cur, chosen) && tagsGone) return cur;
  } else if (cur && (cur !== typed || listClosed) && (toks(cur).size === 0 || overlaps(cur, chosen) || overlaps(cur, typed))) {
    return cur;
  }
  const cont = el.closest('div,li,fieldset');
  if (cont) {
    // On the pick path the caller snapshots the hidden values BEFORE the click: a value that merely
    // shares a token with the chosen label ("People Operations" left over while "Sales Operations" was
    // clicked dead) is the stale state, not a commit — only an exact chosen label or a CHANGED value counts.
    const preHidden = new Set(Array.isArray(args.preHidden) ? args.preHidden : []);
    for (const h of cont.querySelectorAll('input[type=hidden]')) {
      const v = (h.value || '').trim();
      if (!v) continue;
      if (eqi(v, chosen)) return v;
      if (args.noSuggestionList) {
        const declaredHidden = Array.isArray(args.chosenValues) ? args.chosenValues : [];
        if ((overlaps(v, chosen) || declaredHidden.some((d) => eqi(d, v))) && !preHidden.has(v)) return v;
        continue;
      }
      if (overlaps(v, chosen) || eqi(v, typed) || overlaps(v, typed)) return v;
    }
  }
  // React-Select / styled combobox: on commit the value moves OUT of the filter input into a
  // single-value node or token beside it and the input is cleared, so the reads above miss it. Read
  // that committed surface — but only once the widget reports closed (aria-expanded=false), so a
  // still-open list reflecting the typed filter can't read as a commitment, and scoped to the nearest
  // ancestor holding exactly this one combobox trigger, so a sibling field showing the same label
  // can't pre-confirm this one. Mirrors v1's _CUSTOM_SELECT_COMMITTED_STATE_JS.
  const expandedEl = el.getAttribute('aria-expanded') != null ? el : el.closest('[aria-expanded]');
  const expanded = expandedEl ? expandedEl.getAttribute('aria-expanded') : null;
  if (expanded === 'false') {
    const TRIGGER = "[role=combobox],[aria-haspopup=listbox],[aria-haspopup=menu],button[aria-expanded],input[role=combobox],select";
    const SURFACE = "[class*='single-value'],[class*='singleValue'],[class*='multi-value__label'],[role=option][aria-selected=true],.chip,.pill,[class*='token']";
    let scope = null;
    for (let anc = el.parentElement, hops = 1; anc && hops <= 4; hops++, anc = anc.parentElement) {
      const trig = anc.querySelectorAll(TRIGGER);
      if (anc.querySelector(SURFACE) && trig.length === 1 && (trig[0] === el || el.contains(trig[0]))) {
        scope = anc;
        break;
      }
    }
    if (scope) {
      // EXACT normalized match, not token overlap (mirrors v1's matchesExpected): a stale single-value
      // or a leftover multi-select token that merely SHARES a word with the chosen label would read as a
      // false commit when the real selection silently failed. A committed surface normally holds exactly
      // the chosen label (or, for a multi-value chip, it among comma-separated parts).
      const norm = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim().toLowerCase();
      const want = norm(chosen) || norm(typed);
      const surfaceMatches = (raw) => {
        const n = norm(raw);
        if (!n || !want) return false;
        return n === want || n.split(',').map((p) => p.trim()).includes(want);
      };
      for (const s of scope.querySelectorAll(SURFACE)) {
        // textContent OR the accessible name (aria-label): a chip/single-value can carry the committed
        // label only in aria-label with no text node — v1 reads both, so this must too.
        const t = (s.textContent || '').trim();
        if (surfaceMatches(t)) return t;
        const al = (s.getAttribute('aria-label') || '').trim();
        if (surfaceMatches(al)) return al;
      }
    }
  }
  // A button/div trigger with no input and no chip shows its committed label on ITSELF (aria-label
  // "Select country calling code: X" or its text). Read it only once it CHANGED from the pre-click
  // surface, and only as a whole clause of it, so a label that already named the value cannot
  // pre-confirm a click that did nothing.
  if (!['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) && expanded !== 'true' && typeof args.preSurface === 'string') {
    const nrm = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim().toLowerCase();
    // Both surfaces, each judged on its own: a static aria-label ("Choose country") beside a text
    // that shows the value, or a label that carries it while the text is a caret. The whole surface
    // equal to the value wins first (an option like "UTC+01:00" carries its own colon); otherwise
    // the committed clause is what follows the FIRST ':' ("Select country: X"), bounded by '|', split
    // on ',' only inside it, so "Korea, Republic of" stays whole and a later "| Other field: Y"
    // clause never vouches for this one.
    const want = nrm(chosen);
    const holds = (own) => {
      if (!own) return false;
      if (own === want) return true;
      const head = own.split('|')[0];
      const clause = head.includes(':') ? head.slice(head.indexOf(':') + 1).trim() : head.trim();
      return clause === want || clause.split(',').map((x) => x.trim()).includes(want);
    };
    const pre = String(args.preSurface || '').split('\u0001');
    const surfaces = [nrm(el.getAttribute('aria-label')), nrm(el.textContent)];
    if (want && surfaces.some((own, i) => own !== (pre[i] || '') && holds(own))) return chosen;
  }
  return '';
}"""
)

# The surfaces _VERIFY_COMMIT_JS reads off a button/div trigger itself (aria-label and own text,
# \u0001-joined), snapshotted before the click so only a CHANGE can count as the commit.
_ANCHOR_SURFACE_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + r"""
  const el = pQS(arg.sel) || arg.el;
  if (!el) return '';
  const nrm = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim().toLowerCase();
  return nrm(el.getAttribute('aria-label')) + '\u0001' + nrm(el.textContent);
}"""
)

# Whether the field's own widget container shows `chosen` as a committed-selection surface: a pill /
# selected-item row (its leading aria-label clause or leaf text IS the label), or a bare label node the
# widget renders beside a field that commits an opaque id into its value. Scope stops BELOW <body> on
# purpose: a portalled menu hangs off <body>, so its still-open rows can never vouch for a commit.
_COMMIT_SURFACE_JS = (
    r"""(args) => {"""
    + _PIERCED_QUERY_JS
    + r"""
  const el = pQS(args.sel) || (args.el && args.el.isConnected ? args.el : null);
  if (!el) return false;
  const nrm = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim().toLowerCase();
  const want = nrm(args.chosen);
  if (!want) return false;
  const visible = (n) => {
    const r = n.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return false;
    try { const cs = getComputedStyle(n); return cs.visibility !== 'hidden' && cs.display !== 'none'; } catch (e) { return true; }
  };
  // "<label>, press delete to clear value." — the instruction is ONE trailing comma-clause, and the
  // committed label may itself contain commas. Strip the trailing clause ONLY when it reads as a
  // widget instruction (press/delete/clear/…): an unconditional strip would let a bare "Korea" read
  // as holding a suffix-less "Korea, Republic of" label.
  // Actual instruction SYNTAX, not a keyword alone ("Austin, Clear Lake" is a place, not an
  // instruction) — mirror of the Python _INSTRUCTION_CLAUSE_RE.
  const INSTRUCTION_RE = /\b(?:press|tap|click|hit)\s+(?:delete|backspace|enter|escape)\b|\bto\s+(?:delete|remove|clear|dismiss|deselect)\b|\b(?:delete|remove|clear|dismiss|deselect|backspace)\s+(?:the\s+)?(?:value|values|selection|selections|item|items|option|options|entry|entries|choice|choices|tag|tags|this|it|all)\b/i;
  // Exact, or the value plus ONE parenthesized decoration ("United Kingdom (+44)") — the canonical-
  // label idiom that otherwise loops a covered field forever. Nothing looser: a prefix without its
  // own " (" boundary and a non-parenthetical suffix both stay refusals.
  const satisfied = (text) => {
    if (text === want) return true;
    if (!(text.length > want.length + 3 && text.startsWith(want + ' (') && text.endsWith(')'))) return false;
    const inner = text.slice(want.length + 2, -1);
    return inner.length > 0 && !inner.includes('(') && !inner.includes(')');
  };
  const clauseHolds = (raw) => {
    const own = nrm(raw).split('|')[0].trim();
    if (!own) return false;
    if (satisfied(own)) return true;
    const cut = own.lastIndexOf(',');
    if (cut <= 0 || !INSTRUCTION_RE.test(own.slice(cut + 1))) return false;
    return satisfied(own.slice(0, cut).trim());
  };
  // The stamped [data-tv3-sugglist] container is the LIVE option list of the pick in flight: its
  // rows are offers, never commits, and a re-render that strips row tags keeps the container stamp.
  // An explicit aria-selected="false" likewise marks an offered row.
  const excluded = (cand) =>
    cand === el || cand.contains(el)
    || !!cand.closest('[data-tv3-sugg],[data-tv3-menu],[data-tv3-sugglist]')
    || !!cand.querySelector('[data-tv3-sugg],[data-tv3-menu],[data-tv3-sugglist]')
    || cand.getAttribute('aria-selected') === 'false'
    || !!cand.closest('[aria-selected="false"]');
  // Only the field's OWN container may vouch for its commit: the walk stops before any ancestor that
  // holds a second field anchor, so a sibling field's pill (a two-column form, a shared fieldset) or
  // stray page text can never confirm THIS field — mirrors the single-trigger scope the react-select
  // surface read in _VERIFY_COMMIT_JS enforces.
  const FIELD_SEL = 'input:not([type=hidden]),textarea,select,[role="combobox"],[aria-haspopup="listbox"],[aria-haspopup="menu"],button[aria-expanded]';
  const fieldCount = (root) => {
    let n = 0;
    for (const f of root.querySelectorAll(FIELD_SEL)) {
      const fr = f.getBoundingClientRect();
      if (fr.width > 0 && fr.height > 0 && ++n >= 2) break;
    }
    return n;
  };
  // One step up the COMPOSED scope chain: a field at the top level of an open shadow root has a
  // null parentElement, but its committed pill can sit beside it in the same root — the walk
  // continues through the root (a DocumentFragment that still answers querySelectorAll) and out
  // via its host, instead of stopping blind at the boundary.
  const scopeUp = (s) => {
    if (!s) return null;
    if (s.host) {
      const h = s.host;
      if (h.parentElement) return h.parentElement;
      return h.parentNode && h.parentNode.host ? h.parentNode : null;
    }
    if (s.parentElement) return s.parentElement;
    return s.parentNode && s.parentNode.host ? s.parentNode : null;
  };
  let scope = scopeUp(el);
  for (let hops = 0; scope && hops < 4; hops++, scope = scopeUp(scope)) {
    if (scope === document.body || scope === document.documentElement) break;
    if (fieldCount(scope) >= 2) break;
    for (const cand of scope.querySelectorAll('[role="option"],[role="listitem"],li,[class*="pill" i],[class*="chip" i],[class*="token" i]')) {
      if (excluded(cand) || !visible(cand)) continue;
      const t = (cand.textContent || '').trim();
      if (t.length > 160) continue;
      if (clauseHolds(t) || clauseHolds(cand.getAttribute('aria-label'))) return true;
    }
    // Bare label surface: a small childless node whose whole text IS the label (the widget shows the
    // committed label beside a field whose own value is an opaque id). Exact match only.
    for (const cand of scope.querySelectorAll('*')) {
      if (cand.children.length > 0 || excluded(cand) || !visible(cand)) continue;
      if (satisfied(nrm(cand.textContent)) || satisfied(nrm(cand.getAttribute('aria-label')))) return true;
    }
  }
  return false;
}"""
)

# Stamp the suggestion list CONTAINER before the pick click, so the commit-verify can ask whether the
# LIST survived — row tags vanish on any re-render, but the container persists unless the widget truly
# closed. Clears prior stamps first.
_STAMP_SUGG_LIST_JS = (
    r"""(args) => {"""
    + _PIERCED_QUERY_JS
    + _ROW_SEMANTICS_JS
    + r"""
  pQSA('[data-tv3-sugglist]').forEach((e) => e.removeAttribute('data-tv3-sugglist'));
  const row = pQS('[data-tv3-' + args.attr + '="' + args.n + '"]');
  if (!row) return false;
  // Never stamp <body>/<html> — that would read the PAGE as the list and it never closes. With no
  // declared list ancestor and a body-level parent, the tagged node itself is the best stand-in.
  let list = composedClosest(row, LIST_SEL);
  if (!list || list === document.body || list === document.documentElement) {
    const parent = composedParentElement(row);
    list = parent && parent.nodeType === 1 && parent !== document.body && parent !== document.documentElement
      ? parent
      : row;
  }
  if (!list || list.nodeType !== 1) return false;
  list.setAttribute('data-tv3-sugglist', '1');
  return true;
}"""
)

# One-shot 'input'-event probe on the anchor, armed just before the pick click so keystrokes cannot
# pre-satisfy it: a committing widget writes the value back through an input dispatch, a dead click
# fires nothing. 'change' deliberately does not count — Chromium fires a native change for user-typed
# text the moment the row click steals focus, committed or not.
_ARM_COMMIT_EVENT_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + r"""
  let el = null;
  try { el = pQS(arg.sel); } catch (e) { el = null; }
  if (!el && arg.el && arg.el.isConnected) el = arg.el;
  window.__tv3_commit_evt = false;
  if (!el) return false;
  el.addEventListener('input', () => { window.__tv3_commit_evt = true; }, { once: true });
  return true;
}"""
)

# Whether the stamped suggestion list container is still on the page and visible. A stamp that
# VANISHED is ambiguous, not proof of closure: a widget that closes by unmounting destroys the
# stamped node, but so does a dead click's re-render when the stamp had to sit on a replaceable row
# (nothing durable enclosed it). Disambiguated by the dropdown band below the field: fresh visible
# content that postdates the pre-type snapshot means the list is still open; a band holding only
# pre-existing content means it closed. An unreadable anchor fails closed (open).
_SUGG_LIST_STILL_OPEN_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + r"""
  const list = pQS('[data-tv3-sugglist]');
  if (list) {
    const r = list.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return false;
    try { const cs = getComputedStyle(list); return cs.visibility !== 'hidden' && cs.display !== 'none'; } catch (e) { return true; }
  }
  const el = pQS(arg.sel) || (arg.el && arg.el.isConnected ? arg.el : null);
  if (!el) return true;
  const fr = el.getBoundingClientRect();
  for (const cand of pScopeAll()) {
    if (cand === el || pContains(cand, el) || pContains(el, cand)) continue;
    if (preHas(cand)) continue;
    const r = cand.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) continue;
    // Vertical INTERSECTION with the anchor's neighborhood, not a strict above/below split: a list
    // flips above the field near the viewport bottom, and a sloppily-positioned popup can overlap
    // the field's own line — both must still read as open.
    if (r.bottom < fr.top - 400 || r.top > fr.bottom + 400) continue;
    // But content sitting ENTIRELY inside the field's own line (an inline Clear, a saved badge, a
    // currency suffix a commit renders) is commit-adjacent decoration, never an open list.
    if (r.top > fr.top + 2 && r.bottom < fr.bottom - 2) continue;

    if (r.left > fr.right || r.right < fr.left) continue;
    let vis = true;
    try { const cs = getComputedStyle(cand); vis = cs.visibility !== 'hidden' && cs.display !== 'none'; } catch (e) { vis = true; }
    if (!vis) continue;
    // Text keeps empty decorative shells from reading as an open list — but a dead click can swap
    // the rows for a TEXTLESS css spinner (an async widget mid-flight), so busy-shaped fresh
    // content counts without it.
    const busyish = cand.getAttribute('aria-busy') === 'true' || cand.getAttribute('role') === 'progressbar'
      || /load|spinner|progress|busy/i.test(cand.className && cand.className.baseVal !== undefined ? cand.className.baseVal : String(cand.className || ''));
    if (!(cand.textContent || '').trim() && !busyish) continue;
    return true;
  }
  return false;
}"""
)

# A visible in-flight indicator (a busy row, a spinner) NEAR the anchor — the widget is still
# fetching its rows, so a row poll that would otherwise give up is allowed to keep waiting a little
# longer. Bounded to the anchor's dropdown region so an unrelated page spinner (a chat widget, an
# autosave indicator) cannot extend every poll on the page.
_MENU_BUSY_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + r"""
  const el = pQS(arg.sel) || (arg.el && arg.el.isConnected ? arg.el : null);
  const a0 = el ? el.getBoundingClientRect() : null;
  // A hidden (zero-rect) anchor would pin the window to the viewport origin; treat it like an
  // unresolvable one.
  const a = a0 && a0.width > 0 && a0.height > 0 ? a0 : null;
  // With no resolvable anchor (a cascading click detached the clicked row), geometry cannot bound the
  // check — require the busy node to sit in a FLOATING container instead (the replaced menu's own busy
  // row does; an in-flow page spinner does not).
  const floating = (n) => {
    for (let x = n, hops = 0; x && x.nodeType === 1 && hops < 8; hops++, x = composedParentElement(x)) {
      let pos = '';
      try { pos = getComputedStyle(x).position; } catch (e) { return false; }
      if (pos === 'absolute' || pos === 'fixed') return true;
    }
    return false;
  };
  // Conventional CSS spinner classes count like the ARIA signals — same recognition the vanished-
  // stamp band check applies. Still bounded to the anchor's region, so a stray "download" link
  // elsewhere cannot extend every poll.
  for (const n of pQSA('[aria-busy="true"],[role="progressbar"],[class*="load" i],[class*="spinner" i],[class*="progress" i],[class*="busy" i]')) {
    const r = n.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) continue;
    if (!a) { if (floating(n)) return true; continue; }
    if (r.top < a.top - 200 || r.top > a.bottom + 500) continue;
    if (r.right < a.left - 200 || r.left > a.right + 200) continue;
    return true;
  }
  return false;
}"""
)

# Whether the tagged menu row (or its closest aria-selected carrier) already reports selected.
_MENU_ROW_SELECTED_JS = (
    r"""(n) => {"""
    + _PIERCED_QUERY_JS
    + r"""
  const row = pQS('[data-tv3-menu="' + n + '"]');
  // Composed-tree lookups: an option component carries the state on its host, outside the root the
  // tagged leaf lives in, and a row read as unselected there is clicked -- toggling it off.
  const sel = row ? composedClosest(row, '[aria-selected]') : null;
  const selected = !!sel && sel.getAttribute('aria-selected') === 'true';
  const list = sel ? composedClosest(sel, '[aria-multiselectable]') : null;
  return { selected, multi: !!list && list.getAttribute('aria-multiselectable') === 'true' };
}"""
)


# What the tagged row would commit: the pick path passes these as `chosenValues` so a commit that
# stores a code the label never contains still verifies.
_MENU_ROW_VALUES_JS = (
    r"""(n) => {"""
    + _PIERCED_QUERY_JS
    + _ROW_SEMANTICS_JS
    + _DECLARED_VALUES_JS
    + r"""
  const row = pQS('[data-tv3-menu="' + n + '"]');
  return row ? declaredValues(row) : [];
}"""
)

# The hidden-input values _VERIFY_COMMIT_JS would read for this field, in the same div/li/fieldset scope.
_HIDDEN_VALUES_JS = (
    "el => { const c = el.closest('div,li,fieldset'); if (!c) return []; "
    "return Array.from(c.querySelectorAll('input[type=hidden]')).map((h) => (h.value || '').trim()).filter(Boolean); }"
)

# Why a reaction probe's answer about this selector may not carry a claim. `unprobeable` -- in-page
# CSS cannot even parse it (Playwright syntax like `css=`, `>> nth=`, `:visible`, `text=`).
# `component` -- it lives inside a component. The probes DO pierce open shadow roots, so this no
# longer means they are blind to the element itself; it means the widget's own list may still render
# where a pierced query does not reach -- a portal mounted elsewhere in the page, or a closed root,
# which is undetectable from script. So a missing suggestion list there is still not proof the field
# is unfilled, and the softened reading is kept deliberately rather than for lack of reach.
# `` -- neither applies, so any failure to find the element is a fact about the page, not about us.
# Both readings happen in ONE evaluation: as two round trips, an ordinary re-render landing between
# them lets each describe a different moment.
_PROBE_REACH_JS = (
    r"""(arg) => {
  const _roots = """
    + _SHADOW_ROOTS_JS
    + r""";
  try {
    if (document.querySelector(arg.sel)) return '';
  } catch (e) { return 'unprobeable'; }
  try {
    for (const root of _roots(document)) { if (root.querySelector(arg.sel)) return 'component'; }
  } catch (e) { return ''; }
  return (arg.el && arg.el.isConnected ? arg.el : null) ? 'component' : '';
}"""
)

# Whether a selector currently resolves. `true` on a broken selector: existence is only ever used to
# soften/enrich behavior, so an unparseable selector must take the normal (unenriched) path.
# Pierces open shadow roots because the caller compares against what `page.click` would resolve, and
# a document-only probe reports "gone" for every element a web component renders.
_SELECTOR_EXISTS_JS = (
    r"""(arg) => {
  const _roots = """
    + _SHADOW_ROOTS_JS
    + r""";
  try {
    let found = null;
    for (const root of _roots(document)) { found = root.querySelector(arg.sel); if (found) break; }
    // The executor can resolve a selector no single root can match; that is existence too.
    if (!found) found = (arg.el && arg.el.isConnected ? arg.el : null);
    return !!found;
  } catch (e) { return true; }
}"""
)

# How many distinct elements a minted marker resolves to across open shadow roots: a re-render that
# CLONES the marked node copies the attribute, and a non-strict click would land on the first match.
# A root whose query throws is skipped without erasing duplicates already proven; with nothing proven
# it reads as 1, so an unparseable marker takes the normal path, like _SELECTOR_EXISTS_JS.
_MARKER_MATCH_COUNT_JS = (
    r"""(arg) => {
  const _roots = """
    + _SHADOW_ROOTS_JS
    + r""";
  const matches = new Set();
  let unreadable = false;
  try {
    for (const root of _roots(document)) {
      try { for (const e of root.querySelectorAll(arg.sel)) matches.add(e); } catch (e) { unreadable = true; }
    }
    if (matches.size === 0 && arg.el && arg.el.isConnected) matches.add(arg.el);
  } catch (e) { unreadable = true; }
  if (matches.size > 1) return matches.size;
  return unreadable ? 1 : matches.size;
}"""
)

# The single discriminator observe and both act paths share: the element that visibly stands in for a
# control the page renders at zero size. Nothing rendered means a collapsed section, a closed modal or
# an inactive step. Kept as one fragment because three page.evaluate payloads cannot be kept in sync by
# hand, and the whole point is that perception and action agree on what counts as a styled proxy.
# The text the page still shows one control as in flight with, or null. Applied to an element handle
# the caller resolved, never to a selector -- resolution belongs to Playwright's engine, which is what
# the action tools act through.
PENDING_MARKER_JS = (
    "(el) => { let ctl = el;"
    " try { ctl = Element.prototype.closest.call(el,"
    "   'button,input[type=submit],input[type=button],input[type=image],[role=button]') || el }"
    " catch(e) {}"
    # A selector can name a wrapper (a <form>, or a clickable <div> the page put the handler on)
    # rather than the control. Descend when the element is not itself a control and holds exactly
    # one: a card holds several, or none.
    " if (ctl === el && el.tagName !== 'BUTTON' && el.tagName !== 'INPUT') {"
    "   let inners = []; try { inners = el.querySelectorAll("
    "     'button,input[type=submit],input[type=button],input[type=image],[role=button]') } catch(e) {}"
    "   if (inners.length === 1) ctl = inners[0]; }"
    " const isButtonInput = ctl.tagName === 'INPUT'"
    "   && /^(submit|button|image)$/i.test(ctl.getAttribute('type') || '');"
    # .value on a text input is the model's own typed text, not a label the page rendered.
    # The control's OWN label, not its whole subtree: innerText spans every descendant and `closest`
    # can climb to a card-sized [role=button], so a status row ("Processing - Order 4821 - $32.10")
    # would read as an in-flight submit. The subtree is a fallback only for a real <button>/<input>
    # simple enough to be one (a spinner plus a label) -- [role=button] is a claim the page makes,
    # and it is what cards are built from.
    " let own = '';"
    " for (const n of ctl.childNodes) { if (n.nodeType === 3) own += n.nodeValue; }"
    " const isElementControl = ctl.tagName === 'BUTTON' || ctl.tagName === 'INPUT';"
    " let inner = '';"
    " try { inner = isElementControl ? (ctl.innerText || '') : '' } catch(e) {}"
    " const t = String(own.trim() || inner || (isButtonInput ? ctl.value : '') || '').trim().slice(0, 60);"
    " if (!/^(submitting|processing|sending|uploading)\\b/i.test(t)) return null;"
    " const r = ctl.getBoundingClientRect();"
    " if (r.width < 8 || r.height < 8) return null;"
    " let cs; try { cs = getComputedStyle(ctl) } catch(e) { return null }"
    " if (cs.clip && cs.clip !== 'auto') return null;"
    " if (cs.clipPath && cs.clipPath !== 'none') return null;"
    " let shown;"
    " try { shown = ctl.checkVisibility({opacityProperty: true, visibilityProperty: true,"
    "   contentVisibilityAuto: true}) }"
    " catch(e) { shown = !(cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') < 0.05) }"
    " if (!shown) return null;"
    " return ctl.getAttribute('aria-busy') === 'true' ? t + ' (aria-busy)' : t; }"
)


async def pending_marker(page: Any, selector: str) -> str | None:
    """The text the page still shows `selector`'s control as in flight with, or None.

    Resolution goes through Playwright's engine — the one the action tools act through — so the probe
    judges the element the run acted on. A second, hand-rolled resolver would be a second source of
    truth: shadow-piercing CSS, host-anchored selectors straddling a shadow boundary, and the
    text=/xpath forms all resolve here and none of them resolve through an in-page querySelector walk.
    Fails open: an unresolvable control reports nothing, and nothing is not evidence of pending."""
    try:
        handles = await page.query_selector_all(selector)
    except Exception:
        LOG.warning("taskv3 pending-marker probe could not resolve the control", selector=selector, exc_info=True)
        return None
    if not handles:
        # Not an error: the control being gone is the ordinary shape of a submission that landed.
        return None
    if len(handles) > 1:
        # cloneNode copies attributes, so a page that duplicates a control the run acted on can leave
        # two elements answering to one selector. Read them ALL and report the first that is still in
        # flight: taking whichever came first in document order would answer "settled" off a twin the
        # run never touched while the real control was still submitting -- a false completion, where
        # an extra hold costs only a turn.
        LOG.info("taskv3 pending-marker probe found more than one control", selector=selector, matches=len(handles))
    for handle in handles:
        try:
            marker: str | None = await handle.evaluate(PENDING_MARKER_JS)
        except Exception:
            LOG.warning("taskv3 pending-marker probe failed on the control", selector=selector, exc_info=True)
            continue
        if marker:
            return marker
    return None


# Cap on marks a single look draws: more than this yields an unreadable set-of-marks image and a legend
# the model cannot map back. In DOM order (document first), so a truncated look still numbers the
# top-of-page controls the model most likely wants.
_LOOK_MAX_MARKS = 60

# Hard per-run ceiling on look() calls. look bills one image per call and is not an action step, so
# without a cap the only bound is max_turns (~1 image/turn) — a metered vision cost the operator's
# constraint forbids. A last-resort tool rarely needs more than a handful; past this it returns an
# error pointing back at the text tools rather than adding another image.
_LOOK_MAX_PER_RUN = 20

# Enumerate the same interactive controls observe() does, across open shadow roots, keeping only the
# ones with pixels on screen (a visible box intersecting the viewport). Tag each with a transient
# data-tv3-look index and return its CSS-px rect so the marks can be drawn on the screenshot. The
# index is cleared right after handles are grabbed — it exists only to pair a handle to a rect.
_LOOK_ENUM_JS = (
    r"""(() => {
  const _roots = """
    + _SHADOW_ROOTS_JS
    + r""";
  const q = 'input,textarea,select,button,a[href],[role=button],[role=checkbox],[role=radio],[role=combobox],[role=option],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=listbox],[role=switch],[role=spinbutton],[role=tab],[contenteditable=true]';
  // Interpolated from the Python pattern so the two spellings of "opaque" cannot drift apart.
  const _OPAQUE = /"""
    + _OPAQUE_ID_RUN_RE.pattern
    + r"""/i;
  const vw = window.innerWidth, vh = window.innerHeight;
  const seen = new Set();
  const out = [];
  let n = 0;
  let truncated = false;
  for (const root of _roots(document)) {
    let els;
    try { els = root.querySelectorAll(q); } catch (e) { continue; }
    for (const el of els) {
      if (seen.has(el)) continue;
      seen.add(el);
      let r;
      try { r = el.getBoundingClientRect(); } catch (e) { continue; }
      if (r.width < 4 || r.height < 4) continue;
      if (r.bottom <= 0 || r.right <= 0 || r.top >= vh || r.left >= vw) continue;
      let shown = true;
      try {
        shown = el.checkVisibility
          ? el.checkVisibility({opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true})
          : true;
      } catch (e) {}
      if (!shown) continue;
      if (n >= """
    + str(_LOOK_MAX_MARKS)
    + r""") { truncated = true; break; }
      n += 1;
      try { el.setAttribute('data-tv3-look', String(n)); } catch (e) { n -= 1; continue; }
      let label = '';
      let placeholder = '';
      try {
        const t = (el.getAttribute('type') || '').toLowerCase();
        // .value is a useful label for a text/submit field but is 'on'/junk for a checkbox or radio —
        // and is the SECRET for a password field, which (like observe) must never enter the legend.
        const valuable = el.tagName === 'INPUT' && !['checkbox', 'radio', 'password'].includes(t) ? (el.value || '') : '';
        // Cap generously (not the 80-char display width): the value is masked for payload-minted
        // signed URLs Python-side, which needs the WHOLE URL to match by provenance before the label
        // is truncated for display. A tighter cap here would truncate the URL past recognition.
        // As in observe: the associated <label> outranks the placeholder, which is a template hint,
        // not a name -- and travels separately when it differs, since a format hint makes the value typeable.
        let named = '';
        if (el.labels) { for (const l of el.labels) { named = (l.innerText || '').trim(); if (named) break; } }
        placeholder = (el.getAttribute('placeholder') || '').trim().replace(/\s+/g, ' ').slice(0, 2000);
        // An opaque `name` is the identity observe hands out under an alias, not a label: printing
        // it here would give the model the raw id back, in the one tool result masking never scans.
        const nm = el.getAttribute('name') || '';
        label = (el.getAttribute('aria-label') || named || placeholder || valuable
          || el.innerText || el.getAttribute('title') || (_OPAQUE.test(nm) ? '' : nm) || '')
          .trim().replace(/\s+/g, ' ').slice(0, 2000);
      } catch (e) {}
      const rec = { n, x: r.left, y: r.top, w: r.width, h: r.height, tag: (el.tagName || '').toLowerCase(), label };
      if (placeholder && placeholder !== label) rec.placeholder = placeholder;
      out.push(rec);
    }
    if (truncated) break;
  }
  return { vw, vh, truncated, elements: out };
})()"""
)

_ACT_ATTR_RE = re.compile(r'\s*data-tv3-act="[^"]*"')
_ACT_SELECTOR_PREFIX = '[data-tv3-act="'
# Write the act-by-mark attribute on an element handle the caller already resolved (Playwright's
# engine, which pierces open shadow). Returns whether the node is still connected; a detached handle
# errors rather than re-resolving by a stale coordinate. The attribute outlives the call: the submit
# watch re-resolves this selector turns later.
# Keyed on the ELEMENT, which is the only identity that satisfies every consumer at once: the node
# keeps whatever token it was first given, so re-acting one control mints the same tag however the
# marks have been renumbered since, while a different control gets its own and never steals it.
# Returns the token in force, or "" for a detached node or a clobbered accessor.
# Writes only. The DECISION about whether a tag can be trusted as an identity is deliberately NOT
# made here: this runs in the page's own realm, where RegExp, getAttribute and setAttribute are all
# replaceable, so a page could answer "yes, that forged value is yours". The caller reads the result
# back through Playwright's accessor and judges it there.
_ACT_WRITE_HANDLE_JS = (
    "(el, t) => { try { el.setAttribute('data-tv3-act', t); } catch (e) { return false; } return el.isConnected; }"
)


def _annotate_screenshot(png_bytes: bytes, elements: list[dict[str, Any]], vw: int, *, max_width: int = 1024) -> bytes:
    """Draw a numbered set-of-marks box over each element on the viewport screenshot, server-side.

    Boxes are drawn in the SAME numbering the legend and act-by-mark use. The screenshot is in device
    pixels and the rects in CSS pixels; downscaling to `max_width` first and mapping CSS px through the
    single factor `final_width / vw` folds devicePixelRatio and the downscale into one transform, so
    the boxes land regardless of the display's pixel ratio."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    if img.width > max_width:
        scale = max_width / img.width
        img = img.resize((max_width, max(1, round(img.height * scale))))
    factor = (img.width / vw) if vw else 1.0
    draw = ImageDraw.Draw(img)
    for e in elements:
        x0 = e["x"] * factor
        y0 = e["y"] * factor
        x1 = (e["x"] + e["w"]) * factor
        y1 = (e["y"] + e["h"]) * factor
        draw.rectangle([x0, y0, x1, y1], outline=(255, 0, 0), width=2)
        label = str(e["n"])
        tw = 6 * len(label) + 4
        # Sit the label tag just above the box, but drop it just inside the top edge when the box is
        # flush against the top of the viewport so a top-row mark's number stays legible.
        ly = y0 - 12 if y0 >= 12 else y0
        draw.rectangle([x0, ly, x0 + tw, ly + 12], fill=(255, 0, 0))
        draw.text((x0 + 2, ly + 1), label, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# Whether a node paints, read through the FLAT tree: a display:contents element (a <slot> is one by
# default) has no box of its own and renders exactly when something assigned or contained does, so
# `getClientRects` on it, `checkVisibility`, and a light-tree walk all misjudge slotted content. One
# predicate, shared by the cross-root walk and its text reader (the reachability seam keeps its own).
_RENDERS_JS = r"""(() => {
  const styleOf = (n) => { try { return getComputedStyle(n); } catch (e) { return null; } };
  // A box with no area (transform: scale(0), collapsed) paints nothing.
  const hasBox = (n) => {
    try { for (const r of n.getClientRects()) { if (r.width > 0 && r.height > 0) return true; } } catch (e) { /* no box */ }
    return false;
  };
  const textShows = (t) => {
    try {
      if (!String(t.textContent || '').trim()) return false;
      const r = document.createRange(); r.selectNode(t); const b = r.getBoundingClientRect();
      return b.width > 0 && b.height > 0;
    } catch (e) { return false; }
  };
  let budget = 0;
  // Opacity composes down the flat tree: a slotted node is under its slot's ancestors, a shadow
  // tree under its host. An unfinished climb counts as transparent.
  const transparent = (n) => {
    try {
      let e = n;
      for (let i = 0; e; i++) {
        if (i >= 24) return true;
        const st = styleOf(e);
        if (st && st.opacity === '0') return true;
        let next = null;
        try { next = e.assignedSlot; } catch (x) { next = null; }
        if (!next) next = e.parentElement;
        if (!next) { const r = Node.prototype.getRootNode.call(e); next = r && r.nodeType === 11 ? r.host : null; }
        e = next;
      }
    } catch (e) { return true; }
    return false;
  };
  const renders = (n, depth) => {
    const st = styleOf(n);
    if (!st || st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
    if (depth === 0 && transparent(n)) return false;
    if (st.display !== 'contents' && String(n.localName || '') !== 'slot') return hasBox(n);
    if (depth > 12 || budget-- <= 0) return false;
    let kids = null;
    if (String(n.localName || '') === 'slot') { try { kids = n.assignedNodes({ flatten: true }); } catch (e) { kids = null; } }
    if (!kids) kids = n.childNodes;
    for (const c of kids) {
      if (c.nodeType === 3) { if (textShows(c)) return true; continue; }
      if (c.nodeType === 1 && renders(c, depth + 1)) return true;
    }
    return false;
  };
  const api = (n) => { budget = 400; return renders(n, 0); };
  api.transparent = transparent;
  api.textShows = textShows;
  return api;
})()"""

# innerText stops at a <slot>: the slotted light-DOM words belong to the host, so a label spelled
# `<label><slot></slot> *</label>` reads as " *". Only a <label> in a shadow root that holds a slot
# goes through the flat tree; every other label keeps innerText, so a name the chain already produced
# does not move. The flat read has no shortcut: every element on the way is screened by its own
# style, and only a text node that paints with area is emitted. Labels in an ANCESTOR root are not
# read here (SKY-15175).
_LABEL_TEXT_JS = (
    r"""(() => {
  const renders = """
    + _RENDERS_JS
    + r""";
  return (l) => {
  try {
    const CAP = typeof _RETAIN_WIDTH === 'number' ? _RETAIN_WIDTH : 2000;
    const holdsSlot = (n) => { try { return !!n.querySelector('slot'); } catch (e) { return false; } };
    const styleOf = (n) => { try { return getComputedStyle(n); } catch (e) { return null; } };
    let root = null;
    try { root = Node.prototype.getRootNode.call(l); } catch (e) { root = null; }
    const isLabel = String(l.localName || '') === 'label';
    if (!isLabel || !root || root.nodeType !== 11 || !holdsSlot(l)) return (l.innerText || '').trim();
    // Opacity above the label composes down; below it every element is screened on the way.
    if (renders.transparent(l)) return '';
    let out = '';
    let budget = 600;
    const hidden = (n) => {
      const st = styleOf(n);
      return !st || st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0';
    };
    // Only a text node that paints with area is emitted: a zero-area container, a scaled-away
    // box, a hidden ancestor and an overflowing caption are all decided by the text's own rect.
    const emitText = (t) => { if (renders.textShows(t)) out += t.textContent || ''; };
    const flat = (n, depth) => {
      if (depth > 12) return;
      let kids = null;
      if (String(n.localName || '') === 'slot') { try { kids = n.assignedNodes({ flatten: true }); } catch (e) { kids = null; } }
      if (!kids) kids = n.childNodes;
      for (const c of kids) {
        if (out.length > CAP || budget-- <= 0) return;
        if (c.nodeType === 3) { emitText(c); continue; }
        if (c.nodeType !== 1) continue;
        const name = String(c.localName || '');
        if (name === 'style' || name === 'script' || name === 'template') continue;
        if (name === 'br') { out += ' '; continue; }
        if (hidden(c)) continue;
        const st = styleOf(c);
        const block = !!st && st.display !== 'contents' && name !== 'slot' && !/^inline/.test(st.display);
        if (block) out += ' ';
        let sr = null;
        try { sr = c.shadowRoot; } catch (e) { sr = null; }
        if (sr && sr.nodeType === 11) flat(sr, depth + 1); else flat(c, depth + 1);
        if (block) out += ' ';
      }
    };
    if (!hidden(l)) flat(l, 0);
    return out.replace(/\s+/g, ' ').trim().slice(0, CAP);
  } catch (e) { return ''; }
  };
})()"""
)

_VISIBLE_PROXY_JS = r"""(el) => {
  let named = el.labels && el.labels[0];
  if (!named) {
    const lbId = el.getAttribute('aria-labelledby');
    // An IDREF resolves inside the element's OWN tree, so a control in a shadow root must be looked
    // up there -- document.getElementById cannot see it. Read through the prototype like every other
    // root check here; an element whose root cannot hold ids simply has no name.
    let root = null;
    try { root = Node.prototype.getRootNode.call(el); } catch (e) { root = null; }
    named = lbId && root && root.getElementById ? root.getElementById(String(lbId).trim().split(/\s+/)[0]) : null;
  }
  const r = named ? named.getBoundingClientRect() : null;
  return r && r.width > 0 && r.height > 0 ? named : null;
}"""

# The executor's selector engine pierces open shadow roots, and observe now derives selectors from
# every one of them, so a probe resolving a selector against the document alone silently declines to
# act on a control it has just listed. Roots are visited in walk order, the order that engine matches
# in, and gathered once per payload so a probe never walks the page twice.
_ROOT_QUERY_JS = (
    r"""(() => {
  const _roots = """
    + _SHADOW_ROOTS_JS
    + r""";
  const roots = _roots(document);
  // A host-anchored selector's halves straddle a shadow boundary, so no single root matches it. The
  // executor's engine pierces open roots at a descendant combinator; do the same here rather than
  // taking the element from the page's realm, where any handover marker is one the page can move.
  const _parts = (sel) => {
    const parts = [];
    let cur = '', depth = 0, quote = null;
    for (let i = 0; i < sel.length; i++) {
      const c = sel[i];
      if (quote) { cur += c; if (c === '\\') cur += sel[++i] || ''; else if (c === quote) quote = null; continue; }
      if (c === '"' || c === "'") { quote = c; cur += c; continue; }
      if (c === '[' || c === '(') depth++;
      else if (c === ']' || c === ')') depth--;
      if (depth === 0 && /\s/.test(c)) { if (cur) parts.push(cur); cur = ''; continue; }
      if (depth === 0 && (c === '>' || c === '+' || c === '~' || c === ',')) return null;
      cur += c;
    }
    if (cur) parts.push(cur);
    return parts.length > 1 ? parts : null;
  };
  const _within = (anc, n) => {
    let p = n;
    while (p) { p = p.nodeType === 11 ? p.host : p.parentNode; if (p === anc) return true; }
    return false;
  };
  const composed = (sel) => {
    const parts = _parts(sel);
    if (!parts) return [];
    let cands = null;
    for (const part of parts) {
      const found = [];
      for (const root of roots) {
        try { for (const e of root.querySelectorAll(part)) found.push(e); } catch (e) { return []; }
      }
      cands = cands === null ? found : found.filter((m) => cands.some((a) => _within(a, m)));
      if (!cands.length) return [];
    }
    return cands;
  };
  return {
    find: (sel) => {
      for (const root of roots) {
        let f = null;
        // A throw is the ROOT's, not the selector's -- it was already parsed by an earlier root.
        try { f = root.querySelector(sel); } catch (e) { continue; }
        if (f) return f;
      }
      // Only an unambiguous composed match: the engine's ordering across roots is its own, and a
      // probe that reasons about one twin while the action lands on the other is worse than none.
      const c = composed(sel);
      return c.length === 1 ? c[0] : null;
    },
    all: (sel) => {
      const out = [];
      for (const root of roots) {
        try { for (const e of root.querySelectorAll(sel)) out.push(e); } catch (e) { /* this root only */ }
      }
      for (const e of composed(sel)) if (!out.includes(e)) out.push(e);
      return out;
    },
  };
})()"""
)

# A page can shadow `el.labels`/`el.control` with an own-property getter returning an unrelated
# element, so every probe below that needs a control's label (or a label's control) resolves the
# association itself off prototype accessors instead of trusting those IDL properties. These probes
# run in an isolated world (see `_evaluate_isolated`), where the realm's own prototypes are pristine.
_NATIVE_LABEL_JS = r"""
  const _attr = (n, name) => { try { return Element.prototype.getAttribute.call(n, name); } catch (e) { return null; } };
  const _tag = (n) => {
    try { return String(Object.getOwnPropertyDescriptor(Element.prototype, 'tagName').get.call(n) || ''); }
    catch (e) { return ''; }
  };
  const _isLabel = (n) => { try { return n instanceof HTMLLabelElement && _tag(n) === 'LABEL'; } catch (e) { return false; } };
  const _parentOf = (n) => {
    try { return Object.getOwnPropertyDescriptor(Node.prototype, 'parentNode').get.call(n); }
    catch (e) { return null; }
  };
  // A page can shadow a root's own querySelectorAll (own-property getter/override) to hide or forge
  // matches; calling the interface's prototype method un-does that regardless of which root type it is.
  const _qsa = (root, sel) => {
    try {
      const proto = root.nodeType === 9 ? Document.prototype : root.nodeType === 11 ? DocumentFragment.prototype : Element.prototype;
      return Array.from(proto.querySelectorAll.call(root, sel));
    } catch (e) { return []; }
  };
  const _matches = (n, sel) => { try { return Element.prototype.matches.call(n, sel); } catch (e) { return false; } };
  // Per-call memoization only: this snippet is re-evaluated fresh on every probe, so the Map never
  // survives across probes and can't go stale as the page mutates between calls.
  const _idMapCache = new Map();
  // One full pass per root, keeping the FIRST element per id (tree order), so a later `for` target is
  // never lost to a truncation. Past the safety bound the lookup refuses to answer rather than
  // return an element it cannot prove is the first one.
  const _firstById = (root, id) => {
    let m = _idMapCache.get(root);
    if (m === undefined) {
      const all = _qsa(root, '[id]');
      m = all.length > 100000 ? null : new Map();
      if (m) {
        for (const e of all) {
          const v = _attr(e, 'id');
          if (v !== null && !m.has(v)) m.set(v, e);
        }
      }
      _idMapCache.set(root, m);
    }
    return m ? m.get(id) || null : null;
  };
  const _LABELABLE = 'button,input:not([type=hidden]),meter,output,progress,select,textarea';
  // Only a FORM-ASSOCIATED custom element is labelable, and that flag lives on the element's
  // definition in the page's registry, which this realm cannot read. The one signal that carries it
  // across worlds is this realm's own (pristine) `control` getter, so a dashed tag is labelable
  // exactly when the label's native control IS the element.
  let _controlGetter = null;
  try { _controlGetter = Object.getOwnPropertyDescriptor(HTMLLabelElement.prototype, 'control').get; } catch (e) { _controlGetter = null; }
  const _isLabelable = (n, lbl) => {
    if (_matches(n, _LABELABLE)) return true;
    if (!_tag(n).includes('-') || !_matches(n, ':defined') || !_controlGetter) return false;
    try { return _controlGetter.call(lbl) === n; } catch (e) { return false; }
  };
  // HTML spec "labeled control" algorithm: an explicit `for` (even present-but-empty) never falls
  // back to the implicit wrapping-descendant rule, and a duplicate id always resolves to the FIRST
  // matching element in tree order -- a later duplicate's own for-target is simply not this element.
  const nativeControlOf = (lbl) => {
    if (!_isLabel(lbl)) return null;
    try {
      const forAttr = _attr(lbl, 'for');
      if (forAttr !== null) {
        if (!forAttr) return null;
        let root = null;
        try { root = Node.prototype.getRootNode.call(lbl); } catch (e) { root = null; }
        if (!root) return null;
        const first = _firstById(root, forAttr);
        return first && _isLabelable(first, lbl) ? first : null;
      }
      const cands = _qsa(lbl, '*');
      for (let i = 0; i < cands.length && i < 5000; i++) if (_isLabelable(cands[i], lbl)) return cands[i];
      return null;
    } catch (e) { return null; }
  };
  // Every association edge (explicit `for`, wrapping label, duplicate id, shadow-root boundary,
  // multiple labels) is a re-derivation of nativeControlOf, never a parallel rule of its own.
  const nativeLabelsOf = (el) => {
    const out = [];
    try {
      let root = null;
      try { root = Node.prototype.getRootNode.call(el); } catch (e) { root = null; }
      if (!root) return out;
      let candidates = _qsa(root, 'label');
      if (candidates.length > 5000) {
        const id = _attr(el, 'id');
        const ancestors = new Set();
        for (let n = _parentOf(el), hops = 0; n && hops < 256; hops++, n = _parentOf(n)) {
          if (n.nodeType === 1) ancestors.add(n);
        }
        candidates = candidates.filter((lbl) => ancestors.has(lbl) || (id && _attr(lbl, 'for') === id)).slice(0, 5000);
      }
      for (let i = 0; i < candidates.length && i < 5000; i++) {
        if (nativeControlOf(candidates[i]) === el) out.push(candidates[i]);
      }
    } catch (e) { /* best-effort */ }
    return out;
  };
  const _isToggle = (n) => {
    try { return _tag(n) === 'INPUT' && ['checkbox', 'radio'].includes(String(_attr(n, 'type') || '').toLowerCase()); }
    catch (e) { return false; }
  };
"""

# A visible stand-in resolved by THIS realm: the control's native label first, else the first
# aria-labelledby target looked up through the prototype in the element's own root. Requires the
# helpers of _NATIVE_LABEL_JS in scope, and an `arg` whose allowOwnLabel is false in a patchable realm.
_NATIVE_PROXY_JS = r"""
  const _nativeProxy = (el) => {
    if (arg.allowOwnLabel === false) return null;
    const vis = (n) => { try { const b = n.getBoundingClientRect(); return b.width > 0 && b.height > 0; } catch (e) { return false; } };
    for (const l of nativeLabelsOf(el)) if (vis(l)) return l;
    const lbId = _attr(el, 'aria-labelledby');
    if (!lbId) return null;
    let root = null;
    try { root = Node.prototype.getRootNode.call(el); } catch (e) { return null; }
    const named = root ? _firstById(root, String(lbId).trim().split(/\s+/)[0]) : null;
    return named && vis(named) ? named : null;
  };
"""


_REACH_PROBE_NEEDED_JS = (
    r"""(arg) => {
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
"""
    + _NATIVE_LABEL_JS
    + r"""
  const el = _q.find(arg.sel) || (arg.el && arg.el.isConnected ? arg.el : null);
  if (!el) return false;
  try { if (Node.prototype.getRootNode.call(el) !== document) return true; } catch (e) { /* fall through */ }
  // The cheap hit-test runs first and exits on an ordinary unoccluded hit; the DOM-wide label scan
  // only runs for the null/foreign-hit cases where it can actually change the answer.
  try {
    const rect = el.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      const hit = document.elementFromPoint(cx, cy);
      if (hit === el || (hit && el.contains(hit))) return false;
      // A sibling <label for=id> drawn OVER its control trips the driver's containment check the same
      // way a slotted label does; a null hit (off-screen target) also earns the probe when labels exist.
      // Skipped when own-label granting is off (no isolated world): the scan only ever earns a bypass.
      if (arg.allowOwnLabel !== false && (nativeLabelsOf(el).length || (_isLabel(el) && nativeControlOf(el)))) return true;
    }
  } catch (e) { /* best-effort */ }
  return false;
}"""
)

# Whether the element an action just failed on sits under display:none. Asked only after a failure,
# so it costs nothing on the normal path -- and asked of the LIVE page, because an element that was
# hidden while the driver waited may since have been revealed. The climb crosses shadow boundaries
# (`host`) and slots (`assignedSlot`), since a slotted child of a hidden host is hidden with it.
_INERT_TARGET_PROBE_JS = (
    r"""(arg) => {
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
  const el = _q.find(arg.sel) || (arg.el && arg.el.isConnected ? arg.el : null);
  if (!el) return false;
  try {
    // Two hops per shadow boundary (the fragment, then its host), so the cap is well clear of any
    // real component nesting; a climb that ran out returns false and leaves the original error alone.
    for (let n = el, hops = 0; n && hops < 512; hops++) {
      if (n.nodeType === 1 && getComputedStyle(n).display === 'none') return true;
      n = n.assignedSlot || n.parentNode || n.host || null;
    }
  } catch (e) { /* a claim the climb could not establish is not made */ }
  return false;
}"""
)

# A component that mirrors its own id onto the native control inside its shadow root makes a bare
# `#id` match the HOST first (document is the first root, and Playwright picks the first match too).
# observe names such a control by tag (`input[id="…"]`), but the model routinely drops the tag. When the
# selector's first match is a non-control host whose shadow tree holds exactly one form control that
# the same selector also matches, name that control the way observe would have -- the host is what a
# person sees, the control is what accepts the value.
_MIRRORED_HOST_CONTROL_JS = (
    r"""(sel) => {
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
  const CONTROL = 'INPUT,TEXTAREA,SELECT,BUTTON';
  const WIDGET_ROLE = /^(textbox|searchbox|combobox|listbox|button|checkbox|radio|switch|spinbutton|slider)$/i;
  const isControl = (e) => e.matches(CONTROL) || e.isContentEditable || (e.getAttribute('role') || '').trim().split(/\s+/).some((t) => WIDGET_ROLE.test(t));
  let first = null;
  try { first = _q.find(sel); } catch (e) { return null; }
  if (!first) return null;
  let root = null;
  try { root = first.shadowRoot; } catch (e) { return null; }
  // A host that only DECLARES a widget role still delegates to the control inside it.
  if (!root || root.nodeType !== 11 || first.matches(CONTROL) || first.isContentEditable) return null;
  const inside = (e) => { for (let n = e; n; n = n.parentNode || n.host || null) if (n === first) return true; return false; };
  const controls = _q.all(sel).filter((e) => e !== first && inside(e) && isControl(e));
  if (controls.length !== 1) return null;
  const c = controls[0];
  if (!c.id || String(c.id) !== String(first.id)) return null;
  // The same screen observe applies to an id it hands out: this string becomes the selector every
  // later message names, so a forgeable character or an unbounded length must not pass through.
  const FORGEABLE = /[\x00-\x1f\x7f\u0085\u2028\u2029\u200b-\u200f\u202a-\u202e\u2066-\u2069]/;
  const id = String(c.id);
  if (id.length > 200 || FORGEABLE.test(id)) return null;
  const tag = c.tagName.toLowerCase();
  if (!/^[a-z][a-z0-9-]*$/.test(tag)) return null;
  const named = tag + '[id="' + id.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"]';
  return _q.all(named).length === 1 && _q.find(named) === c ? named : null;
}"""
)


# Every other probe here asks whether a control is VISIBLE. This one asks whether it is REACHABLE,
# which is a different question and the only one that separates these two cases: Playwright reports a
# covered input as "visible, enabled, stable" and then fails the separate hit-target check, retrying
# until the timeout.
_TYPE_TARGET_PROBE_JS = (
    r"""(arg) => {
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
"""
    + _NATIVE_LABEL_JS
    + r"""
  // A host-anchored selector's two halves straddle a shadow boundary, so no single root can match it
  // and a per-root lookup finds nothing -- which would read as "no field here" and skip the check on
  // exactly the controls that addressing made reachable. The executor resolves it; take its element.
  const el = _q.find(arg.sel) || (arg.el && arg.el.isConnected ? arg.el : null);
  if (!el) return { exists: false };
  // A LABEL's own disabled/readOnly/type attributes mean nothing; its genuinely associated control's do.
  let ctl = el;
  try { if (_isLabel(el)) ctl = nativeControlOf(el) || el; } catch (e) { ctl = el; }
  let disabled = false;
  try { disabled = !!(ctl.disabled || (ctl.matches && ctl.matches(':disabled'))); } catch (e) { /* best-effort */ }
  let readOnly = false;
  try { readOnly = !!ctl.readOnly; } catch (e) { /* best-effort */ }
  const out = { exists: true, disabled, readOnly };
  try {
    if (_isToggle(ctl)) {
      out.toggle = true;
      out.toggleRadio = String(_attr(ctl, 'type') || '').toLowerCase() === 'radio';
    }
  } catch (e) { /* best-effort */ }
  let r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return out;
  // elementFromPoint answers about the VIEWPORT, so a field below the fold returns null and would
  // read as unoccluded -- which is most fields on a real form. Playwright scrolls before it clicks,
  // so scrolling here asks about the same layout the click is about to meet.
  const inView = r.top >= 0 && r.left >= 0 && r.bottom <= innerHeight && r.right <= innerWidth;
  if (!inView) {
    // 'instant' matters: scrollIntoView inherits CSS scroll-behavior, and a page with smooth
    // scrolling animates over hundreds of ms while the rect below is read synchronously -- the
    // element is still off-screen, elementFromPoint returns null, and the probe reports nothing.
    try { el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); } catch (e) { /* keep the rect */ }
    r = el.getBoundingClientRect();
  }
  // The walk must hop ShadowRoot -> host, because Node.contains stays in the light tree and would
  // read every component control as covered by its own host.
  // Composed-tree containment: a slotted node renders inside the component's shadow (its
  // assignedSlot), so a hit on a control's slotted label is a hit on the control, not a cover.
  const related = (a, b) => {
    for (let n = b, hops = 0; n && hops < 256; hops++, n = n.assignedSlot || n.parentNode || n.host || null) if (n === a) return true;
    return false;
  };
  const domRelated = (a, b) => {
    for (let n = b, hops = 0; n && hops < 256; hops++, n = n.parentNode || n.host || null) if (n === a) return true;
    return false;
  };
  // The re-centre gate must see a pin inherited across a shadow boundary; the `pinned` skin verdict
  // keeps its non-piercing walk (stops at a ShadowRoot).
  const isPinned = (node, pierce) => {
    if (!pierce) {
      for (let n = node; n && n.nodeType === 1; n = n.parentNode || n.host || null) {
        let pos = '';
        try { pos = getComputedStyle(n).position; } catch (e) { break; }
        if (pos === 'fixed' || pos === 'sticky') return true;
      }
      return false;
    }
    for (let n = node, hops = 0; n && hops < 256; hops++, n = n.parentNode || n.host || null) {
      if (n.nodeType !== 1) continue;
      let pos = '';
      try { pos = getComputedStyle(n).position; } catch (e) { break; }
      if (pos === 'fixed' || pos === 'sticky') return true;
    }
    return false;
  };
  // forPaint asks "would a person SEE this", not "could a person interact with it": a scrim with
  // pointer-events:none is still seen even though clicks pass through it, so the paint scan
  // (layerShowsPaint) passes forPaint=true to keep such a child in view. Every other caller omits it
  // and keeps the interaction-strict default.
  const visible = (n, forPaint) => {
    const r = n.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    // pointer-events and visibility are both inherited, but either can be explicitly overridden by
    // a descendant (a click-through overlay with a poking-through button; a hidden wrapper with one
    // child restored via visibility:visible) -- the candidate's own computed value already resolves
    // cascade + override in one read, so both are checked once here, not per-ancestor below.
    // display has no such override: display:none removes the whole subtree from the render tree,
    // so it stays an ancestor-walk check, same as opacity and overflow.
    let ownCs;
    try { ownCs = getComputedStyle(n); } catch (e) { return false; }
    if ((!forPaint && ownCs.pointerEvents === 'none') || ownCs.visibility === 'hidden') return false;
    let steps = 0;
    for (
      let a = n;
      a && a !== document.body && a !== document.documentElement && steps < 40;
      a = a.parentNode || a.host || null, steps++
    ) {
      // A ShadowRoot reached mid-walk (nodeType 11, not 1) carries no style of its own -- skip
      // straight to its host via the update expression's `.host` fallback rather than stopping
      // the walk there, or a hidden host (or anything above it) never gets checked.
      if (a.nodeType !== 1) continue;
      // inert makes a subtree non-focusable and non-actionable without changing any computed style
      // property -- the .inert IDL property reflects the attribute directly, no matching needed.
      if (a.inert) return false;
      let cs;
      try { cs = getComputedStyle(a); } catch (e) { return false; }
      if (cs.display === 'none') return false;
      if (parseFloat(cs.opacity) === 0) return false;
      // A carousel/wizard routinely keeps an inactive slide's markup in the DOM, translated out of
      // its own overflow:hidden container -- present, sized, but never painted. Only 'hidden' is
      // checked (not scroll/auto): those stay reachable via the ordinary auto-scroll a click does
      // on its own, so treating them as clipped would wrongly drop a control that only needs that.
      // The two axes are independent: setting overflow-x:hidden alone computes overflow-y to
      // 'auto' (the CSS interop rule for a hidden/visible pair), so a control merely scrolled out
      // vertically must not be treated as X-clipped just because the container clips X.
      if (a !== n) {
        const clipX = cs.overflowX === 'hidden';
        const clipY = cs.overflowY === 'hidden';
        if (clipX || clipY) {
          const ar = a.getBoundingClientRect();
          if (clipX && (r.right <= ar.left || r.left >= ar.right)) return false;
          if (clipY && (r.bottom <= ar.top || r.top >= ar.bottom)) return false;
        }
      }
    }
    return true;
  };
  // A native control's own <label> is a sibling, not an ancestor, so related()'s composed walk never
  // reaches it on its own -- check the control's genuine labels (and the reverse, a LABEL's genuine
  // control), resolved by nativeLabelsOf/nativeControlOf rather than trusting `.labels`/`.control`.
  // Returns the label (or reverse-case control) the hit sits under: the interactive-descendant walk
  // below stops at that boundary.
  const ownLabelBoundary = (candidate) => {
    try {
      for (const lbl of nativeLabelsOf(el)) if (related(lbl, candidate)) return lbl;
      if (_isLabel(el)) {
        const c = nativeControlOf(el);
        if (c && related(c, candidate)) return c;
      }
    } catch (e) { /* best-effort */ }
    return null;
  };
  // document.elementFromPoint stops at the outermost host, so a control inside a component reads as
  // covered by that host -- and a form-sized outer component is too big to pass as a skin. Descend
  // through each hit host's own root to the composed hit target, the element a real click lands on.
  // `hit` stays the light-DOM element for NAMING below: the model needs a handle it can act on, and
  // a host is that handle when the layer lives inside a component.
  const hitTestAt = (rect) => {
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    let t = null;
    try { t = document.elementFromPoint(cx, cy); } catch (e) { return { top: null, hit: null, blocked: false }; }
    const h = t;
    for (let hops = 0; t && hops < 32; hops++) {
      // A page can make shadowRoot a throwing getter; a throw here would escape the probe and read
      // as "not occluded", so it ends the descent instead.
      let root = null;
      try { root = t.shadowRoot; } catch (e) { break; }
      if (!root || root.nodeType !== 11) break;
      let inner = null;
      try { inner = root.elementFromPoint(cx, cy); } catch (e) { break; }
      if (!inner || inner === t) break;
      t = inner;
    }
    if (!t || t === el) return { top: t, hit: h, blocked: false };
    if (related(el, t)) {
      // Reachable only through slot assignment: the driver's DOM-containment hit-target check will
      // call this label an interceptor, so the caller dispatches without that check.
      return { top: t, hit: h, blocked: false, slotted: !domRelated(el, t) };
    }
    // An own-label hit is not decided here: a label styled as a backdrop is still a cover, so it goes
    // through the skin rules below like any other hit.
    return { top: t, hit: h, blocked: true };
  };
  let hitResult = hitTestAt(r);
  // A wrapper widget puts aria-expanded on a role=combobox ancestor, not the native control itself,
  // so this must check up the tree, not just the field's own attribute.
  let ownPopupOpen = false;
  try {
    // An open accordion wrapping the form is not the FIELD's popup: only the field itself, or an
    // ancestor that behaves like a popup trigger, counts.
    const OWN_POPUP_ROLE = /^(combobox|listbox|textbox|searchbox|button)$/i;
    // A wrapper widget may put aria-expanded on a shadow-hosting ancestor, so hop hosts too.
    for (let n = el, hops = 0; n && hops < 256; hops++, n = n.parentNode || n.host || null) {
      if (n.nodeType !== 1 || !n.getAttribute || n.getAttribute('aria-expanded') !== 'true') continue;
      if (n === el) { ownPopupOpen = true; break; }
      const role = (n.getAttribute('role') || '').trim();
      if (
        (role && OWN_POPUP_ROLE.test(role)) ||
        n.hasAttribute('aria-haspopup') ||
        n.hasAttribute('aria-controls') ||
        n.hasAttribute('aria-owns')
      ) {
        ownPopupOpen = true;
        break;
      }
    }
  } catch (e) { ownPopupOpen = false; }
  // A viewport-pinned cover (fixed bar, sticky header) clears only by moving the FIELD out from under
  // it -- centring, the same retry Playwright's click makes. A static cover is true at any scroll.
  // Not when the field's own popup is open (aria-expanded): a scroll would close what it just opened.
  if (hitResult.blocked && isPinned(hitResult.top, true) && !ownPopupOpen) {
    try { el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); } catch (e) { /* keep the rect */ }
    const r2 = el.getBoundingClientRect();
    // A box that collapsed on scroll leaves nothing to re-test; the earlier blocked verdict stands.
    if (r2.width === 0 || r2.height === 0) { out.occluded = true; return out; }
    r = r2;
    hitResult = hitTestAt(r2);
  }
  if (!hitResult.blocked) {
    if (hitResult.slotted) out.slotted = true;
    return out;
  }
  let top = hitResult.top;
  const hit = hitResult.hit;
  out.occluded = true;
  // Whether to force is a question about the OCCLUDER, not about the field. Structure alone is not
  // enough: when the field sits directly under <body>, or shares a container with a portal target,
  // EVERY overlay on the page is "inside its parent". So the occluder must also be the size of a
  // skin. A decoration drawn over one field stays within that field's box give or take its own
  // border; a dialog, cookie banner or backdrop is dramatically larger, and forcing past one would
  // type into something the user cannot see.
  // One property decides this: is the occluder part of the field's own control, or a surface layered
  // over the region the field sits in? There are three ways to be a layer, and every condition below
  // is one of them -- so a fourth would have to be a fourth way, not another special case.
  //   - it sits outside the field's own subtree (structure);
  //   - it is pinned to the viewport, where a control's decoration scrolls with its field;
  //   - it is the size of the viewport, where a decoration is the size of a control.
  // Ancestors are NOT exempt. "A dialog is never an ancestor of what it covers" was wrong: a wrapper
  // that disables its own contents while busy is exactly that, and so is a full-screen container
  // that wraps the form it blocks.
  const tr = top.getBoundingClientRect();
  const area = (b) => Math.max(1, b.width * b.height);
  const viewport = Math.max(1, innerWidth * innerHeight);
  // Pinning is inherited from whichever ancestor establishes it, so reading the hit element alone
  // misses the ordinary modal shape: a fixed backdrop wrapping a statically-positioned panel. And
  // sticky pins to the viewport too once it sticks -- a sticky header covering a field is not a
  // decoration of that field.
  const pinned = isPinned(top, false);
  // Measured against the VIEWPORT, not the field: 10x a small input is a small box, but 10x a large
  // textarea is bigger than the screen, so a field-relative cap stops meaning anything exactly when
  // the field is big. A decoration covers a control; a dialog or backdrop covers the view.
  const coversTheView = area(tr) > 0.6 * viewport;
  // "The field's own control" is its containing block, not its immediate parent: an overlay skin is
  // positioned against that block, and design systems routinely put an inner wrapper between the
  // input and it. Walking to the nearest positioned ancestor finds the same element the skin itself
  // was laid out against, so a skin one wrapper deeper still reads as part of the control.
  // Walk up from the OCCLUDER to the block it was positioned against, and ask whether the field is
  // inside that block. Asking from the field's side instead stops at the field's own wrapper, and a
  // design system that puts an inner wrapper around the input then hides its own skin from us.
  let block = null;
  for (let n = top.parentElement; n; n = n.parentElement) {
    let pos = '';
    try { pos = getComputedStyle(n).position; } catch (e) { break; }
    if (pos !== 'static') { block = n; break; }
  }
  // With no positioned ancestor the occluder is laid out against the page itself, so fall back to
  // the field's own parent rather than letting it inherit the document as its unit.
  const unit = block || el.parentElement;
  // Small is not the same as THIS field's. A table row, a card or a list item is small and holds
  // several independent controls, so a sibling's dropdown or a row-level "saving" overlay would
  // otherwise read as this field's decoration. A control the field shares with no other control is
  // the field's own; one that holds others is a layout region.
  let unitOwnsOnlyThisField = false;
  if (unit && area(unit.getBoundingClientRect()) <= 0.6 * viewport) {
    try {
      unitOwnsOnlyThisField = !Array.from(
        unit.querySelectorAll('input,select,textarea,button,a[href],[contenteditable],[role~="button" i]')
      ).some((c) => c !== el && !related(el, c));
    } catch (e) { unitOwnsOnlyThisField = false; }
  }
  // A thing that announces itself as an overlay is one. This is the least ambiguous signal here --
  // a decoration has no role, while a tooltip, dialog or toast says so in its markup.
  const LAYER_ROLE = /^(tooltip|dialog|alertdialog|alert|status|menu|listbox|log|marquee)$/i;
  const _roleTokens = (role) => String(role).trim().split(/\s+/);
  const isLayerNode = (n) => {
    const role = n.getAttribute && n.getAttribute('role');
    return !!((role && _roleTokens(role).some((t) => LAYER_ROLE.test(t))) || n.hasAttribute('aria-modal') || n.tagName === 'DIALOG');
  };
  let declaresItselfALayer = false;
  for (let n = top; n && n.nodeType === 1 && n !== unit; n = n.parentNode || n.host || null) {
    if (isLayerNode(n)) {
      declaresItselfALayer = true;
      break;
    }
  }
  // A hit inside the field's own <label> counts as its own subtree unless it landed on an interactive
  // descendant (a link, another control), whose activation would replace the field's.
  const ownLabelBoundaryNode = ownLabelBoundary(top);
  let ownLabelHit = false;
  // A label that paints nothing is the invisible-occluder shape the caller already refuses.
  let boundaryVisible = false;
  try { boundaryVisible = !!(ownLabelBoundaryNode && visible(ownLabelBoundaryNode)); } catch (e) { boundaryVisible = false; }
  // The property being guarded is the CONTROL's own renderability, not the label's: a visible label
  // over a visibility:hidden control is still an invisible-occluder shape, just wearing the label.
  let ownLabelCtl = el;
  try { if (_isLabel(el)) ownLabelCtl = nativeControlOf(el) || el; } catch (e) { ownLabelCtl = el; }
  let ctlRenderable = false;
  try { ctlRenderable = visible(ownLabelCtl, true); } catch (e) { ctlRenderable = false; }
  if (arg.allowOwnLabel !== false && boundaryVisible && ctlRenderable) {
    // `role` may carry a fallback list ("switch checkbox") in any ASCII case; any interactive token makes
    // it a control.
    const INTERACTIVE_HIT_SEL =
      'a[href], button, input, select, textarea, [role~="button" i], [role~="link" i], [role~="checkbox" i], ' +
      '[role~="radio" i], [contenteditable]:not([contenteditable="false" i]), details, summary, iframe, embed, object, ' +
      'area[href], img[usemap], ' +
      'video[controls], audio[controls], [role~="switch" i], [role~="menuitem" i], [role~="tab" i], ' +
      '[role~="option" i], [role~="combobox" i], [role~="textbox" i], [tabindex]:not([tabindex="-1"]), ' +
      '[onclick], [role~="slider" i], [role~="spinbutton" i], [role~="menuitemcheckbox" i], ' +
      '[role~="menuitemradio" i], [role~="treeitem" i], [role~="gridcell" i], [role~="searchbox" i], ' +
      '[role~="scrollbar" i], [draggable="true" i]';
    let interactiveDescendantHit = false;
    let layerOnTheWay = false;
    // A view-sized node anywhere in the hit chain up to the label is a backdrop wearing a label.
    let chainCoversTheView = false;
    // A pseudo-element hit-tests as its originating element, so a control-sized label can paint a
    // fixed full-viewport sheet with no view-sized node in the chain. Measured rather than parsed
    // from CSS: a node returned for most of the view outside its own box paints across the view.
    const paintsAcrossTheView = (n) => {
      let rect = null, root = null;
      try { rect = n.getBoundingClientRect(); root = n.getRootNode(); } catch (e) { return true; }
      if (!root || typeof root.elementsFromPoint !== 'function') root = document;
      const steps = [0.02, 0.26, 0.5, 0.74, 0.98];
      let outside = 0;
      for (const fx of steps) for (const fy of steps) {
        const x = innerWidth * fx, y = innerHeight * fy;
        if (x >= rect.left - 1 && x <= rect.right + 1 && y >= rect.top - 1 && y <= rect.bottom + 1) continue;
        let hits = [];
        try { hits = root.elementsFromPoint(x, y); } catch (e) { return true; }
        if (hits.indexOf(n) !== -1) outside++;
      }
      return outside >= 0.6 * steps.length * steps.length;
    };
    // The other way a pseudo-element can be a layer: pinned to the viewport. Its computed style is
    // the exact signal there, where the node's own position says nothing about its `::before`.
    const pseudoPinned = (n) => {
      for (const which of ['::before', '::after']) {
        let cs = null;
        try { cs = getComputedStyle(n, which); } catch (e) { return true; }
        if (!cs || cs.content === 'none' || cs.display === 'none') continue;
        if (cs.position === 'fixed' || cs.position === 'sticky') return true;
      }
      return false;
    };
    // The boundary itself is never its own interceptor: it IS the control in the LABEL-target case, and
    // a label may carry role=radio/checkbox itself.
    for (let n = top, hops = 0; n && hops < 256; hops++, n = n.assignedSlot || n.parentNode || n.host || null) {
      if (n.nodeType === 1 && (isLayerNode(n) || pseudoPinned(n))) { layerOnTheWay = true; break; }
      if (n.nodeType === 1) {
        let a = 0;
        try { a = area(n.getBoundingClientRect()); } catch (e) { a = 0; }
        if (a > 0.6 * viewport || paintsAcrossTheView(n)) { chainCoversTheView = true; break; }
      }
      if (n === ownLabelBoundaryNode) break;
      if (n !== el && n.nodeType === 1 && n.matches) {
        try {
          if (n.matches(INTERACTIVE_HIT_SEL)) { interactiveDescendantHit = true; break; }
        } catch (e) { /* best-effort */ }
      }
    }
    ownLabelHit = !interactiveDescendantHit && !chainCoversTheView && !layerOnTheWay;
  }
  // An own-label hit is folded in here via ownLabelHit and surfaces below as out.ownLabel.
  const inFieldsOwnSubtree =
    related(top, el) ||
    (unitOwnsOnlyThisField && related(unit, top) && related(unit, el)) ||
    ownLabelHit;
  // Pinning still disqualifies a foreign cover, but not a verified own label that shares its pinned
  // ancestor with the control itself (a toggle and its label inside the same fixed toolbar): the pin
  // is inherited by both, so re-centring can never separate them.
  const pinSharedWithControl =
    pinned &&
    ownLabelHit &&
    (() => {
      let node = null;
      for (let n = top, hops = 0; n && hops < 256; hops++, n = n.parentNode || n.host || null) {
        if (n.nodeType !== 1) continue;
        let pos = '';
        try { pos = getComputedStyle(n).position; } catch (e) { break; }
        if (pos === 'fixed' || pos === 'sticky') { node = n; break; }
      }
      return !!node && related(node, el);
    })();
  out.skinned = (!pinned || pinSharedWithControl) && !coversTheView && !declaresItselfALayer && inFieldsOwnSubtree;
  // This branch only runs when hitResult.blocked, which already set out.occluded above.
  out.ownLabel = !!(out.skinned && ownLabelHit);
  // An OPEN combobox's own popup is not a foreign occluder: the field aria-owns/controls the list it
  // just opened, so being "covered" by it means the widget is working, not blocked. Treat it like the
  // field's own skin -- force past it -- rather than refusing to type into the list the field opened.
  // Gated on aria-expanded="true" so this only fires for a combobox the page itself reports as OPEN,
  // never for a static field that merely happens to reference another element. Only the field's OWN
  // popup qualifies; a shared or unrelated layer never does.
  // Wrapped whole: a page can override getAttribute to throw (the same threat model the naming block
  // below guards against), and an escape here would fault page.evaluate and disable occlusion entirely.
  try {
    if (out.occluded && !out.skinned && el.getAttribute && el.getAttribute('aria-expanded') === 'true') {
      const popupIds = [];
      for (const a of ['aria-controls', 'aria-owns']) {
        const v = el.getAttribute && el.getAttribute(a);
        if (v) for (const id of v.split(/\s+/)) if (id) popupIds.push(id);
      }
      if (popupIds.length) {
        let ownRoot = null;
        try { ownRoot = Node.prototype.getRootNode.call(el); } catch (e) { ownRoot = null; }
        for (const id of popupIds.slice(0, 20)) {
          let pop = null;
          try { pop = ownRoot && ownRoot.getElementById ? ownRoot.getElementById(id) : document.getElementById(id); }
          catch (e) { pop = null; }
          // aria-controls/aria-owns express arbitrary relationships, so require the referenced element
          // to actually be a popup (listbox/menu/tree/grid/dialog -- the ARIA combobox-popup roles)
          // before forcing past it. Without this a field pointing at a plain region that happens to
          // hold a real occluder would type straight through it.
          const popRole = ((pop && pop.getAttribute && pop.getAttribute('role')) || '').toLowerCase();
          if (!/^(listbox|menu|tree|grid|dialog)$/.test(popRole)) continue;
          if (pop === top || related(pop, top)) {
            // The exemption forces past ONLY the layer-self-declaration, never the view-covering guard
            // the outer skin test applies: a full-screen dialog/listbox sheet, or a normal popup that
            // hosts a full-screen wall, hides what a person plainly sees, so it is a real occluder, not
            // the widget's working list. Refuse when EITHER the actually-hit occluder covers the view
            // (coversTheView, computed on `top` above -- catches a small popup hosting a fixed
            // full-screen child) OR the referenced popup itself does (catches a big sheet the hit
            // landed on a small option inside). A normal dropdown is a fraction of the viewport on
            // both counts and still qualifies. A thrown getBoundingClientRect reads as view-sized,
            // so a hostile page cannot forge its way back into the exemption.
            let popBig = true;
            try { popBig = area(pop.getBoundingClientRect()) > 0.6 * viewport; } catch (e) { popBig = true; }
            if (!coversTheView && !popBig) out.skinned = true;
            break;
          }
        }
      }
    }
  } catch (e) { /* best-effort: a thrown getAttribute must not disable occlusion detection */ }
  // The model needs a handle on the thing in the way, not just the fact that something is. Walk from
  // the hit element outward and stop at the FIRST ancestor that still reads as a layer -- pinned,
  // view-sized, or self-declared -- so a small dialog panel that happened to be hit directly is walked
  // past in favor of the backdrop wrapping it, but a real backdrop is never walked past in favor of a
  // still-more-outer app shell or scroll-lock wrapper that also happens to qualify (e.g. is itself
  // view-sized): the backdrop is closer to the hit, so it wins.
  // Named regardless of skinned: the typing path ignores the name when it forces past a skin, but the
  // CLICK path has no force fallback -- a click covered by the field's own open listbox times out, and
  // the model needs the occluder named (its options listed) rather than a bare 15s Page.click Timeout.
  if (out.occluded && hit && hit !== document.body && hit !== document.documentElement) {
   top = hit;
   // A throw anywhere below would otherwise escape page.evaluate() entirely and be read upstream
   // as "the probe failed" -- which _reachable_for_typing treats as reachable=True, skipping
   // occlusion detection altogether. Naming the occluder is best-effort; out.occluded/out.skinned
   // are already decided above and must survive regardless of what happens in here.
   try {
    // Same set observe() already rejects raw ids/testids on: a bidi override or zero-width
    // character in page-authored text can make the rendered guidance read as something different
    // from what the string actually is. Stripped, not rejected -- this is a label the model reads,
    // not an identifier trusted for its exact bytes, so the text minus the forgeable characters is
    // still useful.
    // Two copies, not one reused: a `g`-flagged regex is stateful across .test() calls (lastIndex
    // persists and silently skips matches on alternating calls), so .replace() and .test() each get
    // their own instance rather than sharing one that would behave correctly for only one of them.
    const FORGEABLE = /[\x00-\x1f\x7f\u0085\u2028\u2029\u200b-\u200f\u202a-\u202e\u2066-\u2069]/;
    const FORGEABLE_G = /[\x00-\x1f\x7f\u0085\u2028\u2029\u200b-\u200f\u202a-\u202e\u2066-\u2069]/g;
    const clean = (s) => (s || '').replace(FORGEABLE_G, '').replace(/\s+/g, ' ').trim();
    // A page-controlled string (innerText, an attribute value) is unbounded, so the regex in
    // clean() runs on a capped prefix first -- never on the raw string -- and the result is
    // capped again to the field's display length.
    const boundedClean = (s, cap) => clean(String(s == null ? '' : s).slice(0, 2000)).slice(0, cap);
    // The mint shape observe() uses for data-tv3. A value that does not match it is not a marker
    // we minted, so it must never be interpolated into a selector -- that would let page content
    // forge a selector (e.g. break out of the quoted attribute value) that the model then acts on.
    const MINTED_MARKER_RE = /^t\d+(-\d+)?$/;
    // A selector is only safe to recommend if it is the ONLY match across every root -- _q.all()
    // already pierces open shadow roots, so a control named by an id or marker scoped to its own
    // component (the usual shape) is still counted, unlike a plain document.querySelectorAll would.
    // A cloned subtree (a templated dialog re-rendered from a copy that already carried a live
    // marker) can leave two elements sharing one data-tv3 value just as easily as two elements
    // sharing one id -- the marker's regex shape says it looks minted, not that it is still unique.
    // count === 1 alone is not enough: CSS selector matching reads the real id ATTRIBUTE, not the
    // JS `.id` property, so a page that overrides the property's getter to report a decoy value
    // gets a selector that resolves to whatever element genuinely owns that attribute -- one match,
    // just not `n`. The sole match must be `n` itself, not merely unique.
    const uniqueSelector = (s, n) => {
      let matches = [];
      try { matches = _q.all(s); } catch (e) { matches = []; }
      return matches.length === 1 && matches[0] === n ? s : null;
    };
    // An id carrying a forgeable character (the same set stripped from name/label text above) would
    // still reach the model unstripped here: CSS.escape() preserves it, and this string is a
    // selector interpolated straight into the message, not display text run through clean(). A
    // very long id is capped for the same reason boundedClean caps text -- an uncapped
    // page-controlled string turns into an uncapped escape+query, and this runs on every diagnosis.
    const idSelector = (n) =>
      n.id && n.id.length <= 200 && !FORGEABLE.test(n.id) ? uniqueSelector('#' + CSS.escape(n.id), n) : null;
    const markerSelector = (n) => {
      const m = n.getAttribute && n.getAttribute('data-tv3');
      return m && MINTED_MARKER_RE.test(m) ? uniqueSelector('[data-tv3="' + m + '"]', n) : null;
    };
    // elementFromPoint retargets a hit inside a component to its host, so the layer is often a host
    // whose name and controls live in its OPEN shadow tree, not its (usually empty) light DOM.
    // Bounded so a pathological page (many nested open roots) cannot make this walk unbounded.
    // shadowRoot reads are guarded like every other one in this file: a sealed host (its getter
    // overridden to throw) must drop out of the walk, not crash the whole probe -- a probe that
    // throws is caught upstream and read as "reachable", which skips occlusion detection entirely.
    const deepAll = (node, sel, pred) => {
      const out2 = [];
      let visited = 0;
      const visit = (n, depth) => {
        if (!n || depth > 12 || visited > 5000) return;
        let sr = null;
        try { sr = n.shadowRoot; } catch (e) { sr = null; }
        if (sr) { visited++; visit(sr, depth + 1); }
        let matched = [];
        try { matched = n.querySelectorAll(sel); } catch (e) { matched = []; }
        // The 5000 budget is spent by the shadow-root walk below via `visited`, but a single
        // querySelectorAll on a pathological layer (thousands of matching elements in one root) can
        // otherwise still return an unbounded NodeList here -- cap what actually gets collected too.
        for (const m of matched) {
          if (out2.length >= 5000) return;
          if (!pred || pred(m)) out2.push(m);
        }
        let all = [];
        try { all = n.querySelectorAll('*'); } catch (e) { all = []; }
        for (const child of all) {
          if (++visited > 5000) return;
          let csr = null;
          try { csr = child.shadowRoot; } catch (e) { csr = null; }
          if (csr) visit(csr, depth + 1);
        }
      };
      visit(node, 0);
      return out2;
    };
    const ownName = (n) => {
      if (!n) return '';
      const al = n.getAttribute && n.getAttribute('aria-label');
      if (al) { const v = boundedClean(al, 80); if (v) return v; }
      const lb = n.getAttribute && n.getAttribute('aria-labelledby');
      if (lb) {
        // Root-scoped, not document.getElementById: an id inside an open shadow root is only
        // visible to a getElementById call on that root.
        let root = null;
        try { root = Node.prototype.getRootNode.call(n); } catch (e) { root = null; }
        // Capped before splitting, same as every other page-controlled string here: an uncapped
        // attribute value turns into an uncapped token list, each doing a root lookup, inside
        // page.evaluate() where nothing else bounds the work.
        const txt = lb
          .slice(0, 2000)
          .split(/\s+/)
          .slice(0, 20)
          .map((id) => { const t = root && root.getElementById ? root.getElementById(id) : null; return t ? boundedClean(t.textContent, 2000) : ''; })
          .filter(Boolean)
          .join(' ');
        if (txt) return txt.slice(0, 80);
      }
      return '';
    };
    // Visibility-filtered like the controls loop below: an invisible heading or dialog inside the
    // layer (a hidden template, a not-yet-shown step) is not what a person actually sees naming it.
    const headingNameOf = (n) => {
      const h = deepAll(n, 'h1,h2,h3,h4,h5,h6', visible)[0];
      return h ? boundedClean(h.textContent, 80) : '';
    };
    // Does an element draw a surface a person can see -- a non-transparent background, an image, a
    // border, or a shadow? The alpha-0 forms of a color (`transparent`, `rgba(...,0)`) paint nothing.
    // A color is invisible only when its ALPHA is zero -- parse the alpha channel, never a trailing
    // ",0)", which also matches an opaque color whose blue channel is 0 (rgb(0,0,0), rgb(255,0,0)).
    // A form we can't parse is treated as paint, so the failure mode is under-suppression, not over.
    const opaquePaint = (color) => {
      const c = (color || '').replace(/\s+/g, '');
      if (!c || c === 'transparent') return false;
      const m = c.match(/^rgba?\(([\d.,-]+)\)$/);
      if (!m) return true;
      const comps = m[1].split(',');
      const alpha = comps.length >= 4 ? parseFloat(comps[3]) : 1;
      return !(alpha === 0);
    };
    // A replaced/embedded element paints pixels with no CSS surface of its own -- an icon-only spinner
    // or logo (img/svg/canvas/video/iframe) is plainly visible even though backgroundColor/border are
    // empty, so it must count as paint or such a layer reads as an invisible ghost. The caller filters
    // by visible(), so a zero-sized or hidden replaced element never reaches here.
    const REPLACED_PAINT = /^(img|svg|image|canvas|video|picture|object|embed|iframe)$/;
    const paintsSurface = (n) => {
      if (REPLACED_PAINT.test((n.tagName || '').toLowerCase())) return true;
      let s;
      try { s = getComputedStyle(n); } catch (e) { return false; }
      if (opaquePaint(s.backgroundColor)) return true;
      if (s.backgroundImage && s.backgroundImage !== 'none') return true;
      if (s.boxShadow && s.boxShadow !== 'none') return true;
      // A backdrop-filter (a frosted/blur wall) paints a plainly visible effect with no CSS surface of
      // its own -- no background, border, or shadow -- so without this such a wall reads as an
      // invisible ghost and the model is wrongly told to press Escape at a layer it can see.
      const bdf = s.backdropFilter || s.webkitBackdropFilter;
      if (bdf && bdf !== 'none') return true;
      const bw = (v) => parseFloat(v || '0') || 0;
      if (
        s.borderStyle !== 'none' &&
        bw(s.borderTopWidth) + bw(s.borderBottomWidth) + bw(s.borderLeftWidth) + bw(s.borderRightWidth) > 0
      ) return true;
      return false;
    };
    const hasDirectText = (n) => {
      for (const c of n.childNodes) if (c.nodeType === 3 && c.nodeValue && c.nodeValue.trim()) return true;
      return false;
    };
    // Whether the LAYER shows a person any paint of its own -- a surface, or a visible descendant that
    // paints a surface or renders text. opacity:0 anywhere in its chain zeroes all of it. The covered
    // field's OWN paint (it sits inside the layer in the ancestor case) is never the layer's, so it is
    // excluded. Bounded so a pathological layer cannot make the scan unbounded; the caller runs it only
    // for a control-less layer, keeping it off the hot path for ordinary dialogs.
    const layerShowsPaint = (root2) => {
      for (
        let n = root2;
        n && n.nodeType === 1 && n !== document.body && n !== document.documentElement;
        n = n.parentNode || n.host || null
      ) {
        let s;
        try { s = getComputedStyle(n); } catch (e) { break; }
        if (parseFloat(s.opacity) === 0) return false;
      }
      if (root2 !== el && !related(el, root2) && visible(root2, true) && (paintsSurface(root2) || hasDirectText(root2))) {
        return true;
      }
      // deepAll (not querySelectorAll) so the scan pierces open shadow roots -- a consent widget that
      // renders its visible surface/text entirely inside its own shadow tree must count as paint, the
      // same shadow-aware treatment the control and heading lookups already use. Bounded by deepAll.
      let nodes = [];
      try { nodes = deepAll(root2, '*', (n) => visible(n, true)); } catch (e) { nodes = []; }
      for (const n of nodes) {
        if (n === el || related(el, n)) continue;
        if (paintsSurface(n) || hasDirectText(n)) return true;
      }
      return false;
    };
    // Pinning (fixed/sticky) is a strong enough signal on its own -- a small cookie banner docked
    // to the viewport edge is exactly as real an occluder as a full-screen one. Being merely
    // ABSOLUTE and view-sized is weaker evidence (an ordinary in-flow-adjacent block can be
    // absolutely positioned for layout reasons having nothing to do with occlusion), so that path
    // still requires bigness. Either way, a wrongly-oversized OUTER ancestor (a scroll-lock shell
    // wrapping the real banner/backdrop) can never win: the walk below stops at the first qualifying
    // ancestor, and the real occluder is always closer to the hit point than any shell wrapping it.
    // The document root is layout, never content, and must never stand in as the thing blocking a click.
    const isLayer = (n, isHit) => {
      if (n === document.body || n === document.documentElement) return false;
      let pos = '';
      try { pos = getComputedStyle(n).position; } catch (e) { pos = ''; }
      if (pos === 'fixed' || pos === 'sticky') return true;
      const role = n.getAttribute && n.getAttribute('role');
      if ((role && _roleTokens(role).some((t) => LAYER_ROLE.test(t))) || (n.hasAttribute && n.hasAttribute('aria-modal')) || n.tagName === 'DIALOG') {
        return true;
      }
      // Bigness alone is only trustworthy for an element that is NOT an ancestor of the field --
      // a clipped (not covered) field's hit-point routinely lands on the static layout/clipping
      // container that wraps it, and that container is exactly as big as a genuine backdrop. A
      // real full-screen blocking wrapper is always pinned or role-bearing (both already handled
      // above), so excluding an unpinned ancestor here costs nothing real.
      if (related(n, el)) return false;
      const big = area(n.getBoundingClientRect()) > 0.6 * viewport;
      return isHit ? big : pos === 'absolute' && big;
    };
    let layer = null;
    for (let n = top; n && n.nodeType === 1 && n !== document.body; n = n.parentNode || n.host || null) {
      if (isLayer(n, n === top)) { layer = n; break; }
    }
    if (!layer) {
      // Nothing in the walk qualified, and top is merely an ancestor/clipping container of the
      // field -- there is no honest occluder to name (the field is clipped, not covered). Bail
      // with out.occluder left unset so the caller falls back to its generic message instead of
      // naming a layout wrapper and listing every unrelated button on it.
      if (related(top, el)) {
        // One exception: a view-sized ancestor that paints NOTHING, over a field that is itself
        // un-clipped and visible, is not a clip -- it is a ghost cover (a leftover full-page consent
        // shield that still intercepts the pointer). Report it as invisible so the model is not told
        // to dismiss an overlay it cannot see. A truly clipped field fails visible(el), and a real
        // layout shell paints (its nav/content), so neither is caught here.
        if (visible(el) && coversTheView && !layerShowsPaint(top)) out.occluder = { invisible: true };
        return out;
      }
      layer = top;
    }
    // Own name, then whichever names the DIALOG this layer wraps (deepAll pierces into the layer's
    // shadow tree, since a component-hosted consent widget renders entirely inside one), then a
    // heading anywhere in the layer, then its own text, then its tag -- in that order.
    let layerName = ownName(layer);
    if (!layerName) {
      const dialog = deepAll(layer, '[role="dialog"],[role="alertdialog"],[aria-modal]', visible)[0];
      if (dialog) layerName = ownName(dialog) || headingNameOf(dialog);
    }
    if (!layerName) layerName = headingNameOf(layer);
    if (!layerName) layerName = boundedClean(layer.textContent, 60);
    if (!layerName) layerName = layer.tagName ? layer.tagName.toLowerCase() : 'layer';
    const layerSelector = idSelector(layer) || markerSelector(layer);
    const allControls = [];
    // observe() never mints data-tv3 inside a component, so a marker-shaped selector below can only
    // ever come from the light DOM -- a shadow-piercing find here does not risk minting a fresh one.
    // The role list mirrors observe()'s own _WIDGET_ROLES answer to "is this a control?" (minus the
    // form-field roles observe treats as fillable, not actionable), so a consent switch or a
    // role=menuitem Close action is not omitted just because it isn't a <button>.
    const found = deepAll(
      layer,
      'button,a[href],input[type="button"],input[type="submit"],input[type="image"],'
      + 'input[type="reset"],[role="button"],'
      + '[role="checkbox"],[role="radio"],[role="combobox"],[role="option"],[role="menuitem"],'
      + '[role="menuitemcheckbox"],[role="menuitemradio"],[role="listbox"],[role="switch"],'
      + '[role="spinbutton"],[role="tab"]'
    );
    // A disabled control cannot be the thing to click -- recommending one wastes a click timeout on
    // a target Playwright will refuse, and can crowd the real dismisser out of the eight-slot cap.
    // :disabled (not the .disabled IDL property) is what the browser actually uses to decide this,
    // so it is also true for a button whose OWN disabled attribute is unset but sits inside a
    // <fieldset disabled> -- the property alone would miss exactly that inherited case.
    const isDisabled = (n) => {
      let matched = false;
      try { matched = !!(n.matches && n.matches(':disabled')); } catch (e) { matched = false; }
      return matched || (n.getAttribute && n.getAttribute('aria-disabled') === 'true');
    };
    for (const c of found) {
      if (c === el || !visible(c) || isDisabled(c)) continue;
      const csel = idSelector(c) || markerSelector(c);
      // ownName covers aria-label and root-scoped aria-labelledby, same priority order and same
      // shadow-aware resolution the layer's own name uses.
      const label = boundedClean(ownName(c) || c.textContent || c.value || (c.getAttribute && c.getAttribute('title')) || '', 60);
      if (!label && !csel) continue;
      allControls.push({ selector: csel, label });
    }
    // A real dismisser (Accept, Confirm, Close) routinely comes AFTER a list of category rows or
    // toggles in document order -- a Privacy Preference Center's footer buttons follow its list of
    // per-vendor switches. Capping at the first eight would drop exactly the control the model
    // needs and keep only the toggles it was already flailing between, reproducing the ticket's own
    // motivating bug with more words. Keep both ends: the first few for context, the last few
    // because that is where a footer actually lives.
    const truncated = allControls.length > 8;
    const controls = truncated ? allControls.slice(0, 5).concat(allControls.slice(-3)) : allControls;
    out.occluder = { selector: layerSelector, name: layerName, controls, truncated };
    // Whether a PERSON would see this layer at all. A leftover consent backdrop still intercepts the
    // pointer (elementFromPoint returned it) but can paint nothing -- fully transparent, no visible
    // control, heading or text -- so the field looks clear on screen and "dismiss the overlay you
    // see" is a false instruction. Gated on there being no visible control (a real dialog has some),
    // so the bounded paint scan runs only for the ambiguous, control-less layer.
    if (!controls.length && !layerShowsPaint(layer)) out.occluder.invisible = true;
   } catch (e) { /* best-effort */ }
  }
  return out;
}"""
)

_ACTIVE_IS_JS = (
    r"""(arg) => {
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
  const el = _q.find(arg.sel) || (arg.el && arg.el.isConnected ? arg.el : null);
  if (!el) return null;
  // A control inside a component reports its host as document.activeElement, so ask the root that
  // actually holds the control rather than the document.
  let root = null;
  try { root = Node.prototype.getRootNode.call(el); } catch (e) { root = null; }
  const active = root && root.activeElement ? root.activeElement : document.activeElement;
  return active === el;
}"""
)


class _FieldCovered(Exception):
    """The field exists and is rendered, but something unrelated is on top of it."""

    def __init__(self, selector: str, occluder: dict[str, Any] | None = None) -> None:
        super().__init__(selector)
        self.selector = selector
        self.occluder = occluder


class _FieldNotEditable(Exception):
    """The field cannot accept typed text at all -- it is disabled, or readonly."""

    def __init__(self, selector: str, read_only: bool) -> None:
        super().__init__(selector)
        self.selector = selector
        self.read_only = read_only


# Design-system forms render a <select> at zero size behind a styled listbox proxy. Playwright's
# actionability wait never resolves against it, so select_option probes visibility first and only
# forces past actionability when the element exists but is genuinely hidden this way.
_SELECT_VISIBILITY_JS = (
    r"""(arg) => {
  const sel = arg.sel;
  // A node the page replaced between the executor's lookup and this evaluate is not evidence about
  // the live page: reading a detached one reports a stale value as a current verdict.
  const _executorEl = arg.el && arg.el.isConnected ? arg.el : null;
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
"""
    + _NATIVE_LABEL_JS
    + _NATIVE_PROXY_JS
    + r"""
  try {
    const el = _q.find(sel) || _executorEl;
    if (!el) return { exists: false, visible: false };
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    // Forcing a value onto a select nothing stands in for carries a value the user never saw into
    // whatever the run submits next -- so the stand-in is resolved by this realm, never by the page's.
    return {
      exists: true,
      nodeName: (el.nodeName || '').toLowerCase(),
      visible: r.width > 0 && r.height > 0 && cs.visibility !== 'hidden',
      disabled: !!el.disabled,
      proxied: !!_nativeProxy(el),
    };
  } catch (e) { return { exists: false, visible: false }; }
}"""
)

# Whether a selector's own element is a typeable field (an input/textarea/contenteditable that can
# accept keystrokes) rather than a click-to-open anchor (a button/div that only opens a list). The
# shared custom-combobox commit path types into typeable anchors and refuses non-typeable ones, so a
# page.fill throw ("Element is not an <input>") never replaces the <select> throw this fix removes.
_ANCHOR_TYPEABLE_JS = (
    r"""(arg) => {
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
  const _executorEl = arg.el && arg.el.isConnected ? arg.el : null;
  try {
    const el = _q.find(arg.sel) || _executorEl;
    if (!el) return false;
    const tag = el.tagName;
    if (tag === 'TEXTAREA') return !el.disabled && !el.readOnly;
    if (tag === 'INPUT') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      const NONTEXT = new Set(['checkbox','radio','button','submit','reset','file','image','range','color','hidden']);
      return !NONTEXT.has(t) && !el.disabled && !el.readOnly;
    }
    return !!el.isContentEditable;
  } catch (e) { return false; }
}"""
)

# True when a non-typeable anchor (button/div) declares combobox/listbox semantics — the ARIA contract
# for a click-to-open single-select, as opposed to a plain button with no list behind it.
_ANCHOR_LIST_SEMANTICS_JS = (
    r"""(arg) => {
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
  const _executorEl = arg.el && arg.el.isConnected ? arg.el : null;
  try {
    const el = _q.find(arg.sel) || _executorEl;
    if (!el) return false;
    // A wrapper holding a real input is typed INTO, not clicked open — never route it to the picker.
    if (el.querySelector('input:not([type=hidden]),textarea,[contenteditable=""],[contenteditable=true]')) return false;
    const hp = (el.getAttribute('aria-haspopup') || '').toLowerCase();
    return /(^|\s)combobox(\s|$)/i.test(el.getAttribute('role') || '') || hp === 'listbox';
  } catch (e) { return false; }
}"""
)

# Read back after a forced select_option so a styled proxy that silently didn't sync from its
# native control is caught rather than reported as a successful selection.
_SELECT_READBACK_JS = (
    r"""(arg) => {
  const sel = arg.sel;
  // A node the page replaced between the executor's lookup and this evaluate is not evidence about
  // the live page: reading a detached one reports a stale value as a current verdict.
  const _executorEl = arg.el && arg.el.isConnected ? arg.el : null;
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
  try {
    const el = _q.find(sel) || _executorEl;
    if (!el) return null;
    const idx = el.selectedIndex;
    const opt = idx >= 0 ? el.options[idx] : null;
    // Playwright matches label= against option.label (whitespace-collapsed), not raw text.
    return { value: el.value, selectedIndex: idx, selectedLabel: opt ? opt.label : null };
  } catch (e) { return null; }
}"""
)

# The native radio/checkbox a click on `el` actually toggles: itself, its <label>'s control, or the
# sole native input a thin host wraps. An ARIA-only or ARIA-toggle-role host resolves to null on
# purpose -- aria-checked is app-set on its own schedule, not a readback bearer.
_TOGGLE_OWNER_JS = r"""(el) => {
  if (_isToggle(el)) return el;
  if (_isLabel(el)) {
    const ctl = nativeControlOf(el);
    if (!_isToggle(ctl)) return null;
    const wrapsOther = _qsa(el, 'button,a[href],input,select,textarea').some((c) => c !== ctl);
    return wrapsOther ? null : ctl;
  }
  // A control that is itself interactive (a trigger, a menu row, a link, an ARIA toggle) may wrap
  // a toggle glyph without the click meaning "toggle the native input".
  const role = String(_attr(el, 'role') || '').toLowerCase();
  if (['BUTTON', 'A', 'SELECT', 'TEXTAREA', 'SUMMARY'].includes(_tag(el))) return null;
  if (['button', 'checkbox', 'combobox', 'link', 'listbox', 'menu', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'option', 'radio', 'switch', 'tab', 'treeitem'].includes(role)) return null;
  let found = _qsa(el, 'input[type=radio],input[type=checkbox]');
  try { if (el.shadowRoot) found = found.concat(_qsa(el.shadowRoot, 'input[type=radio],input[type=checkbox]')); } catch (e) {}
  if (found.length !== 1) return null;
  // A host is a thin wrapper, not a region: an input buried deep in a generic container, or one
  // sharing it with another interactive control, is not what this click owns.
  const owner = found[0];
  let depth = 0;
  for (let p = owner; p && p !== el && p !== el.shadowRoot && depth <= 3; p = p.parentNode) depth++;
  if (depth > 3) return null;
  let others = _qsa(el, 'button,a[href],select,textarea,input:not([type=hidden])');
  try { if (el.shadowRoot) others = others.concat(_qsa(el.shadowRoot, 'button,a[href],select,textarea,input:not([type=hidden])')); } catch (e) {}
  return others.some((c) => c !== owner) ? null : owner;
}"""

# A skinned checkbox/radio is a zero-size or invisible native input whose visible <label> is the
# real click target; the label is tagged (stale tags cleared first) so click can act on it.
_SKINNED_CHECKBOX_PROBE_JS = (
    r"""(arg) => {
  const sel = arg.sel;
  // A node the page replaced between the executor's lookup and this evaluate is not evidence about
  // the live page: reading a detached one reports a stale value as a current verdict.
  const _executorEl = arg.el && arg.el.isConnected ? arg.el : null;
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
"""
    + _NATIVE_LABEL_JS
    + _NATIVE_PROXY_JS
    + r"""
  const _toggleOwner = """
    + _TOGGLE_OWNER_JS
    + r""";
  const toggleFields = (owner) =>
    owner ? { toggle: true, radio: String(_attr(owner, 'type') || '').toLowerCase() === 'radio', toggleDisabled: !!owner.disabled } : {};
  try {
    const el = _q.find(sel) || _executorEl;
    if (!el) return { exists: false, skinned: false, labelClick: null };
    const type = String(el.type || '').toLowerCase();
    if (el.tagName === 'INPUT' && type === 'file') return { exists: true, skinned: false, labelClick: null, file: true };
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const invisible = r.width === 0 || r.height === 0 || cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') < 0.05;
    if (el.tagName === 'SELECT') {
      return { exists: true, skinned: false, labelClick: null, select: true, invisible, proxied: !!_nativeProxy(el) };
    }
    if (el.tagName !== 'INPUT' || (type !== 'checkbox' && type !== 'radio')) {
      return { exists: true, skinned: false, labelClick: null, ...toggleFields(_toggleOwner(el)) };
    }
    if (!invisible) return { exists: true, skinned: false, labelClick: null, ...toggleFields(_toggleOwner(el)) };
    const radio = type === 'radio';
    if (!_nativeProxy(el)) return { exists: true, skinned: false, labelClick: null, radio, unproxied: true };
    const disabled = !!el.disabled;
    const none = { exists: true, skinned: true, labelClick: null, radio, disabled };
    // Only a real <label> activates its control on click, and only the association THIS realm derives
    // counts: an `el.labels` the page shadows names a decoy, not a proxy. A realm the page can patch
    // (the main-world fallback) never offers one.
    if (arg.allowOwnLabel === false) return none;
    // A label that also wraps another control (a button, link, or a second input) is not a safe
    // proxy: a real click on it can activate that control instead.
    const wrapsOther = (l) => Array.from(_qsa(l, 'button,a[href],input,select,textarea')).some((c) => c !== el);
    const label = nativeLabelsOf(el).find((l) => {
      const b = l.getBoundingClientRect();
      return b.width > 0 && b.height > 0 && !wrapsOther(l);
    }) || null;
    if (!label || nativeControlOf(label) !== el) return none;
    // The click lands on coordinates this realm measured, never through a marker the page could move
    // onto something else, so the point has to be the label's own: a cover there is a cover.
    let b = label.getBoundingClientRect();
    if (b.bottom < 0 || b.right < 0 || b.top > innerHeight || b.left > innerWidth) {
      try { label.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); } catch (e) { /* keep */ }
      b = label.getBoundingClientRect();
    }
    const x = b.left + b.width / 2, y = b.top + b.height / 2;
    let hit = null;
    try { hit = document.elementFromPoint(x, y); } catch (e) { hit = null; }
    for (let hops = 0; hit && hops < 32; hops++) {
      let root = null;
      try { root = hit.shadowRoot; } catch (e) { break; }
      if (!root || root.nodeType !== 11) break;
      let inner = null;
      try { inner = root.elementFromPoint(x, y); } catch (e) { break; }
      if (!inner || inner === hit) break;
      hit = inner;
    }
    let onLabel = false;
    for (let n = hit, hops = 0; n && hops < 256; hops++, n = n.assignedSlot || n.parentNode || n.host || null) {
      if (n === label || n === el) { onLabel = true; break; }
    }
    if (!onLabel) return { ...none, labelCovered: true };
    return { exists: true, skinned: true, labelClick: { x, y }, radio, disabled };
  } catch (e) { return { exists: false, skinned: false, labelClick: null }; }
}"""
)

# Read twice (before and after the forced click): a proxy that does not sync from its native input
# must fail loud, not read as a successful toggle.
_CHECKBOX_CHECKED_JS = (
    r"""(arg) => {
  const sel = arg.sel;
  // A node the page replaced between the executor's lookup and this evaluate is not evidence about
  // the live page: reading a detached one reports a stale value as a current verdict.
  const _executorEl = arg.el && arg.el.isConnected ? arg.el : null;
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
"""
    + _NATIVE_LABEL_JS
    + r"""
  const _toggleOwner = """
    + _TOGGLE_OWNER_JS
    + r""";
  try {
    const el = _q.find(sel) || _executorEl;
    if (!el) return null;
    // A LABEL target reports the state of the control it genuinely toggles, not its own (undefined) .checked.
    // An unresolvable owner (0 or 2+ bearers under a host) is "unreadable", never a fabricated false.
    const ctl = _toggleOwner(el) || (_isLabel(el) ? (nativeControlOf(el) || el) : (_isToggle(el) ? el : null));
    return ctl ? !!ctl.checked : null;
  } catch (e) { return null; }
}"""
)

# Pre-click state for the dropdown-commit path: whether a click-opened menu (rows tagged
# data-tv3-menu by _FIND_MENU_JS) is currently open, whether the click target IS one of its rows, and
# that row's state fingerprint — aria checked/selected/pressed, class, child count, text — so an option
# click on a multi-select menu (which commits WITHOUT closing) can be verified by its state change.
# Also takes the visible-DOM pre-snapshot (data-tv3-pre) so a menu the click opens reads as a reaction.
_CLICK_PRECHECK_JS = (
    r"""(arg) => {
  const clicked = arg.sel;"""
    + _PIERCED_QUERY_JS
    + r"""
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    // A menu mid-close (opacity fade, pointer-events cut) still has a nonzero rect; reading it as
    // "open" would turn a healthy committed selection into a false "did not commit" error.
    try {
      const s = getComputedStyle(el);
      if (s.visibility === 'hidden' || Number(s.opacity) < 0.05 || s.pointerEvents === 'none') return false;
    } catch (e) {}
    return true;
  };
  const state = (el) => {
    // .checked is a DOM property, not an attribute: a native-checkbox multi-select commits by
    // flipping only it, with no aria/class/text change. Same for inline-style-only toggles.
    let kids = '';
    try { for (const i of el.querySelectorAll('input')) kids += i.checked ? '1' : '0'; } catch (e) {}
    return [
      el.getAttribute('aria-checked'), el.getAttribute('aria-selected'), el.getAttribute('aria-pressed'),
      el.className, el.children.length, (el.innerText || '').trim(),
      el.getAttribute('style'), kids,
    ].join('|');
  };
  // Fixed-arity on purpose: every component is one attribute of the row itself, so a row that
  // restructures cannot change this string, and only being picked can.
  const selState = (el) => {
    return [
      el.getAttribute('aria-checked'), el.getAttribute('aria-selected'), el.getAttribute('aria-pressed'),
    ].join('|');
  };
  const openRows = [];
  for (const el of pQSA('[data-tv3-menu]')) if (vis(el)) openRows.push(el);
  let target = null;
  try { target = pQS(clicked) || (arg.el && arg.el.isConnected ? arg.el : null); } catch (e) { target = (arg.el && arg.el.isConnected ? arg.el : null); }
  let isOption = false;
  let containsMenu = false;
  let optText = '';
  let optState = '';
  let optSel = '';
  let optKids = -1;
  let optH = -1;
  let optVis = -1;
"""
    + _VIS_ROWS_JS
    + r"""
  if (target && openRows.length) {
    for (const el of openRows) {
      // The target being the row or inside it is an option pick. The target merely CONTAINING rows
      // (the card around the menu) is not — and since a center-point click on the card can land on
      // an arbitrary row, that case is flagged so the handler makes no claims about it at all.
      if (el === target || pContains(el, target)) {
        isOption = true;
        optText = (el.innerText || '').trim().slice(0, 80);
        optState = state(el);
        optSel = selState(el);
        optKids = el.children.length;
        optH = Math.round(el.getBoundingClientRect().height);
        optVis = _visRows(el);
        break;
      }
      if (pContains(target, el)) containsMenu = true;
    }
  }
  preReset();
  pScopeEach((el, inShadow) => { if (vis(el)) preMark(el, inShadow); });
  return { menuOpen: openRows.length > 0, isOption, containsMenu, optText, optState, optSel, optKids, optH, optVis };
}"""
)

# Same-document token for the click retry: a navigation destroys window, a pushState does not.
_CLICK_SAME_DOC_PLANT_JS = "() => { window.__tv3_click_same = 1; }"
_CLICK_SAME_DOC_CHECK_JS = "() => window.__tv3_click_same === 1"

# Planted on window before an option click; a navigation clears window, so its absence afterwards is
# the page saying "different document" even when the post-click probe's own JS is what failed.
_CLICK_DOC_PLANT_JS = "() => { window.__tv3_click_doc = 1; }"
_CLICK_DOC_CHECK_JS = "() => window.__tv3_click_doc === 1"

# Post-click menu state: how many previously-tagged menu rows are still visible (a closed menu — nodes
# destroyed or hidden — reads 0), plus the clicked row's current state fingerprint for the multi-select
# commit check. Field names are distinct from _CLICK_PRECHECK_JS's on purpose (tests dispatch on them).
_MENU_AFTER_JS = (
    r"""(arg) => {
  const clicked = arg.sel;"""
    + _PIERCED_QUERY_JS
    + r"""
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    try {
      const s = getComputedStyle(el);
      if (s.visibility === 'hidden' || Number(s.opacity) < 0.05 || s.pointerEvents === 'none') return false;
    } catch (e) {}
    return true;
  };
  const state = (el) => {
    // .checked is a DOM property, not an attribute: a native-checkbox multi-select commits by
    // flipping only it, with no aria/class/text change. Same for inline-style-only toggles.
    let kids = '';
    try { for (const i of el.querySelectorAll('input')) kids += i.checked ? '1' : '0'; } catch (e) {}
    return [
      el.getAttribute('aria-checked'), el.getAttribute('aria-selected'), el.getAttribute('aria-pressed'),
      el.className, el.children.length, (el.innerText || '').trim(),
      el.getAttribute('style'), kids,
    ].join('|');
  };
  // Fixed-arity on purpose: every component is one attribute of the row itself, so a row that
  // restructures cannot change this string, and only being picked can.
  const selState = (el) => {
    return [
      el.getAttribute('aria-checked'), el.getAttribute('aria-selected'), el.getAttribute('aria-pressed'),
    ].join('|');
  };
"""
    + _VIS_ROWS_JS
    + r"""
  let stillOpen = 0;
  const rows = [];
  for (const el of pQSA('[data-tv3-menu]')) if (vis(el)) { stillOpen++; rows.push(el); }
  let target = null;
  try { target = pQS(clicked) || (arg.el && arg.el.isConnected ? arg.el : null); } catch (e) { target = (arg.el && arg.el.isConnected ? arg.el : null); }
  let optState = '';
  let optSel = '';
  let optKids = -1;
  let optH = -1;
  let optVis = -1;
  if (target) {
    for (const el of rows) {
      if (el === target || pContains(el, target)) {
        optState = state(el);
        optSel = selState(el);
        optKids = el.children.length;
        optH = Math.round(el.getBoundingClientRect().height);
        optVis = _visRows(el);
        break;
      }
    }
  }
  return { stillOpen, optState, optSel, optKids, optH, optVis };
}"""
)

# Behavioral, site-agnostic menu finder: after a click (with the pre-snapshot taken first),
# look for the option list the page rendered IN REACTION — a NEW container (not data-tv3-pre: a
# pre-existing visible container whose rows merely changed, e.g. pagination refreshing a results list,
# is never a menu) holding >=2 new, visible, row-sized, mostly-clickable leaf rows, positioned adjacent
# to the clicked element. Keys off reaction + geometry — no CSS-class/ARIA/site vocabulary — mirroring
# _FIND_SUGGESTION_JS. Unlike the typeahead finder this only REPORTS (the model does the clicking), so
# navigational rows are listed too. Tags rows data-tv3-menu="1..N" (top-to-bottom) — in-DOM tags that
# stay valid until the menu re-renders, so the model can pick an option without a re-observe re-minting
# ids (the staging trace's staleness trap). Existing tags are cleared only when a new menu is tagged.
_FIND_MENU_JS = (
    r"""(arg) => {
  const clicked = arg.sel;"""
    + _PIERCED_QUERY_JS
    + _ROW_SEMANTICS_JS
    + r"""
  const MENU_ROW_ROLES = """
    + _MENU_ROW_ROLES_JS
    + r""";
  const vis = (r) => r.width > 0 && r.height > 0;
  // `cascade`: the caller just clicked a row that DETACHED (a category replacing the list with its
  // children). The trigger is gone, so trigger-anchored geometry/ARIA is waived — new rows in a
  // FLOATING container carry the claim instead (enforced below).
  const cascade = !!arg.cascade;
  let trigger = null;
  try { trigger = pQS(clicked) || (arg.el && arg.el.isConnected ? arg.el : null); } catch (e) { return null; }
  if (!trigger && !cascade) return null;
  // Clear the previous scan's tags once the trigger (which may itself be a tagged row) is resolved:
  // an early null below (menu closed since) must not leave stale data-tv3-menu / scroller marks for
  // _MENU_OPTION_TEXTS_JS to read as a live window.
  pQSA('[data-tv3-menu]').forEach((e) => e.removeAttribute('data-tv3-menu'));
  pQSA('[data-tv3-menu-scroller]').forEach((e) => e.removeAttribute('data-tv3-menu-scroller'));
  // The reaction gate below is the whole basis for calling these rows a menu the click just opened.
  // A navigation destroys window, so an absent snapshot here means the page under us is not the page
  // we clicked on, and every row would read as new. Refuse: "cannot judge" beats naming three
  // ordinary links on a fresh document as a menu and telling the model to pick one.
  // `reuse: 'any'` weakens that gate for a list that is ALREADY open instead of one a click just
  // rendered: it admits every visible row but then only accepts a FLOATING container (a positioned
  // popup) — an in-flow static list (a form's own radio group, a sidebar of links) never qualifies as
  // an open menu without reaction evidence.
  const reuse = arg.reuse === 'any' ? arg.reuse : null;
  if (!preReady() && reuse !== 'any') return null;
  const tr0 = trigger ? trigger.getBoundingClientRect() : null;
  // A cascading widget may HIDE its old stage instead of detaching it: a connected trigger with a
  // zeroed rect anchors geometry at the viewport origin and would reject legitimate children.
  const tr = cascade && tr0 && !(tr0.width > 0 && tr0.height > 0) ? null : tr0;
  const rows = [];
  for (const el of pScopeAll()) {
    if (reuse !== 'any' && (preHas(el) || focusHas(el))) continue;
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LABEL' || tag === 'FORM') continue;
    if (el.children.length > 8) continue;
    const r = el.getBoundingClientRect();
    if (!vis(r) || r.height > 90) continue;
    const txt = (el.innerText || '').trim();
    if (!txt || txt.length > 80) continue;
    // Options are individually actionable rows. Requiring it per-row keeps a dialog's title/body
    // text from being listed as "options" (and a horizontal Confirm/Cancel button pair then fails
    // the stacked-rows check below). The role set is observe's, minus the container and
    // navigational ones: a single-select built as a radiogroup and a multi-select built as
    // checkboxes are menus, and a probe that disagreed with observe about that left their rows
    // untagged -- which silently disarms every commit check in _click_reaction.
    const role = el.getAttribute('role');
    let ptr = false;
    try { ptr = getComputedStyle(el).cursor === 'pointer'; } catch (e) { ptr = false; }
    const clickable = tag === 'BUTTON' || tag === 'A' || MENU_ROW_ROLES.has(role) || ptr;
    if (!clickable) continue;
    rows.push({ el, r, txt });
  }
  if (rows.length < 2) return null;
  const leaves = rows.filter((c) => !rows.some((o) => o.el !== c.el && pContains(c.el, o.el)));
  if (leaves.length < 2) return null;
  // Group by parent AND grandparent so both flat menus (card > button*N) and nested ones
  // (ul > li > button) find their shared container.
  const groups = new Map();
  // Role-less lists are grouped only on the trigger's own ARIA word: its aria-controls target, or —
  // when it declares a listbox/combobox — the rows' nearest shared ancestor. A plain button gives
  // no such word, and guessing would merge unrelated option sets on the page.
  const trigDeclares = !!trigger && (/(^|\s)combobox(\s|$)/i.test(trigger.getAttribute('role') || '')
    || (trigger.getAttribute('aria-haspopup') || '').toLowerCase() === 'listbox');
  let controlled = null;
  try {
    // A plain toggle's aria-controls names a revealed panel, not a list: only a declared picker's
    // aria-controls is read as its option container.
    const cid = trigDeclares ? trigger.getAttribute('aria-controls') : null;
    const root = trigger.getRootNode();
    controlled = cid ? ((root && root.getElementById) ? root.getElementById(cid) : document.getElementById(cid)) : null;
    if (controlled && controlled.getBoundingClientRect().height > 500) controlled = null;
  } catch (e) { controlled = null; }
  // parentElement is null at a shadow boundary (a ShadowRoot is not an Element), so a menu whose
  // rows are written straight into the root -- root.innerHTML = '<div role="option">...' -- would
  // group under nothing and never be found. The host stands in for the boundary.
  // A host reached this way stands in for the boundary, and a host necessarily pre-exists the menu
  // its component just rendered -- so the container-is-new check below must not be applied to it.
  // The rows' own newness still carries the reaction evidence.
  const boundaryStandIns = new Set();
  const parentOf = (el) => {
    if (!el) return null;
    const p = composedParent(el);
    if (!p) return null;
    if (p.nodeType === 11) {
      if (!p.host) return null;
      boundaryStandIns.add(p.host);
      return p.host;
    }
    return p.nodeType === 1 ? p : null;
  };
  // querySelectorAll stops at a shadow root, so an ancestor whose rows live inside components would
  // count zero options and never read as their container.
  const allOpts = pQSA(OPT_SEL);
  const optCount = (a) => {
    let n = 0;
    for (const o of allOpts) if (o !== a && pContains(a, o) && ++n >= 2) break;
    return n;
  };
  for (const c of leaves) {
    const p1 = parentOf(c.el);
    // A virtualized listbox nests each row under its own wrapper (option > div > leaf), so parent AND
    // grandparent are unique per row and never group. The declaring LIST_SEL ancestor is the row's real
    // container in that case, added as a third candidate alongside parent/grandparent so every
    // currently-passing (non-virtualized) widget still groups exactly as before.
    const row = composedClosest(c.el, OPT_SEL) || c.el;
    // Without a list role, the list is the nearest ancestor holding more than one option row (a
    // row's own wrapper is as unique per row as its parent already was).
    const sharedRowAncestor = (r) => {
      let first = null;
      let a = composedParent(r);
      for (let h = 0; a && h < 8; h++, a = composedParent(a)) {
        if (a.nodeType !== 1 || optCount(a) < 2) continue;
        // Prefer the clipped container over a virtualiser's full-height spacer, which the geometry
        // pass below rejects as too tall.
        if (a.getBoundingClientRect().height <= 500) return a;
        if (!first) first = a;
      }
      return first || r.parentNode;
    };
    const listKey = composedClosest(row, LIST_SEL)
      || (controlled && (controlled.contains(row) || pContains(controlled, row)) ? controlled : null)
      || (trigDeclares ? sharedRowAncestor(row) : row.parentNode);
    for (const p of [p1, parentOf(p1), listKey]) {
      // listKey can land on a ShadowRoot (nodeType 11), which has no getBoundingClientRect for the
      // geometry pass below; p1/parentOf(p1) never do, since parentOf already promotes a boundary to
      // its host element.
      if (!p || p.nodeType !== 1 || p === document.body || p === document.documentElement) continue;
      if (!groups.has(p)) groups.set(p, new Set());
      groups.get(p).add(c);
    }
  }
  let best = null;
  for (const [p, set] of groups) {
    const g = Array.from(set);
    if (g.length < 2) continue;
    // A pre-existing container normally is not a just-opened menu. The exception: the row you CLICKED
    // (or a container inside it) that expanded to reveal leaves which pre-existed hidden — there the
    // container is old but the leaves are the new reaction. Scoped to within the clicked row so an
    // unrelated pre-existing list that merely gained rows elsewhere is still rejected.
    const withinClicked = !!trigger && (p === trigger || pContains(trigger, p));
    if (reuse === null && !cascade && !boundaryStandIns.has(p) && preHas(p) && !withinClicked) continue;
    // Under `reuse: 'any'` the reaction evidence is gone, so structure must carry the claim alone: an
    // open menu floats (its container or a near ancestor is absolutely/fixed positioned). An in-flow
    // candidate is static page content and is never admitted on this path.
    if (reuse === 'any' || (cascade && !withinClicked)) {
      let floating = false;
      for (let a = p, hops = 0; a && a.nodeType === 1 && hops < 8; hops++, a = composedParentElement(a)) {
        let pos = '';
        try { pos = getComputedStyle(a).position; } catch (e) { break; }
        if (pos === 'absolute' || pos === 'fixed') { floating = true; break; }
      }
      if (!floating) continue;
    }
    // A dialog is a page mode, not a menu — mislabeling its action buttons invites a wrong "pick an
    // option" move. But a real option list legitimately renders inside a modal (an application form's
    // select in a dialog), so exclude a dialog group ONLY when its rows are not explicit menu options:
    // a confirm dialog's Cancel/Confirm pair (plain buttons) stays excluded, a role=option listbox does not.
    try {
      if (composedClosest(p, 'dialog,[role~="dialog"],[aria-modal="true"]')) {
        // Test the closest option-role ANCESTOR, not the reduced leaf: a role=option row with a styled
        // <span> child reduces to the span (null role), so a leaf-only check would wrongly reject it.
        // menuitem rows are enumerable here too: a menu in a dialog is still a menu.
        if (!g.every((c) => composedClosest(c.el, OPT_SEL + ',[role="menuitem"]'))) continue;
      }
    } catch (e) {}
    const pr = p.getBoundingClientRect();
    if (!vis(pr) || pr.height > 500) continue;
    if (tr && (pr.top < tr.top - 200 || pr.top > tr.bottom + 400)) continue;
    if (tr && (pr.right < tr.left - 100 || pr.left > tr.right + 100)) continue;
    const tops = new Set(g.map((c) => Math.round(c.r.top)));
    if (tops.size < 2) continue;
    if (!best || g.length > best.g.length || (g.length === best.g.length && pr.height < best.h)) best = { p, g, h: pr.height };
  }
  if (!best) return null;
  pQSA('[data-tv3-menu]').forEach((e) => e.removeAttribute('data-tv3-menu'));
  best.g.sort((a, b) => a.r.top - b.r.top || a.r.left - b.r.left);
  const options = [];
  let n = 0;
  for (const c of best.g) {
    n++;
    c.el.setAttribute('data-tv3-menu', String(n));
    if (options.length < 15) options.push({ n, text: c.txt.slice(0, 60) });
  }
  // Undeclared virtualisation: a list that renders only a window declares nothing (no aria-setsize),
  // but its scroll container carries the FULL extent (react-window sizes a spacer to the whole list).
  // Rendered-in-full lists fill their scroll extent; a window leaves more than a row of it uncovered.
  let partial = false;
  // Tagged so a caller that hits `partial` can drive this same container's scrollTop to search past
  // the rendered window, without re-deriving which ancestor is the scroller.
  try {
    const first = best.g[0].r, last = best.g[best.g.length - 1].r;
    const span = last.bottom - first.top;
    const rowH = Math.max(1, span / best.g.length);
    // Walk up from the ROW, not the group container: a virtualiser's scroller commonly sits between
    // the rows and the role=listbox (listbox > scroller > spacer > rows), below the group key.
    const rowEl = best.g[0].el && best.g[0].el.nodeType === 1 ? best.g[0].el : best.p;
    const listEl = composedClosest(rowEl, LIST_SEL);
    let inner = null;
    // Composed, not parentElement: a row rendered inside an option component would stop at that
    // component's shadow boundary, leaving the outer scroller untagged -- and a rendered window then
    // reads as the whole list.
    for (let sc = rowEl, hops = 0; sc && sc.nodeType === 1 && hops < 10;
         inner = sc, hops++, sc = composedParentElement(sc)) {
      const ovy = getComputedStyle(sc).overflowY;
      if ((ovy === 'auto' || ovy === 'scroll' || ovy === 'overlay') && sc.scrollHeight > sc.clientHeight + 1) {
        // A scroller inside (or equal to) the list container is the list's own by construction,
        // whatever sizes it (an ancestor spacer or a sibling sizer). One ABOVE the list only counts
        // when the child carrying the rows owns its scroll extent: a modal body that scrolls for
        // unrelated content below a short, fully rendered list is not this list's scroller.
        const insideList = !!listEl && listEl.nodeType === 1 && (sc === listEl || listEl.contains(sc));
        const owned = inner ? inner.getBoundingClientRect().height : span;
        if (!insideList && owned < sc.scrollHeight - 2 * rowH && owned < 0.75 * sc.scrollHeight) continue;
        partial = sc.scrollHeight - span >= 1.5 * rowH;
        sc.setAttribute('data-tv3-menu-scroller', '1');
        break;
      }
    }
  } catch (e) { partial = false; }
  return { count: n, options, partial };
}"""
)

# Read the FULL (untruncated) label of every row tagged data-tv3-<attr>, across the same pierced reach
# the tagger tags in. A tagger caps its returned `options` at 15 and truncates each to 60 chars for
# payload size; the deterministic match must see the whole list at full length so a value beyond the
# 15th row, or a label longer than 60 chars, is neither missed nor matched on a cut-off token. `nav`
# marks a row this tool must not auto-click. `arg.attr` selects which tagger's rows to read ("menu" for
# _FIND_MENU_JS, "sugg" for _FIND_SUGGESTION_JS) so the same full-length read serves both.
# `val` and `label` are the identity a duplicate-rendered candidate carries when its TEXT does not: a
# widget that paints the same option as two DOM rows (an a11y copy, a portal+inline render) still gives
# them the same accessible name, and only a genuinely distinct value ever differs between them.
_MENU_OPTION_TEXTS_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + _ROW_SEMANTICS_JS
    + _DECLARED_VALUES_JS
    + r"""
  const attr = (arg && arg.attr) || 'menu';
  // The tagger tags the innermost leaf, which may hold none of a row's accessible-name attributes
  // (a `<span>` inside `<li role="option" aria-label="...">`) -- so the name is read from BOTH the
  // tagged leaf and its OPT_SEL ancestor, aria-label before aria-labelledby at each, first non-empty wins.
  const resolveLabelledby = (node) => {
    if (!node || !node.getAttribute) return '';
    const idref = node.getAttribute('aria-labelledby');
    if (!idref) return '';
    try {
      const root = node.getRootNode ? node.getRootNode() : document;
      const parts = [];
      for (const id of idref.split(/\s+/).filter(Boolean)) {
        const ref = root && root.getElementById ? root.getElementById(id) : document.getElementById(id);
        if (ref) parts.push((ref.textContent || '').trim());
      }
      return parts.join(' ').trim();
    } catch (e) { return ''; }
  };
  const accessibleName = (node) => {
    if (!node || !node.getAttribute) return '';
    // aria-labelledby outranks aria-label in accessible-name computation: rows sharing a generic
    // aria-label can still be named apart by their labelledby targets.
    const lb = resolveLabelledby(node);
    if (lb) return lb;
    const al = node.getAttribute('aria-label');
    return al && al.trim() ? al.trim() : '';
  };
  return Array.from(pQSA('[data-tv3-' + attr + ']')).map((el) => {
    // An option whose ancestor declares aria-setsize is a child that declares none, so read the
    // closest declaring ancestor or the incomplete-list guard is bypassed.
    const nav = isNavRow(el);
    // A grid combobox stores a candidate's identity on the [role=row] ancestor while the tagged
    // element is its gridcell — the row is the option-equivalent surface for the veto walk.
    const opt = composedClosest(el, OPT_SEL) || composedClosest(el, '[role="row"]');
    const setEl = composedClosest(el, '[aria-setsize]');
    const setsize = setEl ? parseInt(setEl.getAttribute('aria-setsize'), 10) : NaN;
    // `pos` is the row's top in the scroller's own coordinate space (scroll-invariant), so a row seen
    // in two overlapping windows reads the same and two rows wearing one text read apart.
    const sc = pQS('[data-tv3-menu-scroller]');
    const pos = sc ? Math.round(el.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop) : null;
    let val = null;
    if (el.tagName === 'OPTION') {
      // The DOM `.value` IDL is the spec submission value ALREADY -- an explicit `value=""` and an
      // absent attribute (which falls back to the option's own text) are genuinely distinct submission
      // values even though both can display identical text, so this always reads it, never null.
      // NOT trimmed: the DOM preserves whitespace in option submission values, so "x" and "x " are
      // genuinely distinct — the trim rule covers attribute-authoring drift, not submission values.
      val = String(el.value);
    } else {
      // Same presence rule as the OPTION branch: an attribute that EXISTS is a present value even
      // when empty -- `data-value=""` next to `data-value="x"` is a real disagreement, not absence.
      const dv = el.getAttribute('data-value');
      const va = el.getAttribute('value');
      // data-value is authoring metadata (whitespace drift between copies is presentational); a
      // `value` attribute is a submission value and stays byte-exact like the OPTION branch above.
      val = dv !== null ? String(dv).trim() : va !== null ? String(va) : null;
    }
    // The collapse's OWN value read: the verifier's declaredValues drops all-digit and >40-char
    // values on purpose (they must not CONFIRM a commit), but for telling two same-text rows apart a
    // disagreeing "101" vs "202" is exactly the signal — so this keeps every non-empty value on the
    // same attribute allowlist, from the leaf and its option ancestor.
    const VETO_ATTR = /^(value|data-value|data-val|data-v|data-code|data-key|data-option-value|name|title)$/;
    // value/data-value keep presence semantics even when empty: a wrapped ancestor's data-value=""
    // next to its twin's data-value="x" is a real disagreement, not absence.
    const VETO_EXPLICIT = /^(value|data-value)$/;
    const vetoVals = [];
    try {
      for (const node of new Set([el, opt || el])) {
        // Keyed by SURFACE and attribute ("a:" the option row, "l:" a leaf inside it): crossed
        // values across surfaces are disagreements a flat set cannot see. The key must come from
        // DOM structure, not tagging depth — a self-tagged row and its twin tagged at a leaf must
        // read the row's attributes under the same key, or equal renders refuse each other.
        const where = node === (opt || el) ? 'a:' : 'l:';
        for (const a of node.attributes) {
          if (!VETO_ATTR.test(a.name)) continue;
          // Trimmed for comparison: whitespace drift between a portal copy and an inline copy is
          // exactly the duplicate-render shape this collapse exists for.
          let v = String(a.value).trim();
          if (!v && !VETO_EXPLICIT.test(a.name)) continue;
          // A long value is fingerprinted (prefix + length), never dropped: dropping it made the
          // veto blind to distinct long values, while the fingerprint still tells apart any pair
          // differing in prefix or length. Only identical-prefix-same-length pairs read as equal.
          if (v.length > 512) v = v.slice(0, 512) + '#len' + v.length;
          vetoVals.push(where + a.name + '=' + v);
        }
      }
    } catch (e) { /* attributes unreadable: no veto values */ }
    return {
      n: parseInt(el.getAttribute('data-tv3-' + attr), 10),
      text: (el.innerText || el.textContent || '').trim(),
      nav: nav,
      setsize: Number.isFinite(setsize) && setsize > 0 ? setsize : 0,
      pos: pos,
      val: val,
      // 9 allowlisted attributes x 2 nodes = 18 possible entries; 24 can never truncate.
      vals: vetoVals.slice(0, 24),
      // Names are FULL veto inputs (truncation happens only where a message renders them): a slice
      // here would read two labels diverging past the cut as equal and collapse distinct rows.
      label: (accessibleName(el) || accessibleName(opt)) || null,
      // Leaf and ancestor names as separate veto surfaces: a shared leaf label ("Choose") must not
      // mask option ancestors whose names disagree.
      labels: [accessibleName(el) || null, opt ? accessibleName(opt) || null : null],
    };
  });
}"""
)

# Wall-clock cap on the virtualized-list walk: a pathological list degrades to the "cut short" error
# instead of consuming the task's step budget in one call.
_SCROLL_SEARCH_BUDGET_S = 20.0

# What the tagged scroller currently renders first: the first option row's text and its top in the
# scroller's own coordinate space. A virtualiser that has re-rendered for a new scrollTop shows a
# different first row, so the walk can read the window as soon as this changes instead of sleeping
# a fixed settle on every step.
_MENU_WINDOW_FINGERPRINT_JS = (
    r"""() => {"""
    + _PIERCED_QUERY_JS
    + r"""
  const sc = pQS('[data-tv3-menu-scroller]');
  if (!sc) return null;
  // querySelector cannot see a row inside a component's shadow root, and a fingerprint that never
  // changes makes every window pay the full settle instead of reading as soon as it re-rendered.
  const scTop = sc.getBoundingClientRect().top;
  let best = null;
  for (const row of pQSA('[role="option"],[role="menuitem"],[role="menuitemradio"],[role="treeitem"],li')) {
    if (row === sc || !pContains(sc, row)) continue;
    const pos = Math.round(row.getBoundingClientRect().top - scTop + sc.scrollTop);
    if (!best || pos < best.pos) best = { row, pos };
  }
  if (!best) return '';
  return (best.row.textContent || '').trim().slice(0, 40) + '@' + best.pos;
}"""
)

# Drive the scroll container `_FIND_MENU_JS` tagged data-tv3-menu-scroller: set scrollTop to `arg.top`
# when it is a number, then read back the position and extent. Read-only when `arg.top` is not a number,
# so the same call can both prime the search (top: 0) and poll after each step (top omitted).
_MENU_SCROLLER_STEP_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + r"""
  const el = pQS('[data-tv3-menu-scroller]');
  if (!el) return null;
  if (typeof arg.top === 'number') el.scrollTop = arg.top;
  return { scrollTop: el.scrollTop, clientHeight: el.clientHeight, scrollHeight: el.scrollHeight };
}"""
)

# Whether the anchor (or its nearest aria-expanded ancestor) currently reports an OPEN list. Used to
# gate the "close a stray open list" Escape: sending Escape with no menu open would bubble to and close
# a surrounding dialog, so we only send it once a menu is confirmed open.
_MENU_OPEN_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + r"""
  const el = pQS(arg.sel) || (arg.el && arg.el.isConnected ? arg.el : null);
  if (!el) return false;
  const exp = el.getAttribute('aria-expanded') != null ? el : el.closest('[aria-expanded]');
  return !!(exp && exp.getAttribute('aria-expanded') === 'true');
}"""
)

# Whether a DECLARED field's own list is still open: _MENU_OPEN_JS's aria-expanded check, OR the popup
# the field declares (fieldOwnPopup, from _ROW_SEMANTICS_JS) is still rendered. A widget that re-searches
# on the value it just wrote back can leave rows on screen with aria-expanded never having flipped.
_TYPEAHEAD_LIST_OPEN_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + _ROW_SEMANTICS_JS
    + r"""
  const el = pQS(arg.sel) || (arg.el && arg.el.isConnected ? arg.el : null);
  if (!el) return false;
  const exp = el.getAttribute('aria-expanded') != null ? el : el.closest('[aria-expanded]');
  if (exp && exp.getAttribute('aria-expanded') === 'true') return true;
  return !!fieldOwnPopup(el, true);
}"""
)

# True when the anchor (or its combobox ancestor) declares aria-autocomplete list/both/inline -- the ARIA
# contract for a combobox that searches as you type. It is the reaction signal for a searchable widget that
# filtered to ZERO rows on an absent value (nothing new for _FIND_MENU_JS to count), which must still read
# as a genuine no-match rather than fall through to a click-to-open enumeration.
_DECLARES_SEARCH_AUTOCOMPLETE_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + r"""
  const SEARCH = new Set(['list', 'both', 'inline']);
  const read = (n) => (n.getAttribute('aria-autocomplete') || '').toLowerCase();
  let el = pQS(arg.sel) || (arg.el && arg.el.isConnected ? arg.el : null);
  if (!el) return false;
  if (SEARCH.has(read(el))) return true;
  const cb = el.closest('[aria-autocomplete]');
  return !!(cb && SEARCH.has(read(cb)));
}"""
)

# Page total for `group` text across one observe, counted at the 200-character display width of each
# entry; the record retains up to the masking width, which Python masks and then caps to 200.
OBSERVE_GROUP_TEXT_TOTAL_CAP = 4000
# Display width of each masked-then-capped field of the observe digest. Every render site reads its
# width here, so the retain margin below is always sized for the widest window.
OBSERVE_DISPLAY_WIDTHS = {"label": 140, "placeholder": 60, "value": 100, "invalid": 140, "group": 200, "text": 300}
# Floor for the width the enumeration retains per field before Python masks and caps it. Widened per
# call so the longest payload-minted URL fits whole after the widest display window.
OBSERVE_RETAIN_WIDTH_MIN = 2000
OBSERVE_FIELD_DISPLAY_MAX = max(OBSERVE_DISPLAY_WIDTHS.values())

# Raw DOM perception: collect visible interactive elements with a stable selector each.
# Elements without a natural selector get a data-tv3 marker so later actions can target them.
_OBSERVE_JS_TEMPLATE = (
    r"""
async () => {
  // Field text is retained at this width and masked, then capped for display, in Python. Substituted
  // per call from the payload refs: any minted URL that starts inside a display window fits whole.
  const _RETAIN_WIDTH = __OBSERVE_RETAIN_WIDTH__;
  const _GROUP_TEXT_TOTAL_CAP = """
    + str(OBSERVE_GROUP_TEXT_TOTAL_CAP)
    + r""";
  const _GROUP_SEL = 'fieldset,[role=group],li,dd,.form-group,[class*="question"],[class*="field"]';
  // A previous control ends the walk back for question text; ARIA widgets count as controls here
  // exactly as they do in the element list, or a custom checkbox's own caption reads as the question.
  const _CTRL_SEL = 'input:not([type=hidden]),textarea,select,button,[role=button],[role=checkbox],[role=radio],[role=combobox],[role=switch],[role=listbox],[role=spinbutton],[contenteditable]:not([contenteditable="false" i])';
  const _normText = (s) => (s || '').replace(/\s+/g, ' ').trim();
  // A choice control takes its group's text only when the group is purely options: a container
  // that also holds text fields has an innerText naming every question in it.
  const _NONCHOICE_SEL = 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]),textarea,select,[role=combobox],[role=listbox],[role=spinbutton],[contenteditable]:not([contenteditable="false" i])';
  const _CHOICE_SEL = 'input[type=checkbox],input[type=radio],[role=checkbox],[role=radio],[role=switch]';
  // Read through the prototypes: the walk below crosses the control's <form>, whose named controls
  // shadow its own properties (<input name="matches"> makes form.matches that input).
  const _getter = (proto, name) => {
    const d = Object.getOwnPropertyDescriptor(proto, name);
    return d && d.get ? d.get : function () { return this[name]; };
  };
  const _parentOf = _getter(Node.prototype, 'parentElement');
  const _scrollLeftOf = _getter(Element.prototype, 'scrollLeft');
  const _prevOf = _getter(Node.prototype, 'previousSibling');
  const _nextOf = _getter(Node.prototype, 'nextSibling');
  const _firstChildOf = _getter(Node.prototype, 'firstChild');
  const _nodeTypeOf = _getter(Node.prototype, 'nodeType');
  const _contentOf = _getter(Node.prototype, 'textContent');
  const _innerTextOf = _getter(HTMLElement.prototype, 'innerText');
  const _matches = Element.prototype.matches;
  const _qs = Element.prototype.querySelector;
  const _qsa = Element.prototype.querySelectorAll;
  const _bcr = Element.prototype.getBoundingClientRect;
  // A sibling the user cannot see is not the question: unrendered, transparent, aria-hidden, or a
  // box under 2px (a zero-height clip, a 1px screen-reader-only hint). A display:contents wrapper
  // has no box of its own and is judged by the innerText of its rendered children.
  const _unseen = (s) => {
    const cs = window.getComputedStyle(s);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return true;
    if (s.getAttribute('aria-hidden') === 'true') return true;
    if (cs.display === 'contents') return false;
    const r = _bcr.call(s);
    return r.width < 2 || r.height < 2;
  };
  // Text a user can see inside el: innerText still includes transparent, aria-hidden and
  // screen-reader-only descendants, which _unseen excludes. Bounded to 4 levels.
  const _visibleText = (el, depth) => {
    if (depth > 4 || _unseen(el)) return '';
    let out = '';
    for (let c = _firstChildOf.call(el); c; c = _nextOf.call(c)) {
      const kind = _nodeTypeOf.call(c);
      if (kind === 3) out += ' ' + _contentOf.call(c);
      else if (kind === 1) out += ' ' + _visibleText(c, depth + 1);
    }
    return _normText(out);
  };
  const _captionHost = (el) => {
    if (_matches.call(el, _CHOICE_SEL)) return true;
    if (!_qs.call(el, _CHOICE_SEL)) return false;
    try { return _visibleText(el, 0).length < 2; } catch (e) { return true; }
  };
  // Question text for a control whose own name is weak. Choice controls take the text of the
  // nearest group ancestor that has any (legend + options), if it is purely options. Text fields
  // take the nearest text block that PRECEDES the control inside that ancestor, stopping at a
  // previous control: a container holding several questions has an innerText naming all of them,
  // and a group text that names the wrong question is the mis-association this field exists to
  // end. The ancestor's own text is used only when it wraps this one control. Bounded to 6 levels
  // and 8 siblings. Any throw yields no group text, never a dropped element.
  const _groupText = (el, isChoice) => {
    try {
      let node = el;
      for (let depth = 0; depth < 6; depth++) {
        const parent = _parentOf.call(node);
        if (!parent) break;
        if (!isChoice) {
          let scanned = 0;
          for (let s = _prevOf.call(node); s && scanned < 8; s = _prevOf.call(s), scanned++) {
            const kind = _nodeTypeOf.call(s);
            if (kind === 3) {
              const t = _normText(_contentOf.call(s));
              if (t.length >= 2) return t;
              continue;
            }
            if (kind !== 1) continue;
            if (_matches.call(s, _CTRL_SEL) || _qs.call(s, _CTRL_SEL)) break;
            if (_unseen(s)) continue;
            let t = '';
            try { t = _normText(_innerTextOf.call(s)); } catch (e) { continue; }
            // One character is decoration (a required marker), never a question.
            if (t.length < 2) continue;
            // Text right after a checkbox or radio -- bare, or in a wrapper with no text of its own --
            // is that control's caption, not this one's question. A previous question block that
            // happens to hold options is not a wrapper, and the text after it is the next question.
            let before = _prevOf.call(s);
            while (before && _nodeTypeOf.call(before) !== 1 && !_normText(_contentOf.call(before))) before = _prevOf.call(before);
            if (before && _nodeTypeOf.call(before) === 1 && _captionHost(before)) break;
            return t;
          }
        }
        node = parent;
        if (!_matches.call(node, _GROUP_SEL)) continue;
        const t = _normText(_innerTextOf.call(node));
        if (!t) continue;
        if (isChoice) return _qsa.call(node, _NONCHOICE_SEL).length === 0 ? t : '';
        return _qsa.call(node, _CTRL_SEL).length === 1 ? t : '';
      }
    } catch (e) { /* fail open: the record keeps today's shape */ }
    return '';
  };
  const _isAutocomplete = """
    + _IS_AUTOCOMPLETE_JS
    + r""";
  const _visibleProxy = """
    + _VISIBLE_PROXY_JS
    + r""";
  const _labelText = """
    + _LABEL_TEXT_JS
    + r""";
  // [role=textbox] is deliberately absent: on a div without contenteditable it names a control that
  // cannot be filled, and the ones that can are already matched by [contenteditable=true].
  const _WIDGET_ROLES = """
    + _WIDGET_ROLES_JS
    + r""";
  const q = 'input,textarea,select,button,a[href],[role=button],[role=checkbox],[role=radio],[role=combobox],[role=option],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=listbox],[role=switch],[role=spinbutton],[role=tab],[contenteditable=true]';
  // Set wherever we learn that some region of the page cannot be read. Declared here because the
  // walk below is one of those places and it runs before the marker gather.
  let sawUnreadableRoot = false;
  // Narrower than sawUnreadableRoot, which a page-wide flag several unrelated failures also set:
  // this counts only roots the walk never discovered, so a channel that iterates allRoots can say
  // whether allRoots was the whole story.
  let undiscoveredRoots = 0;
  // A web component renders its real input/button inside an open shadow root, which
  // document.querySelectorAll does not cross. Playwright's selector engine does, so these elements
  // were always actionable and only perception was blind — a page of them reads as a handful of
  // chrome controls that never change. Each root's own matches are appended after the light DOM's,
  // NOT spliced in at the host's position, so the element budget spends itself on the page's own
  // controls first and the submit button survives a page of components; what that starves is
  // counted and disclosed. Every root is kept for the uniqueness probe below.
  const allRoots = [];
  const els = [];
  // Roots whose host chain reaches a <form>. `closest` stops at the root it starts in, so a block
  // inside a component cannot see the form its host sits in; this is that answer, carried down.
  const inFormRoots = new Set();
  {
    const seenRoots = new Set();
    // Pushes `root`'s own matches onto `els`, and collects the roots nested directly under it into
    // `kids` in document order.
    const enumerate = (root, host, kids) => {
      // Selected by the same CSS-string query the base used, not el.matches(q). This is base parity,
      // not a defense: a page that overrides querySelectorAll itself can still make <html> and <body>
      // enumerate, measured, exactly as it can on base. What it does avoid is widening the surface
      // to a SECOND overridable entry point for the same outcome.
      for (const el of root.querySelectorAll(q)) els.push({ el, host });
      for (const el of root.querySelectorAll('*')) {
       // Same clobbering hazard as the element loop below, and this walk runs before it: a form's
       // named getter can turn any read here into a foreign object, so one element pays for itself.
       try {
        // nodeType 11 because <input name="shadowRoot"> makes el.shadowRoot that input, and
        // walking it would add a non-root to the list every probe then queries.
        const sr = el.shadowRoot;
        if (sr && sr.nodeType === 11) {
          if (seenRoots.has(sr)) continue;
          seenRoots.add(sr);
          kids.push({ root: sr, host: el, parent: root });
        }
       } catch (e) {
        // This costs the element's entire root, not the element -- its own matches were pushed by
        // the query above. The root never reaches allRoots, so the loss is disclosed here instead.
        sawUnreadableRoot = true;
        undiscoveredRoots++;
       }
      }
    };
    // An explicit stack rather than recursion. Playwright's selector engine descends to any depth,
    // so a root we stop short of is a root resolvesTo cannot count -- an identity recurring beyond
    // the stopping point reads as unique here and as ambiguous to the executor.
    const stack = [];
    // Reversed, so popping walks a root's children in document order and both lists stay pre-order.
    const descend = (kids) => { for (let k = kids.length - 1; k >= 0; k--) stack.push(kids[k]); };
    allRoots.push(document);
    const seed = [];
    // Unguarded, unlike the roots below: a document that cannot be enumerated is an error worth
    // raising, not a page that happens to carry no controls.
    enumerate(document, null, seed);
    descend(seed);
    while (stack.length) {
      const frame = stack.pop();
      allRoots.push(frame.root);
      // Pre-order, so the parent root's answer is already settled when we get here.
      try {
        if (inFormRoots.has(frame.parent) || frame.host.closest('form')) inFormRoots.add(frame.root);
      } catch (e) { /* one host that cannot answer only costs its own root's ranking */ }
      const kids = [];
      // One root that cannot be enumerated costs its own subtree, not the walk. A root that throws
      // for every query is disclosed by the marker gather below; one that throws only for this
      // query is not, and is not defended here.
      // Its own matches are lost, and so is every root nested under it -- those never reach allRoots.
      try { enumerate(frame.root, frame.host, kids); } catch (e) { undiscoveredRoots++; }
      descend(kids);
    }
  }
  const out = [];
  const labelOfControl = new Map();
  // Monotonic across observe() calls (persisted on window), and never reassigned on an element that
  // already has one, so a data-tv3 marker always denotes the same element. Resetting the counter per
  // call let a selector remembered from an earlier observe silently resolve to a different node.
  if (!Number.isInteger(window.__tv3_next) || window.__tv3_next < 0 || window.__tv3_next > 1e9) window.__tv3_next = 0;
  // Unique is not enough -- it must be THIS element. A page can pre-seed a marker or exploit
  // U+0000 folding to U+FFFD so a selector matches exactly one node that is a different one.
  // Counted across every root, not just `document`: Playwright resolves a selector globally and
  // pierces, so a document-only check both misses a collision living in another root and rejects
  // every shadow-hosted element, whose one true match `document.querySelector` cannot see.
  // Gathered once, not re-queried per attempt: the mint search below is bounded at 64 attempts, and
  // when each attempt cost O(roots) a page could freeze window.__tv3_next, seed the 64 candidates in
  // the LAST root so no attempt short-circuits, and multiply the whole search by its own root count.
  // Measured before this was hoisted: 51 ms -> 36.4 s at 24,000 roots, past the 30 s tool bound.
  // Two structures, deliberately: `takenMarkers` is every value known to be in use, gathered AND
  // minted, and exists only so a fresh candidate never collides. `gatheredCounts` records what the
  // GATHER saw and nothing else -- reuse consults it, so a marker this call minted itself can never
  // be handed to a second element as though the page had been observed carrying it once.
  const takenMarkers = new Set();
  const gatheredCounts = new Map();
  for (const root of allRoots) {
    try {
      for (const e of root.querySelectorAll('[data-tv3]')) {
        const v = e.getAttribute('data-tv3');
        takenMarkers.add(v);
        gatheredCounts.set(v, (gatheredCounts.get(v) || 0) + 1);
      }
    }
    // One unreadable root must not cost the element list -- but it does mean takenMarkers is
    // incomplete, so every marker minted below is unverified. On a page whose controls are all
    // anonymous, resolvesTo is never called and this is the ONLY place that learns it.
    catch (e) { sawUnreadableRoot = true; }
  }
  // A root whose querySelectorAll throws makes uniqueness UNVERIFIABLE, not false -- Playwright's
  // engine pierces via CDP and would still see a collision living in there, so we must not hand out
  // a selector we could not check. resolvesTo therefore still refuses. What that used to cost was
  // the whole page: every element fell through to mintOn, whose reuse check runs through here too,
  // so a fresh marker was minted on every observe -- and a payload that churns each turn silently
  // disables the loop's perception-stall terminator. mintOn reuses an existing marker instead when
  // this is set, which keeps the payload byte-stable.
  // A NEW marker minted while this is set is itself unverified -- takenMarkers skipped the throwing
  // root, so a decoy planted in there can collide. Accepted deliberately: refusing to mint costs
  // every selector on the page, and the collision has not been shown to reach a wrong element
  // (Playwright ordered the light-DOM match first in every shape tried). The payload says so.
  // Per-check, unlike sawUnreadableRoot: distinguishes "this element's identity is genuinely
  // ambiguous" from "we could not tell", which are different omissions with different fixes.
  let checkInconclusive = false;
  const resolvesTo = (s, target) => {
    let found = null;
    let n = 0;
    for (const root of allRoots) {
      let hits;
      try { hits = root.querySelectorAll(s); }
      // A throw is the ROOT's, not ours: every candidate is valid by construction -- `#` + an escape
      // gated on CSS.escape being a no-op, a tag from safeTag's whitelist, or a quoted attr whose
      // value already passed _FORGEABLE. Asking the page to classify the error instead (its own
      // e.name, or a probe whose receiver it owns) hands it a one-line switch to the quiet path,
      // where the disclosure vanishes and markers churn every observe.
      catch (e) { sawUnreadableRoot = true; checkInconclusive = true; return false; }
      n += hits.length;
      if (n > 1) return false;
      if (hits.length === 1) found = hits[0];
    }
    return n === 1 && found === target;
  };
  // Values here are page-controlled: an unescaped `"` closes the selector and turns it into a
  // selector list aimed at an element of the page's choosing, which still resolves uniquely.
  // String() because form named getters make el.id/el.name return an ELEMENT, not a string.
  const attr = (name, value) => '[' + name + '="' + String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"]';
  // The model copies these selectors back verbatim, and an escape sequence does not survive that:
  // `#\31 abc` addresses a different codepoint the moment its terminating space is dropped. The
  // escape being a no-op is exactly the condition for the id needing no escaping. The trim check
  // is separate: Playwright trims the selector string, so `#email<NBSP>` -- which CSS.escape leaves
  // alone, being above U+007F -- reaches the page as `#email` and silently selects a different
  // element. Only the tail can be trimmed away; a leading one sits behind the `#` and survives.
  // The selector is rendered bare inside [...] and CANNOT be sanitized -- stripping a character
  // would break the very matching the selector exists to do. So a value carrying anything that could
  // end or restructure a digest line is refused here and the element falls through to a minted
  // marker, the same route U+0000 already takes. U+000A is refused by CSS itself (an unescaped
  // newline is a bad-string, so the selector will not parse and resolvesTo rejects it) -- but U+2028
  // is a legal CSS ident AND string character, so it parses, resolves, and forges a clean line.
  const _FORGEABLE = /[\x00-\x1f\x7f\u0085\u2028\u2029\u200b-\u200f\u202a-\u202e\u2066-\u2069]/;
  // A tag name is rendered bare into the selector and is page-controlled, so it is whitelisted to
  // what a type selector may actually be rather than screened for known-bad characters. A tag may
  // hold `,` `.` `[` `:` and quotes, and `a,b[id="x"]` is a SELECTOR LIST, not a narrowing of
  // `#x` -- it reaches elements the bare id never matched. It may also not be a string at all: a
  // form's named getter makes el.tagName an ELEMENT, and String() of one is
  // "[object HTMLInputElement]", which is a CSS syntax error. Neither survives this.
  const safeTag = (el) => {
    const t = String(el.tagName || '').toLowerCase();
    return /^[a-z][a-z0-9_-]*$/.test(t) ? t : null;
  };
  // Why the last naturalSelector call returned null. The three causes need three different fixes,
  // so a single "no selector" tally would send the follow-up after the wrong one.
  let naturalWhy = '';
  const naturalSelector = (el) => {
    naturalWhy = '';
    checkInconclusive = false;
    const rawTestid = String(el.getAttribute('data-testid') || '');
    // Read before any attempt: a form's named getter can make el.id an ELEMENT, and String() of
    // one is truthy, so "has an identity to try" and "that identity is usable" are separate facts.
    const hasIdentity = !!(el.id || el.name || rawTestid);
    if (_FORGEABLE.test(String(el.id || '')) || _FORGEABLE.test(String(el.name || ''))
        || _FORGEABLE.test(rawTestid)) { naturalWhy = 'unsafe'; return null; }
    if (el.id) {
      const raw = String(el.id);
      const esc = window.CSS && CSS.escape ? CSS.escape(raw) : null;
      const s = esc === raw && raw === raw.trimEnd() ? '#' + esc : attr('id', raw);
      if (resolvesTo(s, el)) return s;
      // A component that mirrors its own id onto the native control inside its root makes the bare
      // id match twice with ONE instance on the page, and naming the tag separates them. Measured on
      // a production capture: the whole named-field set of a real application form resolves this way
      // and no other.
      // What makes this safe is NOT that the qualified form is a subset of `#id` -- it is a subset
      // only while the tag is a simple type selector, and `safeTag` is what keeps it one. The guard
      // that holds in general is resolvesTo's `found === target`: whatever the string turns out to
      // select, it is accepted only if the single element it selects is THIS one.
      const tq = safeTag(el);
      if (tq) { const s2 = tq + attr('id', raw); if (resolvesTo(s2, el)) return s2; }
    }
    const testid = el.getAttribute('data-testid');
    if (testid) { const s = attr('data-testid', testid); if (resolvesTo(s, el)) return s; }
    if (el.name) {
      const tq = safeTag(el);
      if (tq) { const s = tq + attr('name', el.name); if (resolvesTo(s, el)) return s; }
    }
    // An identity that exists but did not resolve uniquely is NOT anonymous. Shadow encapsulation
    // scopes ids to their root, so a design system reuses one internal id in every instance and the
    // cross-root count is 2 -- a duplicate, whose fix is host-anchored scoping. Distinct again from
    // a count we could not take because some root threw.
    naturalWhy = !hasIdentity ? 'anonymous' : (checkInconclusive ? 'unverifiable' : 'duplicated');
    return null;
  };
  // Minting writes an attribute, which is only safe in the light DOM. Inside a root it makes a
  // component watching that root re-render, destroying the marker before the model can click it --
  // and unlike the pre-snapshot the marker cannot move off-DOM, because the marker IS the handle we
  // hand out. Each fresh observe would mint another and the click would fail again, forever.
  // Of the markers this call hands out, those it wrote versus those it found already on the page:
  // the split is what says whether markers churn between observes, which the stall terminator's
  // digest comparison depends on. Both count the handing out, and an entry the post-walk check
  // strips is uncounted again.
  let markersWritten = 0;
  let markersReused = 0;
  // Bumped before every attribute write we make, verified or not: each one can run page code.
  let pageCodeEpoch = 0;
  const _isConnectedDesc = Object.getOwnPropertyDescriptor(Node.prototype, 'isConnected');
  const _isConnected = _isConnectedDesc && _isConnectedDesc.get ? _isConnectedDesc.get : function () { return document.contains(this); };
  const mintOn = (el) => {
    let m = el.getAttribute('data-tv3');
    // A marker already on the element is page-controlled text like any other attribute: it is
    // rendered bare inside the selector, so one carrying a line separator forges a whole element
    // line. Screened before it can be reused, the same as id/name/data-testid.
    if (m && _FORGEABLE.test(String(m))) m = null;
    // Reuse a marker only if it still uniquely resolves; otherwise mint a fresh monotonic one.
    // Keeps a marker stable across observe() calls without trusting a foreign, duplicated, or
    // syntactically-broken data-tv3 value that a remembered selector could resolve to the wrong node.
    if (m) {
      checkInconclusive = false;
      if (resolvesTo(attr('data-tv3', m), el)) { markersReused++; return attr('data-tv3', m); }
      // Reuse only when THIS check could not be taken AND the gather positively saw this marker
      // exactly once. Gating on the page-global let one unreadable root anywhere hand out a marker
      // resolvesTo had proven is a duplicate; gating on inconclusiveness alone still did, because a
      // throwing DOCUMENT root means the duplicate is never counted at all. Both times two payload
      // lines carried one selector and the click landed on whichever the executor matched first.
      if (checkInconclusive && gatheredCounts.get(m) === 1) { markersReused++; return attr('data-tv3', m); }
    }
    // Minting below is NOT verified when a root is unreadable: takenMarkers could not include that
    // root's markers, so a decoy planted in there can collide with a fresh candidate. Dropping
    // instead was measured and is worse -- it costs every selector on the page, including elements
    // whose own id is unique, which is the regression the unreadable-root test exists to prevent.
    // The page-level "uniqueness could not be verified" note discloses it; see SKY-14710.
    // Skip values the page already carries, or a pre-seeded data-tv3 collides with a freshly
    // minted one and two elements share a selector. Bounded, and the suffix varies per attempt:
    // a frozen or saturated counter makes ++ a no-op, and an unbounded search would wedge the
    // renderer for the rest of the run. An element we cannot name uniquely is left unlisted.
    m = null;
    for (let n = 0; n < 64 && m === null; n++) {
      const candidate = 't' + (window.__tv3_next++) + (n ? '-' + n : '');
      // Checked against every root's markers, for the same reason resolvesTo counts that way: a
      // decoy the page planted inside a shadow root is invisible to document.querySelector, and the
      // executor pierces -- so a document-only check hands out a marker that already denotes something.
      if (!takenMarkers.has(candidate)) m = candidate;
    }
    if (m === null) return null;
    // The `host` screened at enumeration time is bookkeeping, not a property of the element at the
    // moment of the write: a page accessor can move an element into a root after we enumerated it,
    // and an overridden document.querySelectorAll can hand us an in-root element with no host at
    // all. Re-read the real root here so "we never write inside a component" holds by construction.
    let rootNow = null;
    // Two independent signals, each read through the prototype rather than the instance. Every
    // in-page check is clobberable on its own -- Node.prototype.getRootNode included -- so they are
    // combined such that DISAGREEMENT refuses the write: a page must corrupt both consistently to
    // obtain one, and a throw from either is itself a refusal. An element outside the document is
    // also refused; we have nothing to gain by marking one.
    try { rootNow = Node.prototype.getRootNode.call(el); } catch (e) { return null; }
    if (!rootNow || rootNow.nodeType === 11) return null;
    try { if (!Node.prototype.contains.call(document, el)) return null; } catch (e) { return null; }
    pageCodeEpoch++;
    el.setAttribute('data-tv3', m);
    // Verify AFTER the write, against the live DOM rather than the gather. The candidate search
    // reads a snapshot taken before any mint on this page, so it cannot see a value the page added
    // since, nor one an ordinary attributeChangedCallback mirrors onto a sibling during this very
    // setAttribute. A PROVEN collision is dropped rather than handed out: a name that denotes two
    // elements is the wrong-element click this whole mechanism exists to prevent. An INCONCLUSIVE
    // check keeps the marker, which is the documented trade -- refusing there was measured and
    // costs every selector on the page. One check per listed element, not per candidate attempt,
    // which is what made the old per-attempt re-query unaffordable.
    checkInconclusive = false;
    if (!resolvesTo(attr('data-tv3', m), el) && !checkInconclusive) {
      pageCodeEpoch++;
      el.removeAttribute('data-tv3');
      return null;
    }
    takenMarkers.add(m);
    markersWritten++;
    return attr('data-tv3', m);
  };
  // Every scope the executor searches under a host: the host's own light subtree, the root it owns,
  // and any root nested beneath either. A descendant combinator is shadow-transparent to the
  // executor, so content SLOTTED into the component matches `#host #ctrl` too -- counting the root
  // alone undercounts, and an undercount is what hands out a selector that denotes two elements.
  // Memoised per host for the walk, or every control under one shell host pays for a fresh walk of
  // that shell's entire subtree. The only thing that runs page code during the evaluate is our own
  // marker write (an attributeChangedCallback can attach a root), so the memo is dropped after every
  // such write; a clobbered getter that mutates on read is left to the executor-side count that
  // gates every action.
  const hostScopeCache = new Map();
  let hostScopeEpoch = -1;
  const hostScopes = (host) => {
    const epoch = pageCodeEpoch;
    if (epoch !== hostScopeEpoch) { hostScopeCache.clear(); hostScopeEpoch = epoch; }
    if (hostScopeCache.has(host)) return hostScopeCache.get(host);
    const scopes = hostScopesWalk(host);
    hostScopeCache.set(host, scopes);
    return scopes;
  };
  const hostScopesWalk = (host) => {
    const scopes = [host];
    const stack = [host];
    const own = host.shadowRoot;
    if (own && own.nodeType === 11) { scopes.push(own); stack.push(own); }
    while (stack.length) {
      const scope = stack.pop();
      let kids;
      try { kids = scope.querySelectorAll('*'); } catch (e) { return null; }
      for (const k of kids) {
        let sr = null;
        try { sr = k.shadowRoot; } catch (e) { return null; }
        if (sr && sr.nodeType === 11 && scopes.indexOf(sr) === -1) { scopes.push(sr); stack.push(sr); }
      }
    }
    return scopes;
  };
  // resolvesTo, scoped to one host. Same shape and same `found === target` guarantee: whatever the
  // string turns out to select under this host, it is accepted only if the one element it selects
  // is THIS one.
  const scopedResolvesTo = (host, s, target) => {
    const scopes = hostScopes(host);
    if (!scopes) return false;
    let found = null;
    let n = 0;
    for (const scope of scopes) {
      let hits;
      try { hits = scope.querySelectorAll(s); } catch (e) { return false; }
      n += hits.length;
      if (n > 1) return false;
      if (hits.length === 1) found = hits[0];
    }
    return n === 1 && found === target;
  };
  // Shadow encapsulation scopes ids to their own root, so a design system reuses one internal id in
  // every instance and no unscoped selector can single one out. The host itself is outside the root
  // it owns, so it can be named the ordinary way, and anchoring on it scopes the reused id without
  // writing anything into the component.
  // The host of the root `n` lives in, read through the prototype so a named getter cannot supply one.
  const hostOf = (n) => {
    let r = null;
    try { r = Node.prototype.getRootNode.call(n); } catch (e) { return null; }
    return r && r.nodeType === 11 && r.host ? r.host : null;
  };
  // Tails for a control with no id of its own, smallest first: its tag, the tag qualified by type,
  // role or class tokens, and finally its position among same-tag siblings. Every tail is ONE
  // compound selector, never a combinator chain: under the executor a descendant combinator is
  // shadow-transparent and a child combinator is too, so a chain the page counts as unique in one
  // tree can denote a second element in a nested root. A compound is matched element by element,
  // and the union of the host's scopes is exactly the set the executor searches; a nested host's
  // anchor composes such compounds link by link, each verified under its own host. A positional tail
  // is a last resort: unlike a tag or class, a sibling inserted before the control retargets it
  // without changing the match count, which is the one drift the executor-side count cannot see.
  const structuralTails = (el) => {
    const tag = String(el.tagName || '').toLowerCase();
    if (!/^[a-z][a-z0-9-]*$/.test(tag)) return [];
    const tails = [tag];
    const type = el.getAttribute('type');
    if (type && /^[a-z-]+$/i.test(String(type))) tails.push(tag + '[type="' + String(type).toLowerCase() + '"]');
    const role = el.getAttribute('role');
    if (role && /^[a-z]+$/i.test(String(role))) tails.push(tag + '[role="' + String(role).toLowerCase() + '"]');
    // A design system's class tokens are as stable as its tags; each is screened to a plain
    // identifier and the whole is verified, so a token the page chose cannot forge a payload line
    // or denote a second element.
    let classes = [];
    try { classes = Array.from(el.classList || []).filter((c) => /^[A-Za-z_][\w-]*$/.test(c)).slice(0, 3); } catch (e) { classes = []; }
    const leaf = classes.length ? tag + '.' + classes.join('.') : tag;
    if (classes.length) tails.push(leaf);
    let k = 1;
    try { for (let sib = el.previousElementSibling; sib; sib = sib.previousElementSibling) { if (sib.tagName === el.tagName) k++; } } catch (e) { return tails; }
    tails.push(leaf + ':nth-of-type(' + k + ')');
    return tails;
  };
  // Shadow encapsulation scopes ids to their own root, so a design system reuses one internal id in
  // every instance and no unscoped selector can single one out; a component's native control often
  // carries no id at all. The host itself is outside the root it owns, so it can be named the
  // ordinary way -- by its own identity, by a marker written on it in the light DOM, or through ITS
  // host in turn -- and anchoring on it scopes the control without writing anything into the component.
  // Naming a host is paid once per walk: the same anchor serves every control under it, and a
  // marker written for the first is reused, not re-minted, for the rest.
  const anchorByHost = new Map();
  // Anchoring is bounded per walk. A control that cannot be named does not spend the element
  // budget, so without this a page of thousands of unnameable component controls would spend the
  // evaluate's whole time bound on tails that all fail.
  let anchorAttempts = 0;
  const _ANCHOR_ATTEMPTS = 3000;
  const _ANCHORED_MAX_LEN = 400;
  // Set when the LAST refusal was ours (a budget) rather than the page's, so the omission is
  // reported as such and not as a claim about the control.
  let anchorRefusedByBudget = false;
  // The host and tail of the last selector hostAnchored composed, kept so the record can be
  // re-resolved under its host later (a composed selector straddles a root; only scoped counting sees it).
  let lastAnchor = null;
  const hostAnchored = (el, host, depth) => {
    depth = depth || 0;
    if (depth === 0) anchorRefusedByBudget = false;
    if (!host) return null;
    if (depth > 8) { anchorRefusedByBudget = true; return null; }
    // A host already named this walk costs a lookup, not an attempt; the budget is charged for
    // naming a host, which is the part that walks the page.
    if (!anchorByHost.has(host) && ++anchorAttempts > _ANCHOR_ATTEMPTS) { anchorRefusedByBudget = true; return null; }
    let tails = [];
    if (el.id) {
      const raw = String(el.id);
      if (!_FORGEABLE.test(raw)) {
        const esc = window.CSS && CSS.escape ? CSS.escape(raw) : null;
        tails.push(esc === raw && raw === raw.trimEnd() ? '#' + esc : attr('id', raw));
      }
    }
    tails = tails.concat(structuralTails(el));
    let ctrl = null;
    for (const t of tails) { if (scopedResolvesTo(host, t, el)) { ctrl = t; break; } }
    if (!ctrl) return null;
    let hostSel = null;
    let hostTrail = [];
    if (anchorByHost.has(host)) {
      const cached = anchorByHost.get(host);
      hostSel = cached.sel;
      hostTrail = cached.trail;
      if (!hostSel && cached.budget) anchorRefusedByBudget = true;
    } else {
      const budgetBefore = anchorRefusedByBudget;
      // naturalSelector reports its cause through shared state; the control's own cause is already
      // settled by the time we get here and must survive naming the host.
      const why = naturalWhy;
      const inconclusive = checkInconclusive;
      hostSel = naturalSelector(host);
      // A host with an identity of its own is named by it or not at all: marking one whose identity
      // could not be verified would hand out a handle on the one page where uniqueness cannot be
      // checked. A host with no identity is marked in the light DOM like any other control there --
      // mintOn refuses to write inside a root by construction, so a host that is itself
      // component-hosted is anchored through its own host instead.
      if (!hostSel && naturalWhy === 'anonymous') {
        // A host that is itself a listed control already carries this walk's marker; reuse it
        // rather than re-entering mintOn, which would count the same marker twice.
        const prior = mintedOn.find((r) => r.el === host);
        if (prior) {
          hostSel = attr('data-tv3', prior.m);
          // The controls anchored on it are bound to a record of their own so losing the marker
          // drops them too; the marker itself is counted by the host's record, not again here.
          anchorsMinted.push({ rec: null, el: host, m: prior.m, fresh: false, shared: true });
        }
      }
      if (!hostSel && naturalWhy === 'anonymous') {
        const writtenBefore = markersWritten;
        hostSel = mintOn(host);
        if (hostSel) {
          let m = null;
          try { m = host.getAttribute('data-tv3'); } catch (e) { m = null; }
          anchorsMinted.push({ rec: null, el: host, m: m, fresh: markersWritten > writtenBefore });
        }
      }
      // A host that could not be marked (it lives in a root) or whose own id is reused by a sibling
      // instance is anchored through ITS host in turn, which can scope either.
      if (hostSel) hostTrail = [{ sel: hostSel, target: host }];
      if (!hostSel && (naturalWhy === 'anonymous' || naturalWhy === 'duplicated')) {
        const outer = hostOf(host);
        if (outer && outer !== host) {
          hostSel = hostAnchored(host, outer, depth + 1);
          if (hostSel) hostTrail = lastAnchor ? lastAnchor.trail : [];
        }
      }
      naturalWhy = why;
      checkInconclusive = inconclusive;
      // A refusal reached through the recursion may be the depth bound, which a control whose own
      // host this is would not hit; only a top-level or successful answer is worth remembering. A
      // refusal that was a budget stays a budget on every later hit.
      if (hostSel || depth === 0) anchorByHost.set(host, { sel: hostSel, trail: hostTrail, budget: !hostSel && anchorRefusedByBudget && !budgetBefore });
    }
    if (!hostSel) return null;
    // Naming the host may have written to it, and a component can re-render its root on any
    // attribute change: the tail was verified before that write, so it is verified again after,
    // or a replacement control would inherit this one's label and state.
    if (!scopedResolvesTo(host, ctrl, el)) return null;
    const sel = hostSel + ' ' + ctrl;
    if (sel.length > _ANCHORED_MAX_LEN) { anchorRefusedByBudget = true; return null; }
    // The whole chain that produced the selector, each link verified where it was taken, so the
    // record can be re-validated link by link: the tail under its host, and the host by its own name.
    lastAnchor = { sel: sel, trail: [{ scope: host, ctrl: ctrl, target: el }].concat(hostTrail) };
    return sel;
  };
  // The caption of a component's control is usually slotted from the host's light DOM, so the
  // control's own innerText is empty. Read the slot's assigned content first; when the control is
  // the only one in its root, the host's composed text is that control's caption.
  const slottedText = (el, host) => {
    let t = '';
    let slots;
    try { slots = el.querySelectorAll('slot'); } catch (e) { slots = []; }
    for (const sl of slots) {
      let nodes;
      try { nodes = sl.assignedNodes({ flatten: true }); } catch (e) { continue; }
      for (const n of nodes) t += ' ' + (n.nodeType === 1 ? (n.innerText || '') : (n.textContent || ''));
    }
    t = t.replace(/\s+/g, ' ').trim();
    if (t) return t;
    let root = null;
    try { root = Node.prototype.getRootNode.call(el); } catch (e) { return ''; }
    if (!root || root.nodeType !== 11) return '';
    let peers;
    try { peers = root.querySelectorAll(q); } catch (e) { return ''; }
    if (peers.length !== 1 || peers[0] !== el) return '';
    // Only a host whose light DOM is bare text is a caption; a card slotting headings and a body
    // beside its one icon button would otherwise hand that button the whole card as its name.
    let textOnly = true;
    try { for (const n of host.childNodes) { if (n.nodeType !== 3) { textOnly = false; break; } } } catch (e) { return ''; }
    if (!textOnly) return '';
    return String(host.innerText || '').replace(/\s+/g, ' ').trim().slice(0, _RETAIN_WIDTH);
  };
  // Controls inside a component that we could not name, split by CAUSE: these need different
  // fixes, and one merged tally would send the follow-up after the wrong one.
  //   anonymous    -- no id/name/data-testid at all
  //   duplicated   -- has one, but shadow encapsulation lets every instance reuse it, so the
  //                   cross-root count is >1 and no unscoped selector can single this one out
  //   unverifiable -- has one, but a root threw, so the count could not be taken
  //   unsafe       -- has one carrying a character that could forge a payload line
  // A duplicated ID is recovered by anchoring on the host (`#host #ctrl`), which is the shape a
  // design system produces; a name or testid reused across instances is not, and neither is the rest.
  // Records named by a marker we wrote, re-checked after the walk: a later element can mutate an
  // earlier one, and an element's own attributeChangedCallback can move our marker onto a peer.
  // Registered before the record is built, so a throw between the two still reaches that check.
  const mintedOn = [];
  const elOfRec = new Map();
  // What a record reported that would change its MEANING: properties, which no MutationObserver
  // records, the ARIA state attributes, and the naming attributes. Text is not fingerprinted -- it
  // is witnessed and answered by re-resolving the record, so a countdown that rewrites its own
  // caption keeps its listing while an aria-label rewritten to another action does not.
  const stampOfRec = new Map();
  const fingerprint = (el) => {
    try {
      return [
        // Sliced at the width the record retains: a change in any byte the rendered line depends on
        // (its masking reads the whole retained value) must invalidate the record.
        el.checked === true, el.type === 'password' ? '' : String(el.value || '').slice(0, _RETAIN_WIDTH), el.disabled === true,
        el.getAttribute('aria-checked'), el.getAttribute('aria-selected'), el.getAttribute('aria-pressed'), el.getAttribute('aria-expanded'),
        el.getAttribute('aria-valuenow'),
        el.getAttribute('aria-label'), el.getAttribute('aria-labelledby'), el.getAttribute('title'), el.getAttribute('placeholder'),
        el.getAttribute('aria-disabled'), el.readOnly === true, el.required === true, el.hidden === true, el.getAttribute('aria-hidden'),
      ].join('\u0001');
    } catch (e) { return null; }
  };
  // Hosts marked to anchor component controls. `anchorsMinted` collects the hosts one hostAnchored
  // call marked; `anchorRecords` keeps every such record with the controls it anchors, so losing
  // the host's marker after the walk drops each of those controls, not the host.
  const anchorsMinted = [];
  const anchorRecords = [];
  let unnamedAnonymous = 0;
  let unnamedBudget = 0;
  let unnamedDuplicated = 0;
  let unnamedUnverifiable = 0;
  let unnamedUnsafe = 0;
  let i = 0;
  let dropped = 0;
  // Two counters, deliberately: hiddenKept bounds the retention work and is spent the moment a
  // control passes the styled-proxy gate, while hiddenListed is what the digest note claims. They
  // diverge whenever a retained control is dropped later for having no selector that names it.
  let hiddenKept = 0;
  let hiddenListed = 0;
  // Candidates the visibility gates below drop. Those drops are silent, so a page whose whole app
  // shell is behind a boot gate renders exactly like an empty one; this is what tells the two apart.
  let hiddenDropped = 0;
  let phantomDropped = 0;
  let truncated = 0;
  let truncatedInComponents = 0;
  let lastGroup = '';
  let groupTotal = 0;
  const _PHANTOM_TEXT_TYPES = /^(?:text|search|email|tel|url|number|password|date|datetime-local|month|week|time)$/;
  // Our own witness for the walk: every marker write can run page code, synchronously or through
  // the page's own MutationObservers after we yield. Anything it changed is re-validated below;
  // an unchanged page pays nothing beyond the connection check.
  let _witness = null;
  const _witnessed = [];
  try {
    // Delivered records are consumed by the callback, so they are kept here and joined with
    // whatever is still queued when the walk asks.
    _witness = new MutationObserver((recs) => { for (const m of recs) _witnessed.push(m); });
    const opts = { subtree: true, childList: true, attributes: true, characterData: true };
    for (const root of allRoots) { try { _witness.observe(root, opts); } catch (e) {} }
  } catch (e) { _witness = null; }
  // v1's hasHorizontallyScrolledAncestor (domUtils.js): a scrolled overflow-x container keeps its
  // off-window columns on the page, so an off-canvas center inside one must not drop the control.
  const _hScrolledAncestor = (node) => {
    // Climb via the prototype getter, not node.parentElement: a <form> exposes named controls as own
    // properties, so <input name="parentElement"> makes form.parentElement that input -- a
    // form<->input 2-cycle that would loop this walk forever and hang the whole page.evaluate.
    for (let p = _parentOf.call(node); p; p = _parentOf.call(p)) {
      // scrollLeft via the prototype getter too: a <form> with <input name="scrollLeft"> would
      // otherwise shadow it with an always-truthy element and fake a scrolled ancestor.
      if (_scrollLeftOf.call(p)) {
        const ox = window.getComputedStyle(p).overflowX;
        if (ox === 'auto' || ox === 'scroll') return true;
      }
    }
    return false;
  };
  // v1 isElementVisible (domUtils.js) force-marks a native form control inside an open shadow root
  // as visible even when CSS hides it: web-component libraries hide the native input via
  // visibility:hidden / off-canvas positioning behind a styled overlay the user actually clicks.
  // Mirror that carve-out so the two gates above do not drop such a control. A closed dropdown host
  // (aria-expanded="false") and a closed combobox-filter sibling are the exceptions v1 still hides.
  const _shadowForcedVisible = (node) => {
    let root = null;
    try { root = Node.prototype.getRootNode.call(node); } catch (e) { return false; }
    if (!(root instanceof ShadowRoot)) return false;
    const tag = String(node.tagName || '').toLowerCase();
    if (tag !== 'input' && tag !== 'textarea' && tag !== 'select') return false;
    if (node.disabled) return false;
    if (tag === 'input' && String(node.type || '').toLowerCase() === 'hidden') return false;
    const host = root.host;
    if (host && host.getAttribute('aria-expanded') === 'false') return false;
    if (/(^|\s)combobox(\s|$)/i.test(node.getAttribute('role') || '')) {
      const prev = node.previousElementSibling;
      if (prev && prev.getAttribute('aria-expanded') === 'false') return false;
    }
    return true;
  };
  // Does a display:contents host actually render visible content? Mirrors v1 isElementVisible's
  // display:contents recursion (domUtils.js): a rendered child is a non-empty visible text node, a
  // visible on-canvas element, or a nested display:contents wrapper that itself renders. Depth-bounded.
  const _contentsRenders = (node, depth) => {
    if (depth > 4) return false;
    for (let c = _firstChildOf.call(node); c; c = _nextOf.call(c)) {
      const k = _nodeTypeOf.call(c);
      if (k === 3) {
        // v1 isVisibleTextNode: a text node renders iff its range has a positive, on-canvas box --
        // so font-size:0 / clipped text (non-empty but zero-area) does not count.
        if (_normText(_contentOf.call(c)).length === 0) continue;
        let tr = null;
        try { const rng = document.createRange(); rng.selectNode(c); tr = rng.getBoundingClientRect(); } catch (e) { tr = null; }
        if (tr && tr.width > 0 && tr.height > 0 && (tr.left + tr.width) / 2 + window.scrollX >= 0) return true;
        continue;
      }
      if (k !== 1) continue;
      const cs = window.getComputedStyle(c);
      if (cs.display === 'contents') { if (_contentsRenders(c, depth + 1)) return true; continue; }
      // visibility !== 'visible' catches collapse too, matching v1's isElementStyleVisibilityVisible.
      if (cs.visibility !== 'visible' || _unseen(c)) continue;
      const cr = _bcr.call(c);
      if ((cr.left + cr.width) / 2 + window.scrollX < 0 && !_hScrolledAncestor(c)) continue;
      return true;
    }
    return false;
  };
  for (let idx = 0; idx < els.length; idx++) {
   const el = els[idx].el;
   const host = els[idx].host;
   let mintedValue = null;
   let minted = null;
   const anchorRecs = [];
   lastAnchor = null;
   // A form exposes its named controls as its own properties, so <input name="tagName"> makes
   // el.tagName that input. Every read below can therefore be a clobbered non-function, and the
   // loop is inside page.evaluate: one throw costs the whole element list, not one element.
   try {
    const r = el.getBoundingClientRect();
    // A native form control inside an open shadow root is force-kept by v1 regardless of CSS/position
    // (web-component overlay pattern), so it skips the two new own-element gates. And v1 judges a
    // native checkbox/radio by its PARENT rather than the control itself (domUtils.js) -- the
    // visually-hidden consent/option pattern -- so for those the gates below are applied to the parent.
    const _elTag = String(el.tagName || '').toLowerCase();
    const _elType = String(el.type || '').toLowerCase();
    const ownGated = !_shadowForcedVisible(el);
    let gateEl = el, gr = r;
    if (_elTag === 'input' && (_elType === 'checkbox' || _elType === 'radio')) {
      const gp = _parentOf.call(el);
      if (gp) { gateEl = gp; gr = _bcr.call(gp); }
    }
    // Off-canvas gate, mirroring v1 isElementVisible (domUtils.js): an element whose horizontal
    // center sits left of the page is off-screen and not interactable, unless a horizontally
    // scrolled ancestor explains it. X only, never Y -- an overflow ancestor makes Y unreliable, so
    // a below-the-fold control (positive center-x) stays listed. Scoped to non-zero-rect elements
    // like v1 (whose center_x check is only reached for a non-zero rect), so the zero-size
    // skinned-proxy carve-out below still runs for an off-screen-positioned skinned control.
    const centerX = (gr.left + gr.width) / 2 + window.scrollX;
    if (ownGated && gr.width !== 0 && gr.height !== 0 && centerX < 0 && !_hScrolledAncestor(gateEl)) { hiddenDropped++; continue; }
    // v1's isElementStyleVisibilityVisible (domUtils.js) drops a control whose own computed
    // visibility is not 'visible'. Scoped to non-zero-rect elements so the zero-size skinned-proxy
    // carve-out below still runs; visibility is read per-element, so a visibility:visible child of a
    // hidden ancestor is kept. A native checkbox/radio judges the parent here instead of itself.
    if (ownGated && gr.width !== 0 && gr.height !== 0 && window.getComputedStyle(gateEl).visibility !== 'visible') { hiddenDropped++; continue; }
    let hidden = false;
    if (r.width === 0 || r.height === 0) {
      // Design systems skin a native SELECT/checkbox/radio/file input at zero size behind a styled
      // proxy widget. Keep only that narrow shape, and only with a visible label pointing at it —
      // a genuinely hidden button/link/text-input is still dropped, same as before.
      const tag = el.tagName;
      const type = String(el.type || '').toLowerCase();
      const skinnable = tag === 'SELECT' || (tag === 'INPUT' && (type === 'checkbox' || type === 'radio' || type === 'file'));
      if (skinnable && _visibleProxy(el)) {
        if (hiddenKept >= 40) { dropped++; continue; }
        hidden = true;
        hiddenKept++;
      } else if (!_unseen(el) && _contentsRenders(el, 0)) {
        // A display:contents host has a zero rect of its own but is not hidden -- its rendered
        // children carry it, matching v1's isElementVisible. _unseen's only non-rect-gated false
        // path is display:contents, so this reaches exactly that case; genuinely hidden zero-rect
        // controls (display:none/visibility:hidden/opacity:0/aria-hidden) still drop below. Keep it
        // only when it actually renders visible content, as v1's recursion does -- an empty,
        // all-hidden, or all-off-canvas host is a phantom.
      } else {
        hiddenDropped++;
        continue;
      }
    }
    // Tree-scoped for the same reason as _VISIBLE_PROXY_JS: the shadow walk feeds this loop
    // elements whose label id lives in their own root, not in the document.
    let lbRoot = null;
    try { lbRoot = Node.prototype.getRootNode.call(el); } catch (e) { lbRoot = null; }
    const byId = (attr) => {
      const id = el.getAttribute(attr);
      const n = id && lbRoot && lbRoot.getElementById ? lbRoot.getElementById(String(id).trim().split(/\s+/)[0]) : null;
      return n ? _labelText(n) : '';
    };
    // The name the page gives the control, placeholder excluded: a placeholder is a hint shared by
    // every field of a template, not a name, so it does not count as one below.
    let strongLabel = (el.getAttribute('aria-label') || '').trim();
    if (!strongLabel && el.labels) {
      for (const l of el.labels) { strongLabel = _labelText(l); if (strongLabel) break; }
    }
    if (!strongLabel) strongLabel = byId('aria-labelledby');
    let slottedName = false;
    if (!strongLabel) strongLabel = (el.innerText || '').trim();
    if (!strongLabel && host) { strongLabel = slottedText(el, host); slottedName = !!strongLabel; }
    // A text control the page itself hides from assistive tech, takes out of the tab order and
    // leaves unnamed is one no person can reach; a non-zero box does not make it a field.
    const isTextLike = el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && _PHANTOM_TEXT_TYPES.test(String(el.type || '').toLowerCase()));
    const unnamed = !strongLabel && !['placeholder', 'aria-labelledby', 'title'].some((a) => (el.getAttribute(a) || '').trim());
    if (isTextLike && el.getAttribute('aria-hidden') === 'true' && el.getAttribute('tabindex') === '-1' && unnamed) {
      phantomDropped++;
      continue;
    }
    let selector = naturalSelector(el);
    if (!selector) {
      // We do not write inside a shadow root. Setting a marker there is a mutation of the
      // component's own subtree, and every mechanism that wrote one and then tried to manage the
      // consequences failed: the mark provokes the re-render that destroys it, and because the mark
      // IS the handle we hand out it cannot move off-DOM. Verifying it needed a wait, every clock
      // belongs to the page, and a fixed wait was accurate under 50 ms and silently wrong past it.
      // Worse, a marker that churns every observe makes the payload differ every turn, which
      // defeats the loop's perception-stall terminator -- so the page burned the whole budget where
      // the base engine terminated cleanly. Not writing restores that behavior exactly. A control
      // with an id, name or data-testid of its own is unaffected, which is the ordinary case.
      if (host) {
        anchorsMinted.length = 0;
        anchorRefusedByBudget = false;
        lastAnchor = null;
        if (naturalWhy === 'duplicated' || naturalWhy === 'anonymous') selector = hostAnchored(el, host);
        // A host marked during this attempt is accounted for whether or not the attempt produced a
        // selector: a marker nobody is bound to still has to be counted, and re-checked, after the walk.
        for (const a of anchorsMinted) { a.ctrls = []; mintedOn.push(a); anchorRecords.push(a); }
        if (selector) {
          for (const a of anchorRecords) { if (selector.indexOf(attr('data-tv3', a.m)) === 0) { a.ctrls.push({ el: el, rec: null }); anchorRecs.push(a); } }
        } else {
          if (anchorRefusedByBudget) unnamedBudget++;
          else if (naturalWhy === 'duplicated') unnamedDuplicated++;
          else if (naturalWhy === 'unverifiable') unnamedUnverifiable++;
          else if (naturalWhy === 'unsafe') unnamedUnsafe++;
          else unnamedAnonymous++;
          continue;
        }
      } else {
        const writtenBefore = markersWritten;
        selector = mintOn(el);
        if (!selector) { dropped++; continue; }
        mintedValue = el.getAttribute('data-tv3');
        minted = { rec: null, el: el, m: mintedValue, fresh: markersWritten > writtenBefore };
        mintedOn.push(minted);
      }
    }
    // The placeholder ranks below every real name (strongLabel already starts with aria-label) and
    // travels separately as a hint: a format placeholder ('dd/mm/yyyy') is what makes the value typeable.
    const placeholder = (el.getAttribute('placeholder') || '').trim();
    let label = strongLabel || placeholder;
    if (!label) label = (el.type === 'password' ? '' : el.value || '').trim();
    if (!label) label = (el.getAttribute('title') || '').trim();
    const role = el.getAttribute('role');
    // el.type is only trustworthy where the UA normalises it to a known keyword. On INPUT, BUTTON
    // and SELECT it is a reflected enum; on <a>, <link>, <embed>, <object> and <source> it hands
    // back the raw attribute, so `type` there is a page-controlled string that reached the rendered
    // line -- and a MIME type is noise to the model anyway.
    const _typed = el.tagName === 'INPUT' || el.tagName === 'BUTTON' || el.tagName === 'SELECT';
    // Label, placeholder and value are capped generously here, not at their display widths: each is
    // masked for payload-minted signed URLs Python-side, which needs the WHOLE URL to match by
    // provenance before the display cap lands. A tighter cap here would truncate the URL past
    // recognition and leak its signing tail.
    const rec = { i, tag: el.tagName.toLowerCase(), type: (_typed && el.type) || null, selector, label: label.slice(0, _RETAIN_WIDTH) };
    if (placeholder && placeholder !== label) rec.placeholder = placeholder.slice(0, _RETAIN_WIDTH);
    if (hidden) rec.hidden = true;
    // A widget role is what the element IS -- a <div role="switch"> renders as a bare div otherwise,
    // and the model cannot tell it from decoration. The role travels with its state below, or it is
    // not worth surfacing: an on switch and an off one that read identically invite toggling the
    // wrong way and calling it success.
    // Only ever one of the roles we queried for. The page's raw attribute never reaches the
    // rendered line: it is page-controlled, and a newline in it would print a second, fabricated
    // element line for a selector that does not exist.
    if (role && _WIDGET_ROLES.indexOf(String(role)) !== -1) rec.role = String(role);
    if (el.tagName === 'SELECT') rec.options = Array.from(el.options).map((o) => o.value + '|' + o.text).slice(0, 60);
    if (el.type === 'password') { if (el.value) rec.value = '(hidden)'; } else if (el.value) rec.value = String(el.value).slice(0, _RETAIN_WIDTH);
    // ARIA defines switch as a checkbox variant carrying the same aria-checked, so it belongs here.
    if (el.type === 'checkbox' || el.type === 'radio') rec.checked = !!el.checked;
    else if (role === 'checkbox' || role === 'radio' || role === 'switch') {
      // Presence-gated like `selected` below: an absent aria-checked, or "mixed", is a state the
      // page never stated, and reporting checked=False for an ON switch is the exact wrong-way
      // toggle this enumeration exists to prevent.
      const ck = el.getAttribute('aria-checked');
      if (ck === 'true' || ck === 'false') rec.checked = ck === 'true';
    }
    const selected = el.getAttribute('aria-selected');
    if ((role === 'tab' || role === 'option') && (selected === 'true' || selected === 'false')) rec.selected = selected === 'true';
    if (role === 'spinbutton') {
      const now = el.getAttribute('aria-valuenow');
      if (now !== null && !rec.value) rec.value = String(now).slice(0, _RETAIN_WIDTH);
    }
    if (el.getAttribute('aria-required') === 'true' || el.required) rec.required = true;
    const isChoice = el.type === 'checkbox' || el.type === 'radio' || role === 'checkbox' || role === 'radio';
    // Read .validity, never checkValidity(): that dispatches an 'invalid' event and perception must
    // not mutate the page. Checkbox/radio .value is the static attribute ("on"), so they are excluded.
    const ai = el.getAttribute('aria-invalid');
    if (ai && ai !== 'false') rec.invalid = true;
    // willValidate excludes readonly/disabled fields the agent cannot fix; password is excluded so
    // validationMessage (which can echo the typed value) never leaks it.
    else if (!isChoice && el.type !== 'password' && el.value && el.willValidate && !(el.form && el.form.noValidate) && el.validity && !el.validity.valid) {
      rec.invalid = (el.validationMessage || '').slice(0, _RETAIN_WIDTH) || true;
    }
    // Flag typeahead/autocomplete inputs so the model treats them as combobox fills instead of typing
    // raw text that never registers as a valid selection (type() also auto-commits them). See _IS_AUTOCOMPLETE_JS.
    if (_isAutocomplete(el)) rec.autocomplete = true;
    // Attach the question text for controls whose meaning lives in nearby non-interactive text
    // (radio/checkbox groups, fields named by nothing or only by a placeholder) so the agent can
    // answer without fetching raw HTML. Deduped against the previous element to keep grouped
    // options compact; capped per page so a long form cannot turn this into a second DOM dump.
    if (isChoice || strongLabel.length < 3) {
      // The description is the last rung: it is routinely a per-field hint ("This field is
      // required") shared by every field, which would name nothing and dedupe to nothing.
      // The record carries the text at the masking width; the budget, the dedupe and the
      // name-vs-description comparison all stay at the 200-char display width.
      const gtFull = (_groupText(el, isChoice) || byId('aria-describedby')).slice(0, _RETAIN_WIDTH);
      const gt = gtFull.slice(0, 200);
      // A slotted caption is compared at the 140 width it used to be stored at.
      const nameLength = slottedName ? Math.min(strongLabel.length, 140) : strongLabel.length;
      if (gt && gt.length > nameLength && gt !== lastGroup && groupTotal + gt.length <= _GROUP_TEXT_TOTAL_CAP) {
        rec.group = gtFull;
        lastGroup = gt;
        groupTotal += gt.length;
      }
    }
    const pressed = el.getAttribute('aria-pressed');
    if (pressed === 'true' || pressed === 'false') rec.pressed = pressed === 'true';
    if (minted !== null) minted.rec = rec;
    for (const a of anchorRecs) { for (const c of a.ctrls) { if (c.el === el) c.rec = rec; } }
    if (hidden) hiddenListed++;
    // A submit or button input is named by its caption, and a caption is what a refusal beside it
    // repeats; a field's own control is the only thing a wrapper holds.
    const captioned = rec.tag === 'input' && /^(?:submit|button|reset|image)$/.test(rec.type || '');
    if ((rec.tag === 'input' && !captioned) || rec.tag === 'select' || rec.tag === 'textarea') labelOfControl.set(el, rec.label.slice(0, 140).replace(/\s+/g, ' ').trim());
    out.push(rec);
    elOfRec.set(rec, el);
    stampOfRec.set(rec, { fp: fingerprint(el), anchor: lastAnchor && lastAnchor.sel === selector ? lastAnchor : null });
    if (++i > 250) {
      // Count what the budget actually cost, not what is left in the array: a zero-size match would
      // have been skipped anyway, and counting it overstates the loss on any page carrying a hidden
      // dialog. Shadow matches are tallied separately because they are appended last and are
      // therefore the first thing the budget starves.
      for (let k = idx + 1; k < els.length; k++) {
        try {
          const r2 = els[k].el.getBoundingClientRect();
          if (r2.width === 0 || r2.height === 0) continue;
          truncated++;
          if (els[k].host) truncatedInComponents++;
        } catch (e) { /* unreadable: not a control we could have listed either */ }
      }
      break;
    }
   } catch (e) { dropped++; continue; }
  }
  // Let the page's own MutationObservers deliver (they are queued, not synchronous), then check
  // marker ownership -- a callback can move a marker onto a peer -- and ask the witness what changed. Our marker writes are our own; anything else means a record may describe
  // an element that was replaced, mutated in place, or re-identified, so every record is re-resolved
  // to the element it was built for and dropped if it no longer denotes exactly that element.
  // `await null` yields through the intrinsic promise machinery: the page cannot replace it the way
  // it can replace setTimeout, and every observer notification queued during the walk is ahead of
  // this continuation in the microtask queue.
  // Bounded: a callback may defer its own work another turn, and each turn is answered by one
  // more yield; a page that keeps queueing forever is left to the witness, which records what it did.
  for (let turn = 0; turn < 16; turn++) await null;
  // A marker we wrote can be gone by the end of the walk: a component that mirrors attributes moves
  // it onto a peer, and the element we named is then addressed by a selector matching nothing. One
  // attribute read per named element, no re-query -- a natural selector cannot be invalidated this
  // way, so only minted ones are checked.
  for (const rem of mintedOn) {
    let still = null;
    try { still = rem.el.getAttribute('data-tv3'); } catch (e) { still = null; }
    // A record never built (the element threw mid-walk) was never handed out either.
    if (rem.ctrls) {
      const lost = still !== rem.m;
      for (const c of rem.ctrls) {
        // A later host's marking can re-render an earlier component, detaching a control that
        // passed its own check; its record would then describe a replacement the tail resolves to.
        let connected = false;
        try { connected = _isConnected.call(c.el); } catch (e) { connected = false; }
        if (!lost && connected) continue;
        const at = c.rec === null ? -1 : out.indexOf(c.rec);
        if (at !== -1) { out.splice(at, 1); labelOfControl.delete(c.el); dropped++; }
      }
      if (lost && !rem.shared) { if (rem.fresh) markersWritten--; else markersReused--; }
      continue;
    }
    if (rem.rec === null || still !== rem.m) {
      const at = rem.rec === null ? -1 : out.indexOf(rem.rec);
      if (at !== -1) { out.splice(at, 1); labelOfControl.delete(rem.el); dropped++; }
      if (rem.fresh) markersWritten--; else markersReused--;
    }
  }
  // Any marker write during the walk can have run page code that re-rendered an EARLIER record's
  // element, whatever named it: a record whose element is no longer connected describes a control
  // that no longer exists, while its selector may resolve to a replacement in a different state.
  let mutated = false;
  if (_witness) {
    try {
      for (const m of _witness.takeRecords()) _witnessed.push(m);
      _witness.disconnect();
      mutated = _witnessed.some((m) => !(m.type === 'attributes' && m.attributeName === 'data-tv3'));
      // A root attached during the walk was never observed, so a change inside it is invisible to
      // the witness; a root count that moved is treated as a change.
      if (!mutated) {
        let rootsNow = 0;
        const stack = [document];
        while (stack.length) {
          const r = stack.pop();
          rootsNow++;
          let kids;
          try { kids = r.querySelectorAll('*'); } catch (e) { mutated = true; break; }
          for (const k of kids) {
            let sr = null;
            try { sr = k.shadowRoot; } catch (e) { continue; }
            if (sr && sr.nodeType === 11) stack.push(sr);
          }
        }
        if (rootsNow !== allRoots.length) mutated = true;
      }
    } catch (e) { mutated = true; }
  }
  for (let k = out.length - 1; k >= 0; k--) {
    const rec = out[k];
    const el = elOfRec.get(rec);
    let connected = true;
    if (el) { try { connected = _isConnected.call(el); } catch (e) { connected = false; } }
    let ok = connected;
    const stamp = ok && el ? stampOfRec.get(rec) : null;
    // A property write (checked, value) leaves no mutation record, so the fingerprint is always
    // compared; re-resolving the selector is paid only when the witness saw the tree change.
    if (ok && stamp && stamp.fp !== fingerprint(el)) ok = false;
    if (ok && stamp && mutated) {
      if (stamp.anchor) {
        for (const link of stamp.anchor.trail) {
          if (link.ctrl) { if (!scopedResolvesTo(link.scope, link.ctrl, link.target)) { ok = false; break; } }
          else { checkInconclusive = false; if (!resolvesTo(link.sel, link.target) && !checkInconclusive) { ok = false; break; } }
        }
      } else { checkInconclusive = false; ok = resolvesTo(rec.selector, el) || checkInconclusive; }
    }
    if (!ok) { labelOfControl.delete(el); out.splice(k, 1); dropped++; }
  }
  // Page-text digest: outcome states (submission confirmations, rejection banners, validation
  // summaries) live in non-interactive nodes the element list can never carry. Three sources in
  // priority order — ARIA status channels (uncapped within the 900 total), class/id-named message
  // blocks (600, or 300 past what ARIA spent, whichever is larger, still inside the 900), then
  // headings (whatever the 900 leaves) — never a body-text
  // dump, so the digest stays bounded and can't regrow the context that transcript compaction
  // bounds. All three carry page-controlled text at the same trust level as element labels.
  const texts = [];
  // The digest is deduped and budgeted at its 300-char display width, and `text` carries exactly that.
  // An entry cut by that width also travels whole (at the masking width) in `textFull`, so Python can
  // mask a minted URL in it before capping the line.
  const fullText = new Map();
  let textTotal = 0;
  let textFull = false;
  let textDropped = 0;
  // `limit` is a cumulative reservation: a channel stops at its limit so the channels after it keep
  // a floor of the 900 total instead of being starved by whichever channel ran first.
  const pushText = (t, limit = 900) => {
    const full = (t || '').replace(/\s+/g, ' ').trim().slice(0, _RETAIN_WIDTH);
    t = full.slice(0, 300);
    if (!t) return;
    // Containment dedupe, richer message wins: an alert's text re-surfaces inside its heading's
    // parent text, and a terse early entry ("Saved") must not suppress a later superset
    // ("Saved — confirmation #A1B2") — supersets REPLACE their contained entries.
    if (texts.some((s) => s.includes(t))) return;
    const kept = texts.filter((s) => !t.includes(s));
    const keptTotal = kept.reduce((total, s) => total + s.length, 0);
    if (keptTotal + t.length > limit) { textFull = textFull || limit >= 900; textDropped++; return; }
    texts.length = 0; texts.push(...kept, t); textTotal = keptTotal + t.length;
    fullText.set(t, full);
  };
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  // Isolated: a hostile page's throwing accessor (fingerprinting scripts poison innerText and
  // friends) must degrade to "no digest", never take element perception down with it.
  try {
    // ~= matches ARIA fallback role lists like role="alert status"; = would silently skip them.
    for (const root of allRoots) {
      if (textFull) break;
      for (const el of root.querySelectorAll('[role~=alert],[role~=status],[aria-live=polite],[aria-live=assertive],output')) {
        if (textFull) break;
        if (visible(el)) pushText(el.innerText);
      }
    }
    // Rejection messages most sites render as a plain styled block with no ARIA; class/id naming is
    // the only signal. A block with several form fields is a container (skipped unless short); one
    // with at most one field up to the digest total is a message; longer is prose.
    let messageCandidates = 0;
    // A prior channel (ARIA) can already occupy the shared total before this loop starts; spend is
    // measured relative to that starting point so this channel still gets its own floor.
    const blockStart = textTotal;
    const blockLimit = Math.min(900, Math.max(600, blockStart + 300));
    // Real validation errors live inside the form; cookie banners and alert dropdowns sit above it
    // in DOM order and must not spend the budget before the form's own error is seen.
    // Only "error" is matched against id: id-named chrome (#alert-count, #cookie-warning) is the
    // false-positive family this channel is most exposed to, and "error" is the one id that isn't.
    const msgSel = '[class*="error" i],[class*="invalid" i],[class*="alert" i],[class*="warning" i],[id*="error" i]';
    // A component renders its validation summary in its own shadow root, where a document query
    // cannot reach it -- the blindness already lifted for controls, on the channel that carries
    // refusal messages. Walked like the ARIA and heading channels above, so this asks the page no
    // new question, only the same one of more roots.
    // A bucket past its cap has to give something up, and a plain prefix gives up the end -- which
    // is where a page renders the outcome of a submission, after the fields it is about. The last
    // 50 are kept alongside the first 200 rather than instead of part of them, so nothing a 200
    // prefix read is given up, and the loss moves into the middle of the walk. The loop below reads
    // up to 250 for the same reason: one full bucket must fit. The other bucket's tail is reached
    // only when the first bucket is small, which is the same priority the concat order states.
    const formMsgs = { head: [], tail: [] };
    const otherMsgs = { head: [], tail: [] };
    const hold = (bucket, item) => {
      if (bucket.head.length < 200) { bucket.head.push(item); return; }
      bucket.tail.push(item);
      if (bucket.tail.length > 50) bucket.tail.shift();
    };
    for (const root of allRoots) {
      // nodeType 11 first: Document has no `host`, so `root.host` would hit the HTML named-property
      // getter and <form name="host"> would supply one.
      const host = root.nodeType === 11 ? root.host || null : null;
      // One component that refuses this query costs its own root, not the digest: an uncaught throw
      // reaches the outer catch and empties every channel. A root that throws for every query is
      // already disclosed by the marker gather; one that throws only for this selector is not, and
      // is not defended here.
      let cands;
      try { cands = root.querySelectorAll(msgSel); } catch (e) { continue; }
      // The iteration is inside the try because a root can hand back a non-iterable instead of
      // throwing, which is the same attack one line later.
      try {
        for (const el of cands) {
          // Per element, because reading `closest` off one is a page-controlled call: a form
          // exposes its named controls over its own methods, so <input name="closest"> turns it
          // into a throw. Uncaught it reaches the digest-wide catch, and an emptied digest is
          // indistinguishable from a page that rendered no messages at all.
          try {
            // Being inside a form is what separates a validation message from page chrome, and it is
            // a structural fact rather than a guess about which roots tend to hold content. Ranking
            // component blocks above light-DOM ones instead would bury a page's own banner under the
            // cookie-consent and chat widgets that also ship as components.
            hold((el.closest('form') || inFormRoots.has(root)) ? formMsgs : otherMsgs, { el: el, host: host });
          } catch (e) { continue; }
        }
      } catch (e) { continue; }
    }
    // A per-field state wrapper (`field--has-error`, `field--no-error`) matches this selector and its
    // text is just the control's own name, so it spends the channel's budget on what the element list
    // already carries -- enough of them and the page's real message never fits. Read off the records
    // already built, so recognising them asks the page nothing.
    const listedLabels = new Set();
    for (const r of out) {
      // Sliced to the label's display width: the messages below are compared at that width.
      const lb = (r.label || '').slice(0, 140).replace(/\s+/g, ' ').trim();
      if (lb) listedLabels.add(lb);
    }
    // Stricter than visible(): this channel's selector is broad and site chrome is routinely present
    // but hidden, whereas an ARIA live region styled invisible is not a pattern worth the extra reads.
    const visibleText = (el) => {
      if (!visible(el) || el.closest('[aria-hidden="true"]')) return false;
      const r = el.getBoundingClientRect();
      if (r.right <= 0 || r.bottom <= 0) return false;
      const cs = getComputedStyle(el);
      return cs.visibility !== 'hidden' && cs.opacity !== '0';
    };
    // The suppression above compares byte for byte, so a wrapper that renders its control's label
    // beside a required-field marker does not match it and spends the budget on a name the element
    // list already carries. Comparing with the decoration stripped is only safe as an ORDERING:
    // dropping on it would also swallow a real message that is nothing but a listed label and
    // punctuation ("Payment declined!" beside a "Payment declined" button), and a dropped banner
    // reads exactly like a page that never rendered one. So a near-match is offered to the budget
    // after every message the page did not build out of a label, and what the budget then does with
    // it -- take it, fold it into an entry that already holds it, or count it as dropped -- is what
    // it would have done at its place in the walk.
    const decoration = /^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu;
    // A word marker ("(required)", "optional") is letters, so stripping edge punctuation leaves
    // it in place and the wrapper still reads as a message. Listed labels include button captions,
    // so "Sign in required" beside a "Sign in" button would read as a wrapper too: the marker is
    // decoration only at an edge, and only on a block that holds the very control the rest names.
    const wordMarker = /^(?:required|optional)(?:[^\p{L}\p{N}]+|$)|(?:^|[^\p{L}\p{N}]+)(?:required|optional)$/giu;
    const nearLabel = (t, src) => {
      const trimmed = t.replace(decoration, '');
      if (listedLabels.has(trimmed.slice(0, 140))) return true;
      const unmarked = trimmed.replace(wordMarker, '').replace(decoration, '');
      if (!unmarked || unmarked === trimmed) return false;
      const key = unmarked.slice(0, 140);
      // A host can shadow querySelectorAll; a block that cannot be asked is not known to be a wrapper.
      try {
        for (const c of src.querySelectorAll('input,select,textarea')) {
          if (labelOfControl.get(c) === key) return true;
        }
      } catch (e) { return false; }
      return false;
    };
    const deferred = [];
    const takeCand = (cand, mayDefer) => {
      const el = cand.el;
      if (!visibleText(el)) return;
      // A component's message block is `<div class="alert"><slot></slot></div>`: the words are
      // slotted from the host's light DOM, so the block's own innerText is empty and the host
      // carries them. Same fallback the heading channel below uses, and the field count comes
      // from whichever node supplied the text.
      let t = (el.innerText || '').replace(/\s+/g, ' ').trim();
      let src = el;
      if (!t && cand.host) {
        t = (cand.host.innerText || '').replace(/\s+/g, ' ').trim();
        src = cand.host;
      }
      if (!t) return;
      // Compared at the width labels are stored at, so a truncated one still matches.
      if (listedLabels.has(t.slice(0, 140))) return;
      if (mayDefer && nearLabel(t, src)) { deferred.push(cand); return; }
      if (t.length <= 300 || (t.length <= 900 && src.querySelectorAll('input,select,textarea').length < 2)) pushText(t, blockLimit);
    };
    for (const cand of formMsgs.head.concat(formMsgs.tail, otherMsgs.head, otherMsgs.tail)) {
      if (textFull || textTotal - blockStart >= 600 || ++messageCandidates > 250) break;
      // This selector set is broad, so one poisoned element degrades to "skip it", not to an
      // emptied digest (the outer catch is for the narrow ARIA channel).
      try { takeCand(cand, true); } catch (e) { continue; }
    }
    // Already counted against the candidate cap on the pass that deferred them, so this pass is
    // bounded by that same cap. Each is offered to pushText like any other entry, with no budget
    // short-circuit, so the dedupe, the length gate and the drop count apply exactly as they would
    // have at its place in the walk.
    for (const cand of deferred) {
      try { takeCand(cand, false); } catch (e) { continue; }
    }
    // role=heading alongside h1-h3: a component's heading is a custom element, so its tag name
    // carries no signal and only the ARIA role does.
    for (const root of allRoots) {
      if (textFull) break;
      // nodeType 11 first: Document has no `host`, so `root.host` would hit the HTML
      // named-property getter and <form name="host"> would supply one.
      const host = root.nodeType === 11 ? root.host || null : null;
      for (const h of root.querySelectorAll('h1,h2,h3,[role=heading]')) {
        if (textFull) break;
        if (!visible(h)) continue;
        // A component heading is `<h2><slot></slot></h2>`: the slotted text belongs to the host's
        // light DOM, so the heading's own innerText is empty and the host carries the words.
        let ht = (h.innerText || '').replace(/\s+/g, ' ').trim();
        if (!ht && host) ht = (host.innerText || '').replace(/\s+/g, ' ').trim();
        // A short parent is a banner/panel whose body text carries the message; a large parent would
        // drag in unrelated content, so the heading stands alone.
        const pt = h.parentElement ? (h.parentElement.innerText || '').replace(/\s+/g, ' ').trim() : '';
        pushText(pt && pt.length <= 300 ? pt : ht);
      }
    }
  } catch (e) { texts.length = 0; textDropped = 0; }
  // Cross-origin iframe PRESENCE: an anti-bot/captcha widget lives in one, and main-frame element
  // perception can never list its contents — record host + signature so the model can see the gate
  // exists. Attributes only, never the frame's document (page.frames-based traversal was considered
  // and rejected: presence is the contract here, not cross-frame reach). Same visibility rule as
  // elements, so hidden tracking pixels stay out. Isolated like the digest above.
  // `failed` and `unread` are this channel's own bookkeeping, not a question put to the page: on the
  // section that reports gates, "found none" and "could not look" must not render as one sentence.
  const iframeInfo = { total: 0, inComponents: 0, entries: [], failed: false, unread: 0 };
  try {
    const sig = /captcha|turnstile|challenges\.cloudflare|arkoselabs|funcaptcha|datadome|perimeterx|verify you are human|security challenge/i;
    // A design system packages the widget inside its own shadow root, where a document query cannot
    // reach it. Walked like the ARIA, message and heading channels above, so this asks the page no
    // new question, only the same one of more roots.
    for (const root of allRoots) {
      // One root that refuses this query costs its own root, not the channel: an uncaught throw
      // reaches the outer catch and empties every entry, including main-document ones a
      // document-only scan reported fine. The iteration is inside the try because a root can hand
      // back a non-iterable instead of throwing, which is the same attack one line later.
      try {
        for (const f of root.querySelectorAll('iframe')) {
          // Walking more roots means reading more frames, so one poisoned frame inside a component
          // must not cost the roots already scanned.
          try {
            const r = f.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            // A frame with srcdoc renders the inline (same-origin) document; its src is a dead fallback.
            if (f.hasAttribute('srcdoc')) continue;
            const src = f.getAttribute('src') || '';
            let u;
            try { u = new URL(src, location.href); } catch (e) { continue; }
            if ((u.protocol !== 'http:' && u.protocol !== 'https:') || u.origin === location.origin) continue;
            const ttl = (f.getAttribute('title') || '').replace(/\s+/g, ' ').trim().slice(0, 80);
            const isCaptcha = sig.test(src + ' ' + ttl);
            // Counted once every throwable read has succeeded: incrementing earlier put a frame in
            // `total` and in `unread` at once, so the two summed past the page's real count.
            iframeInfo.total++;
            if (root !== document) iframeInfo.inComponents++;
            if (iframeInfo.entries.length < 8) {
              iframeInfo.entries.push({ host: u.host.slice(0, 80), title: ttl, captcha: isCaptcha });
            } else if (isCaptcha) {
              // Spending all 8 slots on ad embeds and dropping the one frame this channel exists to
              // report defeats the channel, so a gate displaces an embed; the cap and total hold.
              const at = iframeInfo.entries.findIndex((e) => !e.captcha);
              if (at !== -1) iframeInfo.entries[at] = { host: u.host.slice(0, 80), title: ttl, captcha: isCaptcha };
            }
          } catch (e) { iframeInfo.unread++; continue; }
        }
      } catch (e) { iframeInfo.unread++; continue; }
    }
  } catch (e) { iframeInfo.total = 0; iframeInfo.inComponents = 0; iframeInfo.entries.length = 0; iframeInfo.unread = 0; iframeInfo.failed = true; }

  return JSON.stringify({ url: location.href, title: document.title, text: texts, textFull: texts.map((t) => { const f = fullText.get(t); return f && f !== t ? f : null; }), textTruncated: textFull, textDropped: textDropped, iframes: iframeInfo, dropped: dropped, truncated: truncated, truncatedInComponents: truncatedInComponents, unnamedAnonymous: unnamedAnonymous, unnamedBudget: unnamedBudget, unnamedDuplicated: unnamedDuplicated, unnamedUnverifiable: unnamedUnverifiable, unnamedUnsafe: unnamedUnsafe, unreadableRoot: sawUnreadableRoot, undiscoveredRoots: undiscoveredRoots, rootCount: allRoots.length - 1, hiddenListed: hiddenListed, hiddenDropped: hiddenDropped, phantomDropped: phantomDropped, markersMinted: markersWritten, markersReused: markersReused, pageMutated: mutated, elements: out });
}
"""
)


def observe_js(retain_width: int = OBSERVE_RETAIN_WIDTH_MIN) -> str:
    return _OBSERVE_JS_TEMPLATE.replace("__OBSERVE_RETAIN_WIDTH__", str(int(retain_width)), 1)


_OBSERVE_JS = observe_js()


def _menu_mark_parts(options: list[dict[str, Any]], cap: int) -> list[str]:
    parts = []
    for o in (options or [])[:cap]:
        # option texts are page-controlled and land in the LLM transcript — same sanitation as filenames
        text = _DOWNLOAD_NOTICE_SANITIZE_RE.sub("", str(o.get("text", "")))
        parts.append(f'[data-tv3-menu="{o.get("n")}"] {text!r}')
    return parts


def _menu_open_note(found: dict[str, Any], selector: str, *, clicked_row: bool = False) -> str:
    count = int(found.get("count") or 0)
    parts = _menu_mark_parts(found.get("options") or [], 15)
    overflow = f" (+{count - len(parts)} more — re-observe for the full list)" if count > len(parts) else ""
    # Naming the raw selector would contradict the next sentence when the caller IS a menu row:
    # this note has just renumbered every data-tv3-menu, so the selector clicked to get here is one
    # of the ones it is about to declare stale.
    closer = "the row you just clicked" if clicked_row else selector
    return (
        f"This click opened a menu of {count} options: {'; '.join(parts)}{overflow}. To select one, click "
        f'its [data-tv3-menu="N"] selector NOW — clicking {closer} again or elsewhere closes the menu '
        "and destroys these options. These numbers are freshly assigned: any data-tv3-menu selector "
        "from an earlier result now points at a different row or at nothing."
    )


async def _categories_note(page: Any, selector: str) -> str | None:
    # Enrichment for the typeahead no-match path only: never lets the classifier's own failure become
    # the tool's failure, since a crash here would replace a real (if unhelpful) error with a worse one.
    try:
        found = await page.evaluate(_FIND_CATEGORIES_JS, {"field": selector})
    except Exception:
        return None
    if not found or not found.get("count"):
        return None
    items = "; ".join(_menu_mark_parts(found.get("categories") or [], 8))
    return (
        f"Some rows near this field carry an expand affordance and may be categories whose options are "
        f"nested rather than shown in the flat list: {items}. If one could contain your value, click its "
        '[data-tv3-menu="N"] selector to reveal its options, then re-observe to confirm what the click '
        "did before relying on it. These numbers are freshly assigned and change whenever the list "
        "re-renders: act on the newest data-tv3-menu list, not an earlier one."
    )


def _spec(
    name: str, description: str, params: dict[str, Any], handler: Callable[[dict[str, Any]], Awaitable[ToolResult]]
) -> ToolSpec:
    return ToolSpec(name=name, description=description, parameters=params, handler=handler)


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


# Mirror v1's default inter_action_delay (get_wait_time default 0.5 → random.uniform(base, 2*base)).
# v3's tool factory has no task/workflow context to thread the org-tunable wait_config, so the
# default constant is used; widen the factory only if per-org tuning is later shown to matter.
_UPLOAD_SUBMIT_DELAY_BASE_S = 0.5


async def _settle_after_upload(page: Any) -> None:
    """Let the page finish processing a just-uploaded file before the next action runs.

    v1 already settles after every upload; v3's tool loop can otherwise dispatch the upload and
    the next action back-to-back in one turn, before upload UI (spinner/progress/XHR) has mounted.
    Reuses v1's settle (`_wait_for_upload_processing`), but best-effort: v1 lets an unclassified
    settle error propagate, whereas here the upload has already succeeded, so a settle failure is
    logged and swallowed rather than turned into a tool error.
    """
    from skyvern.webeye.actions.handler import _wait_for_upload_processing

    try:
        # engine_selection is intentionally omitted (v3's tool factory has no engine context); its
        # only effect is error classification inside the settle, and the catch-all below tolerates
        # any settle error regardless.
        await _wait_for_upload_processing(page)
    except Exception:
        LOG.info("post-upload settle failed, continuing", exc_info=True)


async def _upload_submit_delay() -> None:
    """Small randomized delay after an upload, mirroring v1's per-action inter_action_delay default,
    so the upload and the following action are not dispatched in the same instant."""
    await asyncio.sleep(random.uniform(_UPLOAD_SUBMIT_DELAY_BASE_S, _UPLOAD_SUBMIT_DELAY_BASE_S * 2))


# A genuine file upload dispatches at least one of these: the API call that mints the upload handle
# and/or the write to storage. resource_type is limited to xhr/fetch so page analytics pings, image
# beacons, navigations, and static asset loads never register as upload activity.
_UPLOAD_ACTIVITY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_UPLOAD_ACTIVITY_RESOURCE_TYPES = frozenset({"xhr", "fetch"})


class _UploadActivityProbe:
    """Counts upload-like network dispatches during a file_upload: set_input_files populating the input
    at the Playwright layer does not prove the site registered the file (post-navigation the change
    handler may be unwired, so the site dispatches nothing). Counting request dispatch — not completion —
    is the earliest signal that the site reacted at all; a dispatched-but-failed upload still counts."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self._count = 0

    def _on_request(self, request: Any) -> None:
        try:
            if (
                request.method in _UPLOAD_ACTIVITY_METHODS
                and str(request.resource_type).lower() in _UPLOAD_ACTIVITY_RESOURCE_TYPES
            ):
                self._count += 1
        except Exception:
            pass

    def start(self) -> None:
        try:
            self._page.on("request", self._on_request)
        except Exception:
            LOG.info("taskv3 upload-activity probe could not attach", exc_info=True)

    def stop(self) -> None:
        try:
            self._page.remove_listener("request", self._on_request)
        except Exception:
            pass

    def saw_upload(self) -> bool:
        return self._count > 0


# Rendered text across the document and every open shadow root. Used as a before/after pair around
# set_input_files: a filename that was absent and is now present could only have been written by the
# site's own file-handling code, which is the one thing a silent no-op (or ambient network noise) can
# never produce.
_PAGE_TEXT_JS = (
    r"""(start) => {
  const _shadowRoots = """
    + _SHADOW_ROOTS_JS
    + r""";
  // Called on the page (start undefined) for the whole document, or on an element handle for that
  // element's subtree: an element's own innerText is light DOM only, so its shadow roots are walked
  // exactly as the document's are.
  const from = start || document;
  // _shadowRoots walks a root's DESCENDANTS for hosts, so an element start's own shadow root has to
  // be seeded explicitly; from the document every host is a descendant.
  const starts = [from];
  if (from !== document && from.shadowRoot && from.shadowRoot.nodeType === 11) starts.push(from.shadowRoot);
  let out = '';
  for (const root of starts.flatMap(_shadowRoots)) {
    try {
      // Rendered text only: textContent would count hidden nodes, <script> and <style>. A shadow
      // root has no innerText itself, so read each element child — but only rendered ones, since
      // innerText on an element that is not rendered (a <style>, a hidden chip) is its textContent.
      const tops = root === document ? [document.body] : root === from ? [from] : Array.from(root.children);
      // A top with no box of its own (display:contents - the usual :host pattern - or a bare wrapper)
      // is not unrendered; its children may be. Descend under the same filter instead of dropping it.
      const stack = tops.slice().reverse();
      while (stack.length) {
        const el = stack.pop();
        if (!el || typeof el.innerText !== 'string') continue;
        if (el.getClientRects && el.getClientRects().length > 0) { out += ' ' + el.innerText; continue; }
        const style = el.ownerDocument && el.ownerDocument.defaultView ? el.ownerDocument.defaultView.getComputedStyle(el) : null;
        if (style && style.display === 'contents') for (let i = el.children.length - 1; i >= 0; i--) stack.push(el.children[i]);
      }
    } catch (e) {}
  }
  return out;
}"""
)


# Words a site uses when it names a file it refused. A veto only: a spurious match turns a confirmation
# into a recoverable error, never the reverse; a rejection phrased outside this list is the known miss.
_UPLOAD_REJECTION_WORDS = re.compile(
    r"\b(error|invalid|reject\w*|unsupported|fail\w*|unsuccessful|exceed\w*|denied|blocked|declined|removed|"
    r"discarded|corrupt\w*|wrong|issue\w*|too\s+(large|big)|must be|unable|"
    r"(not|never|won['’]t|will not|do not)(\s+\w+){0,2}\s+(allow|support|accept|upload|permit|attach|save)\w*|"
    r"(can|could|would|is|was|do|did|does|has|have)\s?(n['’]?|['’])t|cannot|try(\s+\w+){0,2}\s+again)\b",
    re.IGNORECASE,
)
_FILENAME_MENTION_CHARS = 240


async def _page_rendered_text(target: Any) -> str | None:
    """Rendered text of a page, or of one element handle, across open shadow roots; None when unreadable."""
    try:
        text = await target.evaluate(_PAGE_TEXT_JS)
    except Exception:
        LOG.info("taskv3 page-text readback failed", exc_info=True)
        return None
    return text if isinstance(text, str) else None


def _mentions_filename(text: str, filename: str) -> bool:
    """Whole-token mention of the staged file's full name (the browser reports exactly this basename as
    File.name, so it is what a site renders). A name joined to more name characters ("old-cv.pdf",
    "cv.pdf.bak") is a different file, not this one."""
    name = os.path.basename(filename).strip()
    if not name:
        return False
    return re.search(r"(?<![\w.\-])" + re.escape(name) + r"(?![\w.\-])", text, re.IGNORECASE) is not None


def _newly_rendered_lines(before: str, after: str) -> list[str]:
    seen = {line.strip() for line in before.splitlines()}
    return [line.strip() for line in after.splitlines() if line.strip() and line.strip() not in seen]


async def _input_holds_file(el: Any) -> bool:
    """Playwright-layer readback that set_input_files populated the control — proves the file attached to
    the input element, not that the site registered it. Fail-open: an unreadable control must never turn a
    real upload into a false negative."""
    try:
        count = await el.evaluate("e => (e && e.files) ? e.files.length : 0")
        return bool(count) and int(count) > 0
    except Exception:
        LOG.info("taskv3 file-input populate readback failed, assuming populated", exc_info=True)
        return True


# Counts fields holding in-progress state a reload would discard, piercing shadow roots. Unlike the
# pre-submit form serializer it COUNTS file inputs (files.length > 0) — an attached file is exactly the
# progress the same-URL reload guard exists to protect — and it skips hidden fields (site-managed, always
# present) so their presence alone never trips the guard.
_FILLED_STATE_JS = (
    "(() => { const _q = " + _ROOT_QUERY_JS + "; let n = 0; for (const el of _q.all('input,textarea,select')) { "
    "const t = (el.type || '').toLowerCase(); "
    "if (t === 'hidden') continue; "
    "if (t === 'file') { if (el.files && el.files.length > 0) n++; continue; } "
    "if (t === 'checkbox' || t === 'radio') { if (el.checked) n++; continue; } "
    "if (el.value) n++; } return n; })()"
)


async def _count_filled_fields(page: Any) -> int:
    """How many fields hold state a reload would wipe (incl. an attached file). Fail-open to 0: a probe
    failure must never let this guard block a navigation."""
    try:
        return int(await page.evaluate(_FILLED_STATE_JS))
    except Exception:
        LOG.info("taskv3 filled-state probe failed, treating page as empty", exc_info=True)
        return 0


# A probe that reads the DOM from the page's own JS realm can be answered by the page: prototype
# methods and instance properties are both replaceable there, so a forged label earns a forced click
# through a real cover. These probes run in a per-page isolated world instead, a realm the page has
# no handle on, rebuilt whenever the document it was created against is gone.
_ISOLATED_WORLDS: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()


async def _isolated_world(page: Any, *, fresh: bool = False) -> tuple[Any, int] | None:
    state = _ISOLATED_WORLDS.get(page)
    if state is None:
        # The miss is cached too: a page with no CDP session (a non-Chromium engine) would otherwise
        # pay a failed handshake on every probe for the rest of the run.
        state = {"session": None, "context_id": None}
        _ISOLATED_WORLDS[page] = state
        try:
            state["session"] = await page.context.new_cdp_session(page)
        except Exception:
            LOG.debug("taskv3 probe isolation unavailable, falling back to the page realm", exc_info=True)
    if state["session"] is None:
        return None
    if fresh:
        state["context_id"] = None
    if state["context_id"] is None:
        try:
            state["context_id"] = await _create_isolated_world(state["session"])
        except Exception:
            # A session detached by a renderer swap (cross-process navigation) answers nothing ever
            # again; evict it (not the cached-miss `None`) so the next probe opens a new one.
            LOG.debug("taskv3 probe isolation session lost, dropping it", exc_info=True)
            try:
                await state["session"].detach()
            except Exception:
                pass
            _ISOLATED_WORLDS.pop(page, None)
            raise
    return state["session"], int(state["context_id"])


async def _create_isolated_world(session: Any) -> int:
    tree = await session.send("Page.getFrameTree")
    frame_id = tree["frameTree"]["frame"]["id"]
    # The protocol spells it "Univeral"; the typo is the wire name.
    world = await session.send(
        "Page.createIsolatedWorld",
        {"frameId": frame_id, "worldName": "tv3-probe", "grantUniveralAccess": False},
    )
    return int(world["executionContextId"])


async def _evaluate_isolated(page: Any, js: str, selector: str) -> Any | None:
    """Run `js` (an `(arg) => …` probe) against a pristine realm the page cannot patch. Returns None
    when no isolated world is available, which is the caller's signal to fall back. The realm resolves
    the selector itself: it shares the DOM with the page, so an element handed over through any
    DOM-visible marker is one the page can re-point at a decoy between the marking and the read."""
    expression = (
        "(() => { const arg = {sel: "
        + json.dumps(selector)
        + ", el: null, allowOwnLabel: true}; return ("
        + js
        + ")(arg); })()"
    )
    # One rebuild for a world its document took with it, one more for a session a renderer
    # swap detached (the first rebuild is what discovers and drops that session).
    for attempt in (0, 1, 2):
        try:
            world = await _isolated_world(page, fresh=attempt > 0)
        except Exception:
            continue
        if world is None:
            return None
        session, context_id = world
        try:
            result = await session.send(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "contextId": context_id,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
        except Exception:
            continue
        if not isinstance(result, dict) or result.get("exceptionDetails"):
            return None
        returned = result.get("result")
        return returned.get("value") if isinstance(returned, dict) else None
    return None


# The stamp a finder leaves on its matched row is a DOM attribute the page can move, so the node that
# carries it at click time is re-checked as the row that was matched -- same text, not a navigational
# row -- on a handle that pins its identity through the click.
_STAMPED_ROW_GUARD_JS = (
    r"""(el, want) => {"""
    + _PIERCED_QUERY_JS
    + _ROW_SEMANTICS_JS
    + r"""
  const norm = (v) => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const w = norm(want);
  if (!w || !el || !el.isConnected) return false;
  let text = '', aria = '';
  try { text = norm(el.innerText); aria = norm(el.getAttribute('aria-label')); } catch (e) { return false; }
  const same = text === w || aria === w || (w.length >= 80 && (text.startsWith(w) || aria.startsWith(w)));
  if (!same || isNavRow(el)) return false;
  // isNavRow is inert once a row declares role=option, so a hijack onto an option decoy wrapping a
  // link/submit needs the same wrapsDeparture check the declared-row tagger applies before promoting one.
  return !composedClosest(el, OPT_SEL) || !wrapsDeparture(el);
}"""
)


async def _click_stamped_row(page: Any, stamp: str, want: str, timeout: int) -> bool:
    handle = await page.query_selector(stamp)
    if handle is None:
        return False
    try:
        if not await handle.evaluate(_STAMPED_ROW_GUARD_JS, want):
            return False
        await handle.click(timeout=timeout)
    finally:
        try:
            await handle.dispose()
        except Exception:
            pass
    return True


async def _probe_evaluate(page: Any, js: str, selector: str, arg: dict[str, Any]) -> Any:
    """Isolated-world probe, falling back to the page's own realm with own-label granting disabled --
    a realm the page can patch must not be able to hand a label a hit-test bypass."""
    isolated = await _evaluate_isolated(page, js, selector)
    if isolated is not None:
        return isolated
    return await page.evaluate(js, {**arg, "allowOwnLabel": False})


def build_browser_tools(
    page_provider: PageProvider,
    *,
    downloads_dir: str | None = None,
    organization_id: str | None = None,
    resolve_typed_text: Callable[[str], Any] | None = None,
    opaque_refs: OpaqueUrlRefs | None = None,
    vision_enabled: bool = True,
    semantic_commit_stats: SemanticCommitStats | None = None,
) -> list[ToolSpec]:
    """Raw-browser tools that resolve their page from `page_provider` on every call.

    `vision_enabled` gates the on-demand `look` tool: it is offered only when the run's model can
    actually receive the screenshot it produces (a non-vision model drops it before the request), so
    the tool is never advertised to a model that cannot see its output."""

    def _mask_refs(text: str) -> str:
        # A signed payload URL masked to a token in the payload must not reappear verbatim through a
        # free-text emit surface (observe's url= line, get_html, a download error) and get retyped by
        # the model. Masking is by provenance: only URLs the payload masker minted are rewritten, so a
        # live-page URL the model reasons about is never touched. No refs (page-free) → identity.
        return opaque_refs.mask(text) if opaque_refs is not None else text

    def _observe_js() -> str:
        # Retain exactly what the masker can recognise past the widest display window.
        window = opaque_url_echo_window(opaque_refs.refs.values()) if opaque_refs is not None else 0
        return observe_js(max(OBSERVE_RETAIN_WIDTH_MIN, OBSERVE_FIELD_DISPLAY_MAX + window))

    def _resolve_text(text: str) -> str:
        # Workflow credential values reach the model only as secret placeholders; resolve them to the
        # real value at fill time (the same boundary the step engine uses). Fail open to the literal.
        if resolve_typed_text is None:
            return text
        try:
            resolved = resolve_typed_text(text)
        except Exception:
            LOG.warning("taskv3 typed-text resolution failed; typing the literal text", exc_info=True)
            return text
        return resolved if isinstance(resolved, str) else text

    # INVARIANT: holds at most one page, written only by the preflight wrapper immediately before
    # its handler runs and consumed by that handler's single _resolve_page call; the wrapper clears
    # it in a finally. Relies on the loop dispatching tool calls sequentially — a concurrent
    # dispatcher or a twice-resolving handler must replace this handoff, not reuse it.
    _prefetched_page: list[Any] = []

    # Per-run set-of-marks from the most recent look(): mark index -> {handle, tag, label}. A
    # fresh look replaces it (marks renumber), and act-by-mark resolves mark=N against it at act time.
    _look_manifest: dict[int, dict[str, Any]] = {}
    _act_seq = [0]
    # Unguessable by the page, so a planted data-tv3-act cannot be adopted as an element identity.
    _act_prefix = f"a{secrets.token_hex(4)}"
    _act_token_re = re.compile(re.escape(_act_prefix) + r"[0-9]+")
    # Opaque-id aliases, run-scoped and stable: the same emitted selector maps to the same alias for
    # the whole run, like opaque_url_ tokens, so the model never handles the raw identifier.
    _alias_for_selector: dict[str, str] = {}
    _selector_for_alias: dict[str, str] = {}

    def _alias_for(selector: str) -> str:
        if not _OPAQUE_ID_RUN_RE.search(selector):
            return selector
        alias = _alias_for_selector.get(selector)
        if alias is None:
            alias = f'[data-tv3-ref="{len(_alias_for_selector) + 1}"]'
            _alias_for_selector[selector] = alias
            _selector_for_alias[alias] = selector
        return alias

    def _alias_components(real: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for m in _SELECTOR_ID_COMPONENTS_RE.finditer(real):
            attr = "id" if m.group(3) else m.group(1)
            # Keyed by the DOM value, never the selector's spelling of it: `[id="a\"b"]` and the
            # markup's `id="a&quot;b"` are the same attribute, and only the value joins them.
            raw = m.group(4) if m.group(3) else _decode_css_escapes(m.group(2))
            if raw and _OPAQUE_ID_RUN_RE.search(raw):
                out.append((attr, raw))
        return out

    def _alias_owners() -> dict[tuple[str, str], set[str]]:
        owners: dict[tuple[str, str], set[str]] = {}
        for real, alias in _alias_for_selector.items():
            for component in _alias_components(real):
                owners.setdefault(component, set()).add(alias)
        return owners

    def _mask_aliases(
        text: str,
        markup: bool = False,
        own_alias: str | None = None,
        absent_alias: str | None = None,
        distinct_tags: bool = False,
    ) -> str:
        # data-tv3-ref is never a legitimate page attribute (only this layer writes it), so any
        # pre-existing copy is stripped up front — otherwise a page could spoof the owner loop below
        # into dropping a real handle instead of minting one. Scoped to start tags only: a model's own
        # selector echoed back verbatim in an error string (no markup, no `<`) is not touched.
        text = _map_start_tags(text, lambda tag, _start: _strip_page_refs(tag))
        # The emitted selector -> its alias only as a whole token (never inside an attribute value or a
        # longer identifier) and never in markup, where the rewritten attribute IS the handle; the
        # same raw id also sits in hrefs, style rules and prose, and rewriting those corrupts what the
        # model reads. Every spelling of every selector, longest first, so a host-anchored one is not
        # half-masked by its host's and a repr'd one is not missed for the spelling it is not.
        if not markup:
            tokens = [
                (spelling, alias) for real, alias in _alias_for_selector.items() for spelling in _token_spellings(real)
            ]
            for spelling, alias in sorted(tokens, key=lambda pair: (-len(pair[0]), pair[0])):
                if spelling in text:
                    text = re.sub(r"(?<![\w#.\-])(?<!=[\"'])" + re.escape(spelling) + r"(?![\w\-])", alias, text)
        # The identity-attribute rewrite runs INSIDE start tags only: an `id="<raw>"` sitting in a
        # script body, a CSS rule, a comment or a text node is page content, and rewriting it there
        # corrupts what get_html returns verbatim. The requested element's own tag is located by the
        # attribute it actually owns (not merely the first `<letter`) and matched by its offset.
        owners = _alias_owners()
        ambiguous = _ambiguous_owners(text, owners, absent_alias, distinct_tags=distinct_tags)
        own_span = _owned_start_tag_span(text, owners)
        own_start = own_span[0] if own_span is not None else None
        own_match = _ALIAS_SELECTOR_RE.match(own_alias) if own_alias is not None else None
        own_ref = own_match.group(1) if own_match is not None else None
        # A tag can carry two opaque identities (id + data-testid), each minted its own alias above;
        # every start tag is collapsed to at most one data-tv3-ref, the caller's own where it has it.
        return _map_start_tags(
            text,
            lambda tag, start: _dedupe_single_tag_refs(
                _mask_identity_attrs(tag, owners, own_alias if start == own_start else None, ambiguous),
                own_ref if start == own_start else None,
            ),
        )

    def _holds_owned_run(scoped: str) -> bool:
        """Keyed on the opaque run, not on the spellings the masking passes enumerate: a detector that
        shared their blind spot would call a spelling nobody modeled clean and re-raise it verbatim.
        Every aliased selector's own run counts, not only the runs of the identity components parsed
        out of it -- a selector shape that parses to no component still hands the model an alias;
        `scoped` must already be `_leak_check_text`ed."""
        if any(_text_holds_opaque_run(scoped, raw) for _attr, raw in _alias_owners()):
            return True
        return any(run in scoped for real in _alias_for_selector for run in _OPAQUE_ID_RUN_RE.findall(real))

    def _leaks_owned_raw(text: str) -> bool:
        return _holds_owned_run(_leak_check_text(text))

    def _scrub_owned_spellings(text: str) -> str:
        spellings = sorted(
            {spelling for _attr, raw in _alias_owners() for spelling in _raw_spellings(raw)},
            key=len,
            reverse=True,
        )
        for spelling in spellings:
            text = text.replace(spelling, f"[{_REDACTED_REF_ATTR}]")
        return text

    def _withheld_text(text: str, outcome: str) -> str:
        """Last resort for a message masking could not clean: every spelling of every owned raw is
        replaced outright, and a message that STILL names one is dropped rather than let through."""
        # The scrub reaches page attribute values too, which is harmless here because it runs only
        # once the gate has already fired on an occurrence masking owns; the completeness check below
        # scopes first, since scrubbing a value can leave markup no attribute scan can read.
        if _holds_owned_run(_scrub_owned_spellings(_leak_check_text(text))):
            return f"browser tool {outcome}; details withheld because they name a masked element"
        return _scrub_owned_spellings(text)

    def _withheld_error(text: str) -> RuntimeError:
        return RuntimeError(_withheld_text(text, "failed"))

    def _mask_exception_text(text: str, own_alias: str | None = None) -> str:
        # An error is not page content: whatever raw value survives the token/attribute masking
        # (Playwright's call log quotes the resolved locator and the target's outerHTML) is replaced
        # outright, so the transcript never sees the identifier the alias exists to hide.
        if own_alias is not None and not _names_resolved_target(text, _alias_owners()):
            # Only the "locator resolved to <...>" line is known to render the element this call
            # acted on; any other tag may be a sibling that merely shares the raw id, so redact.
            own_alias = None
        text = _mask_aliases(text, own_alias=own_alias, distinct_tags=True)
        # Playwright escapes a nested selector's quotes, so the exact-token pass above misses it;
        # replace the whole `#raw`/`tag[attr="raw"]` component before the bare-value fallback below.
        # A component is selector text, so only the CSS spellings can appear in it — the markup one
        # belongs to the outerHTML the call log renders, which the start-tag pass above already took.
        for (attr, raw), aliases in _alias_owners().items():
            if not _text_holds_selector(text, raw):
                continue
            alias = next(iter(aliases)) if len(aliases) == 1 else f"[{_REDACTED_REF_ATTR}]"
            alternatives = []
            for spelling in _selector_spellings(raw):
                alternatives.append(
                    r"(?:[A-Za-z][\w-]*)?\[" + re.escape(attr) + r'=\\?["\']' + re.escape(spelling) + r'\\?["\']\]'
                )
                if attr == "id":
                    alternatives.append(r"#" + re.escape(spelling) + r"(?![\w-])")
            component_re = re.compile("|".join(alternatives))

            def _replace_component(m: re.Match[str], alias: str = alias) -> str:
                return alias.replace('"', '\\"') if "\\" in m.group(0) else alias

            text = component_re.sub(_replace_component, text)
        by_spelling: dict[str, set[str]] = {}
        for (_attr, raw), aliases in _alias_owners().items():
            for spelling in _bare_value_spellings(raw):
                by_spelling.setdefault(spelling, set()).update(aliases)
        # Longest spelling first, and boundary-anchored: an id that is a literal prefix of another
        # aliased id (a common child-id convention, e.g. `X` / `X-listbox`) must not swallow the
        # longer one. A spelling more than one alias can name — a shared raw, or an opaque run two
        # aliased ids both embed — is redacted here even when own_alias is known: a bare, tagless
        # mention names no element, so it is not evidence of which one is being talked about.
        for spelling, aliases in sorted(by_spelling.items(), key=lambda kv: -len(kv[0])):
            if spelling not in text:
                continue
            replacement = next(iter(aliases)) if len(aliases) == 1 else f"[{_REDACTED_REF_ATTR}]"
            text = re.sub(r"(?<![\w-])" + re.escape(spelling) + r"(?![\w-])", replacement, text)
        return text

    def _with_alias_resolution(name: str, handler: ToolHandler) -> ToolHandler:
        markup = name == "get_html"

        async def wrapped(args: dict[str, Any]) -> ToolResult:
            selector = args.get("selector")
            alias_match = _ALIAS_SELECTOR_RE.match(selector) if isinstance(selector, str) else None
            own_alias: str | None = None
            if alias_match:
                own_alias = f'[data-tv3-ref="{alias_match.group(1)}"]'
                real = _selector_for_alias.get(own_alias)
                if real is None:
                    return ToolResult.error(
                        f"{alias_match.group(0).strip()} is not a selector from the latest observe — re-observe and "
                        "use a selector from the new observation"
                    )
                args = {**args, "selector": real}
            try:
                result = await handler(args)
            except Exception as exc:
                # Re-raised as the SAME type: a raise softened into ToolResult.error would read as a
                # tool outcome to the wrappers and the loop, not as the failure it is.
                if not _alias_for_selector:
                    raise
                masked_text = _mask_exception_text(str(exc), own_alias=own_alias)
                if _leaks_owned_raw(masked_text):
                    # Nothing the structured passes model reaches this occurrence (an id embedded in
                    # a longer token, a spelling they miss); scrub it, or say nothing at all.
                    raise _withheld_error(masked_text).with_traceback(exc.__traceback__) from None
                if masked_text == str(exc):
                    raise
                masked_exc: BaseException
                try:
                    masked_exc = type(exc)(masked_text)
                except Exception:
                    # A constructor that rejects a lone masked message (needs more args, validates what
                    # it is given): mutate in place instead, so the raise is still the original failure.
                    exc.args = (masked_text,)
                    masked_exc = exc
                if _leaks_owned_raw(str(masked_exc)):
                    # A custom __str__ can compose from attributes the masking never touched. The raw
                    # value must not reach the transcript, even at the cost of the exception's type.
                    raise _withheld_error(masked_text).with_traceback(exc.__traceback__) from None
                raise masked_exc.with_traceback(exc.__traceback__) from None
            if _alias_for_selector and isinstance(result.content, str):
                page_content = markup and result.status == "ok"
                # get_html's text format returns rendered text, which has no attribute a handle could
                # go on: a selector printed there is prose the whole-token pass owns, not markup where
                # the rewritten attribute would be the handle.
                is_markup = page_content and not (result.data or {}).get("rendered_text")
                if result.status != "ok":
                    # A failure result is prose, not page content, and reaches the model exactly as a
                    # raise does: it gets the same passes, including the ones a whole-token match
                    # misses (a selector quoted by repr or by Playwright's call log).
                    masked = _mask_exception_text(result.content, own_alias=own_alias)
                else:
                    # The caller's handle goes on the returned tag only when the handler reports it is
                    # the requested element's own outer HTML; inner HTML may open with a descendant
                    # that happens to share the raw id, and stamping the handle there aims the next
                    # action at the container instead.
                    own_tag_returned = is_markup and (result.data or {}).get("markup_scope") == "outer"
                    # Its own tag is absent from a container's inner HTML but the element still exists,
                    # so a tag here carrying its raw id is a descendant that merely shares it: redact,
                    # never relabel, however few aliases that raw has.
                    absent_alias = own_alias if is_markup and not own_tag_returned else None
                    masked = _mask_aliases(
                        result.content,
                        markup=is_markup,
                        own_alias=own_alias if own_tag_returned else None,
                        absent_alias=absent_alias,
                    )
                if not page_content and _leaks_owned_raw(masked):
                    # get_html's page content is exempt, markup and rendered text alike: a raw id in an
                    # href, a script, prose or visible text is content it returns on purpose. Elsewhere
                    # only the text is dropped, never the status — an outcome reported as its opposite
                    # sends the model to redo a committed side effect.
                    masked = _withheld_text(masked, "failed" if result.status != "ok" else "succeeded")
                if masked != result.content:
                    result = ToolResult(result.status, masked, result.data, result.screenshots)
            return result

        return wrapped

    _look_count = [0]  # per-run look() invocations, capped at _LOOK_MAX_PER_RUN
    # The (canonical URL, filled-field count) of the last same-URL reload the destructive-nav guard
    # refused. A repeat to that URL confirms intent and is allowed — but only if the at-risk state has
    # not GROWN since (else a file attached after the refusal would be wiped by a stale confirmation).
    _reload_confirm_pending: list[tuple[str, int] | None] = [None]
    # Canonical URLs recently touched by navigate (both endpoints of each hop, so maxlen=16 spans
    # the last ~8 hops — hops, not action rounds): a navigation landing back on one is a revisit,
    # not fresh-page progress, for the budget-extension evidence.
    _recent_nav_canonicals: deque[str] = deque(maxlen=16)

    # The page a handler actually acted on. A failure diagnosis must ask about THAT page, not about
    # whatever must_get_working_page resolves afterwards: it can switch to the newest valid tab, so a
    # tab that opened while the action was failing would be probed instead, and a hidden element with
    # the same name there would replace the real failure with a false one. One slot, not one per call,
    # because the loop dispatches one tool at a time; concurrent tool calls on a single build would
    # need this to become per-call state.
    _acted_page: list[Any] = []
    # Original selector -> the one a handler actually acts on, recorded only where a handler rewrites
    # it: a bare `#id` that names a shadow HOST resolves to the mirrored control inside the root, and
    # the driver then waits on THAT element. The guard only sees the original, so without this the
    # diagnosis asks about a visible host while the hidden inner control is what timed out. Cleared in
    # _resolve_page, so it never outlives the call that recorded it.
    _acted_selector: dict[str, str] = {}

    async def _resolve_page() -> tuple[Any, ToolResult | None]:
        # Single-use handoff from the preflight wrapper so a preflighted call resolves the page
        # once, not twice (each resolution is a must_get_working_page with its recovery path).
        page = _prefetched_page.pop() if _prefetched_page else await page_provider()
        if page is None:
            return None, ToolResult.error(PAGE_UNAVAILABLE_ERROR)
        _acted_page[:] = [page]
        # Per call, not per rewrite: only some handlers rewrite their selector, so clearing this where
        # a rewrite succeeds leaves the previous call's mapping in place for one that does not. A later
        # hover on the same `#id` would then be diagnosed against an inner control it never touched.
        _acted_selector.clear()
        return page, None

    async def _url(page: Any) -> str:
        try:
            return page.url
        except Exception:
            return ""

    def _is_context_teardown(exc: BaseException) -> bool:
        # Playwright's wording when a navigation destroys the context an evaluate was running in.
        # Matched by message because the driver raises a generic Error for it. Last resort only:
        # the driver also rewrites some unrelated protocol errors into this message.
        return "execution context was destroyed" in str(exc).lower()

    async def observe(_args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        # Bound the one perception call so a wedged page can't hang the turn indefinitely.
        raw = await asyncio.wait_for(page.evaluate(_observe_js()), timeout=30)
        data = json.loads(raw) if isinstance(raw, str) else raw
        elements = data.get("elements", [])
        omitted_anonymous = data.get("unnamedAnonymous") or 0
        omitted_duplicated = data.get("unnamedDuplicated") or 0
        omitted_unverifiable = data.get("unnamedUnverifiable") or 0
        omitted_unsafe = data.get("unnamedUnsafe") or 0
        omitted_budget = data.get("unnamedBudget") or 0
        omitted_in_components = (
            omitted_anonymous + omitted_duplicated + omitted_unverifiable + omitted_unsafe + omitted_budget
        )
        if omitted_in_components:
            # Sizes the capability this deliberately gives up, split by cause because the causes have
            # different fixes: `duplicated` is answered by host-anchored selectors with executor-side
            # verification (the SKY-14710 family), `anonymous` only by that same path, and neither by
            # another in-root identity mechanism. Merging them would over-report one and under-report
            # the other, and the follow-up would be chosen off the wrong number.
            LOG.info(
                "taskv3 observe omitted component controls it could not name",
                omitted_in_components=omitted_in_components,
                omitted_anonymous=omitted_anonymous,
                omitted_duplicated=omitted_duplicated,
                omitted_unverifiable=omitted_unverifiable,
                omitted_unsafe=omitted_unsafe,
                omitted_budget=omitted_budget,
                listed=len(elements),
            )
        # Compact rendering keeps the persistent-conversation prefix small (cost is ~linear in it).
        raw_url = _mask_refs(str(data.get("url") or ""))
        # Stripping forgery chars is not truncation: only the cap changes what the URL points at, so
        # the note is measured against the sanitized length rather than the raw one.
        sanitized_url = _DOWNLOAD_NOTICE_SANITIZE_RE.sub("", raw_url)
        shown_url = sanitized_url[:OBSERVE_URL_MAX_CHARS]
        # Every other cap in this payload names itself; a URL cut mid-query-string looks complete and
        # is a different, invalid URL.
        url_note = (
            f" (url truncated from {len(sanitized_url)} chars)" if len(sanitized_url) > OBSERVE_URL_MAX_CHARS else ""
        )
        lines = [f"url={shown_url}{url_note} title={data.get('title')!r} ({len(elements)} interactive elements)"]
        hidden_kept = data.get("hiddenListed") or 0
        if hidden_kept:
            lines.append(
                f"note: {hidden_kept} native control(s) hidden behind styled proxies are listed with [hidden-native]"
            )
        hidden_dropped = data.get("hiddenDropped") or 0
        # Scoped to total blindness: present-but-hidden chrome (closed menus, inactive tabs) is on
        # nearly every page, so an unconditional note would cost the prefix on every call to say
        # nothing. With no element listed the count is the whole signal -- it separates an app shell
        # still behind its boot gate from a page that genuinely has no controls, which is the one
        # distinction the model cannot otherwise make and will poll wait->observe for turns to guess.
        # Deliberately time-neutral: a boot gate resolves on its own and a stuck one never does, and
        # this cannot tell which, so the wording must not imply that waiting is what fixes it.
        if not elements and hidden_dropped:
            lines.append(
                f"note: the page has {hidden_dropped} control(s) that are present but not visible "
                "(CSS-hidden or positioned off-canvas): the DOM is populated but none of it is "
                "currently actionable"
            )
        phantom_dropped = data.get("phantomDropped") or 0
        if phantom_dropped:
            lines.append(
                f"note: {phantom_dropped} unreachable input(s) omitted (aria-hidden, out of the tab order, unlabeled)"
            )

        # Mask before capping: the masker matches a payload-minted URL by provenance over its WHOLE
        # text, so a display cap applied first (as the JS once did) leaves a fragment it cannot
        # recognise, signing tail included.
        def _field(raw: object, width: int) -> str:
            return _mask_refs(str(raw))[:width]

        texts = data.get("text") or []
        texts_full = data.get("textFull") or []
        for i, t in enumerate(texts):
            full = texts_full[i] if i < len(texts_full) else None
            lines.append(f"text: {_field(full or t, OBSERVE_DISPLAY_WIDTHS['text'])!r}")
        text_dropped = data.get("textDropped") or 0
        if text_dropped:
            # A capped digest must say it was capped: silently showing the first N reads as "that is all".
            lines.append(f"note: {text_dropped} more page message(s) did not fit the text digest")
        iframe_info = data.get("iframes") or {}
        iframe_entries = iframe_info.get("entries") or []
        iframe_unread = iframe_info.get("unread") or 0
        # Every branch states the scope it actually covered: a confident absence is read as "no gate
        # here" on the page most likely to have one.
        #
        # Not a frame count: one unreadable region is a single frame or a whole root, and a root holds
        # any number of frames.
        if iframe_unread:
            iframe_hedge = f"{iframe_unread} unreadable region(s) may hold more"
        elif data.get("undiscoveredRoots"):
            # A root the walk never found holds frames that are missing from `total` without the scan
            # knowing they exist. Keyed off that count and not `unreadableRoot`, which is page-wide and
            # several failures unrelated to the root walk also set.
            iframe_hedge = "part of this page could not be read, so there may be more"
        else:
            iframe_hedge = ""
        if iframe_info.get("failed"):
            # Never "none" and never a count: the scan did not run, so the page's frames are unknown
            # rather than absent.
            lines.append("iframes: the frame scan failed on this page; frame presence is unknown")
        elif iframe_entries:
            total = iframe_info.get("total", len(iframe_entries))
            parts = []
            for f in iframe_entries:
                flag = "[captcha] " if f.get("captcha") else ""
                title = f" {f['title']!r}" if f.get("title") else ""
                parts.append(f"{flag}{_digest_token(f.get('host') or '?', 80)}{title}")
            overflow = f" (+{total - len(iframe_entries)} more)" if total > len(iframe_entries) else ""
            # `total` counts what was readable, so without this the sentence is an absolute claim
            # about a page some of which was never read.
            lines.append(
                f"iframes: {total} cross-origin in the page and its open component roots "
                "(contents NOT listed here and NOT reachable by selector): "
                + "; ".join(parts)
                + overflow
                + (f"; {iframe_hedge}" if iframe_hedge else "")
            )
        elif iframe_hedge:
            lines.append(f"iframes: none found; {iframe_hedge}")
        elif data.get("rootCount"):
            # Only where a component root actually exists. On a page with no components the line
            # says nothing the element list doesn't, and it would cost a line on every observe of
            # every run.
            lines.append("iframes: none in the page or its open component roots")
        dropped = data.get("dropped") or 0
        if dropped:
            # Without this an element list emptied by unreadable elements is indistinguishable from
            # a page that genuinely has no controls.
            lines.append(f"note: {dropped} element(s) could not be described and are not listed below")
        if data.get("unreadableRoot"):
            # The condition itself, not just its consequences: a root that throws makes uniqueness
            # unverifiable everywhere, so unnamed elements are dropped rather than given a name we
            # could not check. Left unsaid, that reads as a page with fewer controls than it has.
            lines.append(
                "note: part of this page could not be queried, so selector uniqueness could not be "
                "verified here; elements we could not name are not listed"
            )
        if data.get("textTruncated"):
            # Every other cap in this payload names itself. This one binds far more often now that
            # component-rendered live regions feed the digest, and it evicts page headings silently.
            lines.append("note: page-text digest hit its budget; some page text is not shown")
        truncated = data.get("truncated") or 0
        if truncated:
            # A page of components can spend the whole budget before reaching its submit control, and
            # a list that stops silently reads as the complete set of what the page offers. No remedy
            # is suggested because none exists: the list comes from querySelectorAll, so it is
            # viewport-independent and scrolling returns the identical list and the identical count.
            note = f"note: {truncated} more element(s) matched but exceeded the element budget and are not listed"
            in_components = data.get("truncatedInComponents") or 0
            if in_components:
                # The budget is spent light-DOM-first so the page's own submit control survives a
                # page of components — which means component internals are what it starves.
                note += f", {in_components} of them inside components"
            lines.append(note)
        if omitted_in_components:
            # A statement about OUR limitation, not about the page: naming these would mean writing
            # into the component's own root, which provokes the re-render that destroys the mark. No
            # remedy is offered because there is none the model can perform — re-observing returns
            # the same omission. Split by cause: saying "no id of their own" about a control that has
            # one, and whose id is merely reused by a sibling instance, tells the model something
            # false about the page to describe a limitation of ours.
            why = []
            if omitted_anonymous:
                why.append(f"{omitted_anonymous} have no id, name or data-testid of their own")
            if omitted_duplicated:
                why.append(
                    f"{omitted_duplicated} have one that is reused by another instance of the same "
                    "component, so it does not identify a single element"
                )
            if omitted_unverifiable:
                why.append(f"{omitted_unverifiable} could not be verified because a component root was unreadable")
            if omitted_unsafe:
                why.append(f"{omitted_unsafe} carry an identifier we cannot render safely")
            if omitted_budget:
                why.append(f"{omitted_budget} exceeded the naming budget for this page")
            lines.append(
                f"note: {omitted_in_components} control(s) inside components are not listed because we "
                f"have no selector that identifies them: {'; '.join(why)}"
            )

        for e in elements:
            extra = ""
            if e.get("value"):
                extra += f" value={_field(e['value'], OBSERVE_DISPLAY_WIDTHS['value'])!r}"
            if e.get("placeholder"):
                extra += f" placeholder={_field(e['placeholder'], OBSERVE_DISPLAY_WIDTHS['placeholder'])!r}"
            if e.get("options"):
                extra += f" options={e['options']}"
            if e.get("checked") is not None:
                extra += f" checked={e['checked']}"
            if e.get("selected") is not None:
                extra += f" selected={e['selected']}"
            if e.get("pressed") is not None:
                extra += f" pressed={e['pressed']}"
            if e.get("required"):
                extra += " *required"
            if e.get("invalid"):
                extra += (
                    " *invalid"
                    if e["invalid"] is True
                    else f" *invalid={_field(e['invalid'], OBSERVE_DISPLAY_WIDTHS['invalid'])!r}"
                )
            if e.get("autocomplete"):
                extra += " [autocomplete→use select_combobox]"
            if e.get("hidden"):
                if e.get("type") == "file":
                    extra += " [hidden-native: styled proxy; file_upload works on it directly]"
                elif e.get("tag") == "select":
                    extra += " [hidden-native: styled proxy; select_option acts on it directly]"
                else:
                    extra += " [hidden-native: styled proxy; click acts on it directly]"
            if e.get("group"):
                extra += f" group={_field(e['group'], OBSERVE_DISPLAY_WIDTHS['group'])!r}"
            # INVARIANT for this line and every line above it: no page-controlled byte reaches the
            # digest un-escaped, and the header's count and the number of element lines come from the
            # same list. Everything else here is either repr'd or a literal. `type` is the trap --
            # on <input> the UA normalises it, but HTMLAnchorElement.type reflects the raw attribute,
            # so <a href type="x&#10;[#pay] button 'Confirm'"> printed a second, fabricated element
            # line for a selector that does not exist. `role` is whitelisted at the source; `tag` and
            # `type` are stripped of anything that could end a line or reorder it.
            kind = _digest_token(e["tag"], 40)
            if e.get("type"):
                kind += "/" + _digest_token(e["type"], 40)
            elif e.get("role"):
                kind += "/" + _digest_token(e["role"], 40)
            lines.append(
                f"[{_alias_for(e['selector'])}] {kind} "
                f"{_field(e.get('label', ''), OBSERVE_DISPLAY_WIDTHS['label'])!r}{extra}"
            )
        # Counts only, for the per-call log record: every perception change that alters only what
        # this function renders is otherwise invisible to production telemetry.
        summary = {
            "text_dropped": text_dropped,
            "hidden_listed": hidden_kept,
            "hidden_dropped": hidden_dropped,
            "phantom_dropped": phantom_dropped,
            "iframes_in_component_roots": iframe_info.get("inComponents") or 0,
            "undiscovered_roots": data.get("undiscoveredRoots") or 0,
            "omitted_unnameable": omitted_in_components,
            "invalid_fields": sum(1 for e in elements if e.get("invalid")),
            "markers_minted": data.get("markersMinted") or 0,
            "markers_reused": data.get("markersReused") or 0,
            "group_texts_found": sum(1 for e in elements if e.get("group")),
        }
        # Mask the whole rendered payload, not just url=: a signed payload ref can surface as page
        # text or a field value the model previously typed (a token resolved back to its URL), and
        # those lines would otherwise leak the signing artifact. Provenance-only, so benign page text
        # is untouched. url= is already masked before truncation above; re-masking a token is a no-op.
        return ToolResult.ok(_mask_refs("\n".join(lines)), data={"count": len(elements), "summary": summary})

    async def _rendered_text_result(page: Any, selector: str | None) -> ToolResult:
        # A text result carries no start tags of the page's own, so the one thing the alias layer
        # must know is that this is prose: rendered_text routes it to the whole-token pass.
        target = page
        if selector:
            target = await page.query_selector(selector)
            if target is None:
                return ToolResult.error(f"no element for selector {selector!r}")
        text = await _page_rendered_text(target)
        if text is None:
            return ToolResult.error("the rendered text could not be read")
        body = _escape_tags_in_text(_mask_refs(text))
        if len(body) > HTML_MAX_CHARS:
            body = body[:HTML_MAX_CHARS] + _RENDERED_TEXT_CUT
        return ToolResult.ok(body, data={"rendered_text": True})

    async def get_html(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args.get("selector")
        fmt = str(args.get("format") or "html").strip().lower()
        if fmt == "text":
            return await _rendered_text_result(page, selector)
        if fmt != "html":
            # Falling through to markup would hand back the whole-page dump the prompt forbids, on a
            # typo the model cannot see. The enum is advisory: the spec is not emitted strict.
            return ToolResult.error(f'unknown format {args.get("format")!r}: use "html" or "text"')
        # Whether the requested element's OWN start tag is in the answer. The alias masking layer may
        # only stamp the caller's handle on a tag it knows is that element's, never on a descendant.
        markup_scope = "document"
        if selector:
            el = await page.query_selector(selector)
            if el is None:
                return ToolResult.error(f"no element for selector {selector!r}")
            html = await el.inner_html()
            markup_scope = "inner"
            if not html:
                # Void/leaf elements have no inner HTML; their own tag+attributes are the answer,
                # not an empty string the model can't distinguish from a missing element. Best
                # effort: a navigation between the two reads must not turn "" into a tool error.
                try:
                    html = await el.evaluate("el => el.outerHTML")
                    markup_scope = "outer"
                except Exception:
                    html = ""
        else:
            html = await page.content()
        # The click/type reaction gate stamps data-tv3-pre on every visible element; internal bookkeeping
        # that, left in place, costs a third of the truncation budget below in noise.
        html = html.replace(' data-tv3-pre="1"', "")
        # The act-by-mark tag outlives its call, so unlike the other data-tv3-* bookkeeping it is
        # still on the page when this runs. It is a stable handle rather than a dangerous one -- the
        # token belongs to the element, not the number -- but it is ours, not the page's, and it
        # costs truncation budget the model needs for real markup.
        html = _ACT_ATTR_RE.sub("", html)
        html = _mask_refs(html)
        if len(html) > HTML_MAX_CHARS:
            cut = _MARKUP_CUT if selector else _PAGE_MARKUP_CUT
            return ToolResult.ok(html[:HTML_MAX_CHARS] + cut, data={"markup_scope": markup_scope})
        return ToolResult.ok(html, data={"markup_scope": markup_scope})

    async def _unreachable_error(selector: str) -> ToolResult:
        # A native checkbox or <select> inside a hidden template is refused HERE, by a visibility
        # pre-gate, before anything raises -- so it never reaches the guard's diagnosis and used to be
        # told its section is collapsed and to go find the trigger, which a template does not have.
        # Same question, same probe, asked one layer earlier.
        if await _target_is_inert(selector):
            return _inert_target_error(selector)
        return ToolResult.error(
            f"{selector} is not rendered and nothing visible stands in for it — its section is collapsed, "
            "closed or inactive, so a person could not reach this control either. Act on whatever reveals "
            "it (the section header, the step, the modal trigger), then re-observe."
        )

    def _not_editable_error(exc: _FieldNotEditable) -> ToolResult:
        if exc.read_only:
            return ToolResult.error(
                f"{exc.selector} is readonly — typing cannot change it. If it opens a list, click it and "
                "pick an option instead; otherwise act on whatever sets it."
            )
        return ToolResult.error(f"{exc.selector} is disabled — it cannot be typed into until the page enables it")

    def _covered_error(
        selector: str, occluder: dict[str, Any] | None = None, *, verb: str = "typed into"
    ) -> ToolResult:
        also = "" if verb == "clicked" else " — a person could not click it either"
        name = str((occluder or {}).get("name") or "").strip()
        layer_selector = (occluder or {}).get("selector")
        if occluder and occluder.get("invisible"):
            # The layer intercepts the pointer but paints nothing, so it is absent from the screenshot.
            # Telling the model to dismiss an overlay it can see is then a false instruction that makes
            # it flail; name the layer as invisible and point at recovery routes that do not depend on
            # seeing it. Controls are omitted on purpose: a ghost backdrop has none, and a still-present
            # named layer's controls did not dismiss it (that is why it is still here).
            if name and layer_selector:
                layer_desc = f'"{name}" ({layer_selector})'
            elif name:
                layer_desc = f'"{name}"'
            elif layer_selector:
                layer_desc = f"a layer ({layer_selector})"
            else:
                layer_desc = "a layer"
            return ToolResult.error(
                f"{selector} is covered by {layer_desc} that is INVISIBLE — it intercepts clicks but paints "
                f"nothing on screen, so you will not see it in a screenshot{also}. It is most likely a "
                "leftover backdrop from a dialog or cookie banner that was already dismissed. Do not keep "
                "trying to dismiss a visible overlay; press Escape, re-observe, or reach the field another way."
            )
        if not occluder:
            return ToolResult.error(
                f"{selector} is rendered but something else is on top of it, so it cannot be {verb}{also}. "
                "Dismiss whatever covers it (a dialog, an overlay, a cookie banner), then re-observe."
            )
        layer_desc = f'"{name}"' if name else "a layer"
        if layer_selector:
            layer_desc = f"{layer_desc} ({layer_selector})"
        parts = []
        for control in occluder.get("controls") or []:
            control_selector = control.get("selector") if isinstance(control, dict) else None
            label = str((control.get("label") if isinstance(control, dict) else "") or "").strip()
            if control_selector and label:
                parts.append(f'{control_selector} "{label}"')
            elif control_selector:
                parts.append(control_selector)
            elif label:
                parts.append(f'"{label}" (no selector — re-observe to address it)')
        if parts:
            controls_desc = "; ".join(parts)
        else:
            controls_desc = "re-observe — no controls were found on it"
        if occluder.get("truncated"):
            controls_desc += "; more controls exist (re-observe to see the rest)"
        return ToolResult.error(
            f"{selector} is covered by {layer_desc}, so it cannot be {verb}{also}. "
            # The layer may be a general modal, not just a consent wall -- these are every control
            # found on it, not confirmed dismissers, since a destructive or navigational action
            # (e.g. "Delete account") is not distinguishable here from a close/cancel button.
            f"Its controls: {controls_desc}. Pick whichever one actually closes or dismisses the "
            f"layer, then retry {selector}."
        )

    async def _probe_arg(page: Any, selector: str) -> dict[str, Any]:
        # Probes resolve per root, which cannot match a host-anchored selector whose two halves
        # straddle a shadow boundary. The executor's own engine can, so it supplies the element the
        # action will actually land on -- consulted only where the per-root lookup finds nothing.
        # Only a composed selector needs it, and hostAnchored composes with a space, so anything
        # without one keeps its single round trip. A quoted space costs a spare lookup, never a miss.
        if " " not in selector:
            return {"sel": selector, "el": None}
        try:
            element = await page.query_selector(selector)
        except Exception:
            element = None
        return {"sel": selector, "el": element}

    async def _target_is_inert(selector: str) -> bool:
        # _evaluate_isolated directly, NOT _probe_evaluate: that helper falls back to the page's own
        # realm when no isolated world exists, and there a page replacing getComputedStyle could report
        # a visible control as display:none -- turning this diagnosis into a way to dismiss a real
        # blocker. No isolated answer is unknown, and unknown makes no claim.
        if not _acted_page:
            return False
        try:
            return await _evaluate_isolated(_acted_page[0], _INERT_TARGET_PROBE_JS, selector) is True
        except Exception:
            return False

    async def _diagnose_inert_target(selector: str, exc: Exception) -> ToolResult | None:
        # Only a driver wait that ran out, and the driver's call log is what proves it was one. A bare
        # "Timeout" in the type name is not enough: file_upload fetches its source over the network
        # first, and an asyncio timeout there would be blamed on a file input that is display:none by
        # the convention every site follows. Matching the message rather than the type also survives
        # the patchright/playwright fork boundary, as the invalid-selector markers above do.
        if "Call log" not in str(exc):
            return None
        # The element the driver waited on, which is not always the one the caller named.
        acted = _acted_selector.get(selector, selector)
        return _inert_target_error(acted) if await _target_is_inert(acted) else None

    async def _resolve_mirrored_host_control(page: Any, selector: str) -> str:
        # Only a single compound selector can name a host by mistake; a composed (host-anchored) one
        # already points inside a root and a marker selector names exactly what observe marked.
        # Only a selector that names no tag can land on a host by mistake; a tag-qualified one already
        # says which element it means, so the page-wide walk is skipped for it.
        stripped = selector.strip()
        if (
            not stripped.startswith(("#", "["))
            or " " in _TV3_QUOTED_VALUE_RE.sub('""', stripped)
            or _TV3_MARKER_SELECTOR_RE.match(stripped)
        ):
            return selector
        try:
            named = await page.evaluate(_MIRRORED_HOST_CONTROL_JS, selector)
        except Exception:
            return selector
        if isinstance(named, str) and named:
            _acted_selector[selector] = named
            LOG.debug(
                "taskv3 selector resolved to a shadow host; acting on its mirrored control",
                selector=selector,
                control=named,
            )
            return named
        return selector

    async def _post_match_count(page: Any, selector: str) -> int:
        try:
            return await page.locator(selector).count()
        except Exception:
            return 1  # count unavailable → do not block, mirroring _marker_matches' fail-open

    async def _ambiguous_selector_error(page: Any, selector: str) -> ToolResult | None:
        # A host-anchored selector straddles a shadow boundary, which the per-root marker count
        # cannot see through; the executor's own engine can, so it supplies the count. Playwright's
        # actions are non-strict and would otherwise land on whichever match comes first.
        if not (_is_host_anchored_selector(selector) or _TV3_MARKER_SELECTOR_RE.match(selector.strip())):
            return None
        try:
            matches = await page.locator(selector).count()
        except Exception:
            # Left open, as the marker count is: refusing here would block every action on a page
            # whose engine hiccups, and the action's own actionability wait still applies.
            LOG.warning("taskv3 selector count unavailable; acting unverified", selector=selector)
            return None
        if matches == 1:
            return None
        if matches == 0:
            return ToolResult.error(
                f"{selector} no longer matches anything on the page — the page re-rendered since it was "
                "observed. Re-observe and act on fresh selectors from the new observation.",
                data={"page_state_changed": True},
            )
        return ToolResult.error(
            f"{selector} matches {matches} elements, so it does not identify one control. Re-observe and "
            "act on a selector from the new observation, or narrow this one until it matches exactly one."
        )

    async def _marker_matches(page: Any, selector: str) -> int:
        try:
            return int(await page.evaluate(_MARKER_MATCH_COUNT_JS, await _probe_arg(page, selector)))
        except Exception:
            return 1

    async def _click_reaction(
        page: Any, selector: str, pre: dict[str, Any], url_before: str, *, doc_planted: bool
    ) -> tuple[str | None, str | None]:
        # Returns (note, commit_error) — at most one set. Raises are the caller's to swallow (fail-open:
        # a probe failure must degrade to the bare pre-feature ok, never fail the click).
        opt = _DOWNLOAD_NOTICE_SANITIZE_RE.sub("", str(pre.get("optText") or "")) or selector
        if pre.get("isOption"):
            # Commit evidence, any one suffices: navigation, the menu closing, the option's own state
            # changing vs the post-hover baseline (multi-select menus commit WITHOUT closing), or a
            # submenu opening (a cascading option commits nothing yet — reporting the child menu beats
            # a false error).
            baseline = pre.get("optState") or ""
            sel_baseline = pre.get("optSel") or ""
            kids_baseline = pre.get("optKids")
            height_baseline = pre.get("optH")
            vis_baseline = pre.get("optVis")

            def _committed_state(after: dict[str, Any]) -> bool:
                return bool(after.get("optState")) and after.get("optState") != baseline

            def _picked(after: dict[str, Any]) -> bool:
                # One of the row's own selection attributes moved. Nothing a restructuring row does
                # can reach these, so this is commit evidence on its own.
                return bool(after.get("optSel")) and after.get("optSel") != sel_baseline

            def _grew(after: dict[str, Any]) -> bool:
                # The row got bigger of its own accord, which is the one shape where "it committed"
                # has a competitor. Child count, height, or visible descendant rows: a category whose
                # leaves pre-exist hidden and are revealed on click keeps its child COUNT and may keep
                # its height, but its visible descendant row count rises — measured within the clicked
                # row, so a sibling reveal (a real commit that also shows peers) does not trip it.
                def _up(now: Any, before: Any, by: int) -> bool:
                    ok = (int, float)
                    if isinstance(now, bool) or isinstance(before, bool):
                        return False
                    return isinstance(now, ok) and isinstance(before, ok) and before >= 0 and now - before > by

                return (
                    _up(after.get("optKids"), kids_baseline, 0)
                    or _up(after.get("optH"), height_baseline, 2)
                    or _up(after.get("optVis"), vis_baseline, 0)
                )

            async def _state_holds(state: str) -> tuple[dict[str, Any] | None, str | None]:
                # A real commit settles; self-updating content (a countdown, a live price) keeps
                # moving, so only a state that holds across two reads is evidence. The second read is
                # returned because 150ms later is the difference between measuring a CSS expansion
                # and measuring it mid-flight. Three outcomes, not two: a re-read that could not be
                # taken is neither "held" nor "moved" -- the second value names that failure.
                await asyncio.sleep(0.15)
                again, navigated = await _after_read()
                if again is None:
                    return None, "navigated" if navigated else "unreadable"
                return (again if again.get("optState") == state else None), None

            async def _after_read() -> tuple[dict[str, Any] | None, bool]:
                # Returns (read, navigated); read is None only when the probe raised. A raise is NOT
                # evidence of anything unless the page positively left -- a throwing probe, a detached
                # node, or a CDP timeout must never read as a commit. Asked in order of reliability:
                # the window token (the page itself says whether this is the same document, and
                # history.pushState cannot fool it the way it fools the URL); the URL; and only when
                # the page cannot be asked at all, the driver's own destroyed-context wording.
                try:
                    raw = await page.evaluate(_MENU_AFTER_JS, await _probe_arg(page, selector))
                except Exception as exc:
                    same_document = await _same_document()
                    if same_document is not None:
                        return None, not same_document
                    url_after = await _url(page)
                    if url_before and url_after and url_after != url_before:
                        return None, True
                    return None, _is_context_teardown(exc)
                return (raw if isinstance(raw, dict) else {}), False

            def _unverified(why: str) -> tuple[str | None, str | None]:
                return None, (
                    f"clicked option {opt!r} ({selector}) but its effect could not be verified — the {why} "
                    "read failed and the page did not navigate. The click was dispatched: do not repeat it "
                    "blindly and do not assume the selection committed; re-observe first."
                )

            async def _child_menu_note() -> str | None:
                # A row that expands a sub-list mutates ITSELF, so the fingerprint cannot tell
                # "committed" from "expanded" and the child rows can -- a cascading click that opened
                # them committed nothing yet.
                try:
                    found = await page.evaluate(_FIND_MENU_JS, await _probe_arg(page, selector))
                except Exception:
                    return None
                if isinstance(found, dict) and found.get("count"):
                    return _menu_open_note(found, selector, clicked_row=True)
                return None

            async def _cascade_child_note() -> str | None:
                # The clicked row DETACHED: a cascading category replaces the whole list with its
                # children, which reads as "the menu closed" off the row alone — a false verdict
                # exactly when the model must keep drilling. New rows in a floating container are those
                # children. A real commit that closed the menu pays one probe; the wait extends only
                # while the widget shows a busy indicator for the children it is still fetching.
                baseline = time.monotonic() + 0.8
                deadline = time.monotonic() + 2.4
                while True:
                    try:
                        found = await page.evaluate(_FIND_MENU_JS, {"sel": selector, "el": None, "cascade": True})
                    except Exception:
                        return None
                    if isinstance(found, dict) and found.get("count"):
                        return _menu_open_note(found, selector, clicked_row=True)
                    now = time.monotonic()
                    if now < baseline:
                        # Children scheduled on a timer may announce nothing (no busy row) for a beat:
                        # a short unconditional settling window before the busy signal is required.
                        await asyncio.sleep(0.25)
                        continue
                    try:
                        busy = bool(await page.evaluate(_MENU_BUSY_JS, await _probe_arg(page, selector)))
                    except Exception:
                        busy = False
                    if not busy or now >= deadline:
                        return None
                    await asyncio.sleep(0.3)

            async def _state_change_note(opt_text: str, after: dict[str, Any]) -> str:
                picked = f"Selected option {opt_text!r} — its state changed (the menu stayed open)."
                # A row that did not grow cannot have expanded into itself, so nothing competes with
                # the commit reading and the probe is not worth its page walk -- which is the
                # ordinary multi-select click.
                if not _grew(after):
                    return picked
                child = await _child_menu_note()
                if not child:
                    return picked
                # It grew AND opened child rows, so a selection attribute means it did both and
                # dropping either half would be a false report. Without the "menu stayed open" clause:
                # the child note has just renumbered the markers, so what the model was holding is
                # precisely what did not stay.
                if _picked(after):
                    return f"Selected option {opt_text!r} — its state changed.\n{child}"
                return child

            async def _same_document() -> bool | None:
                # The page's own answer, or None when it cannot be asked.
                if not doc_planted:
                    return None
                try:
                    answer = await page.evaluate(_CLICK_DOC_CHECK_JS)
                except Exception:
                    return None
                return answer if isinstance(answer, bool) else None

            url_now = await _url(page)
            if url_before and url_now and url_now != url_before and await _same_document() is not True:
                # A moved URL is a navigation unless the page says it is the same document --
                # a menu that syncs its selection into the query string never left.
                return f"Selected option {opt!r} — the page navigated.", None
            after, navigated = await _after_read()
            if after is None and navigated:
                return f"Selected option {opt!r} — the page navigated.", None
            if after is not None and not after.get("stillOpen"):
                child = await _cascade_child_note()
                if child:
                    return child, None
                return f"Selected option {opt!r} — the menu closed.", None
            if after is not None and _committed_state(after):
                held, failure = await _state_holds(after.get("optState") or "")
                if failure == "navigated":
                    return f"Selected option {opt!r} — the page navigated.", None
                if failure == "unreadable":
                    return _unverified("state-hold")
                if held is not None:
                    return await _state_change_note(opt, held), None
            # Menus routinely close through a fade or an async server ack; declaring "did not commit"
            # off the instantaneous read would turn those healthy commits into false errors. One
            # bounded settle, only on this would-be-error path.
            await asyncio.sleep(0.6)
            settled, navigated = await _after_read()
            if settled is None:
                if navigated:
                    return f"Selected option {opt!r} — the page navigated.", None
                return _unverified("post-click" if after is None else "settle")
            if not settled.get("stillOpen"):
                child = await _cascade_child_note()
                if child:
                    return child, None
                return f"Selected option {opt!r} — the menu closed.", None
            if _committed_state(settled):
                held, failure = await _state_holds(settled.get("optState") or "")
                if failure == "navigated":
                    return f"Selected option {opt!r} — the page navigated.", None
                if failure == "unreadable":
                    return _unverified("state-hold")
                if held is not None:
                    return await _state_change_note(opt, held), None
            # No-commit evidence is already established: a crash of this last informational probe must
            # not fall through to the caller's fail-open bare ok (_child_menu_note swallows).
            late_child = await _child_menu_note()
            if late_child:
                return late_child, None
            return None, (
                f"clicked option {opt!r} ({selector}) but the selection did not commit — the menu is "
                "still open and unchanged. Do not repeat this click; re-observe and try a different "
                "control. (Do NOT press Enter on the FIELD as a shortcut: on many widgets that commits "
                "whichever row is highlighted, not the one you want.)"
            )
        if pre.get("menuOpen"):
            if pre.get("containsMenu"):
                # Clicked the card AROUND the menu: the center-point click may have landed on an
                # arbitrary row, so any open/closed/selected claim could be false. Say nothing.
                return None, None
            try:
                after_raw = await page.evaluate(_MENU_AFTER_JS, await _probe_arg(page, selector))
            except Exception:
                return None, None
            found = await page.evaluate(_FIND_MENU_JS, await _probe_arg(page, selector))
            if isinstance(found, dict) and found.get("count"):
                return _menu_open_note(found, selector), None
            if isinstance(after_raw, dict) and not after_raw.get("stillOpen"):
                return (
                    "Note: this click CLOSED the open menu — no option was selected. To select, click "
                    'an option\'s [data-tv3-menu="N"] selector while the menu is open.'
                ), None
            return None, None
        found = await page.evaluate(_FIND_MENU_JS, await _probe_arg(page, selector))
        if isinstance(found, dict) and found.get("count"):
            return _menu_open_note(found, selector), None
        return None, None

    async def click(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args.get("selector")
        if not selector:
            return ToolResult.error("click needs a selector, or mark=N from the last look().")
        if _TV3_MARKER_SELECTOR_RE.match(selector.strip()):
            matches = await _marker_matches(page, selector)
            if matches == 0:
                # An absent marker cannot reappear without a re-observe, so Playwright's full 15s
                # actionability wait is pure loss (4x in the specimen trace). Short attach grace
                # tolerates a framework re-attaching the same node mid-render.
                try:
                    await page.wait_for_selector(selector, state="attached", timeout=1200)
                except Exception:
                    return ToolResult.error(
                        f"{selector} no longer exists on the page — element markers vanish when the "
                        "page re-renders (a closed menu destroys its options). Re-observe and act on "
                        "fresh selectors from the new observation.",
                        data={"page_state_changed": True},
                    )
                # The re-attach may have been a re-render that cloned the row, so the count is re-read.
                matches = await _marker_matches(page, selector)
            if matches > 1:
                # A clone of the marked element carries the same marker; the click would silently
                # land on whichever comes first in document order, so refuse before dispatching it.
                return ToolResult.error(
                    f"{selector} now matches {matches} elements — the page re-rendered and cloned the "
                    "marked element, so the marker no longer identifies one control. Re-observe and act "
                    "on fresh selectors from the new observation.",
                    data={"page_state_changed": True},
                )
        else:
            ambiguous = await _ambiguous_selector_error(page, selector)
            if ambiguous is not None:
                return ambiguous
        pre: dict[str, Any] | None = None
        try:
            pre_raw = await page.evaluate(_CLICK_PRECHECK_JS, await _probe_arg(page, selector))
            if isinstance(pre_raw, dict):
                pre = pre_raw
        except Exception:
            pre = None
        if pre is not None and pre.get("isOption"):
            # Playwright's click hovers first, and menus routinely restyle a row on hover — so the
            # commit baseline must be the POST-hover fingerprint, or a mere highlight would read as
            # "its state changed" commit evidence on a no-op click.
            try:
                await page.hover(selector, timeout=2000)
                hovered = await page.evaluate(_MENU_AFTER_JS, await _probe_arg(page, selector))
                if isinstance(hovered, dict) and hovered.get("optState"):
                    pre["optState"] = hovered["optState"]
                    # Every baseline the commit checks read, not some of them: whichever is left
                    # behind describes the row before the hover, so the hover's own doing -- an
                    # aria-selected mark, a row-hover toolbar that grows the row -- reads as the
                    # click's.
                    for key in ("optSel", "optKids", "optH", "optVis"):
                        if key in hovered:
                            pre[key] = hovered[key]
            except Exception:
                pass
        url_before = await _url(page)
        doc_planted = False
        if pre is not None and pre.get("isOption"):
            try:
                await page.evaluate(_CLICK_DOC_PLANT_JS)
                doc_planted = True
            except Exception:
                pass
        # One resolution for the whole pre-click phase: these run back to back with no mutation
        # between them, so re-asking the executor per probe would only buy round trips.
        pre_click_arg = await _probe_arg(page, selector)
        # Only a target that can have its own label over it (a component's slotted label, or a sibling
        # <label for=id>) pays the extra round trip; any other light-DOM click keeps its single one.
        reach_pre = None
        try:
            if await _probe_evaluate(page, _REACH_PROBE_NEEDED_JS, selector, pre_click_arg):
                reach_pre = await _probe_evaluate(page, _TYPE_TARGET_PROBE_JS, selector, pre_click_arg)
        except Exception:
            reach_pre = None
        if isinstance(reach_pre, dict) and reach_pre.get("exists") and reach_pre.get("disabled"):
            return ToolResult.error(f"{selector} is disabled — it cannot be clicked until the page enables it")
        # `slotted` only qualifies unoccluded (the composed hit landed cleanly on the control's own
        # slotted label); `ownLabel` already implies occluded+skinned, so it qualifies on its own.
        label_over_control = isinstance(reach_pre, dict) and (
            (bool(reach_pre.get("slotted")) and not reach_pre.get("occluded")) or bool(reach_pre.get("ownLabel"))
        )
        try:
            skin_probe = await _probe_evaluate(page, _SKINNED_CHECKBOX_PROBE_JS, selector, pre_click_arg)
        except Exception:
            skin_probe = None
        if isinstance(skin_probe, dict) and skin_probe.get("file"):
            return ToolResult.error(
                f"{selector} is a file input — clicking it opens a native picker the run cannot drive; "
                "use file_upload with this selector instead"
            )
        if isinstance(skin_probe, dict) and skin_probe.get("select") and skin_probe.get("invisible"):
            # Playwright's actionability wait never resolves against it, so a click here is 15s of pure
            # loss followed by a raise; select_option forces past that on the same selector — but only
            # for one something visible stands in for, so an unreachable select is sent to reveal first
            # rather than to a tool that would refuse it a turn later.
            if not skin_probe.get("proxied"):
                return await _unreachable_error(selector)
            return ToolResult.error(
                f"{selector} is a hidden native <select> — a click cannot open it; use select_option "
                "with this selector instead"
            )
        if isinstance(skin_probe, dict) and skin_probe.get("unproxied"):
            return await _unreachable_error(selector)
        skinned = bool(isinstance(skin_probe, dict) and skin_probe.get("skinned"))
        if skinned and skin_probe.get("labelCovered"):
            return _covered_error(selector, None, verb="clicked")
        label_click = skin_probe.get("labelClick") if isinstance(skin_probe, dict) else None
        if not isinstance(label_click, dict):
            label_click = None
        # A forced own-label click can land on something inside the label that is not the control (a
        # nested <details>, a scripted link); a toggle target earns a before/after readback.
        toggle_target = isinstance(reach_pre, dict) and bool(reach_pre.get("toggle"))
        verify_toggle = bool(reach_pre and reach_pre.get("ownLabel")) and toggle_target
        checked_before: bool | None = None
        if skinned:
            if skin_probe.get("disabled"):
                # Playwright refuses a label bound to a disabled control the same way it refuses the
                # control, so the click path would spend its full timeout and then blame a re-render.
                return ToolResult.error(f"{selector} is disabled — it cannot be toggled until the page enables it")
            try:
                checked_before = await _probe_evaluate(page, _CHECKBOX_CHECKED_JS, selector, pre_click_arg)
            except Exception:
                checked_before = None
            if checked_before is True and skin_probe.get("radio"):
                return ToolResult.ok(f"{selector} is already selected — no change needed")
        elif verify_toggle:
            try:
                checked_before = await _probe_evaluate(page, _CHECKBOX_CHECKED_JS, selector, pre_click_arg)
            except Exception:
                checked_before = None
            if checked_before is True and reach_pre is not None and reach_pre.get("toggleRadio"):
                # Mirrors the skinned rule above: reclicking an already-selected radio is a no-op by
                # native semantics, not a failed commit.
                return ToolResult.ok(f"{selector} is already selected — no change needed")

        # A visible native radio/checkbox, a <label for> that owns one, or a component host whose
        # composed subtree holds exactly one gets the same readback as skinned below. A menu option row
        # is judged by _click_reaction instead (its inner checkbox may be decorative), and a disabled
        # bearer cannot move, so neither gets a toggle verdict.
        toggle = bool(
            isinstance(skin_probe, dict)
            and skin_probe.get("toggle")
            and not skin_probe.get("toggleDisabled")
            and pre is not None
            and not pre.get("isOption")
        )
        if toggle and not skinned and not verify_toggle:
            try:
                checked_before = await _probe_evaluate(page, _CHECKBOX_CHECKED_JS, selector, pre_click_arg)
            except Exception:
                checked_before = None

        if skinned and label_click:
            try:
                await page.mouse.click(float(label_click["x"]), float(label_click["y"]))
            except Exception as e:
                return ToolResult.error(
                    f"click on {selector} via its label failed ({type(e).__name__}) — the page may have "
                    "re-rendered; re-observe and act on fresh selectors"
                )
            base = f"clicked {selector} via its label — now at {await _url(page)}"
        elif skinned:
            # Resolved again here rather than reused: this evaluate is the action, not a probe, and a
            # selector naming a control through its host resolves ONLY through the handle -- a node the
            # page replaced while the probes ran would be clicked off-document, silently.
            fired = await page.evaluate(
                "(arg) => { const _q = "
                + _ROOT_QUERY_JS
                + "; const el = _q.find(arg.sel) || arg.el;"
                + " if (!el || !el.isConnected) return false; el.click(); return true; }",
                await _probe_arg(page, selector),
            )
            if not fired:
                return ToolResult.error(
                    f"{selector} left the page before the click could land — it was replaced by a "
                    "re-render; re-observe and act on fresh selectors",
                    data={"page_state_changed": True},
                )
            base = f"clicked {selector} (hidden native control, toggled directly) — now at {await _url(page)}"
        else:
            try:
                await page.evaluate(_CLICK_SAME_DOC_PLANT_JS)
            except Exception:
                pass
            try:
                if label_over_control:
                    # The probe already answered: the only thing "over" this control is its own label
                    # (slotted, or a sibling for=id), which the driver's containment check would wait
                    # 15s to reject.
                    await page.click(selector, timeout=15000, force=True)
                else:
                    await page.click(selector, timeout=15000)
            except Exception as e:
                gone = False
                try:
                    gone = not await page.evaluate(_SELECTOR_EXISTS_JS, await _probe_arg(page, selector))
                except Exception:
                    gone = False
                if gone:
                    # A same-document re-render: the URL and document nonce read unchanged, so only this
                    # flag tells the loop the rest of the batch was planned against a stale page.
                    return ToolResult.error(
                        f"click on {selector} failed: the element no longer exists on the page — it was "
                        "likely removed by a re-render (e.g. a menu closed and destroyed its options). "
                        f"Re-observe and act on fresh selectors. (original error: {type(e).__name__})",
                        data={"page_state_changed": True},
                    )
                # Diagnosed only now, after the full actionability wait: a transient overlay (a toast,
                # a closing menu) deserves the whole 15s to clear on its own, not a probe-shortened one.
                try:
                    reach_raw = await _probe_evaluate(
                        page, _TYPE_TARGET_PROBE_JS, selector, await _probe_arg(page, selector)
                    )
                except Exception:
                    reach_raw = None
                reach_probe = reach_raw if isinstance(reach_raw, dict) else None
                # `skinned` is the typing path's force-past signal; a click has no force fallback, so a
                # click that timed out on an occluded field is genuinely blocked -- even by the field's
                # own open listbox. Name the occluder rather than re-raise a bare Page.click Timeout.
                # `ownLabel` (the field's own skin-sized label) is the one occluded case that is not a
                # block; it falls through to the force-retry below.
                if reach_probe and reach_probe.get("occluded") and not reach_probe.get("ownLabel"):
                    return _covered_error(selector, reach_probe.get("occluder"), verb="clicked")
                # A URL is the wrong question (pushState moves it without leaving the page); the token
                # planted before the click answers "is this still the same document" exactly.
                try:
                    same_document = bool(await page.evaluate(_CLICK_SAME_DOC_CHECK_JS))
                except Exception:
                    same_document = False
                # Only the driver's own hit-target refusal is retried: any other failure (a download or
                # navigation the click started, a detached node) keeps its original error.
                intercepted = "intercepts pointer events" in str(e)
                if (
                    intercepted
                    and same_document
                    and reach_probe
                    and reach_probe.get("exists")
                    and not reach_probe.get("disabled")
                ):
                    # The driver's hit-target check reads DOM containment, so a control whose visible
                    # label is slotted into its shadow tree reads as intercepted by its own label. The
                    # composed-tree probe just said nothing covers it, so the click a person makes lands
                    # on it; dispatch that click at the same point without the containment check.
                    if reach_probe.get("ownLabel") and reach_probe.get("toggle"):
                        # This retry never ran the verify_toggle/checked_before setup above (it only
                        # saw reach_pre); a toggle reached only via its own label still needs a
                        # before/after readback so the forced click's outcome gets verified.
                        verify_toggle = True
                        if checked_before is None:
                            try:
                                checked_before = await _probe_evaluate(
                                    page, _CHECKBOX_CHECKED_JS, selector, await _probe_arg(page, selector)
                                )
                            except Exception:
                                checked_before = None
                        if checked_before is True and reach_probe.get("toggleRadio"):
                            # Mirrors the pre-click rule: reclicking an already-selected radio is a
                            # no-op by native semantics, so there is nothing to force past.
                            return ToolResult.ok(f"{selector} is already selected — no change needed")
                    try:
                        await page.locator(selector).first.wait_for(state="visible", timeout=3000)
                        await page.click(selector, timeout=5000, force=True)
                    except Exception:
                        raise e from None
                else:
                    raise
            base = f"clicked {selector} — now at {await _url(page)}"

        # url_after vs url_before is the real page-transition signal the shadow net-progress ledger
        # reads (loop.py _ProgressLedger). Surfaced, not newly computed: _url is the page.url property,
        # not a probe, so this adds no evaluate. history.pushState can move the URL without leaving the
        # document, so this is a hint the ledger treats as re-baseline evidence, not a hard assertion.
        url_after = await _url(page)
        transition_data: dict[str, Any] = {
            "page_transitioned": bool(url_before and url_after and url_after != url_before)
        }
        if url_before and url_after and url_after != url_before:
            # Click-driven transitions feed the same visited-URL ring navigate reads, so a later
            # navigate back to a click-reached page is classified as a revisit, not fresh territory.
            _recent_nav_canonicals.append(canonical_url(url_before))
            _recent_nav_canonicals.append(canonical_url(url_after))

        # An already-checked radio legitimately doesn't change on re-click, so the readback is
        # skipped for it -- same as the skinned path's short-circuit above.
        already_checked_radio = (
            toggle
            and not skinned
            and not verify_toggle
            and isinstance(skin_probe, dict)
            and bool(skin_probe.get("radio"))
            and checked_before is True
        )
        if (skinned or verify_toggle or toggle) and not already_checked_radio:
            try:
                checked_after = await _probe_evaluate(
                    page, _CHECKBOX_CHECKED_JS, selector, await _probe_arg(page, selector)
                )
            except Exception:
                checked_after = None
            matches = await _post_match_count(page, selector)
            post_state = {"checked": checked_after} if checked_after is not None else None
            verdict = _classify_commit({"checked": checked_before}, matches, post_state)
            if verdict is CommitStatus.DID_NOT_COMMIT:
                # A controlled control cancels the native flip and re-sets .checked a tick later, so an
                # unchanged first read is re-taken once before it counts as the page's answer.
                await asyncio.sleep(0.15)
                try:
                    checked_after = await _probe_evaluate(
                        page, _CHECKBOX_CHECKED_JS, selector, await _probe_arg(page, selector)
                    )
                except Exception:
                    checked_after = None
                matches = await _post_match_count(page, selector)
                post_state = {"checked": checked_after} if checked_after is not None else None
                verdict = _classify_commit({"checked": checked_before}, matches, post_state)
            if verdict is CommitStatus.UNVERIFIED:
                if post_state is None and not skinned and not verify_toggle:
                    return ToolResult.ok(
                        f"{base} — its toggle could not be read back after the click (the control left the page "
                        "or is no longer the only one), so its state could not be verified; re-observe before "
                        "relying on it",
                        data=transition_data,
                    )
                if post_state is None:
                    return ToolResult.ok(
                        f"{base} — the control left the page after the click, so its state could not be "
                        "verified; re-observe before relying on it",
                        data=transition_data,
                    )
                if matches != 1:
                    return ToolResult.ok(
                        f"{base} — it re-resolved to {matches} elements after the click, so its state could "
                        "not be verified; re-observe before relying on it",
                        data=transition_data,
                    )
                # Readable, singular post-click state but no pre-click baseline to diff against (the
                # pre-read raced): as before the unified verdict, fall through to the ordinary post path
                # rather than claim the control left the page.
            if verdict is CommitStatus.DID_NOT_COMMIT:
                if skinned:
                    return ToolResult.error(
                        f"click on {selector} did NOT commit: the control still reads checked={checked_after!r} — "
                        "the styled proxy may not sync from its hidden control; re-observe and act on the visible "
                        "proxy instead",
                        data=transition_data,
                    )
                if verify_toggle:
                    return ToolResult.error(
                        f"click on {selector} did NOT commit: the control still reads checked={checked_after!r} — "
                        "its label took the click but the control did not change (the page may refuse the toggle, "
                        "or something inside the label took it); re-observe before retrying",
                        data=transition_data,
                    )
                # The click may have done something other than toggle (opened a menu, selected an
                # option); the menu reaction is the authority on that before the toggle verdict stands.
                if pre is not None:
                    try:
                        note, commit_error = await _click_reaction(
                            page, selector, pre, url_before, doc_planted=doc_planted
                        )
                    except Exception:
                        note, commit_error = None, None
                    if commit_error is not None:
                        return ToolResult.error(commit_error, data=transition_data)
                    if note:
                        return ToolResult.ok(base + "\n" + note, data=transition_data)
                return ToolResult.error(
                    f"click on {selector} did NOT commit: the control still reads checked={checked_after!r} — "
                    "the page discarded the toggle, or is asking something first; re-observe before retrying",
                    data=transition_data,
                )

        if pre is None:
            return ToolResult.ok(base, data=transition_data)
        try:
            note, commit_error = await _click_reaction(page, selector, pre, url_before, doc_planted=doc_planted)
        except Exception:
            LOG.debug("taskv3 click reaction probe failed", selector=selector, exc_info=True)
            return ToolResult.ok(base, data=transition_data)
        if commit_error is not None:
            return ToolResult.error(commit_error, data=transition_data)
        return ToolResult.ok(base + "\n" + note if note else base, data=transition_data)

    async def hover(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args["selector"]
        ambiguous = await _ambiguous_selector_error(page, selector)
        if ambiguous is not None:
            return ambiguous
        await page.hover(selector, timeout=15000)
        return ToolResult.ok(f"hovered {selector}")

    async def _reachable_for_typing(page: Any, selector: str) -> tuple[bool, bool, dict[str, Any] | None]:
        """(reachable, occluded, occluder). Raises when the field cannot accept typed text at all. Shared
        by both typing paths: fill() does no hit-testing, so without this a covered password or email
        field is filled silently -- no timeout to notice, and a person could not have reached it."""
        try:
            probe = await _probe_evaluate(page, _TYPE_TARGET_PROBE_JS, selector, await _probe_arg(page, selector))
        except Exception:
            probe = None
        if isinstance(probe, dict) and probe.get("exists"):
            # fill() waits for "enabled" and "editable" on its own, so without these the run pays a
            # second full timeout for a state the probe has already read.
            if probe.get("disabled") or probe.get("readOnly"):
                raise _FieldNotEditable(selector, bool(probe.get("readOnly")))
        occluded = bool(isinstance(probe, dict) and probe.get("occluded"))
        occluder = probe.get("occluder") if isinstance(probe, dict) else None
        if occluded and not probe.get("skinned"):
            return False, occluded, occluder
        # Reachable: a skinned own-popup is force-typed past, so there is no blocking occluder to
        # report. The probe still names it (the click path, which reads the probe directly, needs the
        # name), but surfacing it here would let a force-click that then navigates or remounts the
        # field raise a false "covered by <the field's own list>" message on a field that was reachable.
        return True, occluded, None

    async def _focus_for_typing(page: Any, selector: str) -> tuple[bool, dict[str, Any] | None]:
        """Put the caret in `selector`. A False first element means the field is genuinely covered and
        must not be typed into. A click is how a widget learns to open its suggestion list, so it stays
        the first move."""
        reachable, occluded, occluder = await _reachable_for_typing(page, selector)
        if not reachable:
            return False, occluder
        if occluded:
            # Forcing skips the hit-target check but still dispatches at coordinates, so the wrapper
            # can take the event; the focus check below is what makes the outcome deterministic.
            # Failures are NOT swallowed: force already removed the only reason this click was
            # expected to fail, so what is left (a detached node, a navigation) is real.
            # A URL is the wrong question: history.pushState changes it without leaving the page,
            # and a widget that syncs filter state into the URL on click would abort typing on a
            # field that never moved. A navigation clears window, so a token planted on it answers
            # "is this still the same document" exactly -- the same technique the pre-snapshot uses.
            await page.evaluate("() => { window.__tv3_doc = 1; }")
            await page.click(selector, timeout=15000, force=True)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=1000)
            except Exception:
                pass
            try:
                same_document = bool(await page.evaluate("() => window.__tv3_doc === 1"))
            except Exception:
                same_document = False
            if not same_document:
                # The wrapper was a link and the click followed it. The selector may well match
                # something on the destination, so typing now would put the text somewhere nobody
                # asked for.
                return False, occluder
            try:
                # The click may have remounted or hidden the field -- a wrapper that swaps its input
                # on click is an ordinary SPA shape. fill() would wait its own full timeout for a
                # node that is gone or invisible, which is the cost this whole path exists to avoid.
                await page.wait_for_selector(selector, state="visible", timeout=1200)
            except Exception:
                return False, occluder
        else:
            await page.click(selector, timeout=15000)
        try:
            focused = await page.evaluate(_ACTIVE_IS_JS, await _probe_arg(page, selector))
        except Exception:
            focused = None
        # None is "could not tell" -- a selector document.querySelector cannot parse, or a probe that
        # threw. Only an explicit False is evidence the caret went somewhere else.
        if focused is False:
            # focus() needs no hit target, so it repairs a skin that swallowed the click without
            # forwarding it. Typing then goes to the field rather than wherever the caret was.
            await page.focus(selector, timeout=15000)
        return True, None

    def _occluder_labels_hold(occluder: dict[str, Any] | None, value: str) -> bool:
        # The covering layer IS the committed-selection surface only when its own accessible naming
        # carries the value ("<label>, press delete to clear value." style) — read off the occluder
        # dict the covered-probe already built, no extra DOM round trip.
        def norm(t: str) -> str:
            return " ".join(str(t or "").split()).casefold()

        want = norm(value)
        if not want:
            return False

        def satisfied(text: str) -> bool:
            # Exact, or the value plus ONE parenthesized decoration ("United Kingdom (+44)") — the
            # canonical-label idiom that otherwise loops a covered field forever. Nothing looser:
            # a prefix without its own " (" boundary ("United" vs "United Kingdom (+44)") and a
            # non-parenthetical suffix both stay refusals.
            if text == want:
                return True
            return bool(re.fullmatch(re.escape(want) + r" \([^()]+\)", text))

        def holds(raw: object) -> bool:
            # The committed label may itself contain commas ("Korea, Republic of"); the widget's
            # instruction ("press delete to clear value.") is ONE trailing comma-clause. Strip it
            # only when the trailing clause reads as an instruction — an unconditional strip would
            # let a bare "Korea" read as holding a suffix-less "Korea, Republic of" label.
            own = norm(str(raw or "")).split("|")[0].strip()
            if not own:
                return False
            if satisfied(own):
                return True
            head, _, tail = own.rpartition(",")
            if not head or not _INSTRUCTION_CLAUSE_RE.search(tail):
                return False
            return satisfied(head.strip())

        if holds((occluder or {}).get("name")):
            return True
        for control in (occluder or {}).get("controls") or []:
            if isinstance(control, dict) and holds(control.get("label")):
                return True
        return False

    async def _surface_confirms(page: Any, selector: str, chosen: str) -> bool:
        try:
            return bool(
                await page.evaluate(_COMMIT_SURFACE_JS, {**(await _probe_arg(page, selector)), "chosen": chosen})
            )
        except Exception:
            return False

    async def _semantic_commit_read(
        page: Any, selector: str, intended: str, typed: str, *, typed_trusted: bool
    ) -> str | None:
        # Decisive-accept-only: the committed value when the semantic probe proves the commit, else
        # None ("unknown") — the caller's shape heuristics run unchanged on None, so this tier can
        # never refuse a commit or swallow a failure. Unreadable probe = unknown for the same reason.
        try:
            read = await page.evaluate(
                _SEMANTIC_COMMIT_STATE_JS,
                {
                    **(await _probe_arg(page, selector)),
                    "intended": intended,
                    "typed": typed,
                    "typedTrusted": typed_trusted,
                },
            )
        except Exception:
            return None
        if isinstance(read, dict) and read.get("committed") and str(read.get("value") or "").strip():
            LOG.debug("taskv3 semantic commit accept", selector=selector, via=str(read.get("via") or ""))
            return str(read.get("value")).strip()
        return None

    async def _settled_commit_read(
        page: Any, selector: str, verify_args: dict[str, Any], chosen: str, pre_surface_hit: bool
    ) -> tuple[str, bool]:
        # One read at the settle beat, then a short bounded poll: a widget that commits after an async
        # round trip (a server-registered selection, a re-render that lands the label in a pill while
        # the input keeps an opaque id or nothing) must not be refused on a single instant's read. A
        # surface that already showed this label BEFORE the click proves nothing and is never consulted.
        readable = False
        committed = ""
        counted = False
        for attempt in range(4):
            if attempt:
                await asyncio.sleep(0.7)
            # Fail-closed: a list whose open-state cannot be read counts as still open, so the
            # equal-value acceptance in the verify JS never rests on a failed probe. Read BEFORE the
            # semantic tier: a dead click can REPLACE the live list (stamp and row tags die with the
            # container) and render a fresh matching highlight, so a popup that survives — per the
            # same vanished-stamp machinery the heuristics use — must suppress the aria accept too.
            sugg_list_open = True
            commit_evt = False
            if verify_args.get("suggTagged"):
                try:
                    sugg_list_open = bool(
                        await page.evaluate(_SUGG_LIST_STILL_OPEN_JS, await _probe_arg(page, selector))
                    )
                except Exception:
                    sugg_list_open = True
                # Read fresh each poll: an async widget may dispatch the committing input event a
                # beat after the click. Fail-closed on an unreadable probe, and never consult the
                # window flag when arming failed — it could hold a previous field's stale true.
                if verify_args.get("commitEvtArmed"):
                    try:
                        commit_evt = bool(await page.evaluate("() => !!window.__tv3_commit_evt"))
                    except Exception:
                        commit_evt = False
            if settings.TASK_V3_SEMANTIC_COMMIT_VERIFY:
                # One opportunity per CALL, and only inside this block: the poll early-returns on an
                # accept, so a per-attempt count would track widget latency, and a count taken
                # outside the block would report a 0% accept rate for a tier that cannot fire at all.
                if semantic_commit_stats is not None and not counted:
                    semantic_commit_stats.opportunities += 1
                    counted = True
                semantic = await _semantic_commit_read(
                    page,
                    selector,
                    chosen,
                    str(verify_args.get("typed") or ""),
                    typed_trusted=verify_args.get("typedTrusted") is True,
                )
                if semantic is not None:
                    if semantic_commit_stats is not None:
                        semantic_commit_stats.accepts += 1
                    return semantic, True
            try:
                read = await page.evaluate(
                    _VERIFY_COMMIT_JS,
                    {
                        **verify_args,
                        "suggListOpen": sugg_list_open,
                        "commitEvt": commit_evt,
                        "el": (await _probe_arg(page, selector))["el"],
                    },
                )
                readable = readable or read is not None
                committed = str(read or "").strip()
            except Exception as e:
                LOG.debug("taskv3 commit-verify read failed", selector=selector, error=str(e))
                committed = ""
            if committed:
                return committed, readable
            if not pre_surface_hit and await _surface_confirms(page, selector, chosen):
                return chosen, True
        return committed, readable

    async def _commit_typeahead(
        page: Any, selector: str, value: str, rounds: int, *, exact_only: bool = False, probe: str | None = None
    ) -> _TypeaheadPick:
        # Poll for the suggestion rows rendered IN REACTION to whatever is already typed into `selector`,
        # pick among them, click, and verify the field committed. When the widget DECLARES its rows the
        # commit is exact-only: a row is picked when its whole label IS `value`, never on a stem or
        # prefix hit, so an ambiguous reaction is always handed back rather than guessed at. Where
        # nothing declares a list, the finder returns the single winner it always did and the pick is
        # that row. `candidates` is the full-text reacting rows when a declared pick refused — the
        # caller reports these instead of guessing which one was meant; None when nothing reacted, when
        # a row was clicked, or on the undeclared path, which has no tie to report. committed is None
        # when a suggestion was clicked but no value landed.
        # `exact_only` is for a REDUCED query (the field holds less than `value`) on the undeclared
        # path: the rows on screen answer a broader question than the caller asked, so only a row whose
        # whole label IS `value` may be committed. `probe` is then what the field actually holds, which
        # is what the finder's reaction/overlap gate has to be given: rows answering "Il" need not share
        # a word with "Illinois".

        async def _find() -> dict[str, Any] | None:
            try:
                found = await page.evaluate(
                    _FIND_SUGGESTION_JS,
                    {"value": probe or value, "field": selector, "el": (await _probe_arg(page, selector))["el"]},
                )
            except Exception as e:
                LOG.debug("taskv3 typeahead suggestion-find failed", selector=selector, error=str(e))
                return None
            return found if isinstance(found, dict) and found.get("count") else None

        async def _full_rows() -> list[dict[str, Any]]:
            # The tagger truncates each label to 60 chars for payload size; the match must see the whole
            # text so a value that differs only past char 60, or a >60-char row, is not mismatched. With
            # no full-length read there is no list to match against -- the truncated labels would feed
            # both the uniqueness matcher and a whole-text click guard -- so the caller refuses instead.
            try:
                raw = await page.evaluate(_MENU_OPTION_TEXTS_JS, {"attr": "sugg"})
            except Exception as e:
                LOG.debug("taskv3 typeahead full-text read failed", selector=selector, error=str(e))
                return []
            if not isinstance(raw, list):
                return []
            # Never auto-click a navigational row. The tagger already refuses them, so this is a
            # floor under the pick, not the filter that does the work.
            return [o for o in raw if isinstance(o, dict) and isinstance(o.get("n"), int) and not o.get("nav")]

        def _pick(rows: list[dict[str, Any]]) -> int | None:
            # A declared row commits solely on an exact label match, never on inferred meaning — an
            # unmatched row, lone or not, is refused like any other so the model chooses explicitly.
            return _match_option_exact(value, rows)

        async def _resolve(found: dict[str, Any]) -> tuple[bool, list[dict[str, Any]], int | None, int]:
            # A widget that declares its rows hands the pick to the exact matcher over every row it
            # tagged. Where nothing declares one, the finder already reduced the reaction to a single
            # winner and there is nothing left to choose between — except under a reduced query, whose
            # rows answer a broader question than the caller asked. The 4th value is the declared
            # aria-setsize when it exceeds the rendered row count (0 otherwise), reported in the refusal
            # note so the model knows to name the option's full label rather than retry blindly.
            tagged = [o for o in (found.get("options") or []) if isinstance(o, dict) and isinstance(o.get("n"), int)]
            if not tagged:
                return bool(found.get("declared")), [], None, 0
            if found.get("declared"):
                rows = await _full_rows()
                declared_size = max((int(o.get("setsize") or 0) for o in rows), default=0)
                overflow = declared_size if rows and declared_size > len(rows) else 0
                idx = _pick(rows)
                if idx is None and overflow == 0:
                    # No exact-label winner over the complete rendered list — but "several rows" may be
                    # one candidate wearing more than one face (an a11y duplicate, a portal+inline
                    # render). Collapse only over the FULL list (never a partial/overflowed one), and
                    # only to a genuinely lone survivor; several distinct candidates still refuse below.
                    want = _exact_tier_key(value)
                    matched = [o for o in rows if _exact_tier_key(str(o.get("text") or "")) == want]
                    if len(matched) >= 2:
                        idx = _lone_duplicate_candidate(matched)
                return True, rows, idx, overflow
            return False, tagged, (_match_option_exact(value, tagged) if exact_only else 1), 0

        async def _row_info(n: int) -> dict[str, Any]:
            try:
                info = await page.evaluate(_SUGG_ROW_INFO_JS, {**(await _probe_arg(page, selector)), "n": n})
            except Exception:
                info = None
            return info if isinstance(info, dict) else {}

        # The base poll extends while the widget shows a visible in-flight indicator — the same
        # bounded busy extension the open->observe path applies: production pods run at a fraction
        # of a vCPU, so a fetch that renders instantly on a laptop lands seconds later there.
        found: dict[str, Any] | None = None
        soft_deadline = time.monotonic() + 0.4 * rounds
        hard_deadline = time.monotonic() + 8.0
        while True:
            await asyncio.sleep(0.4)
            found = await _find()
            if found is not None:
                break
            now = time.monotonic()
            if now >= hard_deadline:
                break
            if now >= soft_deadline:
                try:
                    busy = bool(await page.evaluate(_MENU_BUSY_JS, await _probe_arg(page, selector)))
                except Exception:
                    busy = False
                if not busy:
                    break
        if found is None:
            return _TypeaheadPick(None, None, False, None, clicked=False, declared=False)
        declared_rows, rows, idx, overflow = await _resolve(found)
        if idx is None:
            note = (
                f"the list declares {overflow} rows and only {len(rows)} are rendered — type the option's full label"
                if declared_rows and overflow
                else None
            )
            return _TypeaheadPick(
                None,
                None,
                False,
                rows if declared_rows else None,
                clicked=False,
                declared=declared_rows,
                note=note,
            )
        best_txt = next((str(o.get("text") or "") for o in rows if o.get("n") == idx), value)
        info = await _row_info(idx)
        from_focus = bool(info.get("fromFocus"))
        declared = [str(v) for v in (info.get("declared") or []) if isinstance(v, str)]
        # Click the picked row. If the list re-rendered and dropped the tags, re-find/re-pick and click
        # once more — never blind-press ArrowDown/Enter, which would commit whichever row the widget
        # happens to highlight rather than the one just matched.
        # A pick from a focus-opened list is verified under the pick contract, which needs the hidden
        # values as they were BEFORE the click so a stale leftover cannot read as the commit.
        pre_hidden: list[str] = []
        pre_value = value
        try:
            raw_hidden = await page.locator(selector).first.evaluate(_HIDDEN_VALUES_JS, timeout=2000)
            if isinstance(raw_hidden, list):
                pre_hidden = [str(v) for v in raw_hidden if isinstance(v, str)]
        except Exception:
            pre_hidden = []
        pre_value_read = await _read_field_value(page, selector)
        pre_value = pre_value_read if pre_value_read is not None else value
        clicked = False
        pre_surface_hit = await _surface_confirms(page, selector, best_txt)
        try:
            await page.evaluate(_STAMP_SUGG_LIST_JS, {"attr": "sugg", "n": idx})
        except Exception:
            LOG.debug("taskv3 suggestion list stamp failed", selector=selector)
        # A failed arm must read as "no event", not as whatever a PREVIOUS field's probe left in the
        # window flag — the reset lives inside the arm JS, so its success is tracked here.
        commit_evt_armed = False
        try:
            commit_evt_armed = bool(await page.evaluate(_ARM_COMMIT_EVENT_JS, await _probe_arg(page, selector)))
        except Exception:
            LOG.debug("taskv3 commit-event arm failed", selector=selector)
        try:
            if not await _click_stamped_row(page, f'[data-tv3-sugg="{idx}"]', best_txt, 3000):
                raise RuntimeError("stamped suggestion row is no longer the matched row")
            clicked = True
        except Exception:
            try:
                refound = await _find()
                if refound is not None:
                    declared_rows, rows, idx2, _overflow = await _resolve(refound)
                    if idx2 is not None:
                        idx = idx2
                        best_txt = next((str(o.get("text") or "") for o in rows if o.get("n") == idx), value)
                        info = await _row_info(idx)
                        from_focus = bool(info.get("fromFocus"))
                        declared = [str(v) for v in (info.get("declared") or []) if isinstance(v, str)]
                        pre_surface_hit = await _surface_confirms(page, selector, best_txt)
                        try:
                            await page.evaluate(_STAMP_SUGG_LIST_JS, {"attr": "sugg", "n": idx})
                        except Exception:
                            LOG.debug("taskv3 suggestion list stamp failed", selector=selector)
                        commit_evt_armed = False
                        try:
                            commit_evt_armed = bool(
                                await page.evaluate(_ARM_COMMIT_EVENT_JS, await _probe_arg(page, selector))
                            )
                        except Exception:
                            LOG.debug("taskv3 commit-event arm failed", selector=selector)
                        clicked = await _click_stamped_row(page, f'[data-tv3-sugg="{idx}"]', best_txt, 3000)
            except Exception:
                clicked = False
        if not clicked:
            # a suggestion surfaced but we couldn't click it — report un-committed, don't guess
            LOG.debug("taskv3 typeahead could not click suggestion", selector=selector, suggestion=best_txt)
            return _TypeaheadPick(None, best_txt, False, None, clicked=False, declared=declared_rows)
        await asyncio.sleep(0.3)
        # A row the FOCUS click revealed was offered, not filtered: verify it under the pick
        # contract (the value must BE the chosen label), not the typeahead's change-based one.
        # `typed` must be what the field actually HELD, which under a reduced query is the rung —
        # comparing against the fuller request would let leftover rung text read as the commit.
        committed, readable = await _settled_commit_read(
            page,
            selector,
            {
                "field": selector,
                "typed": pre_value if from_focus else (value if probe is None else probe),
                "typedTrusted": (not from_focus) or pre_value_read is not None,
                "chosen": best_txt,
                "noSuggestionList": from_focus,
                "suggTagged": True,
                "commitEvtArmed": commit_evt_armed,
                "declaredRows": declared_rows,
                "fieldDeclared": await _field_declares_list(page, selector),
                "preHidden": pre_hidden,
                "chosenValues": declared,
            },
            best_txt,
            pre_surface_hit,
        )
        return _TypeaheadPick(
            committed or None,
            best_txt,
            readable,
            None,
            clicked=True,
            declared=declared_rows,
            pre_surface_hit=pre_surface_hit,
        )

    async def _read_field_value(page: Any, selector: str) -> str | None:
        try:
            return str(await page.locator(selector).first.input_value(timeout=2000))
        except Exception:
            pass
        # input_value() raises on anything that is not a form control, and a contenteditable combobox
        # is one of those: without this its pre-type text is unreadable and a refusal leaves our query.
        try:
            read = await page.locator(selector).first.evaluate(
                "el => (el.isContentEditable ? el.textContent : el.value)", timeout=2000
            )
        except Exception:
            return None
        return str(read) if isinstance(read, str) else None

    async def _field_declares_list(page: Any, selector: str) -> bool:
        # Fail-closed: without a declared list there is no row rule to lean on, so the caller keeps the
        # path it had rather than asking a widget it cannot read a second, looser question.
        try:
            return bool(await page.evaluate(_FIELD_DECLARES_LIST_JS, await _probe_arg(page, selector)))
        except Exception:
            return False

    async def _restore_pre_type_value(page: Any, selector: str, pre_value: str | None, typed: list[str]) -> None:
        # A refusal must hand the field back the way it found it. Leaving our query behind overwrites
        # whatever the page had already put there -- a cascade-filled code, a prefilled dial code -- with
        # text the widget never accepted, and a later read of the form cannot tell the two apart. Only
        # OUR text may be taken back: if the field now holds something else, the widget wrote it in
        # reaction and that value is the page's, not ours to discard.
        if pre_value is None:
            return
        current = await _read_field_value(page, selector)
        if current is None or current == pre_value:
            return
        if current.strip() and not any(current.strip().lower() == q.strip().lower() for q in typed):
            return
        try:
            await page.fill(selector, pre_value, timeout=15000)
        except Exception:
            LOG.debug("taskv3 pre-type value restore failed", selector=selector)

    async def _type_and_commit(page: Any, selector: str, value: str, rounds: int) -> tuple[_TypeaheadPick, str | None]:
        # Keystroke-type (so a widget's async suggestion fetch fires on real key events). Snapshot the
        # visible DOM BEFORE the focus click, not just before typing: a widget that opens its full list on
        # focus and then filters it in place keeps the same row nodes, so a snapshot taken after the click
        # marks every option as pre-existing and the reaction gate rejects the rows the keystrokes kept.
        # Static page text that merely shares a word with the value is still excluded — it was visible
        # before the click too.
        presnapshot_ok = True
        # Read the field BEFORE clearing it: a refusal further down owes this value back, and after the
        # fill() below no one can recover it.
        pre_value = await _read_field_value(page, selector)
        try:
            await page.evaluate(_PRESNAPSHOT_JS)
        except Exception:
            presnapshot_ok = False
            LOG.info("taskv3 typeahead pre-snapshot failed; skipping suggestion probe", selector=selector)
        focused, occluder = await _focus_for_typing(page, selector)
        if not focused:
            raise _FieldCovered(selector, occluder)
        if presnapshot_ok:
            # Focus may reveal help text or a validation note as well as a menu; only rows of a list
            # are a reaction the finder may pick from, so everything else focus revealed is marked too.
            try:
                await page.evaluate(_FOCUS_SNAPSHOT_JS, await _probe_arg(page, selector))
            except Exception:
                pass
        await page.fill(selector, "", timeout=15000)
        await page.type(selector, value, delay=15, timeout=15000)
        if not presnapshot_ok:
            # Without the pre-snapshot the reaction-gate can't tell a new suggestion from static page
            # text, so don't run the finder ungated (it could click unrelated content) — leave the typed
            # value and let the caller re-observe.
            return _TypeaheadPick(None, None, False, None, clicked=False, declared=False), pre_value
        return await _commit_typeahead(page, selector, value, rounds), pre_value

    async def _close_lingering_typeahead_list(
        page: Any, selector: str, committed: str | None, *, surface_vouched_pre_click: bool = False
    ) -> ToolResult | None:
        # A declared field's widget can re-search on the value it just committed and leave its list
        # open, covering whatever the form has below it. Close it with Escape, best-effort: None means
        # the caller's own OK stands; a ToolResult means closing was not safe to treat as a no-op.
        try:
            still_open = bool(await page.evaluate(_TYPEAHEAD_LIST_OPEN_JS, await _probe_arg(page, selector)))
        except Exception:
            return None
        if not still_open:
            return None
        try:
            await page.press(selector, "Escape", timeout=5000)
        except Exception:
            LOG.debug("taskv3 lingering typeahead list close failed", selector=selector)
            return None
        await asyncio.sleep(0.25)
        try:
            await page.wait_for_selector(selector, state="visible", timeout=500)
        except Exception:
            return ToolResult.error(
                f"selected value for {selector}, but its still-open list covered the form, and closing it "
                "with Escape dismissed the field's own container — re-observe before continuing"
            )
        after = await _read_field_value(page, selector)
        if after is None:
            try:
                after = str(await page.evaluate(_ANCHOR_SURFACE_JS, await _probe_arg(page, selector)) or "") or None
            except Exception:
                after = None
        if committed is not None and after is not None and after.strip() != committed.strip():
            # A pill/side-surface commit keeps an opaque id (or nothing) in the input, so a raw value
            # mismatch is not a revert while the committed surface still holds the label after Escape.
            # A surface that already vouched BEFORE the pick click proves nothing about survival —
            # only a click-caused surface may absorb the mismatch (mirrors pre_surface_hit upstream).
            if surface_vouched_pre_click or not await _surface_confirms(page, selector, committed):
                return ToolResult.error(
                    f"selected {committed!r} for {selector}, but its still-open list did not just sit there: "
                    f"closing it with Escape changed the value to {after!r} — the commit did not survive; "
                    "re-observe and retry"
                )
        try:
            if bool(await page.evaluate(_TYPEAHEAD_LIST_OPEN_JS, await _probe_arg(page, selector))):
                LOG.debug("taskv3 typeahead list stayed open after Escape", selector=selector)
        except Exception:
            pass
        return None

    async def _typeahead_commit_verdict(
        page: Any, selector: str, committed: str | None, readable: bool
    ) -> tuple[CommitStatus, int]:
        # Route the typeahead's own commit truth through the unified classifier so the site gains INV-1
        # (a commit read off n≠1 → unverified) and INV-2 (unreadable → unverified) for free. `committed` (the
        # token-overlap result of _VERIFY_COMMIT_JS) is the value dimension the classifier cannot compute
        # itself, so it is handed in as committed_value; behavior on a single stable element is unchanged.
        matches = await _post_match_count(page, selector)
        # post carries the READABILITY dimension (INV-2), committed_value the value dimension. A field
        # read back empty is readable ("" has state) and did-not-commit; only an unreadable field (read
        # returned null → readable False) is INV-2 unverified. `committed or ""` keeps that split clean.
        post = {"value": committed or ""} if readable else None
        return _classify_commit(None, matches, post, committed_value=bool(committed)), matches

    # Input kinds that are never typeaheads — skip the suggestion probe (and its latency) for these.
    # `textarea` is included: free-text boxes never render a typeahead and would just pay the probe tax.
    _NON_TYPEAHEAD_TYPES = frozenset(
        {
            "textarea",
            "email",
            "tel",
            "number",
            "url",
            "password",
            "date",
            "datetime-local",
            "month",
            "time",
            "week",
            "color",
            "range",
        }
    )

    async def _unverifiable_because(page: Any, selector: str) -> str | None:
        # Returns the clause explaining why a check could not be run, or None when it could. "The
        # probe cannot carry this claim" is not one fact but three: a widget that unmounts its own
        # input, a control inside a component whose list may render beyond a pierced query's reach,
        # and a selector our probe cannot parse. Empty on failure -- claiming a reason we did not
        # establish would be its own false statement.
        try:
            reach = str(await page.evaluate(_PROBE_REACH_JS, await _probe_arg(page, selector)) or "")
        except Exception:
            return None
        if reach == "component":
            return "it is inside a component"
        if reach == "unprobeable":
            return "we cannot resolve that selector ourselves"
        return None

    async def _field_type(page: Any, selector: str) -> str:
        try:
            return (
                await page.eval_on_selector(
                    selector,
                    "el => el.tagName === 'TEXTAREA' ? 'textarea' : (el.getAttribute('type') || 'text').toLowerCase()",
                )
            ) or "text"
        except Exception:
            return "text"

    async def type_text(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args.get("selector")
        if not selector:
            return ToolResult.error("type needs a selector, or mark=N from the last look().")
        ambiguous = await _ambiguous_selector_error(page, selector)
        if ambiguous is not None:
            return ambiguous
        selector = await _resolve_mirrored_host_control(page, selector)
        text = _resolve_text(args.get("text", ""))
        press_enter = args.get("press_enter")
        clear = args.get("clear", True)
        # A typeahead silently rejects raw typed text — it only accepts a picked suggestion — and the
        # model does not reliably reach for select_combobox on its own. So after typing into a plain text
        # field, check whether the page REACTED with a suggestion list and, if so, commit the best match
        # here. Detection is behavioral (no per-site rules), so this holds across ATSes; non-text inputs
        # and append/enter typing skip it and fill normally (fast path, no polling).
        if text and clear and not press_enter and await _field_type(page, selector) not in _NON_TYPEAHEAD_TYPES:
            # A non-typeable anchor (a button/div, not an <input>/<textarea>/contenteditable) can never
            # take page.fill()'s keystrokes — but one that declares list semantics is a click-to-open
            # single-select in disguise, so route it to the same open→enumerate→pick path select_combobox
            # uses instead of letting fill() throw on it. A non-typeable anchor with no list semantics is
            # some other unhandled widget; leave it on today's path rather than guess.
            if not await _anchor_typeable(page, selector) and await _anchor_has_list_semantics(page, selector):
                return await _open_observe_pick(page, selector, text)
            # keystroke-type (via _type_and_commit) so a widget that fetches suggestions on key events —
            # not just on a single `input` from fill — still surfaces them, then commit the best match.
            try:
                pick, pre_value = await _type_and_commit(page, selector, text, rounds=3)
            except _FieldCovered as exc:
                return _covered_error(exc.selector, exc.occluder)
            except _FieldNotEditable as exc:
                return _not_editable_error(exc)
            if pick.suggestion is None and pick.candidates:
                # Several rows reacted and none was a unique precision match, so nothing was picked.
                # The raw text left behind is exactly what a typeahead discards, so "typed into X" here
                # is a false success -- name the rows instead and hand the pick to the tool that makes
                # one. Geometry must not break the tie; only the caller naming a row can. "NOT filled"
                # has to be true of the field as well as of the widget, so the query goes back out --
                # unless the rows are text-indisambiguable (same candidate, or a value-only distinction:
                # see _identical_text_rows_error), where retyping the same text can never pick a
                # different row and the query stays so the list and the rows' tags stay live instead.
                text_key = _exact_tier_key(text)
                same_text = [o for o in pick.candidates if _exact_tier_key(str(o.get("text") or "")) == text_key]
                if len(same_text) >= 2 and _lone_duplicate_candidate(same_text) is None:
                    # Leave the query (keeping the list and tags live) only when there is nothing to
                    # protect: a field that HELD a value must get it back, or the leftover query becomes
                    # every later call's restore baseline and the true value is gone for the run.
                    if not pre_value:
                        return _identical_text_rows_error(selector, text, same_text, note=pick.note)
                    await _restore_pre_type_value(page, selector, pre_value, [text])
                    return _identical_text_rows_error(selector, text, same_text, tags_live=False, note=pick.note)
                await _restore_pre_type_value(page, selector, pre_value, [text])
                return _ambiguous_rows_error(
                    selector,
                    text,
                    pick.candidates,
                    next_step="call select_combobox with the option's full text",
                    note=pick.note,
                )
            if pick.suggestion:
                verdict, matches = await _typeahead_commit_verdict(page, selector, pick.committed, pick.readable)
                if verdict is CommitStatus.OK:
                    if pick.declared:
                        closed = await _close_lingering_typeahead_list(
                            page, selector, pick.committed, surface_vouched_pre_click=pick.pre_surface_hit
                        )
                        if closed is not None:
                            return closed
                    return ToolResult.ok(
                        f"typed into {selector}; it is a typeahead — selected {pick.suggestion!r} "
                        f"(committed value: {pick.committed!r})"
                    )
                if verdict is CommitStatus.UNVERIFIED and matches != 1:
                    # INV-1: the field re-resolved to n≠1 after the click (remounted or now ambiguous), so
                    # there is no stable element to read the commit off — soft, not a false did-not-commit.
                    return ToolResult.ok(
                        f"clicked suggestion {pick.suggestion!r} for {selector}, but it re-resolved to {matches} "
                        "elements so the commit could not be verified — re-observe to confirm the value "
                        "before relying on it"
                    )
                if verdict is CommitStatus.UNVERIFIED:
                    # INV-2 (unreadable). The verifier pierces open roots and also reads the element the
                    # executor resolved, so inside a component the failure is established rather than
                    # guessed. A list portaled elsewhere, or a field in a closed root, is still beyond both
                    # -- and that is exactly what the read reports by returning nothing, so the softening
                    # follows the read.
                    why = await _unverifiable_because(page, selector)
                    if why:
                        return ToolResult.ok(
                            f"clicked suggestion {pick.suggestion!r} for {selector}; {why}, so the commit could not "
                            "be verified — re-observe to confirm the value before relying on it"
                        )
                    return ToolResult.error(
                        f"clicked suggestion {pick.suggestion!r} for {selector} but it did not commit — the field is "
                        "NOT filled; re-observe and retry, do not proceed"
                    )
                # DID_NOT_COMMIT: the field is NOT filled. The loop then skips any later click or Enter
                # in the same batch -- it may be an unvalidated submit, and no production submit guard
                # exists yet.
                return ToolResult.error(
                    f"clicked suggestion {pick.suggestion!r} for {selector} but it did not commit — the field is NOT "
                    "filled; re-observe and retry, do not proceed"
                )
            # No suggestion list surfaced. The finder pierces open shadow roots, so it can see a list
            # inside one -- but not one the widget portals elsewhere in the page or renders in a
            # closed root, so inside a component this is still not evidence of absence. Saying
            # "typed into X" there reads as a verified fill, and on a typeahead that silently rejects
            # raw text it turns an honest failure into a confident wrong answer on a form we submit.
            why = await _unverifiable_because(page, selector)
            if why:
                return ToolResult.ok(
                    f"typed into {selector} — {why}, so the typeahead check could not see it and no "
                    "commit was verified; re-observe to confirm the value before relying on it"
                )
            return ToolResult.ok(f"typed into {selector}")
        # The types that skip the typeahead probe still must not be typed into through an overlay.
        # They reach fill()/type(), which do no hit-testing, so nothing here would fail on its own --
        # the text simply lands in a field the person could not have reached.
        try:
            reachable, _, occluder = await _reachable_for_typing(page, selector)
        except _FieldNotEditable as exc:
            return _not_editable_error(exc)
        if not reachable:
            return _covered_error(selector, occluder)
        if clear:
            await page.fill(selector, text, timeout=15000)
        else:
            await page.type(selector, text, timeout=15000)
        if press_enter:
            await page.press(selector, "Enter")
        return ToolResult.ok(f"typed into {selector}")

    async def _anchor_typeable(page: Any, selector: str) -> bool:
        # Fail-open on a probe error: a transient evaluate failure must not flip a valid typeahead into
        # a refusal (the far more common caller is select_combobox, whose contract is to type).
        try:
            return bool(await page.evaluate(_ANCHOR_TYPEABLE_JS, await _probe_arg(page, selector)))
        except Exception:
            return True

    async def _anchor_has_list_semantics(page: Any, selector: str) -> bool:
        # Fail-closed on a probe error: unlike typeability, a false positive here would route a plain
        # button into the open-list picker instead of today's fill/type path.
        try:
            return bool(await page.evaluate(_ANCHOR_LIST_SEMANTICS_JS, await _probe_arg(page, selector)))
        except Exception:
            return False

    async def _open_observe_pick(page: Any, selector: str, value: str, *, close_open_menu: bool = False) -> ToolResult:
        # Commit a click-to-open single-select in ONE call: open the list, enumerate the option rows the
        # click rendered (v3's own _FIND_MENU_JS tags them data-tv3-menu="N"), deterministically pick the
        # match, click it, and VERIFY — reusing the same commit-verify contract as the typeahead path.
        # This is the branch v3 lacked: a non-searchable react-select (a real <input> that never filters)
        # and a non-typeable button/div anchor both land here. When no deterministic match is found the
        # tool returns a truthful did-not-commit AND the observed options, so the model resolves a
        # genuinely unexpected widget by sight (look()/act-by-mark) — never a blind text-LLM guess.
        if close_open_menu:
            # A prior keystroke attempt may have opened this widget's list; close it so the pre-snapshot
            # captures the CLOSED page and _FIND_MENU_JS counts only rows THIS open-click renders. Escape
            # is sent ONLY once a menu is confirmed open (aria-expanded=true) — a stray Escape with nothing
            # open would bubble to and close a surrounding dialog, discarding the form. If the widget does
            # not expose aria-expanded, we skip Escape and let the reopen self-heal below handle a toggle.
            try:
                menu_open = bool(await page.evaluate(_MENU_OPEN_JS, await _probe_arg(page, selector)))
            except Exception:
                menu_open = False
            if menu_open:
                try:
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.1)
                except Exception:
                    pass

        async def _open_and_enumerate() -> tuple[dict[str, Any] | None, ToolResult | None]:
            try:
                await page.evaluate(_PRESNAPSHOT_JS)
            except Exception:
                return None, ToolResult.error(
                    f"could not snapshot the page to open {selector}'s option list — the field is NOT "
                    "filled; re-observe, then click the control and pick the option you want"
                )
            try:
                # 5s, not the 15s a routine click waits: the control is already present (we just typed
                # into it, or it is a visible button), so it opens at once — a long wait here only delays
                # the error on a field that unmounted itself, which must fail loudly, not slowly.
                await page.click(selector, timeout=5000)
            except Exception:
                return None, ToolResult.error(
                    f"could not click {selector} to open its option list — the field is NOT filled; "
                    "re-observe and retry"
                )
            # 2.4s of polling normally; while the widget shows a visible in-flight indicator (a busy
            # row, a spinner) the rows are still coming, so the poll wait extends — production pods run
            # at a fraction of a vCPU and a fetch that renders instantly on a laptop lands seconds later
            # there. The extension is bounded: a permanently-busy page stops at the hard cap.
            soft_deadline = time.monotonic() + 2.4
            hard_deadline = time.monotonic() + 8.0
            while True:
                await asyncio.sleep(0.4)
                try:
                    menu = await page.evaluate(_FIND_MENU_JS, await _probe_arg(page, selector))
                except Exception as e:
                    LOG.debug("taskv3 open-observe-pick menu-find failed", selector=selector, error=str(e))
                    menu = None
                if isinstance(menu, dict) and menu.get("count"):
                    return menu, None
                now = time.monotonic()
                if now >= hard_deadline:
                    return None, None
                if now >= soft_deadline:
                    try:
                        busy = bool(await page.evaluate(_MENU_BUSY_JS, await _probe_arg(page, selector)))
                    except Exception:
                        busy = False
                    if not busy:
                        return None, None

        found, err = await _open_and_enumerate()
        if err is not None:
            return err
        if found is None:
            # The list may have been open on entry (a prior call, or an Escape the widget ignored), so the
            # open-click above TOGGLED it shut. Try once more: the second open-click reopens it and the
            # re-snapshot inside _open_and_enumerate makes the reopened rows read as new.
            found, err = await _open_and_enumerate()
            if err is not None:
                return err
        if not isinstance(found, dict) or not found.get("count"):
            # Last resort before declaring no list: a menu whose rows survive close (kept-alive nodes the
            # snapshots keep reading as pre-existing) never shows up as a reaction however often we
            # reopen it. After two open-clicks on the anchor, a FLOATING row list next to it is its own;
            # 'any' still refuses in-flow static content, and the pick below still exact-matches and
            # commit-verifies before anything is reported filled.
            try:
                found = await page.evaluate(_FIND_MENU_JS, {**(await _probe_arg(page, selector)), "reuse": "any"})
            except Exception:
                found = None
        if not isinstance(found, dict) or not found.get("count"):
            # The open-click rendered no enumerable option list (portalled/closed-root, or not a menu).
            # Truthful did-not-commit; the model's vision handles it from here.
            return ToolResult.error(
                f"opened {selector} but no option list rendered to pick {value!r} from — the field is NOT "
                "filled; if the field accepts free text, fill it with type() instead (select_combobox is "
                "only for fields that commit from a list); otherwise look() at the control and click the "
                "option you want"
            )
        # Read the whole tagged list at full length so the match is neither missed on a >60-char label
        # nor computed over a truncated ≤15 slice (which would let "unique in the first 15" stand in for
        # "unique in the menu").
        count = int(found.get("count") or 0)
        read: list[dict[str, Any]] = []
        try:
            full_rows = await page.evaluate(_MENU_OPTION_TEXTS_JS, {"attr": "menu"})
            if isinstance(full_rows, list):
                read = [o for o in full_rows if isinstance(o, dict) and isinstance(o.get("n"), int)]
        except Exception as e:
            LOG.debug("taskv3 open-observe-pick full-text read failed", selector=selector, error=str(e))

        def _n_order(o: dict[str, Any]) -> int:
            n = o.get("n")
            return n if isinstance(n, int) else 1 << 30

        # `overflowed` = the enumerated set is not the whole list, so uniqueness cannot be established and
        # ALL auto-commit is refused. That is true when the full read failed, when `_FIND_MENU_JS` tagged
        # more rows than the read returned, OR when a row's `aria-setsize` declares more options than were
        # rendered (a virtualised list whose window is all that is in the DOM — count == len(read) there).
        declared = max((int(o.get("setsize") or 0) for o in read), default=0)
        overflowed = not read or count > len(read) or declared > len(read) or bool(found.get("partial"))
        rows = read or (found.get("options") or [])
        rows.sort(key=_n_order)
        # Never auto-click a navigational row (`<a href>`/`<button>`/menuitem): `_FIND_MENU_JS` enumerates
        # them because it only reports, but clicking one would leave the form.
        options = [o for o in rows if not o.get("nav")]
        if rows and not options:
            # Refusing to auto-click is not the same as having nothing to show: hand the model the marks
            # so a button-built select is one deliberate click away instead of an empty listing.
            shown_nav = "; ".join(
                f'[data-tv3-menu="{o.get("n")}"] {str(o.get("text") or "")[:60]!r}' for o in rows[:15]
            )
            return ToolResult.error(
                f"opened {selector} but every row is a link/button this tool will not auto-click ({shown_nav}"
                f"{'; +' + str(len(rows) - 15) + ' more' if len(rows) > 15 else ''}) — the field is NOT filled; "
                'if one of them is the option you want, click it by its [data-tv3-menu="N"] selector'
            )

        async def _scroll_search_menu_option() -> tuple[int | None, str | None, list[dict[str, Any]], bool]:
            # A virtualised listbox only ever holds a window of rows in the DOM, so `value` may sit
            # outside what we already read. Drive the scroller `_FIND_MENU_JS` tagged, re-enumerating
            # after each step and matching over everything accumulated so far, keyed by TEXT: the
            # virtualiser recycles nodes and `data-tv3-menu` numbers are reassigned 1..N on every scan,
            # so a number from an earlier window is not an identity. An exact hit commits at once; a
            # forward-prefix hit ("United States" -> "United States Minor Outlying Islands") is only
            # trusted once the scroller has been driven to its end, because the exact row may still be
            # below. A label seen at two different list positions (or twice in one window) is two rows
            # wearing one text and is refused as ambiguous rather than collapsed by the dedupe — but only
            # when both were seen before an exact hit committed: an exact hit is taken at first sight.
            seen: dict[str, dict[str, Any]] = {}
            seen_top: dict[str, float] = {}
            seen_pos: dict[str, list[float]] = {}
            all_pos: set[float] = set()
            ambiguous: set[str] = set()
            extent = 0.0

            last_fingerprint: str | None = None

            async def _scan(top: float | None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
                nonlocal last_fingerprint, extent
                try:
                    state = await page.evaluate(_MENU_SCROLLER_STEP_JS, {"top": top})
                except Exception:
                    state = None
                if not isinstance(state, dict):
                    return None, []
                # Read as soon as the scroller shows a different first row (it re-rendered), else after
                # a settle cap only a slow window pays; the clamped last window never changes and
                # simply waits the cap.
                settle_until = time.monotonic() + 0.4
                while True:
                    await asyncio.sleep(0.02)
                    try:
                        fp = await page.evaluate(_MENU_WINDOW_FINGERPRINT_JS)
                    except Exception:
                        fp = None
                    if (isinstance(fp, str) and fp != last_fingerprint) or time.monotonic() >= settle_until:
                        break
                last_fingerprint = fp if isinstance(fp, str) else last_fingerprint
                # Re-read the scroller AFTER the settle: a page appended during the wait must show in
                # the extent this scan reports, or the bottom check would call the walk complete.
                try:
                    settled = await page.evaluate(_MENU_SCROLLER_STEP_JS, {"top": None})
                    if isinstance(settled, dict):
                        state = settled
                except Exception:
                    pass
                try:
                    await page.evaluate(_FIND_MENU_JS, await _probe_arg(page, selector))
                    texts_raw = await page.evaluate(_MENU_OPTION_TEXTS_JS, {"attr": "menu"})
                except Exception:
                    texts_raw = None
                current: list[dict[str, Any]] = []
                here = float(state.get("scrollTop") or 0)
                extent = max(extent, float(state.get("scrollHeight") or 0))
                counts: dict[str, int] = {}
                if isinstance(texts_raw, list):
                    for o in texts_raw:
                        if not isinstance(o, dict) or not isinstance(o.get("n"), int):
                            continue
                        text = str(o.get("text") or "")
                        if not text:
                            continue
                        # Ambiguity is judged on the matcher's canonical form ("US", "us", "U S" with a
                        # zero-width space are one label), while `seen` keeps the raw text for display.
                        key = _canon_label(text)
                        counts[key] = counts.get(key, 0) + 1
                        current.append(o)
                        pos = o.get("pos")
                        if isinstance(pos, (int, float)):
                            all_pos.add(round(float(pos)))
                            positions = seen_pos.setdefault(key, [])
                            positions.append(float(pos))
                            # A row straddling two windows reads the same top (±subpixel rounding).
                            if max(positions) - min(positions) > 3:
                                ambiguous.add(key)
                        seen[text] = o
                        seen_top.setdefault(text, here)
                ambiguous.update(k for k, n in counts.items() if n > 1)
                return state, current

            def _is_ambiguous(text: str) -> bool:
                return _canon_label(text) in ambiguous

            def _match_seen() -> str | None:
                texts = [t for t, o in seen.items() if not o.get("nav") and not _is_ambiguous(t)]
                hit = _match_menu_option(value, [{"n": k, "text": t} for k, t in enumerate(texts)])
                return texts[hit] if hit is not None else None

            def _row_n(current: list[dict[str, Any]], text: str) -> int | None:
                fresh = next((o for o in current if str(o.get("text") or "") == text and not o.get("nav")), None)
                return fresh.get("n") if fresh is not None else None

            want = _canon_label(value)
            deferred: str | None = None

            def _exact_here(current: list[dict[str, Any]]) -> tuple[int, str] | None:
                nonlocal deferred
                text = _match_seen()
                if text is None:
                    return None
                if _canon_label(text) == want:
                    n = _row_n(current, text)
                    return (n, text) if n is not None else None
                deferred = text
                return None

            # Read the window the open-click rendered first (the common in-window hit costs one scan and
            # never moves the list), then walk from the top so the scan order is position-independent.
            state, current = await _scan(None)
            if state is None:
                return None, None, [], False
            start_top = float(state.get("scrollTop") or 0)
            step = max(1.0, float(state.get("clientHeight") or 1))
            reached_end = False
            target = 0.0
            prev_top: float | None = None
            extent_before = float(state.get("scrollHeight") or 0)
            deadline = time.monotonic() + _SCROLL_SEARCH_BUDGET_S
            # An exact hit is taken, but the walk goes on to the list end to catch a second row wearing
            # the same text; only then is the hit clicked. Two identical labels are refused like they
            # are in a fully rendered list.
            hit_text: str | None = None
            for _ in range(200):
                if hit_text is None:
                    hit = _exact_here(current)
                    if hit is not None:
                        hit_text = hit[1]
                elif _is_ambiguous(hit_text):
                    break
                if time.monotonic() > deadline:
                    break
                state, current = await _scan(target)
                if state is None:
                    break
                top = float(state.get("scrollTop") or 0)
                if top == prev_top:
                    # At the bottom: a list that appends a page on reaching its end grows only after a
                    # request; give it a beat and walk on if the extent moved, else the walk is done.
                    await asyncio.sleep(0.3)
                    grown, current = await _scan(None)
                    if grown is not None and float(grown.get("scrollHeight") or 0) > extent_before:
                        extent_before = float(grown.get("scrollHeight") or 0)
                        prev_top = None
                        target = top + step
                        continue
                    reached_end = True
                    break
                prev_top = top
                target = top + step

            def _rows_cover_extent() -> bool:
                # The rows seen tile the scroller's whole extent (no gap wider than ~1.5 rows, none at
                # the tail): only then has the walk shown the full list. A scroller that grows past its
                # rendered rows without re-rendering (or rendered too late) leaves gaps, and a prefix
                # match over a partial list is not a match.
                if len(all_pos) < 2 or extent <= 0:
                    return False
                ps = sorted(all_pos)
                gaps = [b - a for a, b in zip(ps, ps[1:]) if b - a > 0]
                pitch = sorted(gaps)[len(gaps) // 2] if gaps else 0.0
                if pitch <= 0:
                    return False
                if ps[0] > 1.5 * pitch or extent - ps[-1] > 2.5 * pitch:
                    return False
                return all(g <= 1.5 * pitch for g in gaps)

            # A walk that was cut short (deadline, cap, scan failure) or left gaps (a window that never
            # rendered in time) has not shown the rest of the list, so even its exact hit is not
            # clicked: a twin may sit in what was not seen. The caller reports the window as cut short.
            if hit_text is not None and not _is_ambiguous(hit_text) and reached_end and _rows_cover_extent():
                _, current = await _scan(seen_top.get(hit_text, 0.0))
                n = _row_n(current, hit_text)
                if n is not None:
                    return n, hit_text, list(seen.values()), reached_end
            if hit_text is not None and _is_ambiguous(hit_text):
                reached_end = True
            covered = reached_end and _rows_cover_extent()
            if covered and deferred is not None and _match_seen() == deferred:
                _, current = await _scan(seen_top.get(deferred, 0.0))
                n = _row_n(current, deferred)
                if n is not None:
                    return n, deferred, list(seen.values()), reached_end
            # Leave the list where the open-click rendered it so the model's next look() matches the
            # window it already reasoned about. `data-tv3-menu` numbers from earlier windows are not
            # identities, so a full scan reports the option TEXTS it saw and a cut-short one reports only
            # the rows live in the restored window.
            _, current = await _scan(start_top)
            for text, o in seen.items():
                if _is_ambiguous(text):
                    o["ambiguous"] = True
            # A walk that reached the end but left gaps is reported as cut short: its rows are not the
            # whole list, so the definitive no-match/ambiguity verdicts do not apply.
            definitive = reached_end and (_is_ambiguous(hit_text) if hit_text is not None else _rows_cover_extent())
            return None, None, (list(seen.values()) if definitive else current), definitive

        # collapse_duplicates only here: `options` is the COMPLETE, non-overflowed list this call just
        # read in full, so "several rows matched" can safely be checked for one candidate wearing more
        # than one face. The scroll-search's own `_match_seen` call below builds rows from an
        # accumulated, possibly-partial scan and must never collapse on that incomplete evidence.
        idx = None if overflowed else _match_menu_option(value, options, collapse_duplicates=True)
        matched_from_scroll: str | None = None
        scanned_all = False
        if idx is None and overflowed:
            idx, matched_from_scroll, accumulated_rows, scanned_all = await _scroll_search_menu_option()
            if idx is not None and matched_from_scroll is not None:
                options = [{"n": idx, "text": matched_from_scroll, "nav": False}]
                rows = options
            elif accumulated_rows:
                if not scanned_all:
                    accumulated_rows.sort(key=_n_order)
                rows = accumulated_rows
                options = [o for o in rows if not o.get("nav")]
        if idx is None:
            # Match over the FULL list above, but bound the error PAYLOAD: enumerate at most 15 rows,
            # each ≤60 chars, so a miss on a 250-option country list does not ship a 15KB tool message.
            shown = options[:15]
            if scanned_all:

                def _fold(t: str) -> str:
                    return " ".join(t.replace(",", " ").replace("'", "").split()).casefold()

                want_cf = _fold(value)
                contenders = [
                    str(o.get("text") or "")
                    for o in options
                    if _fold(str(o.get("text") or "")) == want_cf
                    or _fold(str(o.get("text") or "")).startswith(want_cf + " ")
                ][:15]
                if contenders:
                    named = "; ".join(repr(t[:60]) for t in contenders)
                    return ToolResult.error(
                        f"{value!r} is ambiguous in {selector}'s list — it names more than one option "
                        f"({named}); pass the one option's full text"
                    )
                vocab = "; ".join(repr(str(o.get("text") or "")[:60]) for o in shown)
                more = f"; +{len(options) - len(shown)} more" if len(options) > len(shown) else ""
                return ToolResult.error(
                    f"{value!r} matched no option in {selector}'s list — scrolled through all "
                    f"{len(options)} options ({vocab}{more}); pass one option's exact text"
                )
            listing = "; ".join(f'[data-tv3-menu="{o.get("n")}"] {str(o.get("text") or "")[:60]!r}' for o in shown)
            if overflowed:
                return ToolResult.error(
                    f"{value!r} matched no option in the part of {selector}'s list we could read "
                    f"({listing}) — the list is longer than we could enumerate; scroll or look() to see "
                    'the rest, then click the option by its [data-tv3-menu="N"] selector'
                )
            # The list read here is complete (not overflowed), so a canon text shared by ≥2 shown rows is
            # the SAME case `_lone_duplicate_candidate` already refused: text alone cannot tell them
            # apart, and only a present `val` or label could. Surface every present one so the escape
            # this refusal offers is convertible (click the row the value or label names), not just
            # repeated text.
            shown_canon_texts = [_canon_label(str(o.get("text") or "")) for o in shown]
            text_counts: dict[str, int] = {}
            for t in shown_canon_texts:
                text_counts[t] = text_counts.get(t, 0) + 1

            def _listed_row(o: dict[str, Any], canon_text: str) -> str:
                entry = f'[data-tv3-menu="{o.get("n")}"] {str(o.get("text") or "")[:60]!r}'
                if text_counts[canon_text] >= 2:
                    same_text = [p for p, pt in zip(shown, shown_canon_texts) if pt == canon_text]
                    entry += _row_value_suffix(o, same_text)
                return entry

            listing = "; ".join(_listed_row(o, t) for o, t in zip(shown, shown_canon_texts))
            not_shown = len(options) - len(shown)
            if not_shown > 0:
                # More selectable rows exist than we listed — do not claim the value is absent.
                return ToolResult.error(
                    f"{value!r} matched no option among the first {len(shown)} of {len(options)} in "
                    f"{selector} ({listing}; +{not_shown} more) — scroll or look() to see the rest, then "
                    'click the option by its [data-tv3-menu="N"] selector'
                )
            return ToolResult.error(
                f"opened {selector} but no option matched {value!r}; the list shows {listing} — "
                'pick the right one by its [data-tv3-menu="N"] selector, or look() to see the full menu'
            )
        matched = next((str(o.get("text") or "") for o in options if o.get("n") == idx), value)
        # Read the field's committable value BEFORE the click. The verifier then rests on a CHANGE from
        # this (passed as `typed`, with noSuggestionList so the always-true listClosed cannot stand in for
        # one) — so leftover text a type attempt left in the field, whether or not a clear would succeed,
        # can never read back as the commit.
        typed_trusted = True
        try:
            pre_value = str(
                await page.eval_on_selector(
                    selector, "el => (el.isContentEditable ? (el.textContent || '') : (el.value || ''))"
                )
                or ""
            )
        except Exception:
            pre_value = ""
            typed_trusted = False
        pre_hidden: list[str] = []
        try:
            raw_hidden = await page.eval_on_selector(selector, _HIDDEN_VALUES_JS)
            if isinstance(raw_hidden, list):
                pre_hidden = [str(v) for v in raw_hidden if isinstance(v, str)]
        except Exception:
            pre_hidden = []
        probe = await _probe_arg(page, selector)
        try:
            pre_surface = str(await page.evaluate(_ANCHOR_SURFACE_JS, {"sel": selector, "el": probe["el"]}) or "")
        except Exception:
            pre_surface = ""
        row_selected = False
        multi_select = False
        try:
            sel_state = await page.evaluate(_MENU_ROW_SELECTED_JS, idx)
            if isinstance(sel_state, dict):
                row_selected = bool(sel_state.get("selected"))
                multi_select = bool(sel_state.get("multi"))
        except Exception:
            row_selected = False
        # In a declared multi-select, aria-selected IS the selection (the trigger may only summarize,
        # "2 selected"). Elsewhere it can mean "active/highlighted" (an opened list often marks its
        # first row), so the field counts as already holding the value only when its own committed
        # surface says so too.
        # Only state this control demonstrably owns corroborates: its own value and its own label/text.
        # A hidden input in the surrounding container may belong to another field.
        already_selected = row_selected and (multi_select or _surface_holds(matched, pre_surface, pre_value))
        if already_selected:
            # Nothing can CHANGE to prove a commit — and a click would TOGGLE the row off on any list
            # that multi-selects, declared or not. Leave it; close the list the way the widget closes
            # itself.
            try:
                if await page.evaluate(_MENU_OPEN_JS, probe):
                    await page.keyboard.press("Escape")
            except Exception:
                pass
            return ToolResult.ok(f"{matched!r} was already selected for {selector}; left it as is")
        chosen_values: list[str] = []
        try:
            raw_values = await page.evaluate(_MENU_ROW_VALUES_JS, idx)
            if isinstance(raw_values, list):
                chosen_values = [str(v) for v in raw_values if isinstance(v, str)]
        except Exception:
            chosen_values = []
        pre_surface_hit = await _surface_confirms(page, selector, matched)
        try:
            await page.evaluate(_STAMP_SUGG_LIST_JS, {"attr": "menu", "n": idx})
        except Exception:
            LOG.debug("taskv3 menu list stamp failed", selector=selector)
        try:
            if not await _click_stamped_row(page, f'[data-tv3-menu="{idx}"]', matched, 5000):
                raise RuntimeError("stamped menu row is no longer the matched row")
        except Exception:
            return ToolResult.error(
                f"opened {selector} and matched {matched!r} but could not click it — re-observe and click "
                f'[data-tv3-menu="{idx}"]'
            )
        await asyncio.sleep(0.3)
        committed, readable = await _settled_commit_read(
            page,
            selector,
            {
                "field": selector,
                "typed": pre_value,
                "chosen": matched,
                "chosenValues": chosen_values,
                "noSuggestionList": True,
                "typedTrusted": typed_trusted,
                "preHidden": pre_hidden,
                "preSurface": pre_surface,
            },
            matched,
            pre_surface_hit,
        )
        verdict, matches = await _typeahead_commit_verdict(page, selector, committed or None, readable)
        if verdict is CommitStatus.OK:
            return ToolResult.ok(f"selected {matched!r} for {selector} (committed value: {committed!r})")
        if verdict is CommitStatus.UNVERIFIED and matches != 1:
            return ToolResult.ok(
                f"selected {matched!r} for {selector}, but it re-resolved to {matches} elements so the "
                "commit could not be verified — re-observe to confirm the value before relying on it"
            )
        if verdict is CommitStatus.UNVERIFIED:
            why = await _unverifiable_because(page, selector)
            if why:
                return ToolResult.ok(
                    f"selected {matched!r} for {selector}; {why}, so the commit could not be verified — "
                    "re-observe to confirm the value before relying on it"
                )
            return ToolResult.error(f"clicked {matched!r} but {selector} did not commit a value")
        return ToolResult.error(f"clicked {matched!r} but {selector} did not commit a value")

    def _surface_holds(matched: str, surface: str, value: str) -> bool:
        def norm(t: str) -> str:
            return " ".join(t.split()).casefold()

        want = norm(matched)
        if not want:
            return False
        if norm(value) == want:
            return True

        def holds(own: str) -> bool:
            # The whole surface equal to the value wins first (an option like "UTC+01:00" carries its
            # own colon); otherwise the committed clause is what follows the label's FIRST ':' bounded
            # by '|' — a later "| Other field: Y" clause belongs to another field.
            if not own:
                return False
            if own == want:
                return True
            head = own.split("|")[0]
            clause = head.split(":", 1)[1].strip() if ":" in head else head.strip()
            return clause == want or want in [p.strip() for p in clause.split(",")]

        return any(holds(norm(part)) for part in surface.split("\u0001"))

    async def _commit_custom_combobox(page: Any, selector: str, value: str) -> ToolResult:
        # Shared custom-combobox commit — the ONE path select_combobox and select_option's non-native
        # branch both route through. Two mechanisms, one tool call: a TYPEAHEAD (searchable react-select /
        # spl-autocomplete) commits by keystroke-type -> WAIT for the reacting suggestion -> click ->
        # verify; a CLICK-TO-OPEN single-select (non-searchable react-select, button/div listbox) commits
        # by open -> observe the rendered options -> pick the match -> verify. A non-typeable anchor goes
        # straight to the open path; a typeable anchor tries typeahead first and falls through to the open
        # path only when NOTHING reacts to keystrokes (the widget doesn't filter). Fails loudly rather than
        # leaving raw typed text a widget won't accept (a false "filled" — the failure mode this prevents).

        async def _typeahead_verdict_result(
            opt_txt: str,
            committed: str | None,
            readable: bool,
            *,
            declared: bool,
            surface_vouched_pre_click: bool = False,
        ) -> ToolResult:
            verdict, matches = await _typeahead_commit_verdict(page, selector, committed, readable)
            if verdict is CommitStatus.OK:
                if declared:
                    closed = await _close_lingering_typeahead_list(
                        page, selector, committed, surface_vouched_pre_click=surface_vouched_pre_click
                    )
                    if closed is not None:
                        return closed
                return ToolResult.ok(f"selected {opt_txt!r} for {selector} (committed value: {committed!r})")
            if verdict is CommitStatus.UNVERIFIED and matches != 1:
                # INV-1: re-resolved to n≠1 after the click — no stable element to read the commit off.
                return ToolResult.ok(
                    f"selected {opt_txt!r} for {selector}, but it re-resolved to {matches} elements so the "
                    "commit could not be verified — re-observe to confirm the value before relying on it"
                )
            if verdict is CommitStatus.UNVERIFIED:
                # INV-2 (unreadable): keep the reach-based softening — a portalled/closed-root field is
                # beyond the verifier, so the read returning nothing is not evidence the value did not commit.
                why = await _unverifiable_because(page, selector)
                if why:
                    return ToolResult.ok(
                        f"selected {opt_txt!r} for {selector}; {why}, so the commit could not be verified — "
                        "re-observe to confirm the value before relying on it"
                    )
                return ToolResult.error(f"selected suggestion {opt_txt!r} but {selector} did not commit a value")
            return ToolResult.error(f"selected suggestion {opt_txt!r} but {selector} did not commit a value")

        def _reduced_queries() -> list[str]:
            # Ask the widget less until it answers. A search field that rendered nothing for the whole
            # value may still hold it under its own coarser form: the leading clause before the first
            # comma ("Springfield, Sangamon, IL" -> "Springfield"). That is the one rung worth a rung —
            # the finder gates rows on whole-WORD overlap with the query, so a halving prefix that cuts a
            # word ("Illi" against "Illinois") can never match whatever the widget renders for it, and
            # spends a poll cycle on every no-match to prove it. The rung is only a way to make rows
            # appear; it may never be committed as if it were the request (_commit_typeahead's exact_only).
            # After it, one SHORT prefix: many closed-vocabulary pickers render nothing for an empty
            # query or on focus and start answering at two characters, which is the only question that
            # reaches a vocabulary the value itself is absent from ("Illinois" -> "Il" -> "IL").
            rungs: list[str] = []
            leading = value.split(",", 1)[0].strip()
            if leading and leading != value:
                rungs.append(leading)
            short = value[:_SHORT_PREFIX_RUNG_CHARS].strip()
            if short and short != value and short not in rungs:
                rungs.append(short)
            return rungs

        async def _reduced_query_ladder() -> ToolResult | list[dict[str, Any]]:
            # Walk the rungs until one renders rows. A row whose whole label IS the requested value
            # commits; anything else is reported, never guessed — the rows a looser query revealed are
            # the field's own vocabulary, which is what a caller needs to name the right label.
            for rung in _reduced_queries():
                typed_queries.append(rung)
                try:
                    await page.fill(selector, "", timeout=15000)
                    await asyncio.sleep(0.2)
                    # A rung is a fresh question, so it needs a fresh answer to "what appeared in
                    # reaction": the snapshot from before the first attempt would mark the rows the
                    # PREVIOUS query left on screen as pre-existing, and a widget that keeps its row
                    # nodes across a re-search would then have no reaction to show at all.
                    await page.evaluate(_PRESNAPSHOT_JS)
                    await page.type(selector, rung, delay=15, timeout=15000)
                except Exception:
                    return []
                rung_pick = await _commit_typeahead(page, selector, value, rounds=4, exact_only=True, probe=rung)
                if rung_pick.suggestion is not None:
                    return await _typeahead_verdict_result(
                        rung_pick.suggestion,
                        rung_pick.committed,
                        rung_pick.readable,
                        declared=rung_pick.declared,
                        surface_vouched_pre_click=rung_pick.pre_surface_hit,
                    )
                if rung_pick.candidates:
                    return rung_pick.candidates
            return []

        async def _read_offered() -> tuple[list[str], int]:
            try:
                raw_offered = await page.evaluate(_FOCUS_OFFERED_LABELS_JS, await _probe_arg(page, selector))
            except Exception:
                return [], 0
            if not isinstance(raw_offered, dict):
                return [], 0
            labels = [str(t) for t in (raw_offered.get("labels") or []) if isinstance(t, str)]
            return labels, int(raw_offered.get("total") or 0)

        async def _offer_on_empty_query() -> None:
            # Last resort for a field whose list never opened on focus: an EMPTY query is the widest
            # question there is, and a closed-vocabulary widget answers it with its whole list. Re-run
            # the focus pass so those rows are recorded as offered, exactly as if the field had opened
            # showing them — _FOCUS_OFFERED_LABELS_JS then reads them under the same own-list attribution.
            try:
                await page.fill(selector, "", timeout=15000)
            except Exception:
                return
            await asyncio.sleep(0.4)
            try:
                await page.evaluate(_FOCUS_SNAPSHOT_JS, await _probe_arg(page, selector))
            except Exception:
                LOG.debug("taskv3 empty-query offer snapshot failed", selector=selector)

        # Every query this call puts in the field, so a refusal can tell OUR text (safe to take back)
        # from a value the widget wrote in reaction (the page's, and not ours to discard).
        typed_queries: list[str] = [value]
        if await _anchor_typeable(page, selector):
            try:
                pick, pre_value = await _type_and_commit(page, selector, value, rounds=8)
            except _FieldCovered as exc:
                # A widget that renders its committed selection as a pill list OVER its own input is
                # not blocked — it is DONE when a pill in its own container already carries the
                # requested value. Refusing it as covered loops the model on a field that already
                # holds what it asked for. Two independent anchors must BOTH carry the value: the
                # field's own single-field container (so a sibling field's pill cannot vouch) AND the
                # covering layer's own name/control labels (so an unrelated overlay over a field whose
                # open inline list merely OFFERS the value cannot read as committed).
                if _occluder_labels_hold(exc.occluder, value) and await _surface_confirms(page, selector, value):
                    return ToolResult.ok(
                        f"{selector} already holds {value!r} — the requested value is already committed; "
                        "no action was needed"
                    )
                return _covered_error(exc.selector, exc.occluder)
            except _FieldNotEditable as exc:
                return _not_editable_error(exc)
            if pick.suggestion is None:
                # More than one row reacted and none was a unique precision match -- refuse and
                # report what actually reacted (list order, ≤15) instead of guessing which one was meant.
                if pick.candidates:
                    # Text-indisambiguable rows (identical text, distinguishable only by a value the
                    # model cannot see) get the dedicated refusal instead: "pass the full text" has no
                    # answer when every candidate already IS the full text.
                    value_key = _exact_tier_key(value)
                    same_text = [o for o in pick.candidates if _exact_tier_key(str(o.get("text") or "")) == value_key]
                    if len(same_text) >= 2 and _lone_duplicate_candidate(same_text) is None:
                        # Same guard as the type() site: only an empty field may keep the query.
                        if not pre_value:
                            return _identical_text_rows_error(selector, value, same_text, note=pick.note)
                        await _restore_pre_type_value(page, selector, pre_value, typed_queries)
                        return _identical_text_rows_error(selector, value, same_text, tags_live=False, note=pick.note)
                    await _restore_pre_type_value(page, selector, pre_value, typed_queries)
                    return _ambiguous_rows_error(
                        selector, value, pick.candidates, next_step="pass the option's full text", note=pick.note
                    )
                # No suggestion reacted at all -- but that alone does not say a list never rendered: a
                # searchable typeahead that filtered to zero and a non-searchable widget that never filters
                # both land here. The finder pierces open shadow roots, so inside a component it saw the
                # list -- but a portalled/closed-root list stays invisible, so keep the honest "re-observe"
                # note for that reach case first.
                why = await _unverifiable_because(page, selector)
                if why:
                    return ToolResult.ok(
                        f"typed {value!r} into {selector}; {why}, so the suggestion list could not be seen "
                        "and no selection was verified — re-observe to confirm the value committed before "
                        "relying on it"
                    )
                # Everything below that re-asks the widget, or hands the field back, is written for a
                # control whose rows a rule can name; a field that declares no list keeps today's path.
                declared_field = await _field_declares_list(page, selector)
                # A drill-down widget hides its options under expandable category rows, so open→observe→pick's
                # flat enumeration cannot reach them — surface the categories and fail loudly instead.
                cats = await _categories_note(page, selector)
                if cats:
                    if declared_field:
                        await _restore_pre_type_value(page, selector, pre_value, typed_queries)
                    return ToolResult.error(
                        f"no autocomplete suggestion matched {value!r} for {selector}; the field is NOT filled. {cats}"
                    )
                # A searchable typeahead whose value is genuinely absent must report the honest no-match
                # rather than reopen a list the value is not in. Two independent signals establish
                # "searchable": (a) _FIND_MENU_JS finds rows NEW since the pre-type snapshot — a list that
                # reacted to the keystrokes (a menu opened on focus/click sits in that snapshot and does not
                # count, so a non-searchable widget still falls through); (b) the anchor declares
                # aria-autocomplete list/both/inline — the ARIA contract that catches a combobox which
                # filtered to ZERO rows, leaving nothing new to count.
                searchable = False
                try:
                    reacted_menu = await page.evaluate(_FIND_MENU_JS, await _probe_arg(page, selector))
                    searchable = isinstance(reacted_menu, dict) and bool(reacted_menu.get("count"))
                except Exception:
                    searchable = False
                if not searchable:
                    try:
                        searchable = bool(
                            await page.evaluate(_DECLARES_SEARCH_AUTOCOMPLETE_JS, await _probe_arg(page, selector))
                        )
                    except Exception:
                        searchable = False
                if searchable:
                    offered: list[str] = []
                    offered_total = 0
                    if declared_field:
                        # The whole value rendered nothing, but a searchable widget will answer a narrower
                        # question: re-ask it with less until rows appear, commit only an exact hit on the
                        # requested value, and otherwise keep what it revealed as the vocabulary to report.
                        # What the field showed when it OPENED, read before the ladder: every rung resets
                        # that record, and it is the only account of the list for a widget that opened
                        # showing its whole vocabulary.
                        on_open, on_open_total = await _read_offered()
                        ladder = await _reduced_query_ladder()
                        if isinstance(ladder, ToolResult):
                            if ladder.status != "ok":
                                await _restore_pre_type_value(page, selector, pre_value, typed_queries)
                            return ladder
                        # Name the choices the widget offered, so the next call can use the exact label
                        # instead of guessing one; the contract stays an honest did-not-commit.
                        offered = [str(o.get("text") or "") for o in ladder if str(o.get("text") or "")]
                        offered_total = len(offered)
                        if not offered:
                            offered, offered_total = on_open, on_open_total
                        if not offered:
                            await _offer_on_empty_query()
                            offered, offered_total = await _read_offered()
                        if len(offered) > 15:
                            offered = offered[:15]
                        await _restore_pre_type_value(page, selector, pre_value, typed_queries)
                    else:
                        offered, offered_total = await _read_offered()
                    if offered and offered_total > len(offered):
                        offered_note = (
                            f". The list offers {offered_total} rows; the first {len(offered)}: "
                            + "; ".join(repr(t[:60]) for t in offered)
                            + " — if the label you want is not among them, click the control and look() at the list"
                        )
                    elif offered:
                        offered_note = (
                            ". The list offers: "
                            + "; ".join(repr(t[:60]) for t in offered)
                            + " — call select_combobox again with one of these exact labels"
                        )
                    else:
                        offered_note = ""
                    return ToolResult.error(
                        f"no autocomplete suggestion matched {value!r} for {selector}; the field is NOT filled "
                        f"— do not assume success or move on as if it were{offered_note}"
                    )
                # The focus-click of the type attempt may have opened this widget's list, so close it first.
                opened = await _open_observe_pick(page, selector, value, close_open_menu=True)
                if declared_field and opened.status != "ok":
                    await _restore_pre_type_value(page, selector, pre_value, typed_queries)
                return opened
            if pick.declared and not pick.clicked:
                # The row was named but the widget re-rendered it away before the click could land. The
                # same row sits on the list a COARSER query renders, which settles instead of churning,
                # so ask that question rather than report a dead end over a selection never delivered.
                laddered = await _reduced_query_ladder()
                if isinstance(laddered, ToolResult) and laddered.status == "ok":
                    return laddered
                await _restore_pre_type_value(page, selector, pre_value, typed_queries)
            return await _typeahead_verdict_result(
                pick.suggestion,
                pick.committed,
                pick.readable,
                declared=pick.declared,
                surface_vouched_pre_click=pick.pre_surface_hit,
            )
        # A non-typeable anchor (a button/div that only opens a list on click): open, observe, pick.
        return await _open_observe_pick(page, selector, value)

    async def select_option(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args["selector"]
        label = args.get("label")
        value = args.get("value")
        ambiguous = await _ambiguous_selector_error(page, selector)
        if ambiguous is not None:
            return ambiguous
        selector = await _resolve_mirrored_host_control(page, selector)
        try:
            probe = await _probe_evaluate(page, _SELECT_VISIBILITY_JS, selector, await _probe_arg(page, selector))
        except Exception:
            probe = None
        # A disabled control cannot be set whichever kind it is; check before diverting so a disabled
        # custom combobox gets the accurate "is disabled" message rather than the typeable-gate refusal.
        if isinstance(probe, dict) and probe.get("exists") and probe.get("disabled"):
            return ToolResult.error(f"{selector} is disabled — it cannot be set until the page enables it")
        # Native-vs-custom gates on the authoritative nodeName (a structural signal, not a heuristic).
        # Divert to the shared custom-combobox path ONLY when the probe positively confirms a
        # non-<select> element (React-Select, spl-autocomplete, div-list) that page.select_option would
        # throw "Element is not a <select> element" on — so it commits in ONE action instead of degrading
        # the model into click-open + click-option flail. When the probe is unavailable or the element
        # could not be read, default to the native path below (unchanged), so a real <select> whose probe
        # momentarily fails is never misrouted into typing.
        probe_node = str(probe.get("nodeName") or "") if isinstance(probe, dict) and probe.get("exists") else None
        if probe_node is not None and probe_node != "select":
            chosen = label if label is not None else value
            if not isinstance(chosen, str) or not chosen:
                return ToolResult.error("select_option needs a label or value to choose")
            return await _commit_custom_combobox(page, selector, _resolve_text(chosen))
        # force bypasses actionability for a select a design system hides behind a styled proxy;
        # Playwright still sets the value and dispatches native input/change on the real element.
        force = bool(isinstance(probe, dict) and probe.get("exists") and not probe.get("visible"))
        if force and not probe.get("proxied"):
            return await _unreachable_error(selector)
        if label is not None:
            await page.select_option(selector, label=label, timeout=15000, force=force)
        else:
            await page.select_option(selector, value=value, timeout=15000, force=force)
        if not force:
            return ToolResult.ok(f"selected on {selector}")
        try:
            readback = await page.evaluate(_SELECT_READBACK_JS, await _probe_arg(page, selector))
        except Exception:
            readback = None
        value_read: Any = None
        post: dict[str, Any] | None = None
        committed_value: bool | None = None
        if isinstance(readback, dict):
            value_read = readback.get("value")
            post = {"value": value_read}
            committed_value = readback.get("selectedLabel") == label if label is not None else value_read == value
        matches = await _post_match_count(page, selector)
        verdict = _classify_commit(None, matches, post, committed_value=committed_value)
        if verdict is CommitStatus.DID_NOT_COMMIT:
            return ToolResult.error(
                f"select on {selector} did NOT commit: native select still reads {value_read!r} — the styled "
                "widget may not sync from its hidden control; re-observe and act on the visible proxy instead"
            )
        if verdict is CommitStatus.UNVERIFIED:
            reason = (
                "the control left the page afterwards"
                if post is None
                else f"it re-resolved to {matches} elements afterwards"
            )
            return ToolResult.ok(
                f"selected on {selector} — {reason}, so the selection could not be verified; re-observe "
                "before relying on it"
            )
        return ToolResult.ok(f"selected on {selector} (hidden native select, set directly)")

    async def press_key(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        key = args["key"]
        selector = args.get("selector")
        if selector:
            ambiguous = await _ambiguous_selector_error(page, selector)
            if ambiguous is not None:
                return ambiguous
            await page.press(selector, key)
        else:
            await page.keyboard.press(key)
        return ToolResult.ok(f"pressed {key}")

    async def scroll(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args.get("selector")
        if selector:
            ambiguous = await _ambiguous_selector_error(page, selector)
            if ambiguous is not None:
                return ambiguous
            el = await page.query_selector(selector)
            if el:
                await el.scroll_into_view_if_needed()
                return ToolResult.ok(f"scrolled {selector} into view")
        amount = int(args.get("amount", 800))
        if args.get("direction") == "up":
            amount = -amount
        await page.mouse.wheel(0, amount)
        return ToolResult.ok(f"scrolled {amount}px")

    async def wait(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args.get("selector")
        if selector:
            state = args.get("state", "visible")
            # Cap the model-supplied timeout so a single wait can't stall the run (mirrors the 20s sleep cap).
            timeout_ms = min(int(args.get("timeout_ms", 15000)), 30000)
            await page.wait_for_selector(selector, state=state, timeout=timeout_ms)
            return ToolResult.ok(f"{selector} is {state}")
        await asyncio.sleep(min(float(args.get("time_ms", 1000)) / 1000.0, 20.0))
        return ToolResult.ok("waited")

    async def navigate(args: dict[str, Any]) -> ToolResult:
        from skyvern.utils.url_validators import validate_fetch_url

        page, error = await _resolve_page()
        if error is not None:
            return error
        requested = args["url"]
        resolved = _resolve_text(requested)
        # Payload provenance means an opaque token was resolved, not any substitution (a credential
        # placeholder resolves too, but a page reached through one is the model's own to see).
        from_ref = opaque_refs is not None and opaque_refs.resolve(requested) != requested
        url = await asyncio.to_thread(validate_fetch_url, resolved)
        # Destructive same-URL reload guard: a full reload of the page we are already on discards any
        # in-progress form state (filled fields, an attached file) — an unforced state-wipe the loop
        # otherwise scores as progress. Refuse it once with an actionable message; a repeat to the same
        # URL confirms the model means to reset and is allowed through.
        target_canonical = canonical_url(url)
        pre_nav_canonical = canonical_url(await _url(page))
        same_page = pre_nav_canonical == target_canonical
        filled = await _count_filled_fields(page) if same_page else 0
        if filled > 0:
            pending = _reload_confirm_pending[0]
            # Confirm only a repeat whose at-risk state did not grow since the refusal: a file attached
            # after the first warning must be re-refused, not silently wiped by a stale confirmation.
            # Keyed by URL + count, not page identity or field content: a second page on the same
            # canonical URL, or a same-count content swap, falls open to a single unguarded reload (the
            # pre-guard behavior), never a new failure.
            if pending is not None and pending[0] == target_canonical and filled <= pending[1]:
                _reload_confirm_pending[0] = None
                LOG.info("taskv3 navigate destructive-reload guard confirmed on repeat")
            else:
                _reload_confirm_pending[0] = (target_canonical, filled)
                LOG.info("taskv3 navigate destructive-reload guard refused", filled_fields=filled)
                return ToolResult.error(
                    "already on this page and it has filled fields (including any attached file); reloading "
                    "it would discard them. Act on the current page instead — or, if you intend to reset the "
                    "form, navigate here again to confirm."
                )
        else:
            _reload_confirm_pending[0] = None
        try:
            response = await page.goto(url, timeout=60000, wait_until="load")
        except Exception as exc:
            if not from_ref:
                raise
            # Playwright names the URL that failed, which after a redirect is not the ref: every URL
            # in the cause was reached by following the ref, so the model sees it as the token.
            return ToolResult.error(f"navigation failed: {URL_IN_TEXT.sub(lambda _m: requested, str(exc))}")
        landed = await _url(page)
        # A payload ref that redirects hands its provenance to wherever it lands, so a credential the
        # landing URL carries is masked at the boundary exactly like the ref itself. An error page
        # (chrome-error://) is not a landing.
        if from_ref and landed.startswith(("http://", "https://")) and canonical_url(landed) != canonical_url(url):
            assert opaque_refs is not None
            opaque_refs.derive(landed)
        # Surface the HTTP status: an error page otherwise reads as a successful navigation, hiding
        # dead URLs and blank shells from the model.
        status = f" (HTTP {response.status})" if response is not None else ""
        # page_state_changed tells the loop's action-loop guard the world moved: a re-attempt after
        # a navigation is a fresh attempt, not a repeat against unchanged state. A same-URL reload is
        # flagged separately: it resets state rather than progressing it, and the loop's budget
        # extension must not read it as page-change evidence.
        data: dict[str, Any] = {"page_state_changed": True}
        # Classified from where the navigation LANDED, not what was requested: a same-URL request
        # that redirects somewhere new is a real transition, while any request (alias, redirect,
        # or the URL itself) landing back on the pre-navigation page is a reload in effect.
        landed_canonical = canonical_url(landed)
        if landed_canonical == pre_nav_canonical:
            data["same_url_reload"] = True
        elif landed_canonical in _recent_nav_canonicals:
            # Landing on a page this run recently navigated through (an A->B->A hop) re-visits
            # known territory: the retry ledger still resets, but it is not fresh-page evidence.
            data["nav_revisit"] = True
        _recent_nav_canonicals.append(pre_nav_canonical)
        _recent_nav_canonicals.append(landed_canonical)
        # A hard 404/410 landing is a dead/removed target: flag it so the loop ends the run as
        # terminated (v1's behavior) rather than defaulting the outcome to failed.
        if response is not None and response.status in NAVIGATION_DEAD_END_STATUSES:
            data["navigation_dead_end"] = response.status
        return ToolResult.ok(f"navigated to {landed}{status}", data=data)

    async def file_upload(args: dict[str, Any]) -> ToolResult:
        # Lazy import: keeps this module importable for unit tests without the full forge/storage graph.
        from skyvern.forge.sdk.api.files import download_file

        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args["selector"]
        ambiguous = await _ambiguous_selector_error(page, selector)
        if ambiguous is not None:
            return ambiguous
        # Validate the selector before fetching: an invalid or missing selector fails here, before anything
        # is staged into downloads_dir, so the selector guard's residual error can never leave a phantom
        # upload for the download-signal wrapper to misread as a browser download.
        if await page.query_selector(selector) is None:
            return ToolResult.error(f"no file input for selector {selector!r}")
        source = _resolve_text(args["file"])
        # A failed download echoes the source back in the loop's generic tool_error; the model-facing
        # masking boundary (hide_from_model) rewrites any signed payload ref to its token there, so this
        # handler no longer catches locally just to mask the URL (the SKY-14492 retype case).
        local_path = await download_file(source, output_dir=downloads_dir, organization_id=organization_id)
        # For http(s) sources download_file stages into downloads_dir; naming the file lets the
        # download-signal wrapper suppress it without swallowing unrelated downloads that complete
        # during this call (for other schemes the key is inert — nothing in the dir matches).
        staged = {"staged_download": os.path.basename(local_path)}
        # Re-resolve after the download: a rerender during a slow fetch can detach the earlier handle, so
        # bind the element fresh immediately before uploading (missing now means it vanished mid-download).
        el = await page.query_selector(selector)
        if el is None:
            return ToolResult(
                "error", f"no file input for selector {selector!r}", {**staged, "page_state_changed": True}
            )
        # Verify the upload took EFFECT, not just that set_input_files did not raise. Watch upload-like
        # network dispatches across the set_input_files + settle window (the window we already dwell in,
        # so this adds no latency); a genuine upload dispatches at least one, a silent no-op none.
        probe = _UploadActivityProbe(page)
        # Read before the attach on every call: the consume-and-clear check needs the pre-attach text,
        # and whether it will be needed is only known afterwards. One local evaluate, no wait.
        text_before = await _page_rendered_text(page)
        probe.start()
        try:
            await el.set_input_files([local_path])
            populated = await _input_holds_file(el)
            # Settle + a small randomized delay so the upload and a following submit are not dispatched
            # in the same instant, matching v1's upload cadence (the engine that clears this step reliably).
            await _settle_after_upload(page)
            await _upload_submit_delay()
        finally:
            probe.stop()
        if not populated:
            # A consume-and-clear dropzone reads the file on change, uploads it and resets the input, so
            # an empty control after a genuine upload is normal there. Confirming it needs every signal
            # a silent no-op cannot fake at once: the file's own name newly rendered on the page AND an
            # upload dispatched (a client-side rejection names the file but sends nothing; ambient
            # traffic sends but never names it), and no rejection wording anywhere the site newly rendered.
            text_after = await _page_rendered_text(page)
            shown_newly = (
                text_before is not None
                and text_after is not None
                and not _mentions_filename(text_before, local_path)
                and _mentions_filename(text_after, local_path)
            )
            if shown_newly:
                assert text_before is not None and text_after is not None
                new_lines = _newly_rendered_lines(text_before, text_after)
                new_text = "\n".join(new_lines)
                said = " | ".join(line for line in new_lines if _mentions_filename(line, local_path))
                said = said[:_FILENAME_MENTION_CHARS]
                if probe.saw_upload() and not _UPLOAD_REJECTION_WORDS.search(new_text):
                    LOG.info("taskv3 file_upload input cleared after attach but the page shows the uploaded file")
                    return ToolResult.ok(
                        f"uploaded 1 file to {selector} (the site consumed the file and now shows it: {said!r})",
                        staged,
                    )
                LOG.info(
                    "taskv3 file_upload input cleared after attach; page names the file without confirming it",
                    upload_activity=probe.saw_upload(),
                )
                return ToolResult(
                    "error",
                    f"the file input {selector} is empty after the attach and the page now says {said!r} — "
                    f"re-observe to confirm the file was accepted before submitting",
                    staged,
                )
            return ToolResult("error", f"file did not attach to {selector} — re-observe the field", staged)
        if not probe.saw_upload():
            # The file is on the input but the site never reacted: report a recoverable error (not a
            # confident OK) so the loop re-verifies before submitting. A submit-time-upload form lands
            # here too and costs one re-plan turn, never a lost file.
            return ToolResult(
                "error",
                f"attached the file to {selector} but observed no upload activity — re-observe the field "
                f"to confirm the file is shown before submitting; if the form uploads on submit this may "
                f"be expected",
                staged,
            )
        return ToolResult.ok(f"uploaded 1 file to {selector}", staged)

    async def select_combobox(args: dict[str, Any]) -> ToolResult:
        # Explicit typeahead fill (type() also drives this automatically). Routes through the shared
        # custom-combobox commit path: type the value, WAIT for the async suggestion list, pick the
        # best-matching suggestion, and VERIFY the field committed. Fails loudly if nothing matches
        # rather than leaving raw typed text the widget won't accept as a valid selection.
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args["selector"]
        ambiguous = await _ambiguous_selector_error(page, selector)
        if ambiguous is not None:
            return ambiguous
        selector = await _resolve_mirrored_host_control(page, selector)
        value = _resolve_text(args["value"])
        return await _commit_custom_combobox(page, selector, value)

    async def _clear_look_tags(page: Any) -> None:
        try:
            await page.evaluate(
                "() => { const _q = " + _ROOT_QUERY_JS + "; "
                "_q.all('[data-tv3-look]').forEach((e) => e.removeAttribute('data-tv3-look')); }"
            )
        except Exception:
            pass

    async def look(_args: dict[str, Any]) -> ToolResult:
        if _look_count[0] >= _LOOK_MAX_PER_RUN:
            return ToolResult.error(
                f"look budget reached ({_LOOK_MAX_PER_RUN} per run) — rely on observe/get_html and act on "
                "what you already saw instead of looking again."
            )
        page, error = await _resolve_page()
        if error is not None:
            return error
        _look_count[0] += 1
        # Passive read + server-side render. The screenshot is a viewport frame (device px); the marks
        # are enumerated separately so the boxes are drawn in PIL, never injected into the DOM.
        try:
            png = await page.screenshot()
        except Exception as exc:
            LOG.warning("taskv3 look screenshot failed", exc_info=True)
            return ToolResult.error(f"look failed to capture the page: {type(exc).__name__}: {exc}")
        try:
            data = await asyncio.wait_for(page.evaluate(_LOOK_ENUM_JS), timeout=30)
        except Exception as exc:
            await _clear_look_tags(page)
            LOG.warning("taskv3 look enumeration failed", exc_info=True)
            return ToolResult.error(f"look failed to enumerate controls: {type(exc).__name__}: {exc}")
        elements = data.get("elements", []) if isinstance(data, dict) else []
        vw = int(data.get("vw") or 0) if isinstance(data, dict) else 0
        # Release the prior look's retained handles before minting a new set (they leak in the driver
        # otherwise), then grab one live handle per mark while the transient index is still on the DOM.
        for old in _look_manifest.values():
            try:
                await old["handle"].dispose()
            except Exception:
                pass
        _look_manifest.clear()
        # From here on the old marks are gone, whatever happens next -- the loop keys on this.
        renumbered = {"marks_renumbered": True}
        for e in elements:
            n = int(e["n"])
            try:
                handle = await page.query_selector(f'[data-tv3-look="{n}"]')
            except Exception:
                handle = None
            if handle is None:
                continue
            _look_manifest[n] = {
                "handle": handle,
                "tag": e.get("tag", ""),
                "label": e.get("label", ""),
            }
        await _clear_look_tags(page)
        # Draw ONLY the marks we retained a handle for, so every number on the image is one the model
        # can actually act on (a control that detached between enumeration and handle-grab is dropped
        # from both the image and the legend, never shown as an unusable box).
        kept = [e for e in elements if int(e["n"]) in _look_manifest]
        try:
            annotated = await asyncio.get_running_loop().run_in_executor(None, _annotate_screenshot, png, kept, vw)
        except Exception as exc:
            LOG.warning("taskv3 look annotation failed", exc_info=True)
            return ToolResult.error(f"look failed to render marks: {type(exc).__name__}: {exc}", data=renumbered)
        if not kept:
            return ToolResult.ok(
                "look: no interactive controls are visible in the viewport. Scroll or re-observe.",
                data=renumbered,
                screenshots=[annotated],
            )

        # Mask payload-minted signed URLs, then truncate for display — in that ORDER. A label taken
        # from an input's value can carry a resolved presigned URL; masking (as observe and get_html do)
        # rewrites it to its opaque token so the model can't retype it, and truncating first would sever
        # the URL past the provenance match and leak a partial signed URL into the transcript.
        def _label(raw: object, width: int = 80) -> str:
            return _digest_token(_mask_refs(str(raw)), width)

        lines = [
            f"[{int(e['n'])}] {_digest_token(e.get('tag', ''), 20)} {_label(e.get('label', ''))!r}"
            + (f" placeholder={_label(e['placeholder'], 60)!r}" if e.get("placeholder") else "")
            for e in kept
        ]
        header = (
            f"look: {len(kept)} visible control(s), numbered on the screenshot. Act on one with "
            "click(mark=N) or type(mark=N, text=...)."
        )
        if isinstance(data, dict) and data.get("truncated"):
            header += f" (only the first {_LOOK_MAX_MARKS} are marked; scroll for more.)"
        legend = header + "\n" + "\n".join(lines)
        return ToolResult.ok(legend, data=renumbered, screenshots=[annotated])

    async def _resolve_mark(page: Any, mark: int) -> tuple[str | None, ToolResult | None]:
        # Turn mark=N into a selector the existing click/type handlers act through. Resolution is the
        # SAME live element handle look retained (Playwright's engine, which pierces open shadow), tagged
        # data-tv3-act at act time so the marker branch uniqueness-checks and commit-verifies it like
        # any other marker. A detached handle errors rather than re-guessing by coordinates: a stale
        # look-time point could hit whatever now occupies those pixels after a scroll, which is exactly
        # the wrong-element class this must not introduce.
        entry = _look_manifest.get(mark)
        if entry is None:
            return None, ToolResult.error(
                f"mark {mark} is not in the current set of marks. Call look() first, then act on a number it drew."
            )
        handle = entry.get("handle")
        stale = ToolResult.error(
            f"mark {mark} no longer points to an element on the page — it moved or the page "
            "re-rendered since look(). Call look() again and act on a fresh number.",
            data={"page_state_changed": True},
        )
        if handle is None:
            return None, stale
        # Every judgement below is made HERE, not in the page's realm. The read goes through
        # Playwright's accessor, the format is matched against this run's own pattern, and the
        # holder count comes from Playwright's engine -- so a page that patches its own RegExp,
        # getAttribute or querySelectorAll cannot talk this into adopting a value it planted.
        token = ""
        try:
            existing = await handle.get_attribute("data-tv3-act")
        except Exception:
            existing = None
        if existing and _act_token_re.fullmatch(existing):
            try:
                # An inherited token is not an identity: cloneNode copies the attribute, so a
                # duplicated control arrives already wearing one. Keep it only while its holder is
                # alone -- counted by the engine that pierces open shadow roots, which is the domain
                # the click gate and the in-flight probe both resolve in.
                if len(await page.query_selector_all(f'{_ACT_SELECTOR_PREFIX}{existing}"]')) == 1:
                    token = existing
            except Exception:
                token = ""
        if not token:
            # Never reissued, so no two elements can share one and no tag left on a page the run
            # navigated away from can match a later selector.
            _act_seq[0] += 1
            minted = f"{_act_prefix}{_act_seq[0]}"
            try:
                if not bool(await handle.evaluate(_ACT_WRITE_HANDLE_JS, minted)):
                    return None, stale
                # Read back through Playwright: a page that hijacks setAttribute can put the token
                # on an element of its choosing, and an unverified write would hand back a selector
                # naming that one instead of this. Confirming this element carries it is necessary
                # but not sufficient -- the same trap can write it to a decoy AS WELL -- so the
                # engine also has to agree the token has exactly one holder. This resolver must
                # never hand back an ambiguous selector, whatever the callers downstream check.
                if await handle.get_attribute("data-tv3-act") != minted:
                    return None, stale
                if len(await page.query_selector_all(f'{_ACT_SELECTOR_PREFIX}{minted}"]')) != 1:
                    return None, stale
            except Exception:
                return None, stale
            token = minted
        return f'{_ACT_SELECTOR_PREFIX}{token}"]', None

    def _with_act_by_mark(handler: ToolHandler) -> ToolHandler:
        async def wrapped(args: dict[str, Any]) -> ToolResult:
            mark = args.get("mark")
            if mark is None:
                return await handler(args)
            existing = args.get("selector")
            # A selector this wrapper minted is not the model passing both: since the resolved
            # selector is now written back into the caller's dict, a re-dispatch of that same dict
            # would otherwise fail a guard aimed at the model. The mark still wins and is re-resolved.
            if existing and not str(existing).startswith(_ACT_SELECTOR_PREFIX):
                return ToolResult.error("Pass either mark or selector to act on a control, not both.")
            try:
                mark_int = int(mark)
            except (TypeError, ValueError):
                return ToolResult.error(f"mark must be an integer from the last look(), got {mark!r}.")
            page, error = await _resolve_page()
            if error is not None:
                return error
            selector, mark_error = await _resolve_mark(page, mark_int)
            if mark_error is not None:
                return mark_error
            # In place rather than into a copy: everything downstream reads this dict AFTER dispatch
            # -- the persisted action's element_id, the submit watch, the repeat guard's key and the
            # nudge's target -- and a copy leaves every one of them seeing only `mark`.
            args["selector"] = selector
            return await handler(args)

        return wrapped

    tools = [
        _spec(
            "observe",
            'Snapshot the page\'s visible interactive elements (raw DOM) with a CSS selector, label, type, value, and options for each. A selector printed as [data-tv3-ref="N"] is a short handle for a control whose real id is long and opaque; copy it exactly as printed. [data-tv3-ref="?"] in get_html output or an error message marks an element whose id several handles share and is not usable as a selector; act through the observe-printed handle instead. Also reports cross-origin iframes present (host + captcha signature); their contents cannot be observed or reached by selector. Call once per page, then act by selector.',
            _obj({}),
            observe,
        ),
        _spec(
            "get_html",
            "Get raw outer/inner HTML of the page or a specific element (for detail beyond observe), or "
            'with format "text" its rendered visible text instead - what a user sees, no markup. Both '
            f"are capped at {HTML_MAX_CHARS} chars and say when they were cut.",
            _obj(
                {
                    "selector": {"type": "string", "description": "CSS selector; omit for whole page"},
                    "format": {
                        "type": "string",
                        "enum": ["html", "text"],
                        "description": 'Default "html". "text" returns the visible text instead of markup.',
                    },
                }
            ),
            get_html,
        ),
        _spec(
            "look",
            "Rare last resort: take ONE annotated screenshot of the viewport when the text tools are "
            "insufficient — the layout is confusing, a control you expect isn't in observe (custom/"
            "shadow-DOM widgets), or an action isn't taking and you can't tell why. Returns the page "
            "image with every visible control boxed and numbered, plus a legend. Then act on a number "
            "with click(mark=N) or type(mark=N, text=...). NEVER call it just to double-check observe.",
            _obj({}),
            look,
        ),
        _spec(
            "click",
            "Click an element by CSS selector (or by mark=N from the last look()). If the click opens a "
            'menu of options, the result lists them with [data-tv3-menu="N"] selectors — click one of '
            "those to select (verified: you get a loud error, not a silent no-op, if the selection does "
            "not commit; do not blindly repeat a failed click). If the click triggers a file download, "
            "the tool result reports it when detected.",
            _obj(
                {
                    "selector": {"type": "string"},
                    "mark": {
                        "type": "integer",
                        "description": "A number from the last look(); use instead of selector",
                    },
                },
            ),
            click,
        ),
        _spec(
            "hover",
            "Hover over an element by CSS selector (e.g. to open a hover menu).",
            _obj({"selector": {"type": "string"}}, ["selector"]),
            hover,
        ),
        _spec(
            "type",
            "Type text into an input/textarea by CSS selector (or by mark=N from the last look()); "
            "clears first by default.",
            _obj(
                {
                    "selector": {"type": "string"},
                    "mark": {
                        "type": "integer",
                        "description": "A number from the last look(); use instead of selector",
                    },
                    "text": {"type": "string"},
                    "clear": {"type": "boolean"},
                    "press_enter": {"type": "boolean"},
                },
                ["text"],
            ),
            type_text,
        ),
        _spec(
            "select_option",
            "Choose an option in a <select> by value or visible label.",
            _obj(
                {"selector": {"type": "string"}, "value": {"type": "string"}, "label": {"type": "string"}}, ["selector"]
            ),
            select_option,
        ),
        _spec(
            "select_combobox",
            "Fill an autocomplete/typeahead/combobox field (location, school, employer lookups): types the "
            "value, waits for the suggestion list to render, selects the best-matching suggestion, and "
            "verifies the field committed. Use this INSTEAD of `type` for such fields — it errors if no "
            "suggestion matches so you never leave uncommitted raw text.",
            _obj({"selector": {"type": "string"}, "value": {"type": "string"}}, ["selector", "value"]),
            select_combobox,
        ),
        _spec(
            "press_key",
            "Press a keyboard key (optionally focused on a selector), e.g. Enter, Escape, Tab.",
            _obj({"key": {"type": "string"}, "selector": {"type": "string"}}, ["key"]),
            press_key,
        ),
        _spec(
            "scroll",
            "Scroll the page (direction up/down + amount) or scroll a selector into view.",
            _obj(
                {
                    "direction": {"type": "string", "enum": ["up", "down"]},
                    "amount": {"type": "integer"},
                    "selector": {"type": "string"},
                }
            ),
            scroll,
        ),
        _spec(
            "wait",
            "Wait for a selector to reach a state (visible/attached/hidden) or wait a fixed time_ms.",
            _obj(
                {
                    "selector": {"type": "string"},
                    "state": {"type": "string"},
                    "timeout_ms": {"type": "integer"},
                    "time_ms": {"type": "integer"},
                }
            ),
            wait,
        ),
        _spec("navigate", "Navigate the browser to a URL.", _obj({"url": {"type": "string"}}, ["url"]), navigate),
        _spec(
            "file_upload",
            "Upload a file (local path or URL) into a file input by CSS selector.",
            _obj({"selector": {"type": "string"}, "file": {"type": "string"}}, ["selector", "file"]),
            file_upload,
        ),
    ]
    if not vision_enabled:
        # A non-vision model drops the screenshot before the request, so `look` would advertise an
        # image the model never sees. Drop the tool entirely; a `mark=N` with no look then just errors
        # "not in the current set of marks" (a clean no-op), so click/type need no further change.
        tools = [t for t in tools if t.name != "look"]
    for _tool_spec in tools:
        if _tool_spec.name in (
            "click",
            "hover",
            "type",
            "select_option",
            "select_combobox",
            "press_key",
            "file_upload",
        ):
            _tool_spec.billable = True
        if _tool_spec.name in ("observe", "get_html", "look"):
            # Large perception dumps: only the latest snapshot is relevant, so let the loop elide older
            # ones from the re-sent transcript (bounds context on perception-heavy pages). look's legend
            # (not its ephemeral image, which never enters the transcript) rides the same rule.
            _tool_spec.compactable = True
        if _tool_spec.name in PREFLIGHT_TOOL_NAMES:
            _tool_spec.handler = _with_preflight(_tool_spec.name, _tool_spec.handler, page_provider, _prefetched_page)
        if _tool_spec.name in _SELECTOR_GUARD_TOOL_NAMES:
            # Outside preflight (it builds its action from the normalized selector), inside act_by_mark
            # (mark=N resolves to a selector first), so every selector tool inherits the guard.
            diagnose = _diagnose_inert_target if _tool_spec.name in _INERT_DIAGNOSIS_TOOL_NAMES else None
            _tool_spec.handler = _with_alias_resolution(
                _tool_spec.name, _with_selector_guard(_tool_spec.handler, diagnose)
            )
        if _tool_spec.name in ("click", "type"):
            # OUTERMOST wrapper: resolve mark=N to a selector before preflight builds its action from
            # args["selector"], so the whole verified click/type path (uniqueness gate, commit-verify)
            # runs on the act-by-mark selector unchanged.
            _tool_spec.handler = _with_act_by_mark(_tool_spec.handler)
    _apply_download_signal(tools, downloads_dir)
    return tools


_DOWNLOAD_UUID_INFIX_RE = re.compile(r"\.[0-9a-f]{32}$")
_DOWNLOAD_SIGNAL_MAX_LINES = 5
# Filenames are server-controlled and get surfaced into the LLM transcript: strip control chars plus
# Unicode line/paragraph separators, zero-width, and bidi-control characters from the DISPLAYED name
# (seen-set tracking keeps the raw filesystem name).
_DOWNLOAD_NOTICE_SANITIZE_RE = re.compile(
    "[\\x00-\\x1f\\x7f\\u0085\\u2028\\u2029\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069]"
)


def _digest_token(value: object, cap: int) -> str:
    """Strip anything a page could use to forge a digest line: line separators, bidi overrides,
    zero-width joiners. For payload fields printed bare rather than through `!r`.

    For `role` and `type` this is the second of two layers -- both are gated at their source
    (`role` against a whitelist, `type` to the tags whose UA normalises it), so removing either
    layer alone leaves the payload safe, and removing both forges a line.

    For `tag` it is the ONLY layer, and a tag name is page-controlled. LF and CR never reach one,
    but U+0085/U+2028/U+2029 do: the HTML tokenizer ends a tag name on ASCII whitespace only, so
    `<a\\u2028b>` parses to a tagName carrying the separator. `createElement` is stricter and
    rejects the same name on some builds but not others, which is why the parser is the case that
    matters. None of them forges a whole element line -- a tag name may not hold `[`, a space or a
    quote -- but they do split a digest line in two.
    """
    return _DOWNLOAD_NOTICE_SANITIZE_RE.sub("", str(value))[:cap]


def _download_signal_identity(name: str) -> str:
    """`report.pdf.<32-hex-uuid>.crdownload` -> `report.pdf` (see cdp_download_interceptor's temp
    naming), so an in-progress file and its completed rename are recognized as the same download.
    The uuid strip applies only to suffix-carrying temp names — a completed file legitimately named
    `export.<32hex>` must keep its full identity."""
    if name.endswith(BROWSER_DOWNLOADING_SUFFIX):
        name = name[: -len(BROWSER_DOWNLOADING_SUFFIX)]
        return _DOWNLOAD_UUID_INFIX_RE.sub("", name)
    return name


def _human_download_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    size = num_bytes / 1024
    for unit in ("KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _apply_download_signal(tools: list[ToolSpec], downloads_dir: str | None) -> None:
    """Wrap every tool in the given list so a file landing in `downloads_dir` during (or between)
    calls is reported in the next tool result, without a dedicated tool call. Tools assembled later
    (finish, auth/captcha extras) are not wrapped; a download landing during those surfaces on the
    next wrapped call. State (seen files, pending re-delivery lines) is shared across all wrapped
    tools via this closure, one instance per build_browser_tools call. No-op without downloads_dir."""
    if not downloads_dir:
        return

    seen_completed: set[str] = set()
    seen_started: set[str] = set()
    pending: list[str] = []
    baseline = {"done": False}

    def _list_split() -> tuple[list[str], list[str]]:
        try:
            names = sorted(os.listdir(downloads_dir))
        except OSError:
            return [], []
        completed = [n for n in names if not n.endswith(BROWSER_DOWNLOADING_SUFFIX)]
        in_progress = [n for n in names if n.endswith(BROWSER_DOWNLOADING_SUFFIX)]
        return completed, in_progress

    for tool_spec in tools:

        async def wrapped(
            args: dict[str, Any],
            _handler: Callable[[dict[str, Any]], Awaitable[ToolResult]] = tool_spec.handler,
            _compactable: bool = tool_spec.compactable,
            _tool_name: str = tool_spec.name,
        ) -> ToolResult:
            if not baseline["done"]:
                baseline["done"] = True
                try:
                    # Snapshot BEFORE the first handler runs, so a download triggered by the very
                    # first tool call is reported rather than absorbed into the baseline.
                    completed0, in_progress0 = _list_split()
                    seen_completed.update(completed0)
                    seen_started.update(_download_signal_identity(n) for n in in_progress0)
                except Exception:
                    LOG.warning("taskv3 download signal baseline snapshot failed", tool=_tool_name, exc_info=True)
            result = await _handler(args)
            try:
                # A tool that stages its own file into downloads_dir (file_upload) names it in
                # result.data; only that exact file is absorbed silently — an unrelated download
                # completing during the same call still gets reported.
                staged_name = (result.data or {}).get("staged_download")
                completed, in_progress = _list_split()
                new_lines: list[str] = []
                for name in completed:
                    if name in seen_completed:
                        continue
                    seen_completed.add(name)
                    seen_started.add(_download_signal_identity(name))
                    if name == staged_name:
                        continue
                    try:
                        size = os.path.getsize(os.path.join(downloads_dir, name))
                    except OSError:
                        size = 0
                    display = _DOWNLOAD_NOTICE_SANITIZE_RE.sub("", name)
                    new_lines.append(f"Downloaded: {display} ({_human_download_size(size)})")
                for name in in_progress:
                    identity = _download_signal_identity(name)
                    if identity in seen_started or identity in seen_completed:
                        continue
                    seen_started.add(identity)
                    display = _DOWNLOAD_NOTICE_SANITIZE_RE.sub("", identity)
                    new_lines.append(f"Download started: {display} (in progress — not yet complete)")
                deliver = list(dict.fromkeys(pending + new_lines))
                if not deliver:
                    pending[:] = []
                    return result
                capped = deliver[:_DOWNLOAD_SIGNAL_MAX_LINES]
                overflow = len(deliver) - len(capped)
                if overflow > 0:
                    capped = capped + [f"+{overflow} more files downloaded"]
                pending[:] = deliver if _compactable else []
                # The flag lets the loop's action-loop guard treat the download as progress without
                # sniffing the notice lines back out of the content string. Preserve screenshots so a
                # result that also carried a look image (or any future image) is not silently dropped.
                return ToolResult(
                    result.status,
                    result.content + "\n" + "\n".join(capped),
                    # download_new marks a download detected on THIS call; a compactable tool
                    # replaying retained pending lines carries only download_notice.
                    {**(result.data or {}), "download_notice": True, "download_new": bool(new_lines)},
                    result.screenshots,
                )
            except Exception:
                LOG.warning("taskv3 download signal computation failed", tool=_tool_name, exc_info=True)
                return result

        tool_spec.handler = wrapped


def _with_preflight(
    name: str,
    handler: Callable[[dict[str, Any]], Awaitable[ToolResult]],
    page_provider: PageProvider,
    prefetched_page: list[Any] | None = None,
) -> Callable[[dict[str, Any]], Awaitable[ToolResult]]:
    async def wrapped(args: dict[str, Any]) -> ToolResult:
        page = await page_provider()
        if page is not None:
            preflight_tool_action(name, args, page)
            if prefetched_page is not None:
                prefetched_page.append(page)
        try:
            return await handler(args)
        finally:
            if prefetched_page is not None:
                prefetched_page.clear()

    return wrapped
