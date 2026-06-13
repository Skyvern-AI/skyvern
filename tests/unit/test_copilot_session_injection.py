"""Tests for copilot session injection and output contract adapters."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core import session_manager
from skyvern.cli.core.result import BrowserContext as MCPBrowserContext
from skyvern.cli.core.session_manager import SessionState, scoped_session
from skyvern.forge.sdk.copilot.runtime import AgentContext, mcp_to_copilot
from skyvern.forge.sdk.copilot.tools import _same_page_ignoring_fragment


@pytest.fixture(autouse=True)
def _reset_session_state() -> None:
    session_manager._current_session.set(None)
    session_manager._global_session = None
    session_manager.set_stateless_http_mode(False)


def _make_stream() -> MagicMock:
    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)
    return stream


def _make_ctx(**overrides: Any) -> AgentContext:
    defaults = dict(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="",
        browser_session_id="pbs_test_123",
        stream=_make_stream(),
        api_key="sk-test-key",
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


def test_copilot_same_page_ignoring_fragment_matches_trailing_slash_variants() -> None:
    assert _same_page_ignoring_fragment("https://example.test/results#section", "https://example.test/results") is True
    assert _same_page_ignoring_fragment("https://example.test/results/", "https://example.test/results") is True
    assert _same_page_ignoring_fragment("https://example.test/results?page=2", "https://example.test/results") is False


@pytest.mark.asyncio
async def test_scoped_session_pushes_and_restores() -> None:
    """scoped_session sets the ContextVar within scope and restores on exit."""
    # ContextVar starts as None (from fixture reset)
    assert session_manager._current_session.get() is None

    injected = SessionState(
        browser=MagicMock(),
        context=MCPBrowserContext(mode="cloud_session", session_id="pbs_injected"),
    )

    async with scoped_session(injected):
        inside = session_manager.get_current_session()
        assert inside is injected
        assert inside.context.session_id == "pbs_injected"

    after = session_manager._current_session.get()
    # Should be restored to None (the value before scoped_session set it)
    assert after is None


@pytest.mark.asyncio
async def test_scoped_session_does_not_touch_global() -> None:
    """scoped_session must NOT mutate _global_session."""
    session_manager._global_session = None

    injected = SessionState(
        browser=MagicMock(),
        context=MCPBrowserContext(mode="cloud_session", session_id="pbs_test"),
    )

    async with scoped_session(injected):
        pass

    assert session_manager._global_session is None


@pytest.mark.asyncio
async def test_scoped_session_concurrent_isolation() -> None:
    """Two concurrent scoped_session calls don't interfere with each other."""
    results: dict[str, str | None] = {}
    both_entered = asyncio.Event()
    entered_count = 0

    async def worker(session_id: str) -> None:
        nonlocal entered_count
        state = SessionState(
            browser=MagicMock(),
            context=MCPBrowserContext(mode="cloud_session", session_id=session_id),
        )
        async with scoped_session(state):
            entered_count += 1
            if entered_count == 2:
                both_entered.set()
            # Wait until BOTH workers are inside their scope before reading —
            # this guarantees ContextVar isolation is the only thing separating them.
            await both_entered.wait()
            current = session_manager.get_current_session()
            results[session_id] = current.context.session_id if current.context else None

    await asyncio.gather(worker("pbs_a"), worker("pbs_b"))

    assert results["pbs_a"] == "pbs_a"
    assert results["pbs_b"] == "pbs_b"
    assert session_manager._global_session is None


def test_mcp_to_copilot_success() -> None:
    mcp_result = {
        "ok": True,
        "action": "skyvern_navigate",
        "browser_context": {"mode": "cloud_session", "session_id": "pbs_1"},
        "data": {"url": "https://example.com", "title": "Example"},
        "timing_ms": {"total": 500},
        "artifacts": [],
    }
    result = mcp_to_copilot(mcp_result)
    assert result["ok"] is True
    assert result["data"]["url"] == "https://example.com"
    assert "action" not in result
    assert "browser_context" not in result
    assert "timing_ms" not in result
    assert "artifacts" not in result


def test_mcp_to_copilot_error() -> None:
    mcp_result = {
        "ok": False,
        "error": {"code": "NO_ACTIVE_BROWSER", "message": "No browser", "hint": "Create one"},
    }
    result = mcp_to_copilot(mcp_result)
    assert result["ok"] is False
    assert "No browser" in result["error"]
    assert "Create one" in result["error"]


class TestMcpBrowserContextBridge:
    """Bridge-specific behavior of mcp_browser_context."""

    def _install_happy_path_mocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock, list[Any]]:
        import skyvern.forge.sdk.copilot.runtime as runtime

        browser_state = MagicMock()
        browser_state.browser_context = MagicMock()
        manager = MagicMock()
        manager.get_browser_state = AsyncMock(return_value=browser_state)
        monkeypatch.setattr(runtime.app, "PERSISTENT_SESSIONS_MANAGER", manager)

        monkeypatch.setattr(runtime, "get_skyvern", lambda: MagicMock())
        monkeypatch.setattr(runtime, "SkyvernBrowser", lambda *a, **kw: MagicMock())
        monkeypatch.setattr(runtime, "get_active_api_key", lambda: "sk-test-key")
        monkeypatch.setattr(runtime, "hash_api_key_for_cache", lambda k: "hash_" + k)

        override_token = object()
        override_calls: list[Any] = []
        monkeypatch.setattr(
            runtime, "set_api_key_override", lambda k: (override_calls.append(("set", k)), override_token)[1]
        )
        monkeypatch.setattr(runtime, "reset_api_key_override", lambda t: override_calls.append(("reset", t)))

        register_mock = MagicMock()
        unregister_mock = MagicMock()
        monkeypatch.setattr(runtime, "register_copilot_session", register_mock)
        monkeypatch.setattr(runtime, "unregister_copilot_session", unregister_mock)

        return manager, register_mock, unregister_mock, browser_state, override_calls

    @pytest.mark.asyncio
    async def test_happy_path_registers_session_and_balances_unregister(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot.runtime import mcp_browser_context

        _, register_mock, unregister_mock, _, override_calls = self._install_happy_path_mocks(monkeypatch)

        ctx = _make_ctx()
        async with mcp_browser_context(ctx):
            # Inside the context, the bridge has registered a SessionState whose
            # session_id matches the agent context — this is the public contract
            # that resolve_browser(session_id=...) relies on.
            args = register_mock.call_args.args
            assert args[0] == ctx.browser_session_id
            registered_state = args[1]
            assert isinstance(registered_state, SessionState)
            assert registered_state.context.session_id == ctx.browser_session_id

        assert register_mock.call_count == 1
        assert unregister_mock.call_count == 1
        unregister_mock.assert_called_with(ctx.browser_session_id)
        # Override installed then reset.
        assert [c[0] for c in override_calls] == ["set", "reset"]

    @pytest.mark.asyncio
    async def test_missing_browser_context_raises_without_leaking_session_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import skyvern.forge.sdk.copilot.runtime as runtime
        from skyvern.forge.sdk.copilot.runtime import mcp_browser_context

        manager = MagicMock()
        manager.get_browser_state = AsyncMock(return_value=None)
        monkeypatch.setattr(runtime.app, "PERSISTENT_SESSIONS_MANAGER", manager)

        ctx = _make_ctx()
        with pytest.raises(RuntimeError, match="No browser context for copilot session") as exc_info:
            async with mcp_browser_context(ctx):
                pytest.fail("should not enter body")

        # Session id must not leak into the user/LLM-visible exception message.
        assert ctx.browser_session_id not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_exception_during_yield_still_tears_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot.runtime import mcp_browser_context

        _, register_mock, unregister_mock, _, override_calls = self._install_happy_path_mocks(monkeypatch)

        class Boom(RuntimeError):
            pass

        ctx = _make_ctx()
        with pytest.raises(Boom):
            async with mcp_browser_context(ctx):
                raise Boom("caller raised inside context")

        # Both teardown paths must fire even when the caller raises.
        assert register_mock.call_count == 1
        assert unregister_mock.call_count == 1
        assert [c[0] for c in override_calls] == ["set", "reset"]

    @pytest.mark.asyncio
    async def test_setup_phase_failure_still_resets_api_key_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If get_skyvern raises AFTER set_api_key_override, the override must
        still be reset so the request-scoped API key does not leak across
        requests."""
        import skyvern.forge.sdk.copilot.runtime as runtime
        from skyvern.forge.sdk.copilot.runtime import mcp_browser_context

        _, register_mock, unregister_mock, _, override_calls = self._install_happy_path_mocks(monkeypatch)

        def _raising_get_skyvern() -> Any:
            raise RuntimeError("skyvern client unavailable")

        monkeypatch.setattr(runtime, "get_skyvern", _raising_get_skyvern)

        ctx = _make_ctx()
        with pytest.raises(RuntimeError, match="skyvern client unavailable"):
            async with mcp_browser_context(ctx):
                pytest.fail("should not enter body")

        # Registration never happened because setup failed before register_copilot_session.
        assert register_mock.call_count == 0
        assert unregister_mock.call_count == 0
        # But the override must have been set AND reset.
        assert [c[0] for c in override_calls] == ["set", "reset"]


class TestScreenshotAdapter:
    @pytest.mark.asyncio
    async def test_screenshot_post_hook_reshapes_data_with_url_and_title(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _screenshot_post_hook

        ctx = _make_ctx()
        raw = {"browser_context": {"url": "https://example.com", "title": "Example"}}
        result = {
            "ok": True,
            "data": {"data": "iVBOR...", "mime": "image/png", "bytes": 1234},
        }

        adapted = await _screenshot_post_hook(result, raw, ctx)

        assert adapted["data"]["screenshot_base64"] == "iVBOR..."
        assert adapted["data"]["url"] == "https://example.com"
        assert adapted["data"]["title"] == "Example"
        assert ctx.composition_page_evidence["source_tool"] == "get_browser_screenshot"
        assert ctx.composition_page_evidence["current_url"] == "https://example.com"

    @pytest.mark.asyncio
    async def test_screenshot_post_hook_does_not_verify_from_url_title_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
        from skyvern.forge.sdk.copilot.tools import _screenshot_post_hook

        async def handler(**_: object) -> dict[str, object]:
            raise AssertionError("screenshot-only observation must not invoke completion verification")

        async def handler_lookup(_: object) -> object:
            return handler

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools.completion._completion_verification_handler", handler_lookup
        )
        ctx = _make_ctx(
            request_policy=RequestPolicy(
                completion_criteria=[CompletionCriterion(id="c0", outcome="the requested item is visible")]
            ),
            last_test_ok=False,
            last_run_blocks_workflow_run_id="wr_failed",
            copilot_run_start_monotonic=time.monotonic(),
        )
        raw = {"browser_context": {"url": "https://example.com/results", "title": "Results"}}
        result = {
            "ok": True,
            "data": {"data": "iVBOR...", "mime": "image/png", "bytes": 1234},
        }

        adapted = await _screenshot_post_hook(result, raw, ctx)

        assert adapted["data"]["url"] == "https://example.com/results"
        assert ctx.post_run_page_observation_after_failed_test is True
        assert ctx.completion_verification_result is None


class TestNavigateAdapter:
    @pytest.mark.asyncio
    async def test_navigate_post_hook_lifts_url_and_adds_next_step(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _navigate_post_hook

        ctx = _make_ctx()
        raw = {"browser_context": {"url": "https://example.com", "title": "Example"}}
        result = {"ok": True, "data": {"url": "https://example.com", "title": "Example"}}

        adapted = await _navigate_post_hook(result, raw, ctx)

        assert adapted["ok"] is True
        assert adapted["url"] == "https://example.com"
        assert "next_step" in adapted
        assert "data" not in adapted


class TestClickAdapter:
    @pytest.mark.asyncio
    async def test_click_post_hook_reshapes_data_with_url_and_title(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _click_post_hook

        ctx = _make_ctx()
        raw = {"browser_context": {"url": "https://ex.com", "title": "Page"}}
        result = {
            "ok": True,
            "data": {"selector": "#btn", "intent": None, "sdk_equivalent": "..."},
        }

        adapted = await _click_post_hook(result, raw, ctx)

        assert adapted["data"]["selector"] == "#btn"
        assert adapted["data"]["url"] == "https://ex.com"
        assert adapted["data"]["title"] == "Page"
        assert "sdk_equivalent" not in adapted["data"]


class TestTypeTextAdapter:
    @pytest.mark.asyncio
    async def test_type_text_post_hook_renames_text_length_to_typed_length(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _type_text_post_hook

        ctx = _make_ctx()
        raw = {"browser_context": {"url": "https://ex.com"}}
        result = {
            "ok": True,
            "data": {"selector": "#email", "text_length": 15, "sdk_equivalent": "..."},
        }

        adapted = await _type_text_post_hook(result, raw, ctx)

        assert adapted["data"]["selector"] == "#email"
        assert adapted["data"]["typed_length"] == 15
        assert adapted["data"]["url"] == "https://ex.com"
        assert "text_length" not in adapted["data"]


class TestEvaluateAdapter:
    def test_evaluate_copilot_contract(self) -> None:
        mcp_result = {
            "ok": True,
            "action": "skyvern_evaluate",
            "data": {
                "result": {"title": "Test"},
                "sdk_equivalent": "await page.evaluate(...)",
            },
            "browser_context": {"mode": "cloud_session"},
        }
        result = mcp_to_copilot(mcp_result)
        assert result["data"]["result"] == {"title": "Test"}

    @pytest.mark.asyncio
    async def test_evaluate_post_hook_marks_browser_observation_as_composition_evidence(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _evaluate_post_hook

        ctx = _make_ctx()
        raw = {"browser_context": {"url": "https://example.com/results", "title": "Results"}}
        result = {
            "ok": True,
            "data": {
                "rows": [{"name": "Test User", "credential": "RBT"}],
                "text": "Test User RBT",
            },
        }

        adapted = await _evaluate_post_hook(result, raw, ctx)

        assert adapted["data"]["url"] == "https://example.com/results"
        assert ctx.composition_page_evidence["source_tool"] == "evaluate"
        assert ctx.composition_page_evidence["current_url"] == "https://example.com/results"
        assert ctx.composition_page_evidence["result_containers"] == [
            {
                "tag": "table",
                "id": "",
                "selector": "",
                "row_count": 1,
                "sample_rows": ["Test User"],
            }
        ]

    @pytest.mark.asyncio
    async def test_evaluate_post_hook_lifts_bounded_page_schema_from_mcp_observation(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _evaluate_post_hook

        ctx = _make_ctx()
        raw = {"browser_context": {"url": "https://example.com/lookup", "title": "Lookup"}}
        result = {
            "ok": True,
            "data": {
                "forms": [
                    {
                        "id": "search",
                        "fields": [
                            {"label": "First Name", "name": "first_name", "type": "text"},
                            {"labels": ["Last", "Name"], "name": "last_name", "type": "text", "required": True},
                        ],
                        "submit_controls": [{"text": "Search", "selector": "#searchButton"}],
                    }
                ],
                "bodyText": "credential lookup search",
            },
        }

        await _evaluate_post_hook(result, raw, ctx)

        assert ctx.composition_page_evidence["source_tool"] == "evaluate"
        assert ctx.composition_page_evidence["evidence_sources"] == ["mcp_evaluate"]
        assert ctx.composition_page_evidence["forms"][0]["fields"][0]["label"] == "First Name"
        assert ctx.composition_page_evidence["forms"][0]["fields"][1]["label"] == "Last Name"
        assert ctx.composition_page_evidence["forms"][0]["fields"][1]["required"] is True
        assert ctx.composition_page_evidence["forms"][0]["submit_controls"][0]["selector"] == "#searchButton"

    @pytest.mark.asyncio
    async def test_evaluate_post_hook_does_not_overwrite_typed_composition_evidence(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _evaluate_post_hook

        ctx = _make_ctx()
        ctx.composition_page_evidence = {
            "inspected_url": "https://example.com/search",
            "current_url": "https://example.com/search",
            "source_tool": "inspect_page_for_composition",
            "forms": [{"fields": [{"label": "Name"}], "submit_controls": [{"text": "Search"}]}],
        }
        raw = {"browser_context": {"url": "https://example.com/results", "title": "Results"}}
        result = {"ok": True, "data": {"text": "Test User RBT"}}

        await _evaluate_post_hook(result, raw, ctx)

        assert ctx.composition_page_evidence["source_tool"] == "inspect_page_for_composition"
        assert ctx.composition_page_evidence["current_url"] == "https://example.com/search"


class TestUpdateWorkflowDirect:
    @pytest.mark.asyncio
    async def test_calls_internal_with_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """update_workflow uses direct path even when api_key is set."""
        from skyvern.forge.sdk.copilot.tools import _update_workflow

        ctx = _make_ctx(api_key="sk-test-key", workflow_permanent_id="wpid_abc123")

        mock_workflow = MagicMock()
        mock_workflow.title = "Test"
        mock_workflow.description = ""
        mock_workflow.workflow_definition = MagicMock()
        mock_workflow.workflow_definition.blocks = [MagicMock(), MagicMock()]

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools.workflow_update._process_workflow_yaml",
            lambda **kwargs: mock_workflow,
        )

        mock_wf_service = MagicMock()
        mock_wf_service.update_workflow_definition = AsyncMock()
        mock_wf_service.get_workflow = AsyncMock(return_value=None)
        monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.app.WORKFLOW_SERVICE", mock_wf_service)

        yaml_str = "title: Test\nworkflow_definition:\n  blocks: []"
        result = await _update_workflow({"workflow_yaml": yaml_str}, ctx)

        assert result["ok"] is True
        assert result["data"]["block_count"] == 2
        assert result["_workflow"] is mock_workflow
        assert ctx.workflow_yaml == yaml_str
        mock_wf_service.update_workflow_definition.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_calls_internal_without_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """update_workflow uses direct path when api_key is None."""
        from skyvern.forge.sdk.copilot.tools import _update_workflow

        ctx = _make_ctx(api_key=None)

        mock_workflow = MagicMock()
        mock_workflow.title = "Test"
        mock_workflow.description = ""
        mock_workflow.workflow_definition = MagicMock()
        mock_workflow.workflow_definition.blocks = []

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools.workflow_update._process_workflow_yaml",
            lambda **kwargs: mock_workflow,
        )

        mock_wf_service = MagicMock()
        mock_wf_service.update_workflow_definition = AsyncMock()
        mock_wf_service.get_workflow = AsyncMock(return_value=None)
        monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.app.WORKFLOW_SERVICE", mock_wf_service)

        result = await _update_workflow({"workflow_yaml": "title: Test"}, ctx)

        assert result["ok"] is True
        mock_wf_service.update_workflow_definition.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_yaml_parse_error_returns_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """YAML parse errors return ok: false."""
        import yaml as _yaml

        from skyvern.forge.sdk.copilot.tools import _update_workflow

        ctx = _make_ctx(api_key="sk-test-key")

        def raise_yaml_error(**kwargs: Any) -> None:
            raise _yaml.YAMLError("bad yaml")

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools.workflow_update._process_workflow_yaml",
            raise_yaml_error,
        )

        result = await _update_workflow({"workflow_yaml": "bad: yaml: {"}, ctx)

        assert result["ok"] is False
        assert "Workflow validation failed" in result["error"]

    @pytest.mark.asyncio
    async def test_ctx_workflow_yaml_not_updated_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ctx.workflow_yaml should NOT be updated when processing fails."""
        from pydantic import ValidationError as _ValidationError

        from skyvern.forge.sdk.copilot.tools import _update_workflow

        ctx = _make_ctx(
            api_key="sk-test-key",
            workflow_yaml="original yaml",
        )

        def raise_validation_error(**kwargs: Any) -> None:
            raise _ValidationError.from_exception_data(
                title="WorkflowCreateYAMLRequest",
                line_errors=[],
            )

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools.workflow_update._process_workflow_yaml",
            raise_validation_error,
        )

        await _update_workflow({"workflow_yaml": "new broken yaml"}, ctx)

        assert ctx.workflow_yaml == "original yaml"

    @pytest.mark.asyncio
    async def test_rejects_stale_block_metadata_before_persistence(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _update_workflow

        prior_yaml = """
title: Count example.com topic alpha results
workflow_definition:
  blocks:
    - block_type: navigation
      label: search_topic_alpha
      title: Search Topic Alpha
      next_block_label: null
      navigation_goal: Search example.com for topic alpha.
"""
        submitted_yaml = """
title: Count example.com sample beta results
workflow_definition:
  blocks:
    - block_type: navigation
      label: search_topic_alpha
      title: Search Topic Alpha
      next_block_label: null
      navigation_goal: Search example.com for sample beta.
"""
        ctx = _make_ctx(workflow_yaml=prior_yaml)

        result = await _update_workflow({"workflow_yaml": submitted_yaml}, ctx)

        assert result["ok"] is False
        assert "corrected block metadata still appears stale" in result["error"]
        assert "search_topic_alpha" in result["error"]
        assert ctx.workflow_yaml == prior_yaml

    @pytest.mark.asyncio
    async def test_stale_check_prefers_in_turn_emission_over_turn_start_yaml(self) -> None:
        # Cross-path flow: an earlier inline REPLACE_WORKFLOW left
        # ctx.last_workflow_yaml at the renamed state. A subsequent
        # update_workflow call submits a regression that re-stales the label
        # — this must compare against ctx.last_workflow_yaml, not the older
        # ctx.workflow_yaml that still points at turn-start.
        from skyvern.forge.sdk.copilot.tools import _update_workflow

        turn_start_yaml = """
title: Alpha workflow
workflow_definition:
  blocks:
    - block_type: navigation
      label: review_alpha_one
      title: Review Alpha One
      next_block_label: null
      navigation_goal: Review the alpha one records on the directory.
"""
        in_turn_emission_yaml = """
title: Beta workflow
workflow_definition:
  blocks:
    - block_type: navigation
      label: review_beta_two
      title: Review Beta Two
      next_block_label: null
      navigation_goal: Review the beta two records on the directory.
"""
        regressed_yaml = """
title: Beta workflow
workflow_definition:
  blocks:
    - block_type: navigation
      label: review_beta_two
      title: Review Beta Two
      next_block_label: null
      navigation_goal: Review the gamma three records on the directory.
"""
        ctx = _make_ctx(workflow_yaml=turn_start_yaml)
        ctx.last_workflow_yaml = in_turn_emission_yaml

        result = await _update_workflow({"workflow_yaml": regressed_yaml}, ctx)

        assert result["ok"] is False
        assert "review_beta_two" in result["error"]


class TestWorkflowUpdatePersistence:
    def test_record_marks_last_workflow_but_not_legacy_persisted(self) -> None:
        """SKY-10318: ``_record_workflow_update_result`` no longer flips the
        legacy ``workflow_persisted`` flag (canonical write is now gated by
        ``_update_workflow``'s degraded-path check, not by tool success). The
        staging-aware signal is ``ctx.has_staged_proposal``, assigned inside
        ``_update_workflow`` itself rather than this bookkeeping helper. The
        helper still records ``last_workflow`` and ``last_workflow_yaml`` for
        downstream callers (e.g. frontier diff, terminal AgentResult)."""
        from skyvern.forge.sdk.copilot.tools import _record_workflow_update_result

        ctx = MagicMock()
        ctx.workflow_yaml = "title: Empty workflow"
        ctx.workflow_persisted = False

        workflow = MagicMock()
        workflow.workflow_definition = MagicMock()
        workflow.workflow_definition.blocks = []

        _record_workflow_update_result(
            ctx,
            {
                "ok": True,
                "_workflow": workflow,
                "data": {"block_count": 0},
            },
        )

        assert ctx.last_workflow is workflow
        assert ctx.last_workflow_yaml == "title: Empty workflow"
        assert ctx.workflow_persisted is False
