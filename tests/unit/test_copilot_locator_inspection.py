"""The locator inspection tool: what it reports, and which browser it reports from.

A match count cannot separate a wrapper that uniquely resolves and holds the wrong text from the
child that carries the value. These pin the distinction the tool exists to expose, and the target
binding that decides which page it is read from.
"""

from __future__ import annotations

import asyncio
import html
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from playwright.async_api import Error as PlaywrightError

from skyvern.forge.sdk.copilot.browser_target import resolve_browser_session_binding
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm
from skyvern.forge.sdk.copilot.runtime import (
    SENSITIVE_ORIGIN_PAGE_ERROR,
    OriginRunRedactionRegistry,
)
from skyvern.forge.sdk.copilot.secret_scrub import (
    REDACTED_SECRET_PLACEHOLDER,
    clear_session_scrub_values,
    register_secret_scrub_value,
)
from skyvern.forge.sdk.copilot.tools import NATIVE_TOOLS, inspect_locator_matches_tool, run_execution
from skyvern.forge.sdk.copilot.tools.locator_inspection import (
    MAX_DESCENDANTS,
    MAX_MATCHES,
    MAX_SELECTORS,
    TOOL_DESCRIPTION,
    TOOL_NAME,
    TOOL_SCHEMA,
    inspect_locator_matches,
)
from skyvern.forge.sdk.copilot.tools.run_execution import build_test_evidence_packet
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.schemas.workflows import BlockType
from tests.unit.copilot_test_helpers import (
    SENSITIVE_DISCLOSURE_WITHHOLDING_ARMS,
    make_copilot_ctx,
    remove_sensitive_disclosure_prerequisite,
    taint_by_terminal_run,
)

STAR_BUTTON = 'button.pill:has-text("Star")'
STAR_VALUE = 'button.pill:has-text("Star") span.n'
DEAD = 'button[aria-label="You must be signed in to star a repository"]'


class _FakeLocator:
    def __init__(self, page: _FakePage, selector: str) -> None:
        self._page = page
        self._selector = selector

    async def count(self) -> int:
        if self._selector in self._page.invalid:
            raise PlaywrightError(f'Unexpected token while parsing css "{self._selector}"')
        return len(self._page.elements.get(self._selector, []))

    def nth(self, index: int) -> _FakeMatch:
        return _FakeMatch(self._page.elements[self._selector][index])


class _FakeMatch:
    def __init__(self, facts: dict[str, Any]) -> None:
        self._facts = facts

    async def evaluate(self, _expression: str) -> dict[str, Any]:
        return dict(self._facts)


class _FakePage:
    """Stands in for a Playwright page: the tool only ever counts and evaluates against it."""

    def __init__(self, elements: dict[str, list[dict[str, Any]]], invalid: set[str] | None = None) -> None:
        self.elements = elements
        self.invalid = invalid or set()
        self.url = "https://example.test/repo"
        self.navigations: list[str] = []

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    async def goto(self, url: str) -> None:
        self.navigations.append(url)


def _star_page() -> _FakePage:
    value_node = {"tag": "span", "classes": ["n"], "text_content": "22.6k", "descendants": []}
    wrapper = {
        "tag": "button",
        "classes": ["pill"],
        "text_content": "⭐ Star22.6k",
        "outer_html": '<button class="pill">⭐ Star<span class="n">22.6k</span></button>',
        "descendants": [{"index": 0, "tag": "span", "classes": ["n"], "text_content": "22.6k"}],
    }
    return _FakePage(
        {
            STAR_BUTTON: [wrapper],
            STAR_VALUE: [value_node],
            DEAD: [],
            "button.pill": [wrapper, dict(wrapper), dict(wrapper)],
        }
    )


@pytest.mark.asyncio
async def test_a_unique_wrapper_and_the_node_holding_the_value_are_distinguishable() -> None:
    # Both resolve to exactly one element, so a match count alone cannot tell them apart. The
    # descendants are what let a repair pick the node that carries the number.
    page = _star_page()

    result = await inspect_locator_matches(page, [STAR_BUTTON, STAR_VALUE])

    by_selector = {entry["selector"]: entry for entry in result["selectors"]}
    wrapper = by_selector[STAR_BUTTON]["matches"][0]
    value = by_selector[STAR_VALUE]["matches"][0]
    assert by_selector[STAR_BUTTON]["match_count"] == by_selector[STAR_VALUE]["match_count"] == 1
    assert "Star" in wrapper["text_content"]
    assert [d["classes"] for d in wrapper["descendants"]] == [["n"]]
    assert wrapper["descendants"][0]["text_content"] == "22.6k"
    assert value["text_content"] == "22.6k"


@pytest.mark.asyncio
async def test_a_dead_selector_reports_zero_rather_than_erroring() -> None:
    result = await inspect_locator_matches(_star_page(), [DEAD])

    entry = result["selectors"][0]
    assert entry["match_count"] == 0
    assert entry["matches"] == []
    assert "error" not in entry


@pytest.mark.asyncio
async def test_a_multi_match_selector_reports_every_match_it_returns() -> None:
    result = await inspect_locator_matches(_star_page(), ["button.pill"])

    entry = result["selectors"][0]
    assert entry["match_count"] == 3
    assert len(entry["matches"]) == 3
    assert [m["index"] for m in entry["matches"]] == [0, 1, 2]


@pytest.mark.asyncio
async def test_an_unparseable_selector_is_isolated_to_its_own_entry() -> None:
    # One bad selector in a batch must not cost the model the answers for the others.
    page = _star_page()
    page.invalid = {"button.pill:has-text("}

    result = await inspect_locator_matches(page, ["button.pill:has-text(", STAR_VALUE])

    bad, good = result["selectors"]
    assert "error" in bad and "match_count" not in bad
    assert good["match_count"] == 1


@pytest.mark.asyncio
async def test_more_selectors_than_the_cap_are_dropped_and_disclosed() -> None:
    page = _FakePage({f"sel-{i}": [] for i in range(MAX_SELECTORS + 3)})

    result = await inspect_locator_matches(page, [f"sel-{i}" for i in range(MAX_SELECTORS + 3)])

    assert len(result["selectors"]) == MAX_SELECTORS
    assert result["selectors_truncated"] is True


@pytest.mark.asyncio
async def test_matches_beyond_the_cap_are_bounded_and_the_count_still_reports_the_total() -> None:
    element = {"tag": "li", "classes": [], "text_content": "row", "descendants": []}
    page = _FakePage({"li": [dict(element) for _ in range(MAX_MATCHES + 4)]})

    result = await inspect_locator_matches(page, ["li"])

    entry = result["selectors"][0]
    assert entry["match_count"] == MAX_MATCHES + 4
    assert len(entry["matches"]) == MAX_MATCHES
    assert entry["matches_truncated"] is True


@pytest.mark.asyncio
async def test_reading_the_page_neither_navigates_nor_changes_its_url() -> None:
    page = _star_page()
    before = page.url

    await inspect_locator_matches(page, [STAR_BUTTON, "button.pill"])

    assert page.url == before
    assert page.navigations == []


@pytest.mark.asyncio
async def test_the_same_call_twice_returns_byte_identical_output() -> None:
    page = _star_page()

    first = await inspect_locator_matches(page, [STAR_BUTTON, STAR_VALUE])
    second = await inspect_locator_matches(page, [STAR_BUTTON, STAR_VALUE])

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_the_advertised_contract_is_the_tested_contract() -> None:
    # Step 5 measured this description and this schema. Shipping a paraphrase, or a schema that
    # drops the parameter descriptions, would ship a different tool than the one that was measured.
    assert inspect_locator_matches_tool.description == TOOL_DESCRIPTION
    assert inspect_locator_matches_tool.params_json_schema == TOOL_SCHEMA
    assert TOOL_SCHEMA["additionalProperties"] is False
    assert all("description" in prop for prop in TOOL_SCHEMA["properties"].values())


def test_the_tool_sits_immediately_beside_broad_inspection() -> None:
    names = [tool.name for tool in NATIVE_TOOLS]

    assert names.index(TOOL_NAME) == names.index("inspect_page_for_composition") + 1


def test_a_last_run_that_shares_the_chats_browser_installs_no_override() -> None:
    # Pinning the recorded id here would strand the call on a retired browser once the chat's
    # session is re-established; following the live session is what keeps it readable.
    ctx = make_copilot_ctx(browser_session_id="pbs_same")
    ctx.last_run_blocks_browser_session_id = "pbs_same"

    binding = resolve_browser_session_binding(ctx, {"target": "last_run"})

    assert binding.target == "last_run"
    assert binding.session_id_override is None
    assert binding.source_matches_target is True
    assert binding.provenance()["browser_target"] == "last_run"


def test_a_last_run_in_its_own_browser_carries_that_session_as_the_override() -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.last_run_blocks_browser_session_id = "pbs_run"

    binding = resolve_browser_session_binding(ctx, {"target": "last_run"})

    assert binding.session_id_override == "pbs_run"
    assert binding.unavailable_reason is None


def test_a_last_run_with_no_recorded_run_is_refused_rather_than_served_from_debug() -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.last_run_blocks_browser_session_id = None

    binding = resolve_browser_session_binding(ctx, {"target": "last_run"})

    assert binding.unavailable_reason is not None
    assert binding.session_id_override is None
    assert binding.source_matches_target is False


def test_debug_targeting_follows_the_chats_own_browser() -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.last_run_blocks_browser_session_id = "pbs_run"

    binding = resolve_browser_session_binding(ctx, {"target": "debug"})

    assert binding.target == "debug"
    assert binding.session_id_override is None
    assert binding.session_id_for(ctx) == "pbs_chat"


@pytest.mark.asyncio
async def test_a_browser_with_no_open_page_is_reported_rather_than_given_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Creating a page would both mutate browser state and answer from a page the run never reached.
    created: list[str] = []

    async def _no_working_page() -> None:
        return None

    async def _would_create() -> None:
        created.append("created")

    state = SimpleNamespace(get_working_page=_no_working_page, get_or_create_page=_would_create)

    async def _resolve(_ctx: CopilotContext) -> SimpleNamespace:
        return state

    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.resolve_browser_state_for_context", _resolve)
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.last_run_blocks_browser_session_id = "pbs_run"

    raw = await inspect_locator_matches_tool.on_invoke_tool(
        SimpleNamespace(context=ctx), json.dumps({"target": "last_run", "selectors": [STAR_VALUE]})
    )

    result = json.loads(raw)
    assert result["ok"] is False
    assert "no open page" in result["error"]
    assert result["browser_target"] == "last_run"
    assert created == []


@pytest.mark.asyncio
async def test_locator_result_is_suppressed_when_session_becomes_tainted_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_debug")
    page = _star_page()

    async def _working_page() -> _FakePage:
        return page

    async def _taint_during_inspection(_page: _FakePage, _selectors: list[str]) -> list[dict[str, Any]]:
        ctx.sensitive_origin_browser_session_ids.add("pbs_debug")
        return [{"authored_selector": STAR_VALUE, "match_count": 1}]

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.resolve_browser_state_for_context",
        AsyncMock(return_value=SimpleNamespace(get_working_page=_working_page)),
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.inspect_locator_matches", _taint_during_inspection)

    raw = await inspect_locator_matches_tool.on_invoke_tool(
        SimpleNamespace(context=ctx), json.dumps({"target": "debug", "selectors": [STAR_VALUE]})
    )

    result = json.loads(raw)
    assert result["ok"] is False
    assert "specific named URL" in result["error"]
    assert "data" not in result


OTP_SELECTOR = "#token"
RUN_OTP = "424242"


def _terminal_credential_run_ctx() -> CopilotContext:
    ctx = make_copilot_ctx(browser_session_id="pbs_run")
    clear_session_scrub_values("pbs_run")
    ctx.last_run_blocks_browser_session_id = "pbs_run"
    ctx.last_run_blocks_workflow_run_id = "wr_credential"
    taint_by_terminal_run(ctx, workflow_run_id="wr_credential", session_id="pbs_run")
    ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
        workflow_run_id="wr_credential",
        parameters={"totp": RUN_OTP},
        contains_sensitive_values=True,
        contains_all_sensitive_values=True,
    )
    return ctx


def _authenticator_page() -> _FakePage:
    return _FakePage(
        {
            OTP_SELECTOR: [
                {
                    "tag": "input",
                    "text_content": f"code {RUN_OTP}",
                    "outer_html": f'<input id="token" value="{RUN_OTP}">',
                    "descendants": [{"index": 0, "tag": "span", "text_content": RUN_OTP}],
                }
            ]
        }
    )


def _bind_authenticator_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.resolve_browser_state_for_context",
        AsyncMock(return_value=SimpleNamespace(get_working_page=AsyncMock(return_value=_authenticator_page()))),
    )


@pytest.mark.asyncio
async def test_locator_reports_from_the_page_a_terminal_credential_run_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _terminal_credential_run_ctx()
    _bind_authenticator_page(monkeypatch)

    raw = await inspect_locator_matches_tool.on_invoke_tool(
        SimpleNamespace(context=ctx), json.dumps({"target": "last_run", "selectors": [OTP_SELECTOR]})
    )

    result = json.loads(raw)
    assert result["ok"] is True
    assert result["browser_target_workflow_run_id"] == "wr_credential"
    entry = result["data"]["selectors"][0]
    assert entry["selector"] == OTP_SELECTOR
    assert entry["match_count"] == 1
    assert RUN_OTP not in raw
    assert REDACTED_SECRET_PLACEHOLDER in raw


@pytest.mark.asyncio
async def test_locator_scrubs_encoded_forms_of_the_run_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """This read never crosses the MCP adapter: the URL carries the username URL-encoded and the
    outer HTML the password HTML-escaped, and neither form may reach the model."""
    username, password = "demo.user@pathfold.test", "Sp1r!t&Level<2026>"
    ctx = _terminal_credential_run_ctx()
    ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
        workflow_run_id="wr_credential",
        parameters={"credential": {"username": username, "password": password}},
        contains_sensitive_values=True,
        contains_all_sensitive_values=True,
    )
    page = SimpleNamespace(url=f"http://pathfold.test/app?user={quote(username, safe='')}")
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.resolve_browser_state_for_context",
        AsyncMock(return_value=SimpleNamespace(get_working_page=AsyncMock(return_value=page))),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.inspect_locator_matches",
        AsyncMock(
            return_value={
                "selectors": [
                    {
                        "selector": OTP_SELECTOR,
                        "match_count": 1,
                        "matches": [{"tag": "input", "outer_html": f'<input value="{html.escape(password)}">'}],
                    }
                ]
            }
        ),
    )

    raw = await inspect_locator_matches_tool.on_invoke_tool(
        SimpleNamespace(context=ctx), json.dumps({"target": "last_run", "selectors": [OTP_SELECTOR]})
    )

    assert json.loads(raw)["ok"] is True
    for form in (username, quote(username, safe=""), password, html.escape(password)):
        assert form not in raw
    assert REDACTED_SECRET_PLACEHOLDER in raw


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", SENSITIVE_DISCLOSURE_WITHHOLDING_ARMS)
async def test_locator_withholds_the_same_page_when_a_prerequisite_is_absent(
    monkeypatch: pytest.MonkeyPatch, arm: str
) -> None:
    ctx = _terminal_credential_run_ctx()
    remove_sensitive_disclosure_prerequisite(ctx, arm)
    _bind_authenticator_page(monkeypatch)

    raw = await inspect_locator_matches_tool.on_invoke_tool(
        SimpleNamespace(context=ctx), json.dumps({"target": "last_run", "selectors": [OTP_SELECTOR]})
    )

    result = json.loads(raw)
    assert result["ok"] is False
    assert result["error"] == SENSITIVE_ORIGIN_PAGE_ERROR
    assert "data" not in result


@pytest.mark.asyncio
async def test_no_selectors_is_answered_without_touching_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved: list[str] = []

    async def _resolve(_ctx: CopilotContext) -> None:
        resolved.append("resolved")

    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.resolve_browser_state_for_context", _resolve)
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")

    raw = await inspect_locator_matches_tool.on_invoke_tool(
        SimpleNamespace(context=ctx), json.dumps({"target": "debug", "selectors": ["   "]})
    )

    result = json.loads(raw)
    assert result["ok"] is False
    assert result["error"] == "No selectors supplied."
    assert resolved == []


@pytest.mark.asyncio
async def test_descendants_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    element = {
        "tag": "div",
        "classes": [],
        "text_content": "x",
        "descendants": [{"index": i, "tag": "span"} for i in range(MAX_DESCENDANTS)],
    }
    page = _FakePage({"div": [element]})

    result = await inspect_locator_matches(page, ["div"])

    assert len(result["selectors"][0]["matches"][0]["descendants"]) <= MAX_DESCENDANTS


@pytest.mark.asyncio
async def test_an_unreachable_browser_becomes_a_factual_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A read-only inspection whose target browser is gone should steer the model, and must not
    # quietly answer from the chat's browser instead.
    inspected: list[str] = []

    async def _unreachable(_ctx: CopilotContext) -> None:
        return None

    async def _should_not_run(_page: object, _selectors: list[str]) -> dict[str, Any]:
        inspected.append("inspected")
        return {}

    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.resolve_browser_state_for_context", _unreachable)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.inspect_locator_matches", _should_not_run)
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.last_run_blocks_browser_session_id = "pbs_run"
    recorded: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.record_tool_step_result_for_ctx",
        lambda _ctx, name, _args, result: recorded.append((name, result)),
    )

    raw = await inspect_locator_matches_tool.on_invoke_tool(
        SimpleNamespace(context=ctx), json.dumps({"target": "last_run", "selectors": [STAR_VALUE]})
    )

    result = json.loads(raw)
    assert result["ok"] is False
    assert "no longer available" in result["error"]
    assert result["browser_target"] == "last_run"
    assert inspected == []
    assert recorded and recorded[0][1] == result


@pytest.mark.asyncio
async def test_a_secret_survives_neither_the_returned_nor_the_recorded_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Page text is untrusted content that can carry a value the user gave the copilot. The scrub
    # runs once on the finished structure, so the recorded copy and the returned copy are the same
    # sanitized object rather than two chances to leak.
    secret = "hunter2-super-secret-token"
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    register_secret_scrub_value(ctx, secret)
    try:
        page = _FakePage(
            {
                STAR_VALUE: [
                    {
                        "tag": "span",
                        "classes": ["n"],
                        "text_content": f"value {secret}",
                        "outer_html": f'<span class="n">{secret}</span>',
                        "descendants": [],
                    }
                ]
            }
        )
        page.url = f"https://example.test/?token={secret}"

        async def _resolve(_ctx: CopilotContext) -> SimpleNamespace:
            async def _working_page() -> _FakePage:
                return page

            return SimpleNamespace(get_working_page=_working_page)

        monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.resolve_browser_state_for_context", _resolve)
        recorded: list[object] = []
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools.record_tool_step_result_for_ctx",
            lambda _ctx, _name, _args, result: recorded.append(result),
        )

        raw = await inspect_locator_matches_tool.on_invoke_tool(
            SimpleNamespace(context=ctx), json.dumps({"target": "debug", "selectors": [STAR_VALUE]})
        )

        assert secret not in raw
        assert "[REDACTED_SECRET]" in raw
        returned = json.loads(raw)
        assert recorded and secret not in json.dumps(recorded[0])
        assert recorded[0] == returned
    finally:
        clear_session_scrub_values(ctx.browser_session_id)


@pytest.mark.asyncio
async def test_a_closed_run_page_reports_nothing_rather_than_zero_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counting against a page minted after the run's own closed would tell the model every
    authored locator is dead, when nothing was observed at all."""
    from skyvern.forge.sdk.copilot.tools import run_execution

    class _BlankPage:
        """What get_or_create_page mints once the run's own page is gone: it answers every
        selector with zero, which is the reading this test exists to prevent."""

        def locator(self, _selector: str):  # type: ignore[no-untyped-def]
            return SimpleNamespace(count=AsyncMock(return_value=0), evaluate_all=AsyncMock(return_value=[]))

    class _ClosedBrowserState:
        async def get_working_page(self):  # type: ignore[no-untyped-def]
            return None

        async def get_or_create_page(self):  # type: ignore[no-untyped-def]
            return _BlankPage()

    async def _resolve(**_kwargs: object) -> object:
        return _ClosedBrowserState()

    monkeypatch.setattr(run_execution, "resolve_persistent_browser_state", _resolve)

    observed = await run_execution._observe_authored_locators(
        SimpleNamespace(organization_id="org-1"),  # type: ignore[arg-type]
        run_session_id="pbs_gone",
        failed_block_code='page.locator("#rate").inner_text()',
    )

    assert observed is not None
    assert observed == [{"authored_selector": "#rate", "unobserved_reason": "run_page_unavailable"}]


class _AuthoredLocatorMatch:
    def __init__(
        self,
        candidates: list[dict[str, str]] | None = None,
        *,
        failure: BaseException | None = None,
    ) -> None:
        self._candidates = candidates or []
        self._failure = failure

    async def evaluate(self, _expression: str) -> list[dict[str, str]]:
        if self._failure is not None:
            raise self._failure
        return self._candidates


class _AuthoredLocator:
    def __init__(
        self,
        count: int = 0,
        *,
        candidates: list[dict[str, str]] | None = None,
        count_failure: BaseException | None = None,
        count_waits: bool = False,
        identity_failure: BaseException | None = None,
    ) -> None:
        self._count = count
        self._count_failure = count_failure
        self._count_waits = count_waits
        self._match = _AuthoredLocatorMatch(candidates, failure=identity_failure)

    async def count(self) -> int:
        if self._count_failure is not None:
            raise self._count_failure
        if self._count_waits:
            await asyncio.Event().wait()
        return self._count

    def nth(self, index: int) -> _AuthoredLocatorMatch:
        assert index == 0
        return self._match


class _AuthoredLocatorPage:
    def __init__(self, locators: dict[str, _AuthoredLocator]) -> None:
        self._locators = locators

    def locator(self, selector: str) -> _AuthoredLocator:
        return self._locators[selector]


def _observation_browser(page: object | None) -> SimpleNamespace:
    return SimpleNamespace(get_working_page=AsyncMock(return_value=page))


@pytest.mark.asyncio
async def test_worker_owned_observation_types_every_selector_without_resolving_a_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge.sdk.copilot.tools import run_execution

    resolver = AsyncMock()
    monkeypatch.setattr(run_execution, "resolve_persistent_browser_state", resolver)

    rows = await run_execution._observe_authored_locators(
        SimpleNamespace(organization_id="org-1"),  # type: ignore[arg-type]
        run_session_id="pbs_worker",
        failed_block_code='page.locator("#first")\npage.locator("#first")\npage.locator("text=Second")',
        worker_owned=True,
    )

    assert rows == [
        {"authored_selector": "#first", "unobserved_reason": "worker_owned_run"},
        {"authored_selector": "text=Second", "unobserved_reason": "worker_owned_run"},
    ]
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_browser_and_missing_page_have_distinct_typed_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.copilot.tools import run_execution

    monkeypatch.setattr(run_execution, "resolve_persistent_browser_state", AsyncMock(return_value=None))
    missing_browser = await run_execution._observe_authored_locators(
        SimpleNamespace(organization_id="org-1"),  # type: ignore[arg-type]
        run_session_id="pbs_missing",
        failed_block_code='page.locator("#target")',
    )
    monkeypatch.setattr(
        run_execution,
        "resolve_persistent_browser_state",
        AsyncMock(return_value=_observation_browser(None)),
    )
    missing_page = await run_execution._observe_authored_locators(
        SimpleNamespace(organization_id="org-1"),  # type: ignore[arg-type]
        run_session_id="pbs_closed",
        failed_block_code='page.locator("#target")',
    )

    assert missing_browser == [{"authored_selector": "#target", "unobserved_reason": "run_browser_unavailable"}]
    assert missing_page == [{"authored_selector": "#target", "unobserved_reason": "run_page_unavailable"}]


@pytest.mark.asyncio
async def test_resolution_and_identity_failures_never_become_zero_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.copilot.tools import run_execution

    page = _AuthoredLocatorPage(
        {
            "bad engine": _AuthoredLocator(count_failure=PlaywrightError("locator resolution failed")),
            "#identity": _AuthoredLocator(count=1, identity_failure=PlaywrightError("identity read failed")),
            "#absent": _AuthoredLocator(count=0),
        }
    )
    monkeypatch.setattr(
        run_execution,
        "resolve_persistent_browser_state",
        AsyncMock(return_value=_observation_browser(page)),
    )

    rows = await run_execution._observe_authored_locators(
        SimpleNamespace(organization_id="org-1"),  # type: ignore[arg-type]
        run_session_id="pbs_1",
        failed_block_code=('page.locator("bad engine")\npage.locator("#identity")\npage.locator("#absent")'),
    )

    assert rows == [
        {"authored_selector": "bad engine", "unobserved_reason": "locator_resolution_failed"},
        {"authored_selector": "#identity", "unobserved_reason": "identity_read_failed"},
        {"authored_selector": "#absent", "match_count": 0},
    ]


@pytest.mark.parametrize(
    ("failed_locator", "expected_reason"),
    [
        (_AuthoredLocator(count_failure=RuntimeError("browser transport closed")), "locator_resolution_failed"),
        (
            _AuthoredLocator(count=1, identity_failure=RuntimeError("browser transport closed")),
            "identity_read_failed",
        ),
    ],
)
@pytest.mark.asyncio
async def test_ordinary_observation_exception_is_typed_without_losing_the_failure(
    monkeypatch: pytest.MonkeyPatch,
    failed_locator: _AuthoredLocator,
    expected_reason: str,
) -> None:
    page = _AuthoredLocatorPage({"#failure": failed_locator, "#absent": _AuthoredLocator(count=0)})
    monkeypatch.setattr(
        run_execution,
        "resolve_persistent_browser_state",
        AsyncMock(return_value=_observation_browser(page)),
    )

    rows = await run_execution._observe_authored_locators(
        SimpleNamespace(organization_id="org-1"),  # type: ignore[arg-type]
        run_session_id="pbs_1",
        failed_block_code='page.locator("#failure")\npage.locator("#absent")',
    )

    assert rows == [
        {"authored_selector": "#failure", "unobserved_reason": expected_reason},
        {"authored_selector": "#absent", "match_count": 0},
    ]


@pytest.mark.parametrize(
    "cancelled_locator",
    [
        _AuthoredLocator(count_failure=asyncio.CancelledError()),
        _AuthoredLocator(count=1, identity_failure=asyncio.CancelledError()),
    ],
)
@pytest.mark.asyncio
async def test_observation_cancellation_is_not_converted_to_typed_absence(
    monkeypatch: pytest.MonkeyPatch,
    cancelled_locator: _AuthoredLocator,
) -> None:
    page = _AuthoredLocatorPage({"#cancelled": cancelled_locator})
    monkeypatch.setattr(
        run_execution,
        "resolve_persistent_browser_state",
        AsyncMock(return_value=_observation_browser(page)),
    )

    with pytest.raises(asyncio.CancelledError):
        await run_execution._observe_authored_locators(
            SimpleNamespace(organization_id="org-1"),  # type: ignore[arg-type]
            run_session_id="pbs_1",
            failed_block_code='page.locator("#cancelled")',
        )


@pytest.mark.asyncio
async def test_observation_deadline_preserves_partial_rows_and_types_every_remainder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge.sdk.copilot.tools import run_execution

    page = _AuthoredLocatorPage(
        {
            "#done": _AuthoredLocator(count=0),
            "#wedged": _AuthoredLocator(count_waits=True),
            "#later": _AuthoredLocator(count=0),
        }
    )
    monkeypatch.setattr(
        run_execution,
        "resolve_persistent_browser_state",
        AsyncMock(return_value=_observation_browser(page)),
    )
    monkeypatch.setattr(run_execution, "_OBSERVED_LOCATOR_BUDGET_SECONDS", 0.01)

    rows = await run_execution._observe_authored_locators(
        SimpleNamespace(organization_id="org-1"),  # type: ignore[arg-type]
        run_session_id="pbs_1",
        failed_block_code='page.locator("#done")\npage.locator("#wedged")\npage.locator("#later")',
    )

    assert rows == [
        {"authored_selector": "#done", "match_count": 0},
        {"authored_selector": "#wedged", "unobserved_reason": "observation_deadline_exceeded"},
        {"authored_selector": "#later", "unobserved_reason": "observation_deadline_exceeded"},
    ]


@pytest.mark.asyncio
async def test_observer_packet_and_sanitizer_preserve_typed_rows_and_exact_omission_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge.sdk.copilot.tools import run_execution

    selectors = [f"#item-{index}" for index in range(6)]
    page = _AuthoredLocatorPage({selector: _AuthoredLocator(count=0) for selector in selectors})
    monkeypatch.setattr(
        run_execution,
        "resolve_persistent_browser_state",
        AsyncMock(return_value=_observation_browser(page)),
    )
    rows = await run_execution._observe_authored_locators(
        SimpleNamespace(organization_id="org-1"),  # type: ignore[arg-type]
        run_session_id="pbs_1",
        failed_block_code="\n".join(f'page.locator("{selector}")' for selector in selectors),
    )
    ctx = make_copilot_ctx(
        workflow_yaml="workflow_definition:\n  blocks: []\n",
        persisted_workflow_yaml="workflow_definition:\n  blocks: []\n",
    )
    result = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_1",
            "overall_status": "failed",
            "requested_block_labels": ["failed"],
            "executed_block_labels": ["failed"],
            "blocks": [{"label": "failed", "status": "failed", "failure_reason": "failed"}],
            "authored_locator_observations": rows,
        },
    }
    result["data"]["build_test_packet"] = build_test_evidence_packet(ctx, result).model_dump(mode="json")

    sanitized = sanitize_tool_result_for_llm("run_blocks_and_collect_debug", result)
    packet = sanitized["data"]["build_test_packet"]

    assert len(packet["failure"]["locator_observations"]) == 4
    assert all("match_count" in row or "unobserved_reason" in row for row in packet["failure"]["locator_observations"])
    assert any(
        notice == "failure.locator_observations shortened: 2 item(s) omitted." for notice in packet["omission_notices"]
    )


@pytest.mark.asyncio
async def test_prior_run_result_uses_its_exact_failed_row_and_workflow_for_typed_locator_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge.sdk.copilot.tools import run_execution

    ctx = make_copilot_ctx()
    run = SimpleNamespace(
        workflow_run_id="wr_prior",
        workflow_id="w_prior",
        workflow_permanent_id=ctx.workflow_permanent_id,
        browser_session_id="pbs_prior",
        status="failed",
        failure_reason="click failed",
    )
    now = datetime.now(UTC)
    block = WorkflowRunBlock(
        workflow_run_block_id="wrb_prior",
        workflow_run_id="wr_prior",
        organization_id=ctx.organization_id,
        label="click_submit",
        block_type=BlockType.CODE,
        status="failed",
        failure_reason="click failed",
        error_codes=["user_code_error"],
        output=None,
        created_at=now,
        modified_at=now,
    )
    workflow = SimpleNamespace(
        workflow_definition={
            "blocks": [
                {
                    "label": "click_submit",
                    "block_type": "code",
                    "code": 'await page.locator("#submit").click()',
                }
            ]
        }
    )
    database = SimpleNamespace(
        workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=run)),
        observer=SimpleNamespace(get_workflow_run_blocks=AsyncMock(return_value=[block])),
        workflows=SimpleNamespace(
            get_workflow=AsyncMock(return_value=workflow),
            get_workflows_by_permanent_id=AsyncMock(),
        ),
    )
    monkeypatch.setattr(run_execution.app, "DATABASE", database)
    monkeypatch.setattr(
        run_execution.app.AGENT_FUNCTION,
        "should_dispatch_copilot_block_run_to_worker",
        AsyncMock(return_value=False),
    )

    async def _attach_trace(_blocks: list[object], results: list[dict[str, object]], _organization_id: str) -> None:
        results[0]["action_trace"] = [{"action": "NULL_ACTION", "status": "failed", "code_line": 4}]

    monkeypatch.setattr(run_execution, "_attach_action_traces", _attach_trace)
    monkeypatch.setattr(run_execution, "_attach_failed_block_screenshots", AsyncMock())
    observe = AsyncMock(return_value=[{"authored_selector": "#submit", "unobserved_reason": "run_page_unavailable"}])
    monkeypatch.setattr(run_execution, "_observe_authored_locators", observe)

    result = await run_execution._get_run_results({"workflow_run_id": "wr_prior"}, ctx, read_live_page=False)

    assert result["data"]["authored_locator_observations"] == [
        {"authored_selector": "#submit", "unobserved_reason": "run_page_unavailable"}
    ]
    observe.assert_awaited_once_with(
        ctx,
        run_session_id="pbs_prior",
        failed_block_code='await page.locator("#submit").click()',
        worker_owned=False,
        observation_deadline_exceeded=False,
    )
