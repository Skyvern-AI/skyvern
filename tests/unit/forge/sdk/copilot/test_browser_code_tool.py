from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import Page

from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.copilot.browser_ablation import (
    CopilotBrowserCodeMode,
    CopilotToolSurface,
    resolve_copilot_tool_surface,
)
from skyvern.forge.sdk.copilot.browser_code_contract import (
    BrowserCodeCellResult,
    BrowserCodeOperation,
    BrowserCodeSession,
    BrowserCodeSessionUnavailableError,
)
from skyvern.forge.sdk.copilot.runtime import CopilotBrowserGenerationRetired
from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays
from skyvern.forge.sdk.copilot.tools import browser_code as browser_code_module
from skyvern.forge.sdk.copilot.tools import copilot_native_tools, get_skyvern_mcp_alias_map
from skyvern.webeye.persistent_sessions_manager import BrowserRetirementReason
from tests.unit.conftest import make_copilot_context


@pytest.mark.asyncio
async def test_the_code_tool_mode_decides_whether_it_joins_or_replaces_the_direct_tools() -> None:
    aliases = get_skyvern_mcp_alias_map()
    overlays = _build_skyvern_mcp_overlays()
    oss_mode = await AgentFunction().copilot_browser_code_mode(organization_id="o", workflow_permanent_id="wp")

    def surface(mode: CopilotBrowserCodeMode) -> CopilotToolSurface:
        return resolve_copilot_tool_surface(
            mode=None,
            native_tools=copilot_native_tools(
                supports_question_tool=True, browser_code_available=mode != CopilotBrowserCodeMode.OFF
            ),
            alias_map=aliases,
            overlays=overlays,
            browser_code_mode=mode,
        )

    oss = surface(oss_mode)
    added = surface(CopilotBrowserCodeMode.ADD)
    replaced = surface(CopilotBrowserCodeMode.REPLACE)
    direct = {"click", "type_text", "evaluate", "navigate_browser"}

    assert oss_mode == CopilotBrowserCodeMode.OFF
    assert "run_browser_code" not in oss.ordered_native_names
    assert direct <= set(oss.ordered_mcp_names)
    # Added: the code tool sits beside every direct tool the surface already advertised.
    assert "run_browser_code" in added.ordered_native_names
    assert direct <= set(added.ordered_mcp_names)
    assert set(oss.ordered_native_names) | {"run_browser_code"} == set(added.ordered_native_names)
    # Replaced: the browser is reachable only through the code tool.
    assert "run_browser_code" in replaced.ordered_native_names
    assert direct.isdisjoint(replaced.ordered_mcp_names)
    assert {"fill_credential_field", "inspect_page_for_composition", "inspect_locator_matches"} <= set(
        replaced.ordered_native_names
    )
    assert len({oss.sha256, added.sha256, replaced.sha256}) == 3


class _LeaseProbeSession:
    """Records whether the browser lease was still held when each cell ran."""

    def __init__(
        self,
        lease_held: list[bool] | None = None,
        retire_mid_cell: bool = False,
        *,
        denials_sink: list[bool] | None = None,
    ) -> None:
        self._lease_held = lease_held if lease_held is not None else []
        self._retire_mid_cell = retire_mid_cell
        self._denials_sink = denials_sink
        self.session_id = "bcs_probe"
        self.page = SimpleNamespace(url="https://example.com/")
        self.closed = False

    async def rebind(self, page: Page) -> None:
        return None

    async def run_cell(self, code: str, *, timeout_seconds: float, deny_pixels: bool = False) -> BrowserCodeCellResult:
        self._lease_held.append(bool(_LEASE_DEPTH))
        if self._denials_sink is not None:
            self._denials_sink.append(deny_pixels)
        if self._retire_mid_cell:
            raise asyncio.CancelledError()
        return BrowserCodeCellResult(
            ok=True,
            value=1,
            stdout=None,
            stdout_truncated=False,
            error_code=None,
            error=None,
            failing_line=None,
            operations=(),
            operations_omitted=0,
            session_alive=True,
            current_url="https://example.com/",
        )

    def last_dispatched_operation(self) -> BrowserCodeOperation | None:
        return None

    async def close(self) -> None:
        self.closed = True


_LEASE_DEPTH: list[int] = []


@pytest.mark.parametrize("retire_mid_cell", [False, True])
@pytest.mark.asyncio
async def test_the_cell_runs_inside_the_browser_lease_and_a_retired_lease_reports_session_loss(
    monkeypatch: pytest.MonkeyPatch, retire_mid_cell: bool
) -> None:
    lease_held: list[bool] = []
    probe = _LeaseProbeSession(lease_held, retire_mid_cell=retire_mid_cell)

    @asynccontextmanager
    async def lease(_ctx: object, **_kwargs: object) -> AsyncIterator[None]:
        _LEASE_DEPTH.append(1)
        try:
            yield
        except asyncio.CancelledError:
            raise CopilotBrowserGenerationRetired("pbs_1", BrowserRetirementReason.session_ending) from None
        finally:
            _LEASE_DEPTH.pop()

    async def current_page(session_id: str | None = None) -> tuple[SimpleNamespace, None]:
        return SimpleNamespace(page=probe.page), None

    async def open_session(
        *,
        page: Page,
        lifetime_seconds: float,
        organization_id: str,
        chat_id: str,
        turn_id: str,
        browser_session_id: str | None,
    ) -> BrowserCodeSession:
        return probe

    async def prepared(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
        return None, None, None

    async def loss_disposition(*_args: object, **_kwargs: object) -> str:
        return "failed"

    monkeypatch.setattr(browser_code_module, "mcp_browser_context", lease)
    monkeypatch.setattr(browser_code_module, "get_page", current_page)
    monkeypatch.setattr(browser_code_module, "_prepare_browser_session_for_dispatch", prepared)
    monkeypatch.setattr(browser_code_module, "_browser_session_error_disposition", loss_disposition)
    monkeypatch.setattr(app.AGENT_FUNCTION, "open_copilot_browser_code_session", open_session)
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_1"

    result = await browser_code_module.run_browser_code(ctx, "1")

    assert lease_held == [True]
    if retire_mid_cell:
        assert result["ok"] is False
        assert probe.closed
        assert ctx.browser_code_host.session is None
        assert result["data"]["browser_session_continuity"]["disposition"] == "failed"
    else:
        assert result["ok"] is True
        assert result["value"] == 1


@pytest.mark.parametrize("session_ended", [True, False], ids=["session over", "capacity refusal"])
@pytest.mark.asyncio
async def test_a_refusal_that_says_the_session_is_over_drops_it_now(
    monkeypatch: pytest.MonkeyPatch, session_ended: bool
) -> None:
    """A typed refusal from a session the API has already marked dead is dropped on the spot. Kept, the
    retry the refusal asks for is spent rediscovering that, and only the call after reopens one. A
    refusal that leaves the session alive — a full runner — keeps it, so the retry lands on it."""

    class RefusingSession(_LeaseProbeSession):
        async def run_cell(
            self, code: str, *, timeout_seconds: float, deny_pixels: bool = False
        ) -> BrowserCodeCellResult:
            raise BrowserCodeSessionUnavailableError(
                "The code session's relay could not be reached." if session_ended else "The runner is full.",
                error_code="relay_unavailable" if session_ended else "busy",
                session_ended=session_ended,
                retry_after_seconds=None if session_ended else 5.0,
            )

    probe = RefusingSession()

    @asynccontextmanager
    async def lease(_ctx: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    async def current_page(session_id: str | None = None) -> tuple[SimpleNamespace, None]:
        return SimpleNamespace(page=probe.page), None

    async def open_session(**_kwargs: object) -> BrowserCodeSession:
        return probe

    async def prepared(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
        return None, None, None

    monkeypatch.setattr(browser_code_module, "mcp_browser_context", lease)
    monkeypatch.setattr(browser_code_module, "get_page", current_page)
    monkeypatch.setattr(browser_code_module, "_prepare_browser_session_for_dispatch", prepared)
    monkeypatch.setattr(app.AGENT_FUNCTION, "open_copilot_browser_code_session", open_session)
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_1"

    result = await browser_code_module.run_browser_code(ctx, "1")

    assert result["ok"] is False
    if session_ended:
        assert probe.closed
        assert ctx.browser_code_host.session is None
    else:
        assert not probe.closed
        assert ctx.browser_code_host.session is probe
        assert result["retry_after_seconds"] == 5.0


@pytest.mark.asyncio
async def test_a_session_that_ended_under_a_cell_still_tells_the_model_what_reached_the_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session can end under a cell and return a result with no operations — a fallback written over
    one that had landed. The session knows what last reached the browser; told only that the session
    ended, the model retries a click or a submission that already happened."""
    click = BrowserCodeOperation(operation="click", selector="#submit", succeeded=None)

    class EndedSession(_LeaseProbeSession):
        async def run_cell(
            self, code: str, *, timeout_seconds: float, deny_pixels: bool = False
        ) -> BrowserCodeCellResult:
            return BrowserCodeCellResult(
                ok=False,
                value=None,
                stdout=None,
                stdout_truncated=False,
                error_code="session_lost",
                error="The code session failed.",
                failing_line=None,
                operations=(),
                operations_omitted=0,
                session_alive=False,
                current_url=None,
            )

        def last_dispatched_operation(self) -> BrowserCodeOperation | None:
            return click

    probe = EndedSession()

    @asynccontextmanager
    async def lease(_ctx: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    async def current_page(session_id: str | None = None) -> tuple[SimpleNamespace, None]:
        return SimpleNamespace(page=probe.page), None

    async def open_session(**_kwargs: object) -> BrowserCodeSession:
        return probe

    async def prepared(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
        return None, None, None

    monkeypatch.setattr(browser_code_module, "mcp_browser_context", lease)
    monkeypatch.setattr(browser_code_module, "get_page", current_page)
    monkeypatch.setattr(browser_code_module, "_prepare_browser_session_for_dispatch", prepared)
    monkeypatch.setattr(app.AGENT_FUNCTION, "open_copilot_browser_code_session", open_session)
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_1"

    result = await browser_code_module.run_browser_code(ctx, "1")

    assert result["ok"] is False
    evidence = result["session_ended_during_call"]
    assert evidence["last_operation"]["operation"] == "click"
    assert evidence["page_state"].startswith("unknown")
    assert probe.closed and ctx.browser_code_host.session is None


@pytest.mark.asyncio
async def test_a_replaced_browser_session_reopens_the_interpreter_rather_than_rebinding_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker resolves tabs against the browser session it was started with.

    Rebinding only moves the interpreter to a tab, so a session pointed at a browser that has been
    replaced would keep resolving against the retired one. It has to be reopened instead.
    """
    rebinds: list[Page] = []
    opened: list[str | None] = []

    class _Session:
        def __init__(self) -> None:
            self.session_id = "bcs_probe"
            self.page = SimpleNamespace(url="https://example.com/")
            self.closed = False

        async def rebind(self, page: Page) -> None:
            rebinds.append(page)

        async def run_cell(
            self, code: str, *, timeout_seconds: float, deny_pixels: bool = False
        ) -> BrowserCodeCellResult:
            return BrowserCodeCellResult(
                ok=True,
                value=1,
                stdout=None,
                stdout_truncated=False,
                error_code=None,
                error=None,
                failing_line=None,
                operations=(),
                operations_omitted=0,
                session_alive=True,
                current_url="https://example.com/",
            )

        def last_dispatched_operation(self) -> BrowserCodeOperation | None:
            return None

        async def close(self) -> None:
            self.closed = True

    sessions: list[_Session] = []

    @asynccontextmanager
    async def lease(_ctx: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    async def current_page(session_id: str | None = None) -> tuple[SimpleNamespace, None]:
        return SimpleNamespace(
            page=sessions[-1].page if sessions else SimpleNamespace(url="https://example.com/")
        ), None

    async def open_session(
        *,
        page: Page,
        lifetime_seconds: float,
        organization_id: str,
        chat_id: str,
        turn_id: str,
        browser_session_id: str | None,
    ) -> BrowserCodeSession:
        opened.append(browser_session_id)
        session = _Session()
        session.page = page
        sessions.append(session)
        return session

    async def prepared(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
        return None, None, None

    monkeypatch.setattr(browser_code_module, "mcp_browser_context", lease)
    monkeypatch.setattr(browser_code_module, "get_page", current_page)
    monkeypatch.setattr(browser_code_module, "_prepare_browser_session_for_dispatch", prepared)
    monkeypatch.setattr(app.AGENT_FUNCTION, "open_copilot_browser_code_session", open_session)

    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_1"
    assert (await browser_code_module.run_browser_code(ctx, "1"))["ok"] is True

    # Continuity recovery hands the turn a different browser.
    ctx.browser_session_id = "pbs_2"
    second = await browser_code_module.run_browser_code(ctx, "2")

    assert second["ok"] is True
    assert opened == ["pbs_1", "pbs_2"], "the interpreter was not reopened against the new browser"
    assert rebinds == [], "a rebind cannot move the worker to a different browser"
    assert sessions[0].closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", ["#pay", None])
async def test_interaction_credit_follows_what_the_workbench_can_actually_promote(selector: str | None) -> None:
    """A handle-backed locator leaves no selector, so nothing is recorded for it.

    Crediting the evidence that follows to an interaction anyway offers workflow authoring an action
    there is no saved form of.
    """
    from skyvern.forge.sdk.copilot.tools import browser_code as browser_code_module

    ctx = make_copilot_context()
    cell = BrowserCodeCellResult(
        ok=True,
        value=None,
        stdout=None,
        stdout_truncated=False,
        error_code=None,
        error=None,
        failing_line=None,
        operations=(),
        operations_omitted=0,
        actions=(BrowserCodeOperation(operation="click", selector=selector, succeeded=True),),
        session_alive=True,
        current_url="https://example.com/checkout",
    )

    browser_code_module._record_outcome(ctx, cell, {"ok": True}, browser_session_id="pbs_1", generation=0)

    armed = ctx.pending_browser_interaction_observation is not None
    assert armed is (selector is not None), "interaction credit did not follow what was recorded"


@pytest.mark.asyncio
@pytest.mark.parametrize("tainted", [True, False])
async def test_a_page_used_for_a_sensitive_sign_in_denies_the_cell_its_pixels(
    monkeypatch: pytest.MonkeyPatch, tainted: bool
) -> None:
    """Facts stop being withheld once the secret registry is complete, but the page stays tainted.

    Result scrubbing compares values and an image is not a value, so a cell that can photograph the
    page can carry it out a chunk at a time. The refusal is decided here, where the taint is known.
    """
    denials: list[bool] = []
    probe = _LeaseProbeSession(denials_sink=denials)

    @asynccontextmanager
    async def lease(_ctx: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    async def current_page(session_id: str | None = None) -> tuple[SimpleNamespace, None]:
        return SimpleNamespace(page=probe.page), None

    async def open_session(**_kwargs: object) -> BrowserCodeSession:
        return probe

    async def prepared(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
        return None, None, None

    monkeypatch.setattr(browser_code_module, "mcp_browser_context", lease)
    monkeypatch.setattr(browser_code_module, "get_page", current_page)
    monkeypatch.setattr(browser_code_module, "_prepare_browser_session_for_dispatch", prepared)
    monkeypatch.setattr(app.AGENT_FUNCTION, "open_copilot_browser_code_session", open_session)
    # The state the gap lives in: the run has finished and its values are bound, so facts are no longer
    # withheld. Stubbed because reaching it otherwise means rebuilding the whole secret registry; the
    # taint below is the real thing, and it is what this test is about.
    monkeypatch.setattr(browser_code_module, "sensitive_origin_page_facts_withheld", lambda *_a, **_k: False)

    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_1"
    # The real taint, not a stand-in for it: this is what a sensitive-origin run leaves behind.
    ctx.sensitive_origin_browser_session_ids = {"pbs_1"} if tainted else set()

    result = await browser_code_module.run_browser_code(ctx, "1")

    assert result["ok"] is True
    assert denials == [tainted]


@pytest.mark.asyncio
@pytest.mark.parametrize(("result_url", "lifted"), [("https://x.test/", True), ("https://portal.test/a#top", False)])
async def test_a_readable_tainted_page_regains_pixels_only_after_a_recorded_navigation_off_it(
    monkeypatch: pytest.MonkeyPatch, result_url: str, lifted: bool
) -> None:
    """With the navigate tool withdrawn, a cell's own navigation is the only way off a tainted page
    whose facts are readable again; a fragment hop leaves the sensitive document up."""
    from skyvern.forge.sdk.copilot.runtime import sensitive_origin_page_is_tainted

    denials: list[bool] = []

    class _NavigatingSession(_LeaseProbeSession):
        async def run_cell(
            self, code: str, *, timeout_seconds: float, deny_pixels: bool = False
        ) -> BrowserCodeCellResult:
            cell = await super().run_cell(code, timeout_seconds=timeout_seconds, deny_pixels=deny_pixels)
            op = BrowserCodeOperation(
                operation="goto",
                selector=None,
                succeeded=True,
                source_url="https://portal.test/a",
                result_url=result_url,
            )
            return replace(cell, operations=(op,), current_url=result_url)

    probe = _NavigatingSession(denials_sink=denials)

    @asynccontextmanager
    async def lease(_ctx: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    async def current_page(session_id: str | None = None) -> tuple[SimpleNamespace, None]:
        return SimpleNamespace(page=probe.page), None

    async def open_session(**_kwargs: object) -> BrowserCodeSession:
        return probe

    async def prepared(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
        return None, None, None

    monkeypatch.setattr(browser_code_module, "mcp_browser_context", lease)
    monkeypatch.setattr(browser_code_module, "get_page", current_page)
    monkeypatch.setattr(browser_code_module, "_prepare_browser_session_for_dispatch", prepared)
    monkeypatch.setattr(app.AGENT_FUNCTION, "open_copilot_browser_code_session", open_session)
    monkeypatch.setattr(browser_code_module, "sensitive_origin_page_facts_withheld", lambda *_a, **_k: False)
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_1"
    ctx.sensitive_origin_browser_session_ids = {"pbs_1"}

    await browser_code_module.run_browser_code(ctx, f'await page.goto("{result_url}")')
    await browser_code_module.run_browser_code(ctx, "1")

    assert denials == [True, not lifted]
    assert sensitive_origin_page_is_tainted(ctx) is not lifted


@pytest.mark.asyncio
@pytest.mark.parametrize("restarted", [True, False])
async def test_an_interpreter_restarted_on_the_worker_is_reported_to_the_model(
    monkeypatch: pytest.MonkeyPatch, restarted: bool
) -> None:
    """This side's handle is unchanged when the far side reopens, so silence reads as continuity.

    The promise is that values survive between calls, and a call that cannot keep it has to say so
    rather than let the model discover it by a NameError.
    """

    class RestartingSession(_LeaseProbeSession):
        async def run_cell(self, code: str, *, timeout_seconds: float, deny_pixels: bool = False) -> Any:
            cell = await super().run_cell(code, timeout_seconds=timeout_seconds, deny_pixels=deny_pixels)
            return replace(cell, interpreter_restarted=restarted)

    probe = RestartingSession()

    @asynccontextmanager
    async def lease(_ctx: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    async def current_page(session_id: str | None = None) -> tuple[SimpleNamespace, None]:
        return SimpleNamespace(page=probe.page), None

    async def open_session(**_kwargs: object) -> BrowserCodeSession:
        return probe

    async def prepared(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
        return None, None, None

    monkeypatch.setattr(browser_code_module, "mcp_browser_context", lease)
    monkeypatch.setattr(browser_code_module, "get_page", current_page)
    monkeypatch.setattr(browser_code_module, "_prepare_browser_session_for_dispatch", prepared)
    monkeypatch.setattr(app.AGENT_FUNCTION, "open_copilot_browser_code_session", open_session)

    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_1"

    result = await browser_code_module.run_browser_code(ctx, "1")

    assert result["ok"] is True
    assert ("session_restarted" in result) is restarted, "the model was not told its earlier values are gone"


@pytest.mark.asyncio
async def test_a_cell_targeted_at_the_last_run_acts_in_that_runs_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test run that minted its own browser leaves the page the model just observed there; the
    replace surface has no other action route to it, so the cell must follow the same target the
    read tools honour rather than the chat's browser."""
    probe = _LeaseProbeSession([], retire_mid_cell=False)
    looked_up: list[str | None] = []
    opened_for: list[str | None] = []
    leased: list[object] = []

    @asynccontextmanager
    async def lease(_ctx: object, **kwargs: object) -> AsyncIterator[None]:
        leased.append(kwargs.get("session_id_override"))
        yield

    async def current_page(session_id: str | None = None) -> tuple[SimpleNamespace, None]:
        looked_up.append(session_id)
        return SimpleNamespace(page=probe.page), None

    async def open_session(*, browser_session_id: str | None, **_kwargs: object) -> BrowserCodeSession:
        opened_for.append(browser_session_id)
        return probe

    async def prepared(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
        return None, None, None

    monkeypatch.setattr(browser_code_module, "mcp_browser_context", lease)
    monkeypatch.setattr(browser_code_module, "get_page", current_page)
    monkeypatch.setattr(browser_code_module, "_prepare_browser_session_for_dispatch", prepared)
    monkeypatch.setattr(app.AGENT_FUNCTION, "open_copilot_browser_code_session", open_session)
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_chat"

    unavailable = await browser_code_module.run_browser_code(ctx, "1", target="last_run")

    ctx.last_run_blocks_workflow_run_id = "wr_1"
    ctx.last_run_blocks_browser_session_id = "pbs_run"
    on_run = await browser_code_module.run_browser_code(ctx, "1", target="last_run")
    on_chat = await browser_code_module.run_browser_code(ctx, "1")

    assert unavailable["ok"] is False
    assert "No test run has recorded a browser session" in unavailable["error"]
    assert on_run["ok"] is True and on_run["browser_target"] == "last_run"
    assert on_chat["ok"] is True and on_chat["browser_target"] == "debug"
    assert looked_up == ["pbs_run", "pbs_chat"]
    assert opened_for == ["pbs_run", "pbs_chat"]
    # The lease admits the same browser the cell drives; a ContextVar alone does not reach it.
    assert leased == ["pbs_run", None]


@pytest.mark.asyncio
async def test_a_withheld_page_lifts_only_on_a_recorded_navigation_to_another_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The replace surface withdraws every other route off a tainted page, so the code tool offers the
    recovery the error names, keyed on the worker's record of the navigation rather than on the cell's
    text: a rebound `page.goto` runs no operation, and a fragment hop leaves the sensitive document up."""
    from skyvern.forge.sdk.copilot.runtime import (
        record_sensitive_origin_run_taint,
        register_sensitive_origin_run_lease,
        release_sensitive_origin_run_lease,
        sensitive_origin_page_facts_withheld,
    )

    tainted_url = "https://portal.test/account?tab=billing"
    scripted: list[tuple[BrowserCodeOperation, ...]] = []
    denials: list[bool] = []
    sessions_discarded: list[bool] = []

    def goto(result_url: str) -> BrowserCodeOperation:
        return BrowserCodeOperation(
            operation="goto", selector=None, succeeded=True, source_url=tainted_url, result_url=result_url
        )

    class _ScriptedSession(_LeaseProbeSession):
        async def run_cell(
            self, code: str, *, timeout_seconds: float, deny_pixels: bool = False
        ) -> BrowserCodeCellResult:
            denials.append(deny_pixels)
            operations = scripted.pop(0)
            current_url = operations[-1].result_url if operations else tainted_url
            return BrowserCodeCellResult(
                ok=True,
                value="<response>",
                stdout="leaked?",
                stdout_truncated=False,
                error_code=None,
                error=None,
                failing_line=None,
                operations=operations,
                operations_omitted=0,
                session_alive=True,
                current_url=current_url,
            )

    probe = _ScriptedSession()

    @asynccontextmanager
    async def lease(_ctx: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    async def current_page(session_id: str | None = None) -> tuple[SimpleNamespace, None]:
        return SimpleNamespace(page=probe.page), None

    opened: list[bool] = []

    async def open_session(**_kwargs: object) -> BrowserCodeSession:
        opened.append(True)
        return probe

    async def prepared(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
        return None, None, None

    monkeypatch.setattr(browser_code_module, "mcp_browser_context", lease)
    monkeypatch.setattr(browser_code_module, "get_page", current_page)
    monkeypatch.setattr(browser_code_module, "_prepare_browser_session_for_dispatch", prepared)
    monkeypatch.setattr(app.AGENT_FUNCTION, "open_copilot_browser_code_session", open_session)
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_1"
    # An interpreter from before the page turned sensitive, whose namespace a recovery must not inherit.
    ctx.browser_code_host.session = probe
    ctx.browser_code_host.browser_session_id = "pbs_1"
    record_sensitive_origin_run_taint(ctx, workflow_run_id="wr_secret", session_id="pbs_1")
    assert sensitive_origin_page_facts_withheld(ctx, None)
    bare = 'await page.goto("https://x.test/")'

    mixed = await browser_code_module.run_browser_code(ctx, f"print(await page.title())\n{bare}")
    register_sensitive_origin_run_lease(ctx, workflow_run_id="wr_secret", session_id="pbs_1")
    while_active = await browser_code_module.run_browser_code(ctx, bare)
    release_sensitive_origin_run_lease(ctx, workflow_run_id="wr_secret")
    scripted.append(())
    no_operation = await browser_code_module.run_browser_code(ctx, bare)
    still_withheld_after_no_operation = sensitive_origin_page_facts_withheld(ctx, None)
    sessions_discarded.append(ctx.browser_code_host.session is None)
    scripted.append((goto(tainted_url + "#top"),))
    fragment = await browser_code_module.run_browser_code(ctx, bare)
    still_withheld_after_fragment = sensitive_origin_page_facts_withheld(ctx, None)
    sessions_discarded.append(ctx.browser_code_host.session is None)
    scripted.append((goto("https://x.test/"),))
    recovered = await browser_code_module.run_browser_code(ctx, bare)

    assert mixed["ok"] is False and 'await page.goto("<url>")' in mixed["error"]
    assert while_active["ok"] is False and "while a run with sensitive inputs is active" in while_active["error"]
    assert denials == [True, True, True], "every recovery attempt runs with pixels denied"
    for refused in (no_operation, fragment):
        assert refused["ok"] is False
        assert "current_url" not in refused and "value" not in refused and "stdout" not in refused
        assert tainted_url not in json.dumps(refused)
        assert "session_ended" in refused
    assert "session_ended" in recovered
    # Every recovery ran in a fresh interpreter, never the one that predates the taint.
    assert len(opened) == 3 and sessions_discarded == [False, False]
    assert still_withheld_after_no_operation and still_withheld_after_fragment
    assert recovered["ok"] is True and recovered["current_url"] == "https://x.test/"
    assert "value" not in recovered and "stdout" not in recovered
    assert not sensitive_origin_page_facts_withheld(ctx, None)


@pytest.mark.asyncio
async def test_a_debug_reestablishment_does_not_rebind_a_cell_pinned_to_the_last_run_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the chat's own browser moves the continuity generation, so a last-run cell keeps its
    interpreter and the handles it saved across cells; a debug cell still rebinds on the same bump."""
    rebinds: list[str] = []

    class _RebindCounting(_LeaseProbeSession):
        async def rebind(self, page: Page) -> None:
            rebinds.append(ctx.browser_code_host.browser_session_id or "")

    probe = _RebindCounting()

    @asynccontextmanager
    async def lease(_ctx: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    async def current_page(session_id: str | None = None) -> tuple[SimpleNamespace, None]:
        return SimpleNamespace(page=probe.page), None

    async def open_session(**_kwargs: object) -> BrowserCodeSession:
        return probe

    async def prepared(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
        return None, None, None

    monkeypatch.setattr(browser_code_module, "mcp_browser_context", lease)
    monkeypatch.setattr(browser_code_module, "get_page", current_page)
    monkeypatch.setattr(browser_code_module, "_prepare_browser_session_for_dispatch", prepared)
    monkeypatch.setattr(app.AGENT_FUNCTION, "open_copilot_browser_code_session", open_session)
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_chat"
    ctx.last_run_blocks_workflow_run_id = "wr_1"
    ctx.last_run_blocks_browser_session_id = "pbs_run"

    await browser_code_module.run_browser_code(ctx, "1", target="last_run")
    ctx.browser_session_continuity_generation += 1
    pinned = await browser_code_module.run_browser_code(ctx, "1", target="last_run")

    assert pinned["ok"] is True and "handles_invalidated" not in pinned
    assert rebinds == []

    await browser_code_module.run_browser_code(ctx, "1")
    ctx.browser_session_continuity_generation += 1
    debug = await browser_code_module.run_browser_code(ctx, "1")

    assert "handles_invalidated" in debug and rebinds == ["pbs_chat"]
