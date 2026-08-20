from __future__ import annotations

import base64
import inspect
import json
from typing import Any

import pytest

import skyvern.cli.core.page_read as page_read
from skyvern.cli.core.page_read import (
    DEFAULT_MAX_CHARS,
    MAX_CURSOR_CHARS,
    CursorError,
    PageMode,
    paginate_content,
    prune_html,
    read_page,
)
from skyvern.cli.mcp_tools import inspection
from skyvern.cli.mcp_tools.inspection import skyvern_page


def _binding(*, session: int = 1, page: int = 10, frame: int | None = None) -> tuple[int, int, int | None, str]:
    return session, page, frame, "https://example.test/page"


class _ReplicaScope:
    def __init__(self, content: str, document_epoch: float) -> None:
        self.content_value = content
        self.document_epoch = document_epoch

    async def content(self) -> str:
        return self.content_value

    async def evaluate(self, _expression: str) -> float:
        return self.document_epoch


class _ReplicaFrame:
    def __init__(self, url: str, name: str, parent_frame: _ReplicaFrame | None) -> None:
        self.url = url
        self.name = name
        self.parent_frame = parent_frame


class _ReplicaPage:
    def __init__(
        self,
        *,
        url: str = "https://example.test/page",
        frame_chain: tuple[tuple[str, str], ...] = (),
        content: str = "x" * 150_000,
        document_epoch: float = 1234.5,
    ) -> None:
        self.url = url
        self.locator_scope = _ReplicaScope(content, document_epoch)
        parent: _ReplicaFrame | None = None
        for frame_url, frame_name in frame_chain:
            parent = _ReplicaFrame(frame_url, frame_name, parent)
        self.working_frame = parent


_STABLE_FRAME_CHAIN = (
    ("https://example.test/page", ""),
    ("https://example.test/frame", "checkoutFrame"),
)


def test_prune_html_is_deterministic_and_reports_every_pruning_tier() -> None:
    source = """<!doctype html><html><head>
    <style>.hidden { display:none }</style><script>alert('x')</script>
    </head><body><!-- remove me -->
    <button onclick="doThing()" onmouseover="hover()" aria-label="Keep">Go</button>
    <img alt="Receipt" src="data:image/png;base64,AAAA" />
    <a href="/safe">Safe</a></body></html>"""

    first, stats = prune_html(source)
    second, second_stats = prune_html(source)

    assert (first, stats) == (second, second_stats)
    assert "<script" not in first
    assert "<style" not in first
    assert "remove me" not in first
    assert "onclick" not in first
    assert "onmouseover" not in first
    assert "base64" not in first
    assert 'aria-label="Keep"' in first
    assert 'href="/safe"' in first
    assert stats == {
        "scripts_removed": 1,
        "styles_removed": 1,
        "comments_removed": 1,
        "event_handlers_removed": 2,
        "base64_blobs_removed": 1,
        "chars_removed": len(source) - len(first),
    }


@pytest.mark.parametrize("tag", ["script", "style"])
def test_prune_html_removes_self_closing_script_and_style_without_suppressing_following_content(tag: str) -> None:
    content, stats = prune_html(f"<div><{tag}/>keep</div>")

    assert content == "<div>keep</div>"
    assert stats[f"{tag}s_removed"] == 1


def test_prune_html_preserves_xml_serialized_svg_after_self_closing_style() -> None:
    source = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" preserveAspectRatio="xMidYMid meet">'
        '<style/><linearGradient id="chartGradient"><stop offset="0%"/></linearGradient>'
        '<title>Chart title</title><text x="1" y="10">LABEL TEXT</text></svg>'
    )

    content, stats = prune_html(source)

    assert content == (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" preserveAspectRatio="xMidYMid meet">'
        '<linearGradient id="chartGradient"><stop offset="0%" /></linearGradient>'
        '<title>Chart title</title><text x="1" y="10">LABEL TEXT</text></svg>'
    )
    assert 'viewBox="0 0 20 20"' in content
    assert 'preserveAspectRatio="xMidYMid meet"' in content
    assert "<linearGradient" in content
    assert stats["styles_removed"] == 1


def test_prune_html_reports_exact_chars_removed_on_multi_line_content() -> None:
    source = "<div>first</div>\n<!-- remove me -->\n<p>second</p>"
    expected_content = "<div>first</div>\n\n<p>second</p>"

    content, stats = prune_html(source)

    assert content == expected_content
    assert stats["chars_removed"] == len(source) - len(expected_content)

    passthrough = "<div>x</div>\nR&amp;D\n<p>&#169; stays</p>"
    content, stats = prune_html(passthrough)

    assert content == passthrough
    assert stats["chars_removed"] == 0


def test_prune_html_surfaces_injection_as_negative_chars_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stat is a corruption tripwire: if the reducer ever rewrites text so output outgrows the
    input, the raw difference must go negative — a clamp here would hide exactly that signal."""

    class _InjectingParser:
        def __init__(self) -> None:
            self.parts: list[str] = []
            self.stats = dict.fromkeys(page_read._PRUNE_COUNTER_KEYS, 0)

        def feed(self, data: str) -> None:
            self.parts.append(data + ";")

        def close(self) -> None:
            pass

    monkeypatch.setattr(page_read, "_LeanHTMLParser", _InjectingParser)

    content, stats = page_read.prune_html("<p>x</p>")

    assert content == "<p>x</p>;"
    assert stats["chars_removed"] == -1


def test_prune_html_does_not_stack_void_elements_that_never_close() -> None:
    """Browsers serialize void elements bare, so they reach handle_starttag and used to be pushed
    onto the open-tag stack with nothing able to pop them — every later close tag scanned the pile."""
    parser = page_read._LeanHTMLParser()
    parser.feed('<div><input name="a"><br><img src="x"><meta charset="utf-8"></div>')
    parser.close()

    assert parser.kept_tags == []
    assert "".join(parser.parts) == '<div><input name="a"><br><img src="x"><meta charset="utf-8"></div>'


@pytest.mark.parametrize(
    "source",
    [
        "<noscript>R&D spending</noscript>",
        "<noscript>Q&A</noscript>",
        "<p>R&amp;D</p>",
        "<p>&#169; &#xA9;</p>",
        "<p>&nbsp;x</p>",
        "<svg><![CDATA[x]]></svg>",
        "<div>x</div>\nR&amp;D and A&amp;M and B&amp;N",
        "<p>&#169;</p>\n<p>&#xA9; and &#38; stay</p>",
        "<p>x</p>\nR&D bare <p>A&amp;B</p>",
        "<p>A&amp;B</p>\r\nC&amp;D and E&#38;F",
        "<noscript>line one\nR&D spending</noscript>",
    ],
    ids=[
        "bare-amp",
        "bare-amp-no-space",
        "escaped-amp",
        "charrefs",
        "entityref",
        "cdata",
        "multiline-entityrefs",
        "multiline-charrefs",
        "multiline-bare-amp",
        "crlf",
        "multiline-raw-text",
    ],
)
def test_prune_html_round_trips_references_and_marked_sections(source: str) -> None:
    """html.parser drops the trailing ';' and the ']]>' terminator; rebuilding them blindly
    rewrote real text ("R&D" -> "R&D;") and left CDATA unterminated."""
    assert prune_html(source)[0] == source


def test_prune_html_keeps_reference_semicolons_past_the_first_line() -> None:
    source = "<div>x</div>\nR&amp;D and A&amp;M and B&amp;N"

    output, stats = prune_html(source)

    assert output == source
    assert "&ampD" not in output
    assert stats["chars_removed"] == 0


def test_pruning_stats_keys_are_identical_across_modes() -> None:
    lean = prune_html("<p>x</p>")[1]

    assert set(lean) == set(page_read._PRUNING_STAT_KEYS)


@pytest.mark.parametrize(
    "character",
    ["x", "\u4e2d", "\u00e9", "\U0001f600"],
    ids=["ascii", "cjk", "accented", "emoji"],
)
def test_chunk_budget_is_not_spent_on_escapes_the_wire_never_carries(character: str) -> None:
    """The response cap measures with ensure_ascii=False, so budgeting with True cost non-ASCII
    pages 6-12x smaller chunks — and each chunk re-serializes the whole document."""
    page = paginate_content(character * 80_000, binding=_binding(), max_chars=60_000)

    assert len(page["content"]) == 60_000


def test_pagination_hash_is_deterministic_for_lone_surrogates() -> None:
    content = "before\ud800after" * 6_000
    cursor: str | None = None
    chunks: list[str] = []

    while True:
        page = paginate_content(content, binding=_binding(), max_chars=50_000, cursor=cursor)
        retry = paginate_content(content, binding=_binding(), max_chars=50_000, cursor=cursor)
        assert retry == page
        chunks.append(page["content"])
        cursor = page["cursor_next"]
        if cursor is None:
            break

    assert "".join(chunks) == content


@pytest.mark.asyncio
async def test_cursor_uses_real_binding_across_replicas(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.core.result import BrowserContext

    # Distinguishable blocks, so resuming at 50,000 cannot be confused with restarting at 0.
    document = "A" * 50_000 + "B" * 50_000 + "C" * 50_000

    # A fresh equivalent wrapper per call, which is what a replica hop actually hands back.
    async def fake_get_page(**_kwargs: object) -> tuple[_ReplicaPage, BrowserContext]:
        return (
            _ReplicaPage(frame_chain=_STABLE_FRAME_CHAIN, content=document),
            BrowserContext(mode="cloud_session", session_id="pbs_same_logical_session"),
        )

    monkeypatch.setattr(inspection, "get_page", fake_get_page)
    monkeypatch.setattr(page_read.settings, "SECRET_KEY", "shared-replica-test-key")
    monkeypatch.setattr(page_read, "_CURSOR_FALLBACK_SECRET", b"replica-a")

    first = await inspection.skyvern_page(
        mode="html",
        max_chars=50_000,
        session_id="pbs_same_logical_session",
    )
    assert first["data"]["content"] == "A" * 50_000
    cursor = first["data"]["cursor_next"]
    assert cursor is not None

    # Only the process-local fallback secret changes: a different replica, same configured key.
    monkeypatch.setattr(page_read, "_CURSOR_FALLBACK_SECRET", b"replica-b")
    second = await inspection.skyvern_page(
        mode="html",
        max_chars=50_000,
        cursor=cursor,
    )

    assert second["data"]["content"] == "B" * 50_000
    assert second["data"]["total_size"] == len(document)


@pytest.mark.asyncio
async def test_read_page_text_mode_falls_back_when_the_node_is_not_an_html_element() -> None:
    """A matched SVG/XML node fails inner_text(), so text mode used to fail on a selector that hit."""

    class SvgLocator:
        first: SvgLocator

        def __init__(self) -> None:
            self.first = self

        async def count(self) -> int:
            return 1

        async def inner_text(self) -> str:
            raise RuntimeError("Node is not an HTMLElement")

        async def text_content(self) -> str:
            return "LABEL TEXT"

    class Scope:
        def locator(self, _selector: str) -> SvgLocator:
            return SvgLocator()

        async def evaluate(self, _expression: str) -> float:
            return 1.0

    class Page:
        locator_scope = Scope()

    result = await read_page(
        Page(), binding=_binding(), selector="svg text", mode="text", max_chars=DEFAULT_MAX_CHARS, cursor=None
    )

    assert result["content"] == "LABEL TEXT"


@pytest.mark.asyncio
async def test_read_page_text_mode_propagates_unrelated_errors_from_inner_text() -> None:
    class ErrorLocator:
        first: ErrorLocator

        def __init__(self) -> None:
            self.first = self
            self.text_content_calls = 0

        async def count(self) -> int:
            return 1

        async def inner_text(self) -> str:
            raise RuntimeError("Page is not an active target")

        async def text_content(self) -> str:
            self.text_content_calls += 1
            return "UNREACHABLE"

    class Scope:
        def __init__(self) -> None:
            self.target = ErrorLocator()

        def locator(self, _selector: str) -> ErrorLocator:
            return self.target

        async def evaluate(self, _expression: str) -> float:
            return 1.0

    class Page:
        def __init__(self) -> None:
            self.locator_scope = Scope()

    page = Page()
    with pytest.raises(RuntimeError):
        await read_page(
            page, binding=_binding(), selector="svg text", mode="text", max_chars=DEFAULT_MAX_CHARS, cursor=None
        )

    assert page.locator_scope.target.text_content_calls == 0


def test_cursor_local_placeholder_uses_process_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    content = "x" * 150_000
    placeholder = type(page_read.settings).model_fields["SECRET_KEY"].default
    monkeypatch.setattr(page_read.settings, "SECRET_KEY", placeholder)
    monkeypatch.setattr(page_read, "_CURSOR_FALLBACK_SECRET", b"local-process-a")
    cursor = paginate_content(content, binding=_binding(), max_chars=50_000)["cursor_next"]
    assert cursor is not None
    paginate_content(content, binding=_binding(), max_chars=50_000, cursor=cursor)

    monkeypatch.setattr(page_read, "_CURSOR_FALLBACK_SECRET", b"local-process-b")
    with pytest.raises(CursorError, match="expired or invalid"):
        paginate_content(content, binding=_binding(), max_chars=50_000, cursor=cursor)


def test_cursor_signature_failure_names_secret_key_when_fallback_signed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = "x" * 150_000
    placeholder = type(page_read.settings).model_fields["SECRET_KEY"].default
    monkeypatch.setattr(page_read.settings, "SECRET_KEY", placeholder)
    monkeypatch.setattr(page_read, "_CURSOR_FALLBACK_SECRET", b"process-a")
    cursor = paginate_content(content, binding=_binding(), max_chars=50_000)["cursor_next"]
    assert cursor is not None

    monkeypatch.setattr(page_read, "_CURSOR_FALLBACK_SECRET", b"process-b")
    with pytest.raises(CursorError, match="expired or invalid") as excinfo:
        paginate_content(content, binding=_binding(), max_chars=50_000, cursor=cursor)
    assert "SECRET_KEY" in excinfo.value.hint

    monkeypatch.setattr(page_read.settings, "SECRET_KEY", "a-real-configured-secret")
    configured_cursor = paginate_content(content, binding=_binding(), max_chars=50_000)["cursor_next"]
    assert configured_cursor is not None
    replacement = "A" if configured_cursor[-2] != "A" else "B"
    tampered_cursor = f"{configured_cursor[:-2]}{replacement}{configured_cursor[-1]}"

    with pytest.raises(CursorError, match="expired or invalid") as configured_excinfo:
        paginate_content(content, binding=_binding(), max_chars=50_000, cursor=tampered_cursor)
    assert "SECRET_KEY" not in configured_excinfo.value.hint

    # A truncated cursor is malformed input, not evidence about the key: under the same
    # fallback-signed process it must keep the plain hint, not blame SECRET_KEY.
    monkeypatch.setattr(page_read.settings, "SECRET_KEY", placeholder)
    short_cursor = base64.urlsafe_b64encode(b"tiny").rstrip(b"=").decode()
    with pytest.raises(CursorError, match="expired or invalid") as truncated_excinfo:
        paginate_content(content, binding=_binding(), max_chars=50_000, cursor=short_cursor)
    assert "SECRET_KEY" not in truncated_excinfo.value.hint


@pytest.mark.asyncio
async def test_read_page_returns_pinned_response_keys_and_pruning_stats() -> None:
    class Scope:
        async def content(self) -> str:
            return "<html><!--gone--><body><script>gone()</script><p>Keep</p></body></html>"

        async def evaluate(self, _expression: str) -> float:
            return 1.0

    class Page:
        locator_scope = Scope()

    result = await read_page(
        Page(),
        binding=_binding(),
        selector=None,
        mode="lean_html",
        max_chars=DEFAULT_MAX_CHARS,
        cursor=None,
    )

    assert set(result) == {"content", "total_size", "cursor_next", "pruning_stats"}
    assert result["content"] == "<html><body><p>Keep</p></body></html>"
    assert result["pruning_stats"]["scripts_removed"] == 1
    assert result["pruning_stats"]["comments_removed"] == 1


@pytest.mark.asyncio
async def test_read_page_text_mode_falls_back_from_missing_body_to_html() -> None:
    class Locator:
        def __init__(self, count: int, text: str = "") -> None:
            self._count = count
            self._text = text

        @property
        def first(self) -> Locator:
            return self

        async def count(self) -> int:
            return self._count

        async def inner_text(self) -> str:
            return self._text

    class Scope:
        calls: list[str] = []

        def locator(self, selector: str) -> Locator:
            self.calls.append(selector)
            return Locator(0) if selector == "body" else Locator(1, "HTML fallback text")

        async def evaluate(self, _expression: str) -> float:
            return 1.0

    scope = Scope()

    class Page:
        locator_scope = scope

    result = await read_page(
        Page(), binding=_binding(), selector=None, mode="text", max_chars=DEFAULT_MAX_CHARS, cursor=None
    )

    assert result["content"] == "HTML fallback text"
    assert scope.calls == ["body", "html"]


@pytest.mark.asyncio
async def test_read_page_selector_not_found_is_actionable() -> None:
    class MissingLocator:
        first: MissingLocator

        def __init__(self) -> None:
            self.first = self

        async def count(self) -> int:
            return 0

    class Scope:
        def locator(self, _selector: str) -> MissingLocator:
            return MissingLocator()

        async def evaluate(self, _expression: str) -> float:
            return 1.0

    class Page:
        locator_scope = Scope()

    with pytest.raises(ValueError, match="did not match an element on the current page or frame"):
        await read_page(
            Page(),
            binding=_binding(),
            selector="#missing",
            mode="text",
            max_chars=DEFAULT_MAX_CHARS,
            cursor=None,
        )


@pytest.mark.asyncio
async def test_read_page_cursor_expires_on_same_content_document_navigation() -> None:
    class Scope:
        async def content(self) -> str:
            return "x" * 150_000

    class Page:
        locator_scope = Scope()

    first = await read_page(
        Page(), binding=(*_binding(), 1.0), selector=None, mode="html", max_chars=50_000, cursor=None
    )

    with pytest.raises(CursorError, match="expired or invalid"):
        await read_page(
            Page(),
            binding=(*_binding(), 2.0),
            selector=None,
            mode="html",
            max_chars=50_000,
            cursor=first["cursor_next"],
        )


def _page_resolver(*pages: _ReplicaPage) -> tuple[Any, list[int]]:
    """Stands in for get_page, which skyvern_page calls again to see where a continuation lands."""
    from skyvern.cli.core.result import BrowserContext

    remaining = iter(pages)
    calls: list[int] = []

    async def fake_get_page(**_kwargs: object) -> tuple[_ReplicaPage, BrowserContext]:
        calls.append(1)
        return next(remaining), BrowserContext(mode="local")

    return fake_get_page, calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "next_page",
    [
        _ReplicaPage(document_epoch=2.0),
        _ReplicaPage(url="https://example.test/popup", document_epoch=1.0),
    ],
    ids=["document-swapped", "popup-stole-the-default-target"],
)
async def test_skyvern_page_withholds_a_cursor_the_next_call_could_not_redeem(
    monkeypatch: pytest.MonkeyPatch,
    next_page: _ReplicaPage,
) -> None:
    """Serialization is an await. The page can navigate under it, or a popup can become the last
    context page and so the next unpinned target. Either way the minted cursor is already dead."""
    from skyvern.cli.mcp_tools import inspection

    fake_get_page, _ = _page_resolver(_ReplicaPage(document_epoch=1.0), next_page)
    monkeypatch.setattr(inspection, "get_page", fake_get_page)

    result = await inspection.skyvern_page(mode="html")

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "without cursor" in result["error"]["hint"]


@pytest.mark.asyncio
async def test_skyvern_page_single_chunk_read_never_re_resolves_the_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is continued, so there is no unredeemable cursor to withhold and no reason to look."""
    from skyvern.cli.mcp_tools import inspection

    fake_get_page, calls = _page_resolver(_ReplicaPage(content="short", document_epoch=1.0))
    monkeypatch.setattr(inspection, "get_page", fake_get_page)

    result = await inspection.skyvern_page(mode="html")

    assert result["ok"] is True
    assert result["data"]["cursor_next"] is None
    assert len(calls) == 1


def test_oversized_cursor_is_rejected_before_being_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad signature rejects it either way; what matters is not allocating the decode first."""

    def fail_if_called(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("oversized cursor reached the decoder")

    monkeypatch.setattr(page_read.base64, "b64decode", fail_if_called)

    with pytest.raises(CursorError, match="expired or invalid"):
        paginate_content("x" * 150_000, binding=_binding(), max_chars=50_000, cursor="A" * (MAX_CURSOR_CHARS + 1))


def test_minted_cursors_stay_within_the_declared_bound() -> None:
    cursor = paginate_content("x" * 150_000, binding=_binding(), max_chars=50_000)["cursor_next"]

    assert cursor is not None
    assert len(cursor) <= MAX_CURSOR_CHARS


@pytest.mark.asyncio
async def test_cursor_input_is_bounded_in_the_published_tool_schema() -> None:
    from skyvern.cli.mcp_tools import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}
    cursor_schema = json.dumps(tools["skyvern_page"].parameters["properties"]["cursor"])

    assert f'"maxLength": {MAX_CURSOR_CHARS}' in cursor_schema


def test_large_content_paginates_without_loss_or_overlap_and_has_stable_retry() -> None:
    content = "".join(f"<{i:06d}>" for i in range(20_000))  # 160,000 chars
    cursor: str | None = None
    chunks: list[str] = []

    while True:
        page = paginate_content(content, binding=_binding(), max_chars=50_000, cursor=cursor)
        retry = paginate_content(content, binding=_binding(), max_chars=50_000, cursor=cursor)
        assert retry == page
        assert page["total_size"] == len(content)
        assert len(page["content"]) <= 50_000
        chunks.append(page["content"])
        cursor = page["cursor_next"]
        if cursor is None:
            break

    assert len(content) > 140_000
    assert "".join(chunks) == content


def test_control_character_content_paginates_within_json_budget_without_loss() -> None:
    content = "\x01" * 25_000
    cursor: str | None = None
    chunks: list[str] = []
    consumed = 0

    while True:
        page = paginate_content(content, binding=_binding(), cursor=cursor)
        assert paginate_content(content, binding=_binding(), cursor=cursor) == page
        assert len(json.dumps(page["content"])) - 2 <= DEFAULT_MAX_CHARS
        chunks.append(page["content"])
        consumed += len(page["content"])
        cursor = page["cursor_next"]
        if consumed < len(content):
            assert cursor is not None
        else:
            assert cursor is None
            break

    assert "".join(chunks) == content


def test_minimum_max_chars_always_progresses_and_loses_nothing() -> None:
    """At max_chars=1 every slice must still advance at least one character — even when that
    character's JSON-escaped form exceeds the requested budget — or pagination would spin forever."""
    content = "\x01a\x02b\x03"
    cursor: str | None = None
    chunks: list[str] = []

    for _ in range(len(content)):
        page = paginate_content(content, binding=_binding(), max_chars=1, cursor=cursor)
        assert len(page["content"]) == 1
        chunks.append(page["content"])
        cursor = page["cursor_next"]
        if cursor is None:
            break

    assert "".join(chunks) == content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed_page,changed_session",
    [
        (_ReplicaPage(frame_chain=_STABLE_FRAME_CHAIN), "pbs_other_session"),
        (
            _ReplicaPage(url="https://example.test/navigated", frame_chain=_STABLE_FRAME_CHAIN),
            "pbs_session",
        ),
        (
            _ReplicaPage(
                frame_chain=(
                    ("https://example.test/page", ""),
                    ("https://example.test/other-frame", "otherFrame"),
                )
            ),
            "pbs_session",
        ),
    ],
    ids=["cross-session", "navigation", "frame-change"],
)
async def test_cursor_rejects_real_session_page_and_frame_binding_changes(
    changed_page: _ReplicaPage,
    changed_session: str,
) -> None:
    from skyvern.cli.core.result import BrowserContext

    content = "x" * 150_000
    original_page = _ReplicaPage(frame_chain=_STABLE_FRAME_CHAIN)
    binding = await inspection._page_cursor_binding(
        original_page,
        ctx=BrowserContext(mode="cloud_session", session_id="pbs_session"),
    )
    cursor = paginate_content(content, binding=binding, max_chars=50_000)["cursor_next"]
    assert cursor is not None
    changed_binding = await inspection._page_cursor_binding(
        changed_page,
        ctx=BrowserContext(mode="cloud_session", session_id=changed_session),
    )

    with pytest.raises(CursorError, match="expired or invalid"):
        paginate_content(content, binding=changed_binding, max_chars=50_000, cursor=cursor)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_selector,first_mode,second_selector,second_mode",
    [
        ("#a", "html", "#b", "html"),
        (None, "html", None, "lean_html"),
    ],
    ids=["selector-switch", "mode-switch"],
)
async def test_cursor_rejects_selector_or_mode_switch_on_byte_identical_content(
    first_selector: str | None,
    first_mode: PageMode,
    second_selector: str | None,
    second_mode: PageMode,
) -> None:
    """Identical cards share outerHTML and lean markup survives prune_html, so document_revision
    matches across the switch. Only binding selector and mode rejects the foreign cursor."""
    markup = '<div class="card"><p>hello</p></div>' * 4000
    assert prune_html(markup)[0] == markup, "fixture must be lean so html and lean_html agree byte-for-byte"

    class Locator:
        first: Locator

        def __init__(self) -> None:
            self.first = self

        async def count(self) -> int:
            return 1

        async def evaluate(self, _expression: str) -> str:
            return markup

    class Scope:
        async def content(self) -> str:
            return markup

        def locator(self, _selector: str) -> Locator:
            return Locator()

        async def evaluate(self, _expression: str) -> float:
            return 1.0

    class Page:
        locator_scope = Scope()

    first = await read_page(
        Page(), binding=_binding(), selector=first_selector, mode=first_mode, max_chars=50_000, cursor=None
    )
    assert first["cursor_next"] is not None

    with pytest.raises(CursorError, match="expired or invalid"):
        await read_page(
            Page(),
            binding=_binding(),
            selector=second_selector,
            mode=second_mode,
            max_chars=50_000,
            cursor=first["cursor_next"],
        )


def test_cursor_rejects_document_revision_and_tampering() -> None:
    content = "x" * 150_000
    cursor = paginate_content(content, binding=_binding(), max_chars=50_000)["cursor_next"]
    assert cursor is not None

    with pytest.raises(CursorError, match="expired or invalid"):
        paginate_content("y" + content[1:], binding=_binding(), max_chars=50_000, cursor=cursor)

    tampered = (
        cursor[: len(cursor) // 2] + ("A" if cursor[len(cursor) // 2] != "A" else "B") + cursor[len(cursor) // 2 + 1 :]
    )
    with pytest.raises(CursorError, match="expired or invalid"):
        paginate_content(content, binding=_binding(), max_chars=50_000, cursor=tampered)


def test_skyvern_page_default_is_bounded_for_common_mcp_harnesses() -> None:
    default = inspect.signature(skyvern_page).parameters["max_chars"].default

    assert default == DEFAULT_MAX_CHARS
    assert default <= 60_000


@pytest.mark.asyncio
async def test_skyvern_page_registration_is_additive_and_static() -> None:
    from skyvern.cli.mcp_tools import mcp
    from skyvern.cli.mcp_tools.scopes import SCOPES

    tools = {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}
    tool = tools["skyvern_page"]

    assert set(tool.tags) == {"page_read", "lean"}
    # `lean` was tagged here ahead of the scope that consumes it, so lean membership is deliberate.
    # Every scope that predates this tool must still resolve without it.
    assert not set(tool.tags) & {tag for scope, tags in SCOPES.items() if scope != "lean" for tag in tags}
    assert len((tool.description or "").split()) <= 150
    assert "skyvern_get_html" in tools
    assert set(tools["skyvern_get_html"].tags) == {"inspection", "lean"}


@pytest.mark.asyncio
async def test_skyvern_page_keeps_terminal_cursor_key_in_concise_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.core import result as result_module
    from skyvern.cli.core.result import BrowserContext
    from skyvern.cli.mcp_tools import inspection

    async def fake_get_page(**_kwargs: object) -> tuple[_ReplicaPage, BrowserContext]:
        return _ReplicaPage(url="url"), BrowserContext(mode="local")

    async def terminal_page(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"content": "done", "total_size": 4, "cursor_next": None, "pruning_stats": {}}

    monkeypatch.setattr(result_module, "_concise_responses", True)
    monkeypatch.setattr(inspection, "get_page", fake_get_page)
    monkeypatch.setattr(inspection, "read_page", terminal_page)

    result = await inspection.skyvern_page()

    assert result["data"]["cursor_next"] is None


@pytest.mark.asyncio
async def test_skyvern_page_returns_structured_error_when_cursor_binding_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.cli.core.result import BrowserContext
    from skyvern.cli.mcp_tools import inspection

    class BrokenPage:
        working_frame = None

        @property
        def url(self) -> str:
            raise RuntimeError("binding unavailable")

    async def fake_get_page(**_kwargs: object) -> tuple[BrokenPage, BrowserContext]:
        return BrokenPage(), BrowserContext(mode="local")

    monkeypatch.setattr(inspection, "get_page", fake_get_page)

    result = await inspection.skyvern_page()

    assert result["ok"] is False
    assert result["error"]["code"] == "ACTION_FAILED"
    assert result["error"]["message"] == "binding unavailable"


@pytest.mark.asyncio
async def test_skyvern_page_reports_no_browser_when_it_disappears_before_re_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second get_page can hit a browser that went away, which is the same condition the
    first one reports — not a generic action failure."""
    from skyvern.cli.core.result import BrowserContext
    from skyvern.cli.mcp_tools import inspection

    calls: list[int] = []

    async def fake_get_page(**_kwargs: object) -> tuple[_ReplicaPage, BrowserContext]:
        calls.append(1)
        if len(calls) > 1:
            raise inspection.BrowserNotAvailableError()
        return _ReplicaPage(), BrowserContext(mode="local")

    monkeypatch.setattr(inspection, "get_page", fake_get_page)

    result = await inspection.skyvern_page(mode="html", max_chars=50_000)

    assert result["ok"] is False
    assert result["error"]["code"] == "NO_ACTIVE_BROWSER"


@pytest.mark.asyncio
async def test_skyvern_page_returns_structured_actionable_cursor_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.core.result import BrowserContext
    from skyvern.cli.mcp_tools import inspection

    page = _ReplicaPage()

    # Resolution follows the requested session, so the second call lands somewhere else.
    async def fake_get_page(**kwargs: object) -> tuple[_ReplicaPage, BrowserContext]:
        requested = kwargs.get("session_id") or "pbs_different_session"
        return page, BrowserContext(mode="cloud_session", session_id=str(requested))

    monkeypatch.setattr(inspection, "get_page", fake_get_page)

    first = await inspection.skyvern_page(
        mode="html",
        max_chars=50_000,
        session_id="pbs_original_session",
    )
    result = await inspection.skyvern_page(
        mode="html",
        max_chars=50_000,
        cursor=first["data"]["cursor_next"],
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "expired or invalid" in result["error"]["message"]
    assert "without cursor" in result["error"]["hint"]
