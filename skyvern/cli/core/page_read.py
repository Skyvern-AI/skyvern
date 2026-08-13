from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
from functools import lru_cache
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Literal, Sequence

import structlog

from skyvern.config import settings

LOG = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from skyvern.library.skyvern_browser_page import SkyvernBrowserPage

DEFAULT_MAX_CHARS = 60_000
# A real cursor is ~256 chars (two hex digests, an offset, a version, plus a 32-byte signature).
# Bounded so an oversized caller-supplied string is rejected before it is padded and decoded.
MAX_CURSOR_CHARS = 512
_MAX_JSON_ESCAPED_CHARACTER_CHARS = 6
# One source for the pruning counters, so every mode reports the same keys and a new tier cannot
# appear in lean_html responses while silently missing from html/text ones.
_PRUNE_COUNTER_KEYS = (
    "scripts_removed",
    "styles_removed",
    "comments_removed",
    "event_handlers_removed",
    "base64_blobs_removed",
)
_PRUNING_STAT_KEYS = (*_PRUNE_COUNTER_KEYS, "chars_removed")
# HTML void elements, which never have a closing tag.
_VOID_ELEMENTS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_CURSOR_VERSION = 1
_CURSOR_KEY_DOMAIN = b"skyvern.mcp.page_cursor.v1"
_CURSOR_FALLBACK_SECRET = secrets.token_bytes(32)
_CURSOR_ERROR = "Cursor expired or invalid for this browser session, page, frame, or document revision"
_CURSOR_HINT = "Call skyvern_page again without cursor to restart from the current page state"
_CURSOR_FALLBACK_KEY_HINT = (
    f"{_CURSOR_HINT}. SECRET_KEY is unset or still the placeholder default, so cursors are signed"
    " with a per-process key and cannot outlive a process restart or replica hop; set SECRET_KEY"
    " to make pagination durable"
)
_BASE64_BLOB = re.compile(r"data:[^\s\"']*;base64,", re.IGNORECASE)
# Deliberately broad: fullmatch drops any handler-shaped `on...` attribute. Over-stripping is free
# in a read-only view; under-stripping puts inline-JS bulk back into lean_html.
_EVENT_HANDLER = re.compile(r"on[a-z]+", re.IGNORECASE)
_RAW_START_TAG = re.compile(
    r"(?P<open><\s*(?P<tag>[^\s/>]+))(?P<attrs>.*?)(?P<close>\s*/?\s*>)\Z",
    re.DOTALL,
)
_RAW_ATTRIBUTE = re.compile(r"""(?P<space>\s+)(?P<name>[^\s=/>]+)(?:\s*=\s*(?P<value>"[^"]*"|'[^']*'|[^\s"'=<>`]+))?""")

PageMode = Literal["html", "lean_html", "text"]


class CursorError(ValueError):
    def __init__(self, hint: str | None = None) -> None:
        super().__init__(_CURSOR_ERROR)
        self.hint = hint if hint is not None else _CURSOR_HINT


class _LeanHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.suppressed_tag: str | None = None
        self.kept_tags: list[tuple[str, str]] = []
        self.stats = dict.fromkeys(_PRUNE_COUNTER_KEYS, 0)
        self._source = ""
        self._line_starts: list[int] = [0]

    def feed(self, data: str) -> None:
        # getpos() reports (line, per-line column); mapping that to an absolute index needs the
        # full stream and its line starts. self.rawdata only holds the unconsumed tail, and
        # offset alone resets to zero at every newline, so the parser keeps its own copy.
        self._source += data
        self._line_starts = [0] + [match.end() for match in re.finditer("\n", self._source)]
        super().feed(data)

    def _start_tag(self, *, tag: str, self_closing: bool) -> tuple[str, str]:
        raw = self.get_starttag_text() or f"<{tag}>"
        match = _RAW_START_TAG.fullmatch(raw)
        if match is None:
            return (f"<{tag} />" if self_closing else f"<{tag}>"), tag

        kept: list[str] = []
        attributes = match.group("attrs")
        position = 0
        for attribute in _RAW_ATTRIBUTE.finditer(attributes):
            kept.append(attributes[position : attribute.start()])
            name = attribute.group("name")
            value = attribute.group("value")
            if _EVENT_HANDLER.fullmatch(name):
                self.stats["event_handlers_removed"] += 1
            elif value is not None and _BASE64_BLOB.search(value):
                self.stats["base64_blobs_removed"] += 1
            else:
                kept.append(attribute.group(0))
            position = attribute.end()
        kept.append(attributes[position:])
        closing = " />" if self_closing else ">"
        return f"{match.group('open')}{''.join(kept).rstrip()}{closing}", match.group("tag")

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if self.suppressed_tag is not None:
            return
        if tag in {"script", "style"}:
            self.stats[f"{tag}s_removed"] += 1
            self.suppressed_tag = tag
            return
        rendered, source_tag = self._start_tag(tag=tag, self_closing=False)
        # Void elements get no close tag, so stacking them leaves entries nothing can ever pop.
        # Browsers serialize them bare (<input>, not <input/>), so they arrive here, not in
        # handle_startendtag — and every later close tag then scans past the whole pile.
        if tag not in _VOID_ELEMENTS:
            self.kept_tags.append((tag, source_tag))
        self.parts.append(rendered)

    def handle_startendtag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if self.suppressed_tag is not None:
            return
        if tag in {"script", "style"}:
            self.stats[f"{tag}s_removed"] += 1
            return
        rendered, _ = self._start_tag(tag=tag, self_closing=True)
        self.parts.append(rendered)

    def handle_endtag(self, tag: str) -> None:
        if self.suppressed_tag is not None:
            if tag == self.suppressed_tag:
                self.suppressed_tag = None
            return
        source_tag = tag
        for index in range(len(self.kept_tags) - 1, -1, -1):
            normalized, candidate = self.kept_tags[index]
            if normalized == tag:
                source_tag = candidate
                self.kept_tags.pop(index)
                break
        self.parts.append(f"</{source_tag}>")

    def handle_data(self, data: str) -> None:
        if self.suppressed_tag is None:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self.suppressed_tag is None:
            self.parts.append(f"&{name}{self._reference_terminator(f'&{name}')}")

    def handle_charref(self, name: str) -> None:
        if self.suppressed_tag is None:
            self.parts.append(f"&#{name}{self._reference_terminator(f'&#{name}')}")

    def _reference_terminator(self, reference: str) -> str:
        # html.parser strips a trailing ';' and never says whether the source had one. Re-adding it
        # unconditionally rewrites bare ampersands in text: "R&D" would come back as "R&D;". Raw-text
        # elements (noscript, xmp, noembed, noframes) serialize unescaped, so this is reachable.
        # getpos() during a handler is the position of the construct's '&' (updatepos runs after the
        # handler), but offset is a per-line column, so it must be rebased onto its line start.
        lineno, column = self.getpos()
        position = self._line_starts[lineno - 1] + column
        return ";" if self._source[position:].startswith(f"{reference};") else ""

    def handle_comment(self, data: str) -> None:
        if self.suppressed_tag is None:
            self.stats["comments_removed"] += 1

    def handle_decl(self, decl: str) -> None:
        if self.suppressed_tag is None:
            self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        if self.suppressed_tag is None:
            self.parts.append(f"<?{data}>")

    def unknown_decl(self, data: str) -> None:
        if self.suppressed_tag is None:
            # parse_marked_section hands over the body with the "]]>" terminator already stripped,
            # so both brackets have to come back or a CDATA section is left unterminated.
            self.parts.append(f"<![{data}]]>")


def prune_html(source: str) -> tuple[str, dict[str, int]]:
    """Remove non-perceptual or high-volume HTML deterministically."""
    parser = _LeanHTMLParser()
    parser.feed(source)
    parser.close()
    content = "".join(parser.parts)
    # Pruning can only remove characters, so a negative value means the reducer rewrote text and must stay visible as a tripwire, not be clamped away.
    return content, {**parser.stats, "chars_removed": len(source) - len(content)}


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


@lru_cache(maxsize=1)
def _warn_cursor_key_fallback() -> None:
    # Once per process: a pagination loop would flood the log. Structured so it is alertable.
    LOG.warning(
        "MCP page cursors signed with a per-process key",
        setting="SECRET_KEY",
        reason="unset or still the placeholder default",
        impact="pagination fails across replicas and process restarts",
    )


def _cursor_key_is_fallback() -> bool:
    configured_key = settings.SECRET_KEY
    return not configured_key or configured_key == type(settings).model_fields["SECRET_KEY"].default


def _cursor_signing_key() -> bytes:
    if _cursor_key_is_fallback():
        _warn_cursor_key_fallback()
        key = _CURSOR_FALLBACK_SECRET
    else:
        key = settings.SECRET_KEY.encode("utf-8")
    return hmac.digest(key, _CURSOR_KEY_DOMAIN, "sha256")


def _encode_cursor(*, offset: int, binding: Sequence[object], document_revision: str) -> str:
    payload = json.dumps(
        {"b": _fingerprint(list(binding)), "d": document_revision, "o": offset, "v": _CURSOR_VERSION},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.digest(_cursor_signing_key(), payload, "sha256")
    return base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode()


def _decode_cursor(cursor: str) -> dict[str, Any]:
    # Checked before padding and decoding so an oversized string is never materialized, even if
    # a caller reaches this without the tool's field validation.
    if len(cursor) > MAX_CURSOR_CHARS:
        raise CursorError
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        payload, signature = decoded[:-32], decoded[-32:]
        if len(signature) != 32:
            # Too short to even hold a signature: malformed input, not evidence about the key.
            raise CursorError
        if not hmac.compare_digest(signature, hmac.digest(_cursor_signing_key(), payload, "sha256")):
            # Only a signature mismatch is evidence of a replica hop under a fallback key;
            # malformed cursors say nothing about key configuration.
            if _cursor_key_is_fallback():
                LOG.warning(
                    "MCP page cursor rejected under per-process signing key",
                    setting="SECRET_KEY",
                    impact="pagination fails across replicas and process restarts",
                )
                raise CursorError(_CURSOR_FALLBACK_KEY_HINT)
            raise CursorError
        data = json.loads(payload)
        if set(data) != {"b", "d", "o", "v"} or data["v"] != _CURSOR_VERSION:
            raise CursorError
        return data
    except CursorError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CursorError from exc


def _json_escaped_length(value: str) -> int:
    # ensure_ascii must match how the response cap measures (response.py::_response_size), or the
    # budget is spent on \uXXXX escapes the wire never carries: 6x for CJK, 12x for emoji.
    return len(json.dumps(value, ensure_ascii=False)) - 2


def _json_escaped_chunk_end(content: str, *, offset: int, max_chars: int) -> int:
    upper = min(offset + max_chars, len(content))
    escaped_budget = max(max_chars, _MAX_JSON_ESCAPED_CHARACTER_CHARS)
    if _json_escaped_length(content[offset:upper]) <= escaped_budget:
        return upper

    lower = offset + 1
    while lower < upper:
        middle = (lower + upper + 1) // 2
        if _json_escaped_length(content[offset:middle]) <= escaped_budget:
            lower = middle
        else:
            upper = middle - 1
    return lower


def paginate_content(
    content: str,
    *,
    binding: Sequence[object],
    max_chars: int = DEFAULT_MAX_CHARS,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Return a lossless, JSON-envelope-safe slice and a retry-stable signed continuation cursor."""
    if not 1 <= max_chars <= DEFAULT_MAX_CHARS:
        raise ValueError(f"max_chars must be between 1 and {DEFAULT_MAX_CHARS}")

    document_revision = hashlib.sha256(content.encode("utf-8", "surrogatepass")).hexdigest()
    offset = 0
    if cursor is not None:
        data = _decode_cursor(cursor)
        if data["b"] != _fingerprint(list(binding)) or data["d"] != document_revision:
            raise CursorError
        offset = data["o"]
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 < offset < len(content):
            raise CursorError

    end = _json_escaped_chunk_end(content, offset=offset, max_chars=max_chars)
    return {
        "content": content[offset:end],
        "total_size": len(content),
        "cursor_next": (
            _encode_cursor(offset=end, binding=binding, document_revision=document_revision)
            if end < len(content)
            else None
        ),
    }


async def _read_text(locator: Any) -> str:
    """inner_text() rejects non-HTMLElement nodes, which a matched SVG/XML selector is."""
    try:
        return await locator.inner_text()
    except Exception as exc:
        # Playwright raises "Node is not an HTMLElement" from inner_text() when the matched node
        # is SVG/XML. Only that exact condition reroutes to text_content(); anything else (e.g.
        # "page is not an active target") must propagate, so the shared "is not an" helper for
        # fill()/clear() is deliberately NOT reused here.
        if "is not an htmlelement" not in str(exc).lower():
            raise
        return await locator.text_content() or ""


async def _serialize(page: SkyvernBrowserPage, *, selector: str | None, mode: PageMode) -> tuple[str, dict[str, int]]:
    scope = page.locator_scope
    if selector is None:
        if mode == "text":
            locator = scope.locator("body").first
            if await locator.count() == 0:
                locator = scope.locator("html").first
            content = await _read_text(locator)
        else:
            content = await scope.content()
    else:
        locator = scope.locator(selector).first
        if await locator.count() == 0:
            raise ValueError(f"Selector {selector!r} did not match an element on the current page or frame")
        content = (
            await _read_text(locator) if mode == "text" else await locator.evaluate("element => element.outerHTML")
        )

    if mode == "lean_html":
        # prune_html is pure-Python and costs ~0.22s/MB. On the hosted mount that is event-loop
        # time every other request in the process waits on, and a paginated read pays it per chunk.
        return await asyncio.to_thread(prune_html, content)
    return content, dict.fromkeys(_PRUNING_STAT_KEYS, 0)


async def read_page(
    page: SkyvernBrowserPage,
    *,
    binding: Sequence[object],
    selector: str | None,
    mode: PageMode,
    max_chars: int,
    cursor: str | None,
) -> dict[str, Any]:
    # Re-serializing every call IS the mutation check: document_revision is this hash. Costs
    # O(N x document) per N-chunk read; a content cache would break replica-safe stateless cursors.
    content, pruning = await _serialize(page, selector=selector, mode=mode)
    # binding carries the page snapshot (identity, url, frames, document epoch); the caller owns
    # capturing it, because only the caller can tell whether it still holds after this await.
    # selector/mode belong to cursor identity too: two reads can serialize byte-identically
    # (identical cards; lean_html on already-lean markup), so the hash alone cannot separate them.
    page_slice = paginate_content(
        content,
        binding=(*binding, selector, mode),
        max_chars=max_chars,
        cursor=cursor,
    )
    return {**page_slice, "pruning_stats": pruning}
