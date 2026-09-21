from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Page

from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.copilot import mcp_adapter
from skyvern.forge.sdk.copilot import runtime as runtime_module
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
from skyvern.forge.sdk.copilot.runtime import CopilotBrowserGenerationRetired, browser_session_recovery
from skyvern.forge.sdk.copilot.secret_scrub import clear_session_scrub_values, register_secret_scrub_value
from skyvern.forge.sdk.copilot.tools import (
    _build_skyvern_mcp_overlays,
)
from skyvern.forge.sdk.copilot.tools import browser_code as browser_code_module
from skyvern.forge.sdk.copilot.tools import (
    copilot_native_tools,
    edit_block_and_run_tool,
    get_skyvern_mcp_alias_map,
)
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
from skyvern.forge.sdk.copilot.tools import (
    update_and_run_blocks_tool,
)
from skyvern.forge.sdk.copilot.workflow_yaml import stored_block_code
from skyvern.webeye.browser_errors import BrowserCdpConnectionError
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


@pytest.mark.asyncio
async def test_a_debug_cell_that_cannot_reach_its_browser_states_the_last_run_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def prepared(*_args: object, **_kwargs: object) -> tuple[dict[str, Any], None, None]:
        return {"ok": False, "error": "The browser session could not be reached."}, None, None

    monkeypatch.setattr(browser_code_module, "_prepare_browser_session_for_dispatch", prepared)
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_debug"
    ctx.last_run_blocks_browser_session_id = "pbs_run"
    ctx.last_run_blocks_workflow_run_id = "wr_1"

    result = await browser_code_module.run_browser_code(ctx, "1")

    assert result["ok"] is False
    assert result["last_run_browser_session_id"] == "pbs_run"
    assert result["last_run_workflow_run_id"] == "wr_1"
    assert ctx.browser_session_id == "pbs_debug"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_session_id", "states_last_run"),
    [pytest.param("pbs_run", True, id="distinct-run-browser"), pytest.param("pbs_debug", False, id="same-browser")],
)
async def test_a_cell_whose_browser_cannot_be_entered_returns_the_acquisition_fact(
    monkeypatch: pytest.MonkeyPatch, run_session_id: str, states_last_run: bool
) -> None:
    async def prepared(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
        return None, None, None

    manager = MagicMock()
    manager.get_browser_state = AsyncMock(side_effect=BrowserCdpConnectionError("CDP connection failed"))
    monkeypatch.setattr(runtime_module, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=manager))
    monkeypatch.setattr(browser_code_module, "_prepare_browser_session_for_dispatch", prepared)
    ctx = make_copilot_context()
    ctx.api_key = "sk-test"
    ctx.browser_session_id = "pbs_debug"
    ctx.last_run_blocks_browser_session_id = run_session_id
    ctx.last_run_blocks_workflow_run_id = "wr_1"

    result = await browser_code_module.run_browser_code(ctx, "1")

    assert result["ok"] is False
    assert "CDP connection failed" in result["error"]
    assert result["browser_session_id"] == "pbs_debug"
    assert ("last_run_browser_session_id" in result) is states_last_run
    assert ctx.browser_session_id == "pbs_debug"


@pytest.mark.asyncio
async def test_an_unclassified_acquisition_failure_is_named_by_type_not_by_its_own_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A classified error's message has been through CDP-endpoint redaction; an unclassified one has
    not, so its text can carry an internal address and stays out of the model's result."""

    async def prepared(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
        return None, None, None

    manager = MagicMock()
    manager.get_browser_state = AsyncMock(side_effect=RuntimeError("connect ws://10.1.2.3:9222/devtools refused"))
    monkeypatch.setattr(runtime_module, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=manager))
    monkeypatch.setattr(browser_code_module, "_prepare_browser_session_for_dispatch", prepared)
    ctx = make_copilot_context()
    ctx.api_key = "sk-test"
    ctx.browser_session_id = "pbs_debug"

    result = await browser_code_module.run_browser_code(ctx, "1")

    assert result["ok"] is False
    assert "RuntimeError" in result["error"]
    assert "10.1.2.3" not in json.dumps(result)
    assert "devtools" not in json.dumps(result)


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


@pytest.mark.asyncio
async def test_browser_code_uses_the_existing_page_then_recovery_lock_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_held = asyncio.Event()
    browser_code_waiting_for_page = asyncio.Event()
    probe = _LeaseProbeSession([])
    real_page_custody_lock = browser_code_module.browser_page_custody_lock

    @asynccontextmanager
    async def observed_page_custody(ctx: Any) -> AsyncIterator[None]:
        browser_code_waiting_for_page.set()
        async with real_page_custody_lock(ctx):
            yield

    @asynccontextmanager
    async def lease(_ctx: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    async def current_page(session_id: str | None = None) -> tuple[SimpleNamespace, None]:
        return SimpleNamespace(page=probe.page), None

    async def open_session(**_kwargs: object) -> BrowserCodeSession:
        return probe

    async def prepared(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
        return None, None, None

    async def page_then_recovery() -> None:
        async with real_page_custody_lock(ctx):
            page_held.set()
            await browser_code_waiting_for_page.wait()
            async with browser_session_recovery(ctx):
                pass

    monkeypatch.setattr(browser_code_module, "browser_page_custody_lock", observed_page_custody)
    monkeypatch.setattr(browser_code_module, "mcp_browser_context", lease)
    monkeypatch.setattr(browser_code_module, "get_page", current_page)
    monkeypatch.setattr(browser_code_module, "_prepare_browser_session_for_dispatch", prepared)
    monkeypatch.setattr(app.AGENT_FUNCTION, "open_copilot_browser_code_session", open_session)
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_original"
    competing_call = asyncio.create_task(page_then_recovery())
    await page_held.wait()

    try:
        result = await asyncio.wait_for(browser_code_module.run_browser_code(ctx, "1"), timeout=0.5)
    finally:
        if not competing_call.done():
            competing_call.cancel()
        await asyncio.gather(competing_call, return_exceptions=True)

    assert result["ok"] is True


def _cell(*, ok: bool) -> BrowserCodeCellResult:
    return BrowserCodeCellResult(
        ok=ok,
        value={"observed": True} if ok else None,
        stdout=None,
        stdout_truncated=False,
        error_code=None if ok else "user_code_error",
        error=None if ok else "NameError: stale_helper is not defined",
        failing_line=None if ok else 3,
        operations=(),
        operations_omitted=0,
        session_alive=True,
        current_url="https://fixture.test/",
    )


@pytest.mark.asyncio
async def test_fresh_namespace_rebinds_to_the_current_browser_before_resetting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_replacement"
    order: list[str] = []
    session = MagicMock(session_id="browser_code_session", page=object())

    async def reset_namespace() -> None:
        order.append("reset")

    async def session_for_page(*_args: object, **_kwargs: object) -> object:
        order.append("rebind")
        return session

    async def run_cell(*_args: object, **_kwargs: object) -> BrowserCodeCellResult:
        order.append("run")
        return _cell(ok=True)

    session.reset_namespace = AsyncMock(side_effect=reset_namespace)
    session.close = AsyncMock()
    ctx.browser_code_host.session = session

    @asynccontextmanager
    async def browser_scope(_ctx: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(browser_code_module, "_authority_tool_error", lambda *_args: None)
    monkeypatch.setattr(
        browser_code_module,
        "_prepare_browser_session_for_dispatch",
        AsyncMock(return_value=(None, None, None)),
    )
    monkeypatch.setattr(browser_code_module, "mcp_browser_context", browser_scope)
    monkeypatch.setattr(
        browser_code_module,
        "get_page",
        AsyncMock(return_value=(SimpleNamespace(page=object()), None)),
    )
    monkeypatch.setattr(browser_code_module, "_session_for_page", session_for_page)
    monkeypatch.setattr(browser_code_module, "_run_cell", run_cell)

    result = await browser_code_module.run_browser_code(ctx, "result = 1", fresh_namespace=True)

    assert result["ok"] is True
    assert order == ["rebind", "reset", "run"]


@pytest.mark.asyncio
async def test_fresh_namespace_does_not_reset_a_session_opened_for_a_replacement_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_replacement"
    old_session = MagicMock(session_id="old_browser_code_session")
    replacement_session = MagicMock(session_id="replacement_browser_code_session")
    replacement_session.reset_namespace = AsyncMock()
    ctx.browser_code_host.session = old_session

    @asynccontextmanager
    async def browser_scope(_ctx: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(browser_code_module, "_authority_tool_error", lambda *_args: None)
    monkeypatch.setattr(
        browser_code_module,
        "_prepare_browser_session_for_dispatch",
        AsyncMock(return_value=(None, None, None)),
    )
    monkeypatch.setattr(browser_code_module, "mcp_browser_context", browser_scope)
    monkeypatch.setattr(
        browser_code_module,
        "get_page",
        AsyncMock(return_value=(SimpleNamespace(page=object()), None)),
    )
    monkeypatch.setattr(
        browser_code_module,
        "_session_for_page",
        AsyncMock(return_value=replacement_session),
    )
    monkeypatch.setattr(browser_code_module, "_run_cell", AsyncMock(return_value=_cell(ok=True)))

    result = await browser_code_module.run_browser_code(ctx, "result = 1", fresh_namespace=True)

    assert result["ok"] is True
    replacement_session.reset_namespace.assert_not_awaited()


@pytest.mark.asyncio
async def test_sensitive_origin_refusal_does_not_claim_the_namespace_was_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_context()
    session = MagicMock(session_id="browser_code_session")
    session.reset_namespace = AsyncMock()
    ctx.browser_code_host.session = session
    monkeypatch.setattr(browser_code_module, "_authority_tool_error", lambda *_args: None)
    monkeypatch.setattr(
        browser_code_module,
        "_prepare_browser_session_for_dispatch",
        AsyncMock(return_value=(None, None, None)),
    )
    monkeypatch.setattr(browser_code_module, "sensitive_origin_page_facts_withheld", lambda *_args: True)

    result = await browser_code_module.run_browser_code(ctx, "result = 1", fresh_namespace=True)

    assert result["ok"] is False
    assert "fresh_namespace" not in result
    session.reset_namespace.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_fresh_namespace_discards_session_and_records_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_browser_code"
    reset_started = asyncio.Event()
    never_finishes = asyncio.Event()

    async def blocked_reset() -> None:
        reset_started.set()
        await never_finishes.wait()

    session = MagicMock(session_id="browser_code_session")
    session.reset_namespace = AsyncMock(side_effect=blocked_reset)
    session.close = AsyncMock()
    session.last_dispatched_operation.return_value = BrowserCodeOperation(
        operation="click",
        selector="#previous",
        succeeded=True,
    )
    ctx.browser_code_host.session = session
    monkeypatch.setattr(browser_code_module, "_authority_tool_error", lambda *_args: None)
    monkeypatch.setattr(
        browser_code_module,
        "_prepare_browser_session_for_dispatch",
        AsyncMock(return_value=(None, None, None)),
    )

    @asynccontextmanager
    async def browser_scope(_ctx: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(browser_code_module, "mcp_browser_context", browser_scope)
    monkeypatch.setattr(
        browser_code_module,
        "get_page",
        AsyncMock(return_value=(SimpleNamespace(page=object()), None)),
    )
    monkeypatch.setattr(browser_code_module, "_session_for_page", AsyncMock(return_value=session))

    reset = asyncio.create_task(browser_code_module.run_browser_code(ctx, "result = 1", fresh_namespace=True))
    await asyncio.wait_for(reset_started.wait(), timeout=1)
    reset.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reset

    assert ctx.browser_code_host.interrupted is True
    assert ctx.browser_code_host.interrupted_operation is None
    assert browser_code_module._take_interruption_note(ctx.browser_code_host) == {
        "previous_call_interrupted": {
            "state": "the previous run_browser_code call was cancelled and its interpreter stopped"
        }
    }
    session.last_dispatched_operation.assert_not_called()
    session.close.assert_awaited_once()
    assert ctx.browser_code_host.session is None


@pytest.mark.asyncio
async def test_failed_fresh_namespace_reset_discards_the_closed_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_reset_failure"
    session = MagicMock(session_id="browser_code_session")
    session.reset_namespace = AsyncMock(
        side_effect=BrowserCodeSessionUnavailableError(
            "The replacement runner refused the session.", error_code="runner_unavailable"
        )
    )
    session.close = AsyncMock()
    ctx.browser_code_host.session = session
    monkeypatch.setattr(browser_code_module, "_authority_tool_error", lambda *_args: None)
    monkeypatch.setattr(
        browser_code_module,
        "_prepare_browser_session_for_dispatch",
        AsyncMock(return_value=(None, None, None)),
    )

    @asynccontextmanager
    async def browser_scope(_ctx: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(browser_code_module, "mcp_browser_context", browser_scope)
    monkeypatch.setattr(
        browser_code_module,
        "get_page",
        AsyncMock(return_value=(SimpleNamespace(page=object()), None)),
    )
    monkeypatch.setattr(browser_code_module, "_session_for_page", AsyncMock(return_value=session))

    result = await browser_code_module.run_browser_code(ctx, "result = 1", fresh_namespace=True)

    assert result["error_code"] == "runner_unavailable"
    assert "refused" in result["error"]
    session.close.assert_awaited_once()
    assert ctx.browser_code_host.session is None


@pytest.mark.asyncio
async def test_build_test_browser_acquisition_waits_for_source_promotion_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_original"
    verification_started = asyncio.Event()

    async def replace_browser(_ctx: object, **_kwargs: object) -> None:
        verification_started.set()
        ctx.browser_session_id = "pbs_replacement"

    monkeypatch.setattr(run_execution_module, "verify_build_test_browser_session_by_attaching", replace_browser)

    async with ctx.browser_session_recovery_lock:
        acquisition = asyncio.create_task(run_execution_module.acquire_build_test_browser_session(ctx, fresh=False))
        await asyncio.sleep(0)
        assert verification_started.is_set() is False
        assert acquisition.done() is False
        assert ctx.browser_session_id == "pbs_original"

    assert await acquisition is None
    assert verification_started.is_set() is True
    assert ctx.browser_session_id == "pbs_replacement"


@pytest.mark.asyncio
async def test_unavailable_workbench_does_not_remove_anchored_edit_and_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_yaml = """workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: repair_me
      code: |
        return {"value": "before"}
"""
    ctx = make_copilot_context(workflow_yaml)
    captured: dict[str, str] = {}

    async def persist(payload: dict[str, str], _context: object, **_kwargs: object) -> dict[str, object]:
        captured["workflow_yaml"] = payload["workflow_yaml"]
        return {"ok": True, "data": {"block_count": 1}}

    assert (
        await AgentFunction().copilot_browser_code_mode(
            organization_id=ctx.organization_id,
            workflow_permanent_id=ctx.workflow_permanent_id,
        )
        is CopilotBrowserCodeMode.OFF
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.await_pending_credential_pause", AsyncMock())
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._authority_tool_error", lambda *_args: None)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._update_and_run_requires_skipped_run", lambda *_args: False)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools._clear_pending_browser_interaction_observation", lambda *_args: None
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._get_prior_workflow_definition", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._update_workflow", persist)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._record_workflow_update_result", lambda *_args: None)
    run_updated = AsyncMock(return_value=json.dumps({"ok": True, "data": {"workflow_run_id": "wr_anchored"}}))
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._run_updated_workflow_blocks", run_updated)

    result = await edit_block_and_run_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="edit_block_and_run"),
        json.dumps(
            {
                "label": "repair_me",
                "expected_code": '"before"',
                "replacement_code": '"after"',
                "block_labels": ["repair_me"],
            }
        ),
    )

    assert json.loads(result)["ok"] is True
    assert stored_block_code(captured["workflow_yaml"], "repair_me") == 'return {"value": "after"}\n'
    run_updated.assert_awaited_once()


@pytest.mark.asyncio
async def test_source_provenance_is_pinned_to_the_browser_leased_for_the_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leased_session_ids: list[str | None] = []
    retained_session_ids: list[str | None] = []
    replacement_tasks: list[asyncio.Task[None]] = []
    probe = _LeaseProbeSession([])

    @asynccontextmanager
    async def lease(ctx: Any, **_kwargs: object) -> AsyncIterator[None]:
        async def replace_browser() -> None:
            async with browser_session_recovery(ctx):
                ctx.browser_session_id = "pbs_replacement"
                ctx.browser_session_continuity_generation += 1

        replacement_tasks.append(asyncio.create_task(replace_browser()))
        await asyncio.sleep(0)
        yield

    async def current_page(session_id: str | None = None) -> tuple[SimpleNamespace, None]:
        leased_session_ids.append(session_id)
        return SimpleNamespace(page=probe.page), None

    async def open_session(**_kwargs: object) -> BrowserCodeSession:
        return probe

    async def prepared(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
        return None, None, None

    def retain_source(
        _ctx: object,
        _code: str,
        _cell: BrowserCodeCellResult,
        *,
        browser_session_id: str | None,
        browser_session_generation: int,
        last_run_workflow_run_id: str | None = None,
    ) -> str:
        retained_session_ids.append(browser_session_id)
        return f"browser-code-source:{browser_session_generation}"

    monkeypatch.setattr(browser_code_module, "mcp_browser_context", lease)
    monkeypatch.setattr(browser_code_module, "get_page", current_page)
    monkeypatch.setattr(browser_code_module, "_prepare_browser_session_for_dispatch", prepared)
    monkeypatch.setattr(app.AGENT_FUNCTION, "open_copilot_browser_code_session", open_session)
    monkeypatch.setattr(browser_code_module, "retain_executed_browser_code_source", retain_source)
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_original"

    result = await browser_code_module.run_browser_code(ctx, "1")
    await asyncio.gather(*replacement_tasks)

    assert result["ok"] is True
    assert leased_session_ids == retained_session_ids == ["pbs_original"]


def test_executed_source_references_are_exact_outcome_bound_and_context_scoped() -> None:
    ctx = make_copilot_context()
    successful_source = 'value = "café"\n\nvalue'
    failed_source = "stale_helper()\n"

    successful_reference = browser_code_module.retain_executed_browser_code_source(
        ctx,
        successful_source,
        _cell(ok=True),
        browser_session_id=ctx.browser_session_id,
        browser_session_generation=4,
    )
    failed_reference = browser_code_module.retain_executed_browser_code_source(
        ctx,
        failed_source,
        _cell(ok=False),
        browser_session_id=ctx.browser_session_id,
        browser_session_generation=4,
    )
    ctx.browser_session_continuity_generation = 4

    successful = browser_code_module.resolve_executed_browser_code_source(ctx, successful_reference)
    failed = browser_code_module.resolve_executed_browser_code_source(ctx, failed_reference)
    assert successful.status == "valid"
    assert successful.source == successful_source
    assert successful.execution_ok is True
    assert failed.status == "valid"
    assert failed.source == failed_source
    assert failed.execution_ok is False
    assert failed.execution_error_code == "user_code_error"

    another_turn = make_copilot_context()
    assert browser_code_module.resolve_executed_browser_code_source(another_turn, successful_reference).status == (
        "wrong_turn"
    )
    another_owner = make_copilot_context()
    another_owner.organization_id = "other-owner"
    assert browser_code_module.resolve_executed_browser_code_source(another_owner, successful_reference).status == (
        "wrong_owner"
    )

    ctx.browser_session_continuity_generation = 5
    assert browser_code_module.resolve_executed_browser_code_source(ctx, successful_reference).status == (
        "wrong_generation"
    )
    ctx.browser_session_continuity_generation = 4
    missing = successful_reference.rsplit(":", 1)[0] + ":missing"
    assert browser_code_module.resolve_executed_browser_code_source(ctx, missing).status == "missing"

    browser_code_module.expire_executed_browser_code_sources(ctx.browser_code_host)
    expired = browser_code_module.resolve_executed_browser_code_source(ctx, successful_reference)
    assert expired.status == "expired"
    assert expired.source is None


def test_executed_source_reference_expires_when_browser_session_is_replaced_without_generation_change() -> None:
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_original"
    reference = browser_code_module.retain_executed_browser_code_source(
        ctx,
        "result = {'ok': True}",
        _cell(ok=True),
        browser_session_id="pbs_original",
        browser_session_generation=0,
    )

    ctx.browser_session_id = "pbs_replacement"

    resolution = browser_code_module.resolve_executed_browser_code_source(ctx, reference)
    assert resolution.status == "wrong_session"


@pytest.mark.asyncio
async def test_run_browser_code_preserves_opaque_source_reference_during_secret_scrub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_browser_code"
    register_secret_scrub_value(ctx, "code")

    @asynccontextmanager
    async def browser_scope(_ctx: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(browser_code_module, "_authority_tool_error", lambda *_args: None)
    monkeypatch.setattr(
        browser_code_module,
        "_prepare_browser_session_for_dispatch",
        AsyncMock(return_value=(None, None, None)),
    )
    monkeypatch.setattr(browser_code_module, "mcp_browser_context", browser_scope)
    monkeypatch.setattr(
        browser_code_module,
        "get_page",
        AsyncMock(return_value=(SimpleNamespace(page=object()), None)),
    )
    monkeypatch.setattr(browser_code_module, "_session_for_page", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        browser_code_module,
        "_run_cell",
        AsyncMock(return_value=replace(_cell(ok=True), value={"secret": "code"})),
    )
    monkeypatch.setattr(browser_code_module, "_record_outcome", lambda *_args, **_kwargs: None)

    try:
        result = await browser_code_module.run_browser_code(ctx, "result = 1\nresult")

        reference = result["executed_source_reference"]
        assert isinstance(reference, str)
        assert reference in ctx.browser_code_host.executed_sources
        assert browser_code_module.resolve_executed_browser_code_source(ctx, reference).status == "valid"
        assert result["value"]["secret"] != "code"
    finally:
        clear_session_scrub_values(ctx.browser_session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_code", ["stale_helper()\n", ""])
async def test_edit_block_and_run_promotes_the_complete_executed_source_without_retyping(
    monkeypatch: pytest.MonkeyPatch,
    initial_code: str,
) -> None:
    code_yaml = f"      code: |\n        {initial_code.strip()}\n" if initial_code else '      code: ""\n'
    workflow_yaml = (
        """workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: repair_me
"""
        + code_yaml
    )
    candidate = 'async def renamed_helper():\n    return "café"\n\nresult = {"value": await renamed_helper()}\nresult'
    ctx = make_copilot_context(workflow_yaml)
    reference = browser_code_module.retain_executed_browser_code_source(
        ctx,
        candidate,
        _cell(ok=True),
        browser_session_id=ctx.browser_session_id,
        browser_session_generation=0,
    )
    captured: dict[str, Any] = {}

    async def persist(payload: dict[str, Any], _context: object, **_kwargs: object) -> dict[str, object]:
        captured["workflow_yaml"] = payload["workflow_yaml"]
        captured["expected_exact_code_by_label"] = payload["_expected_exact_code_by_label"]
        return {"ok": True, "data": {"block_count": 1}}

    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.await_pending_credential_pause", AsyncMock())
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._authority_tool_error", lambda *_args: None)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._update_and_run_requires_skipped_run", lambda *_args: False)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools._clear_pending_browser_interaction_observation", lambda *_args: None
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._get_prior_workflow_definition", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._update_workflow", persist)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._record_workflow_update_result", lambda *_args: None)
    run_updated = AsyncMock(return_value=json.dumps({"ok": True, "data": {"workflow_run_id": "wr_saved"}}))
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._run_updated_workflow_blocks", run_updated)

    result = await edit_block_and_run_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="edit_block_and_run"),
        json.dumps(
            {
                "label": "repair_me",
                "executed_source_reference": reference,
                "block_labels": ["repair_me"],
                "parameters": {"ordinary_input": "kept for the saved run"},
            }
        ),
    )

    assert json.loads(result)["ok"] is True
    assert stored_block_code(captured["workflow_yaml"], "repair_me") == candidate
    assert captured["expected_exact_code_by_label"] == {"repair_me": candidate}
    assert run_updated.await_args.kwargs["parameters"] == {"ordinary_input": "kept for the saved run"}


@pytest.mark.asyncio
async def test_promotion_blocks_concurrent_browser_recovery_until_source_is_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_yaml = """workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: repair_me
      code: |
        stale_helper()
"""
    candidate = 'result = {"value": "repaired"}\nresult'
    ctx = make_copilot_context(workflow_yaml)
    ctx.browser_session_id = "pbs_original"
    reference = browser_code_module.retain_executed_browser_code_source(
        ctx,
        candidate,
        _cell(ok=True),
        browser_session_id=ctx.browser_session_id,
        browser_session_generation=0,
    )
    update_started = asyncio.Event()
    allow_update = asyncio.Event()
    persisted_generations: list[int] = []

    async def persist(_payload: dict[str, str], _context: object, **_kwargs: object) -> dict[str, object]:
        update_started.set()
        await allow_update.wait()
        persisted_generations.append(ctx.browser_session_continuity_generation)
        return {"ok": True, "data": {"block_count": 1}}

    outcome = mcp_adapter._BrowserSessionContinuityOutcome(
        lost_session_id="pbs_original",
        root_session_id="pbs_original",
        disposition="reestablished",
        replacement_session_id="pbs_replacement",
    )
    monkeypatch.setattr(mcp_adapter, "_get_continuity_outcome", AsyncMock(return_value=outcome))
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.await_pending_credential_pause", AsyncMock())
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._authority_tool_error", lambda *_args: None)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._update_and_run_requires_skipped_run", lambda *_args: False)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools._clear_pending_browser_interaction_observation", lambda *_args: None
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._get_prior_workflow_definition", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._update_workflow", persist)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._record_workflow_update_result", lambda *_args: None)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools._run_updated_workflow_blocks",
        AsyncMock(return_value=json.dumps({"ok": True, "data": {"workflow_run_id": "wr_saved"}})),
    )

    promotion = asyncio.create_task(
        edit_block_and_run_tool.on_invoke_tool(
            SimpleNamespace(context=ctx, tool_name="edit_block_and_run"),
            json.dumps(
                {
                    "label": "repair_me",
                    "executed_source_reference": reference,
                    "block_labels": ["repair_me"],
                }
            ),
        )
    )
    await asyncio.wait_for(update_started.wait(), timeout=1)
    recovery = asyncio.create_task(
        mcp_adapter._handle_browser_session_loss(
            ctx,
            tool_name="evaluate",
            call_path="model",
            lost_session_id="pbs_original",
        )
    )
    try:
        await asyncio.sleep(0)
        assert recovery.done() is False
        assert ctx.browser_session_continuity_generation == 0
    finally:
        allow_update.set()
        promotion_result = await promotion
        recovery_result = await recovery

    assert json.loads(promotion_result)["ok"] is True
    assert recovery_result == "reestablished"
    assert persisted_generations == [0]
    assert ctx.browser_session_continuity_generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "edit_source",
    [
        {},
        {"expected_code": "old"},
        {"replacement_code": "new"},
        {
            "expected_code": "old",
            "replacement_code": "new",
            "executed_source_reference": "browser_code_source:also-present",
        },
    ],
)
async def test_edit_block_and_run_requires_exactly_one_complete_edit_source(
    edit_source: dict[str, str],
) -> None:
    ctx = make_copilot_context("workflow_definition:\n  parameters: []\n  blocks: []\n")
    ctx.completion_verification_result = {"success": True, "reason": "stale"}
    result = await edit_block_and_run_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="edit_block_and_run"),
        json.dumps({"label": "repair_me", "block_labels": ["repair_me"], **edit_source}),
    )

    parsed = json.loads(result)
    assert parsed["ok"] is False
    assert parsed["error_code"] == "invalid_edit_source"
    assert ctx.completion_verification_result is None


@pytest.mark.asyncio
async def test_invalid_executed_source_reference_cannot_persist_or_start_a_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_yaml = """workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: repair_me
      code: |
        stale_helper()
"""
    persist = AsyncMock()
    run_updated = AsyncMock()
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._update_workflow", persist)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._run_updated_workflow_blocks", run_updated)

    source_ctx = make_copilot_context(workflow_yaml)
    source_reference = browser_code_module.retain_executed_browser_code_source(
        source_ctx,
        "return {'ok': True}",
        _cell(ok=True),
        browser_session_id=source_ctx.browser_session_id,
        browser_session_generation=0,
    )
    another_turn = make_copilot_context(workflow_yaml)
    another_owner = make_copilot_context(workflow_yaml)
    another_owner.organization_id = "other-owner"
    wrong_generation = source_ctx
    wrong_generation.browser_session_continuity_generation = 1
    wrong_session = make_copilot_context(workflow_yaml)
    wrong_session.browser_session_id = "pbs_original"
    wrong_session_reference = browser_code_module.retain_executed_browser_code_source(
        wrong_session,
        "return {'ok': True}",
        _cell(ok=True),
        browser_session_id="pbs_original",
        browser_session_generation=0,
    )
    wrong_session.browser_session_id = "pbs_replacement"
    expired = make_copilot_context(workflow_yaml)
    expired_reference = browser_code_module.retain_executed_browser_code_source(
        expired,
        "return {'ok': True}",
        _cell(ok=True),
        browser_session_id=expired.browser_session_id,
        browser_session_generation=0,
    )
    browser_code_module.expire_executed_browser_code_sources(expired.browser_code_host)

    cases = [
        (make_copilot_context(workflow_yaml), "browser-code-source:not-present", "missing"),
        (another_turn, source_reference, "wrong_turn"),
        (another_owner, source_reference, "wrong_owner"),
        (wrong_generation, source_reference, "wrong_generation"),
        (wrong_session, wrong_session_reference, "wrong_session"),
        (expired, expired_reference, "expired"),
    ]
    for ctx, reference, expected_status in cases:
        # A credential skip left by an earlier call must not be reported as the reason this edit failed.
        ctx.last_run_skipped_unbound_credentials = True
        result = await edit_block_and_run_tool.on_invoke_tool(
            SimpleNamespace(context=ctx, tool_name="edit_block_and_run"),
            json.dumps(
                {
                    "label": "repair_me",
                    "executed_source_reference": reference,
                    "block_labels": ["repair_me"],
                }
            ),
        )

        parsed = json.loads(result)
        assert parsed["ok"] is False
        assert parsed["error_code"] == "invalid_executed_source_reference"
        assert parsed["reference_status"] == expected_status
        assert ctx.last_run_skipped_unbound_credentials is False
    persist.assert_not_awaited()
    run_updated.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_only_helpers_are_persisted_and_reported_by_the_saved_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_yaml = """workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: repair_me
      code: |
        stale_helper()
"""
    source = """async def candidate():
    await tabs()
    await switch_tab(0)
    await click_and_wait_for_popup("#open")
    await click_and_download("#download")
    await files.write("note.txt", "hello")
    await fill_credential("#password", "cred_1", "password")

await candidate()
"""
    persist = AsyncMock(return_value={"ok": True, "data": {"block_count": 1}})
    run_updated = AsyncMock(return_value=json.dumps({"ok": False, "error": "NameError: tabs is not defined"}))
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._update_and_run_requires_skipped_run", lambda *_args: False)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools._clear_pending_browser_interaction_observation", lambda *_args: None
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._get_prior_workflow_definition", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._update_workflow", persist)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._record_workflow_update_result", lambda *_args: None)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._run_updated_workflow_blocks", run_updated)
    ctx = make_copilot_context(workflow_yaml)
    reference = browser_code_module.retain_executed_browser_code_source(
        ctx,
        source,
        _cell(ok=True),
        browser_session_id=ctx.browser_session_id,
        browser_session_generation=0,
    )

    result = await edit_block_and_run_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="edit_block_and_run"),
        json.dumps(
            {
                "label": "repair_me",
                "executed_source_reference": reference,
                "block_labels": ["repair_me"],
            }
        ),
    )

    parsed = json.loads(result)
    assert parsed == {"ok": False, "error": "NameError: tabs is not defined"}
    persisted_payload = persist.await_args.args[0]
    assert stored_block_code(persisted_payload["workflow_yaml"], "repair_me") == source
    run_updated.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_is_valid", [True, False])
async def test_update_and_run_blocks_fills_a_new_block_from_the_executed_source(
    monkeypatch: pytest.MonkeyPatch,
    reference_is_valid: bool,
) -> None:
    submitted_yaml = """workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: extract_rows
      code: |
"""
    candidate = 'rows = {"WC-101": "Ironclad"}\nreturn rows'
    ctx = make_copilot_context("workflow_definition:\n  parameters: []\n  blocks: []\n")
    reference = browser_code_module.retain_executed_browser_code_source(
        ctx,
        candidate,
        _cell(ok=True),
        browser_session_id=ctx.browser_session_id,
        browser_session_generation=0,
    )
    persisted: list[dict[str, Any]] = []

    async def persist(payload: dict[str, Any], _context: object, **_kwargs: object) -> dict[str, object]:
        persisted.append(payload)
        return {"ok": True, "data": {"block_count": 1}}

    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.await_pending_credential_pause", AsyncMock())
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._authority_tool_error", lambda *_args: None)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._update_and_run_requires_skipped_run", lambda *_args: False)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools._clear_pending_browser_interaction_observation", lambda *_args: None
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._get_prior_workflow_definition", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._update_workflow", persist)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._record_workflow_update_result", lambda *_args: None)
    run_updated = AsyncMock(return_value=json.dumps({"ok": True, "data": {"workflow_run_id": "wr_saved"}}))
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._run_updated_workflow_blocks", run_updated)

    result = await update_and_run_blocks_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="update_and_run_blocks"),
        json.dumps(
            {
                "workflow_yaml": submitted_yaml,
                "block_labels": ["extract_rows"],
                "executed_source_references": {
                    "extract_rows": reference if reference_is_valid else "browser_code_source:not:a:real:one"
                },
            }
        ),
    )

    if reference_is_valid:
        assert json.loads(result)["ok"] is True
        # Exact bytes, from the empty block scalar a model writes when told to leave the code empty.
        assert stored_block_code(persisted[0]["workflow_yaml"], "extract_rows") == candidate
        assert persisted[0]["_expected_exact_code_by_label"] == {"extract_rows": candidate}
        run_updated.assert_awaited_once()
    else:
        assert json.loads(result)["error_code"] == "invalid_executed_source_reference"
        assert persisted == []
        run_updated.assert_not_awaited()


def test_a_last_run_cell_reference_follows_that_run_not_the_chat_browser() -> None:
    ctx = make_copilot_context()
    ctx.browser_session_id = "pbs_debug"
    ctx.browser_session_continuity_generation = 2
    ctx.last_run_blocks_workflow_run_id = "wr_failed"
    ctx.last_run_blocks_browser_session_id = "pbs_run"
    reference = browser_code_module.retain_executed_browser_code_source(
        ctx,
        "return {'ok': True}",
        _cell(ok=True),
        browser_session_id="pbs_run",
        browser_session_generation=0,
        last_run_workflow_run_id="wr_failed",
    )

    assert browser_code_module.resolve_executed_browser_code_source(ctx, reference).status == "valid"

    ctx.last_run_blocks_workflow_run_id = "wr_newer"
    ctx.last_run_blocks_browser_session_id = "pbs_newer"
    assert browser_code_module.resolve_executed_browser_code_source(ctx, reference).status == "wrong_session"
