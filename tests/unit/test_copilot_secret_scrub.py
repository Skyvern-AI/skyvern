"""Per-turn secret scrub set: registration plus exact-string scrubbing of page-readback tool results.

OSS-synced: only example.* / authenticationtest.com fixtures with fake secret values.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.copilot import mcp_adapter, secret_scrub
from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, SkyvernOverlayMCPServer
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.secret_scrub import (
    REDACTED_SECRET_PLACEHOLDER,
    clear_session_scrub_values,
    register_secret_scrub_value,
    scrub_secrets_from_structure,
    scrub_secrets_from_text,
)

_FAKE_PASSWORD = "fake-pa55w0rd-7x9"
_FAKE_OTP = "392817"


@pytest.fixture(autouse=True)
def _isolate_session_scrub_registry() -> Iterator[None]:
    secret_scrub._SESSION_SCRUB_VALUES.clear()
    yield
    secret_scrub._SESSION_SCRUB_VALUES.clear()


def _agent_ctx(browser_session_id: str = "pbs_1") -> AgentContext:
    return AgentContext(
        organization_id="o_1",
        workflow_id="w_1",
        workflow_permanent_id="wpid_1",
        workflow_yaml="",
        browser_session_id=browser_session_id,
        stream=MagicMock(),
    )


class TestRegistration:
    def test_registers_and_dedupes(self) -> None:
        ctx = _agent_ctx()
        register_secret_scrub_value(ctx, _FAKE_PASSWORD)
        register_secret_scrub_value(ctx, _FAKE_PASSWORD)
        register_secret_scrub_value(ctx, _FAKE_OTP)
        assert ctx.secret_scrub_values == [_FAKE_PASSWORD, _FAKE_OTP]

    def test_ignores_empty_and_non_string(self) -> None:
        ctx = _agent_ctx()
        register_secret_scrub_value(ctx, "")
        register_secret_scrub_value(ctx, None)
        assert ctx.secret_scrub_values == []

    def test_tolerates_context_without_scrub_list(self) -> None:
        register_secret_scrub_value(object(), _FAKE_PASSWORD)  # type: ignore[arg-type]


class TestScrubStructure:
    def test_replaces_in_nested_dicts_lists_and_keys(self) -> None:
        ctx = _agent_ctx()
        register_secret_scrub_value(ctx, _FAKE_PASSWORD)
        register_secret_scrub_value(ctx, _FAKE_OTP)
        result = scrub_secrets_from_structure(
            ctx,
            {
                "data": {
                    "result": [f"input value is {_FAKE_PASSWORD}", {_FAKE_OTP: "totp input"}],
                    "html": f"<input value='{_FAKE_PASSWORD}'><input value='{_FAKE_OTP}'>",
                },
                "count": 3,
            },
        )
        dumped = json.dumps(result)
        assert _FAKE_PASSWORD not in dumped
        assert _FAKE_OTP not in dumped
        assert REDACTED_SECRET_PLACEHOLDER in dumped
        assert result["count"] == 3

    def test_no_registered_values_returns_object_unchanged(self) -> None:
        ctx = _agent_ctx()
        payload = {"data": {"result": f"value {_FAKE_PASSWORD}"}}
        assert scrub_secrets_from_structure(ctx, payload) is payload

    def test_overlapping_values_scrub_longest_first(self) -> None:
        ctx = _agent_ctx()
        register_secret_scrub_value(ctx, "abc")
        register_secret_scrub_value(ctx, "abcdef")
        assert scrub_secrets_from_text(ctx, "xabcdefy") == f"x{REDACTED_SECRET_PLACEHOLDER}y"

    def test_image_base64_is_not_corrupted(self) -> None:
        png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"q" * 200).decode()
        embedded = png_b64[20:30]
        ctx = _agent_ctx()
        register_secret_scrub_value(ctx, embedded)
        result = scrub_secrets_from_structure(
            ctx,
            {"data": {"screenshot_base64": png_b64, "note": f"code {embedded} typed"}},
        )
        assert result["data"]["screenshot_base64"] == png_b64
        assert embedded not in result["data"]["note"]


class _FakeRawResult:
    def __init__(self, payload: dict[str, Any], is_error: bool = False) -> None:
        self.structured_content = payload
        self.is_error = is_error
        self.content: list[Any] = []


class _FakeClient:
    def __init__(self, payload: dict[str, Any] | Exception) -> None:
        self._payload = payload

    async def call_tool(self, name: str, args: dict[str, Any], raise_on_error: bool = False) -> _FakeRawResult:
        if isinstance(self._payload, Exception):
            raise self._payload
        return _FakeRawResult(self._payload)


def _evaluate_readback_payload() -> dict[str, Any]:
    """An evaluate-shaped DOM readback of a credential form after a scout fill."""
    return {
        "ok": True,
        "data": {
            "url": "https://authenticationtest.com/totpChallenge/",
            "title": "TOTP Challenge",
            "result": {
                "inputs": [
                    {"name": "password", "selector": "#password", "type": "password"},
                    {"name": "totp", "selector": "#totpmfa", "type": "text"},
                ],
                "rows": [{"cells": [{"text": "password"}, {"value": _FAKE_PASSWORD}, {"value": _FAKE_OTP}]}],
                "html": f"<input id='password' value='{_FAKE_PASSWORD}'><input id='totpmfa' value='{_FAKE_OTP}'>",
            },
        },
    }


def _make_server(
    ctx: AgentContext, payload: dict[str, Any] | Exception, overlay: SchemaOverlay
) -> SkyvernOverlayMCPServer:
    server = SkyvernOverlayMCPServer(
        transport=MagicMock(),
        overlays={"evaluate": overlay},
        alias_map={},
        allowlist=frozenset(),
        context_provider=lambda: ctx,
    )
    server._client = _FakeClient(payload)
    return server


class TestAdapterScrubChokepoint:
    @pytest.mark.asyncio
    async def test_post_fill_evaluate_readback_is_redacted_in_result_record_and_loop_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.forge.sdk.copilot.tools.mcp_hooks import _evaluate_post_hook, _evaluate_pre_hook

        ctx = _agent_ctx()
        register_secret_scrub_value(ctx, _FAKE_PASSWORD)
        register_secret_scrub_value(ctx, _FAKE_OTP)

        recorded: list[dict[str, Any]] = []
        monkeypatch.setattr(
            mcp_adapter,
            "record_tool_step_result_for_ctx",
            lambda _ctx, _tool, _args, result: recorded.append(dict(result)),
        )

        overlay = SchemaOverlay(pre_hook=_evaluate_pre_hook, post_hook=_evaluate_post_hook)
        server = _make_server(ctx, _evaluate_readback_payload(), overlay)

        result = await server.call_tool("evaluate", {"expression": "scan()"})

        tool_text = result.content[0].text
        assert _FAKE_PASSWORD not in tool_text
        assert _FAKE_OTP not in tool_text
        assert REDACTED_SECRET_PLACEHOLDER in tool_text

        assert recorded, "tool result was not recorded"
        recorded_text = json.dumps(recorded)
        assert _FAKE_PASSWORD not in recorded_text
        assert _FAKE_OTP not in recorded_text
        assert REDACTED_SECRET_PLACEHOLDER in recorded_text

        loop_context_text = json.dumps(
            {
                "flow_evidence": ctx.flow_evidence,
                "composition_page_evidence": ctx.composition_page_evidence,
                "scouted_interactions": ctx.scouted_interactions,
                "scout_trajectory": ctx.scout_trajectory,
            }
        )
        assert _FAKE_PASSWORD not in loop_context_text
        assert _FAKE_OTP not in loop_context_text
        assert ctx.flow_evidence, "evaluate evidence was not recorded into the loop context"
        assert REDACTED_SECRET_PLACEHOLDER in loop_context_text

    @pytest.mark.asyncio
    async def test_no_fill_this_turn_leaves_result_unscrubbed(self) -> None:
        ctx = _agent_ctx()
        server = _make_server(ctx, _evaluate_readback_payload(), SchemaOverlay())

        result = await server.call_tool("evaluate", {"expression": "scan()"})

        tool_text = result.content[0].text
        assert _FAKE_PASSWORD in tool_text
        assert _FAKE_OTP in tool_text
        assert REDACTED_SECRET_PLACEHOLDER not in tool_text

    @pytest.mark.asyncio
    async def test_tool_exception_text_is_redacted(self) -> None:
        ctx = _agent_ctx()
        register_secret_scrub_value(ctx, _FAKE_PASSWORD)
        server = _make_server(
            ctx, RuntimeError(f"locator resolved to <input value='{_FAKE_PASSWORD}'>"), SchemaOverlay()
        )

        result = await server.call_tool("evaluate", {"expression": "scan()"})

        tool_text = result.content[0].text
        assert result.isError is True
        assert _FAKE_PASSWORD not in tool_text
        assert REDACTED_SECRET_PLACEHOLDER in tool_text


class TestCrossTurnSessionScrub:
    def test_session_registry_survives_a_fresh_turn_context(self) -> None:
        turn1 = _agent_ctx()
        register_secret_scrub_value(turn1, _FAKE_PASSWORD)

        turn2 = _agent_ctx()
        assert turn2.secret_scrub_values == []
        assert scrub_secrets_from_text(turn2, f"value {_FAKE_PASSWORD}") == f"value {REDACTED_SECRET_PLACEHOLDER}"

    def test_registry_is_scoped_per_browser_session(self) -> None:
        turn1 = _agent_ctx("pbs_1")
        register_secret_scrub_value(turn1, _FAKE_PASSWORD)

        other_session = _agent_ctx("pbs_2")
        assert scrub_secrets_from_text(other_session, f"value {_FAKE_PASSWORD}") == f"value {_FAKE_PASSWORD}"

    def test_clear_session_scrub_values_drops_the_session(self) -> None:
        turn1 = _agent_ctx()
        register_secret_scrub_value(turn1, _FAKE_PASSWORD)
        clear_session_scrub_values("pbs_1")

        turn2 = _agent_ctx()
        assert scrub_secrets_from_text(turn2, f"value {_FAKE_PASSWORD}") == f"value {_FAKE_PASSWORD}"

    def test_session_registry_is_bounded_and_evicts_oldest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(secret_scrub, "_MAX_SCRUB_SESSIONS", 3)
        for i in range(5):
            register_secret_scrub_value(_agent_ctx(f"pbs_{i}"), _FAKE_PASSWORD)

        assert len(secret_scrub._SESSION_SCRUB_VALUES) == 3
        assert set(secret_scrub._SESSION_SCRUB_VALUES) == {"pbs_2", "pbs_3", "pbs_4"}
        # The evicted oldest session no longer scrubs; the newest still does.
        assert scrub_secrets_from_text(_agent_ctx("pbs_0"), _FAKE_PASSWORD) == _FAKE_PASSWORD
        assert scrub_secrets_from_text(_agent_ctx("pbs_4"), _FAKE_PASSWORD) == REDACTED_SECRET_PLACEHOLDER

    @pytest.mark.asyncio
    async def test_readback_in_later_turn_is_redacted_in_result_and_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.forge.sdk.copilot.tools.mcp_hooks import _evaluate_post_hook, _evaluate_pre_hook

        turn1 = _agent_ctx()
        register_secret_scrub_value(turn1, _FAKE_PASSWORD)
        register_secret_scrub_value(turn1, _FAKE_OTP)

        turn2 = _agent_ctx()
        assert turn2.secret_scrub_values == []

        recorded: list[dict[str, Any]] = []
        monkeypatch.setattr(
            mcp_adapter,
            "record_tool_step_result_for_ctx",
            lambda _ctx, _tool, _args, result: recorded.append(dict(result)),
        )

        overlay = SchemaOverlay(pre_hook=_evaluate_pre_hook, post_hook=_evaluate_post_hook)
        server = _make_server(turn2, _evaluate_readback_payload(), overlay)

        result = await server.call_tool("evaluate", {"expression": "scan()"})

        tool_text = result.content[0].text
        assert _FAKE_PASSWORD not in tool_text
        assert _FAKE_OTP not in tool_text
        assert REDACTED_SECRET_PLACEHOLDER in tool_text

        assert recorded, "tool result was not recorded"
        recorded_text = json.dumps(recorded)
        assert _FAKE_PASSWORD not in recorded_text
        assert _FAKE_OTP not in recorded_text
