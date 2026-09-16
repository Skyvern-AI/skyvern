from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.copilot.runtime import effective_browser_session_id
from skyvern.forge.sdk.copilot.tools import inspect_page_for_composition_tool
from tests.unit.copilot_test_helpers import make_copilot_ctx


def test_composition_inspection_schema_exposes_typed_browser_target_default() -> None:
    schema = inspect_page_for_composition_tool.params_json_schema

    target = schema["properties"]["target"]
    if "$ref" in target:
        target = schema["$defs"][target["$ref"].rsplit("/", 1)[-1]]
    assert target["enum"] == ["debug", "last_run"]
    assert schema["properties"]["target"]["default"] == "debug"


@pytest.mark.asyncio
async def test_composition_inspection_binds_default_and_last_run_through_final_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_debug")
    ctx.last_run_blocks_browser_session_id = "pbs_run"
    ctx.last_run_blocks_workflow_run_id = "wr_run"
    ctx.recorded_build_test_outcome_history = [{"workflow_run_id": "wr_run", "status": "failed"}]
    recorded_outcome_history = list(ctx.recorded_build_test_outcome_history)
    observed: list[str | None] = []
    recorded: list[tuple[dict[str, object], dict[str, object]]] = []

    async def inspect(inner_ctx: object, _target_url: str, _reads: tuple[object, ...]) -> dict[str, object]:
        observed.append(effective_browser_session_id(inner_ctx))  # type: ignore[arg-type]
        return {"ok": True, "data": {"source_browser_session_id": observed[-1]}}

    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._authority_tool_error", lambda *_args: None)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._inspect_page_for_composition_impl", inspect)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.record_tool_step_result_for_ctx",
        lambda _ctx, _name, arguments, result: recorded.append((arguments, result)),
    )

    answers = []
    for arguments in ({"target_url": "current_page"}, {"target_url": "current_page", "target": "last_run"}):
        raw = await inspect_page_for_composition_tool.on_invoke_tool(
            SimpleNamespace(context=ctx, tool_name="inspect_page_for_composition"), json.dumps(arguments)
        )
        answers.append(json.loads(raw))

    assert observed == ["pbs_debug", "pbs_run"]
    assert [answer["browser_target"] for answer in answers] == ["debug", "last_run"]
    assert answers[1]["browser_target_workflow_run_id"] == "wr_run"
    assert [entry[0]["target"] for entry in recorded] == ["debug", "last_run"]
    assert [entry[1] for entry in recorded] == answers
    assert ctx.browser_session_id == "pbs_debug"
    assert ctx.recorded_build_test_outcome_history == recorded_outcome_history


@pytest.mark.asyncio
async def test_missing_last_run_is_recorded_unavailable_without_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_debug")
    ctx.last_run_blocks_browser_session_id = None
    inspect = AsyncMock()
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._authority_tool_error", lambda *_args: None)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._inspect_page_for_composition_impl", inspect)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.record_tool_step_result_for_ctx",
        lambda _ctx, _name, _arguments, result: recorded.append(result),
    )

    raw = await inspect_page_for_composition_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="inspect_page_for_composition"),
        json.dumps({"target_url": "current_page", "target": "last_run"}),
    )
    result = json.loads(raw)

    inspect.assert_not_awaited()
    assert result["ok"] is False
    assert result["browser_target"] == "last_run"
    assert result["source_matches_target"] is False
    assert result["source_browser_session_id"] is None
    assert recorded == [result]


@pytest.mark.asyncio
async def test_pre_dispatch_refusal_has_no_claimed_browser_source(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_debug")
    inspect = AsyncMock()
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._authority_tool_error", lambda *_args: "blocked")
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools._diagnosis_repair_tool_error",
        lambda *_args: json.dumps({"ok": False, "error": "blocked"}),
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._inspect_page_for_composition_impl", inspect)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.record_tool_step_result_for_ctx",
        lambda _ctx, _name, _arguments, result: recorded.append(result),
    )

    raw = await inspect_page_for_composition_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="inspect_page_for_composition"),
        json.dumps({"target_url": "current_page"}),
    )
    result = json.loads(raw)

    inspect.assert_not_awaited()
    assert result["source_browser_session_id"] is None
    assert result["source_matches_target"] is False
    assert recorded == [result]


@pytest.mark.asyncio
async def test_mixed_inner_provenance_is_not_replaced_with_the_target_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_debug")
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._authority_tool_error", lambda *_args: None)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools._inspect_page_for_composition_impl",
        AsyncMock(
            return_value={
                "ok": True,
                "data": {
                    "source_browser_session_id": "pbs_replacement",
                    "browser_session_provenance": {
                        "mixed": True,
                        "start_browser_session_id": "pbs_debug",
                        "end_browser_session_id": "pbs_replacement",
                    },
                },
            }
        ),
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.record_tool_step_result_for_ctx", lambda *_args: None)

    raw = await inspect_page_for_composition_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="inspect_page_for_composition"),
        json.dumps({"target_url": "current_page"}),
    )
    result = json.loads(raw)

    assert result["source_browser_session_id"] == "pbs_replacement"
    assert result["source_matches_target"] is False


@pytest.mark.asyncio
async def test_opposite_composition_targets_are_task_local(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_debug")
    ctx.last_run_blocks_browser_session_id = "pbs_run"
    ctx.last_run_blocks_workflow_run_id = "wr_run"
    both_entered = asyncio.Event()
    entrants = 0

    async def inspect(inner_ctx: object, _target_url: str, _reads: tuple[object, ...]) -> dict[str, object]:
        nonlocal entrants
        entrants += 1
        if entrants == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=1)
        source = effective_browser_session_id(inner_ctx)  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert effective_browser_session_id(inner_ctx) == source  # type: ignore[arg-type]
        return {"ok": True, "data": {"source_browser_session_id": source}}

    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._authority_tool_error", lambda *_args: None)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._inspect_page_for_composition_impl", inspect)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.record_tool_step_result_for_ctx", lambda *_args: None)

    async def invoke(target: str) -> dict[str, object]:
        raw = await inspect_page_for_composition_tool.on_invoke_tool(
            SimpleNamespace(context=ctx, tool_name="inspect_page_for_composition"),
            json.dumps({"target_url": "current_page", "target": target}),
        )
        return json.loads(raw)

    debug, last_run = await asyncio.gather(invoke("debug"), invoke("last_run"))

    assert debug["data"]["source_browser_session_id"] == "pbs_debug"
    assert last_run["data"]["source_browser_session_id"] == "pbs_run"
    assert ctx.browser_session_id == "pbs_debug"


@pytest.mark.asyncio
async def test_requested_output_probe_remains_inside_target_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_debug")
    ctx.last_run_blocks_browser_session_id = "pbs_run"
    ctx.last_run_blocks_workflow_run_id = "wr_run"
    observed: list[str | None] = []

    async def verify(inner_ctx: object, *_args: object) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        observed.append(effective_browser_session_id(inner_ctx))  # type: ignore[arg-type]
        return [], []

    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._authority_tool_error", lambda *_args: None)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools._inspect_page_for_composition_impl",
        AsyncMock(return_value={"ok": True, "data": {}}),
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._verify_requested_output_reads", verify)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.record_tool_step_result_for_ctx", lambda *_args: None)

    await inspect_page_for_composition_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="inspect_page_for_composition"),
        json.dumps(
            {
                "target_url": "current_page",
                "target": "last_run",
                "requested_output_reads": [{"output_path": "value", "value_text": "7", "label": "Value"}],
            }
        ),
    )

    assert observed == ["pbs_run"]
