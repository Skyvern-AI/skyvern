"""Bounded AI fallback escalation for a failed copilot-authored code block.

block.prompt is the operative goal; a confidently-matched step only narrows it, so a rotted
selector heals even when no step covers the failing line (the common case).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum, auto
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from structlog.testing import capture_logs

import skyvern.webeye.navigation as navigation_module
from skyvern.errors.errors import UserDefinedError
from skyvern.exceptions import NO_ADDRESS_RECORD_NAV_ERROR_CODE
from skyvern.forge import app
from skyvern.forge.agent_functions import CodeBlockEngineFailure, CodeBlockEngineResult
from skyvern.forge.sdk.api.llm.schema_validator import validate_and_fill_extraction_result, validate_schema
from skyvern.forge.sdk.copilot.code_block_steps import derive_code_block_steps
from skyvern.forge.sdk.copilot.nav_attribution import (
    block_nav_error_codes,
    proxy_owns_nav_codes,
    target_owns_nav_codes,
)
from skyvern.forge.sdk.copilot.reached_download_target import REGISTERED_DOWNLOAD_OUTPUT_KEYS
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.repositories.workflow_runs import WorkflowRunsRepository
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import (
    BlockResult,
    BlockStatus,
    BlockType,
    CodeBlock,
    CodeBlockStep,
    ErrorCode,
)
from skyvern.forge.sdk.workflow.models.parameter import (
    BitwardenCreditCardDataParameter,
    CredentialParameter,
    OutputParameter,
    Parameter,
    ParameterType,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun
from skyvern.forge.sdk.workflow.service import _merge_workflow_run_errors
from skyvern.forge.taskv3.goal_composition import (
    CodeProgressRecord,
    GoalDirectives,
    compose_goal,
    render_code_progress_section,
    typed_value_rows,
)
from skyvern.schemas.runs import RunEngine
from skyvern.schemas.self_heal import HealClassification, HealSkipReason, HealStatus
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.navigation import clear_task_nav_error_code, record_task_nav_error_code

SECRET_VALUE = "hunter2-super-secret"
SHORT_SECRET_VALUE = "abc"
CVV_VALUE = "739"
DEFAULT_PROMPT = "Log in and download the report"

ExtractedInformation = list[Any] | dict[str, Any] | str | None


class _Unset(Enum):
    token = auto()


_UNSET = _Unset.token


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _string_values(item)]
    if isinstance(value, (list, tuple, set)):
        return [text for item in value for text in _string_values(item)]
    return []


def _make_code_block(
    steps: list[CodeBlockStep] | None = None,
    prompt: str | None = DEFAULT_PROMPT,
    code: str = "await page.click('#missing')",
    error_code_mapping: dict[str, str] | None = None,
    data_schema: dict[str, Any] | None = None,
) -> CodeBlock:
    now = datetime.now(timezone.utc)
    output_parameter = OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="code_output",
        description="test output",
        output_parameter_id="op_code",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )
    return CodeBlock(
        label="code_1",
        code=code,
        prompt=prompt,
        steps=steps,
        output_parameter=output_parameter,
        error_code_mapping=error_code_mapping,
        data_schema=data_schema,
    )


@pytest.mark.asyncio
async def test_inline_declared_error_redacts_secret_reasoning() -> None:
    block = _make_code_block(
        code=f"raise ErrorCode('missing', 'contains {SECRET_VALUE}')",
        error_code_mapping={"missing": "Missing"},
    )
    context = _make_context(with_secret=True)
    function = block.generate_async_user_function(block.code, MagicMock())
    with pytest.raises(ErrorCode) as exc_info:
        await function()
    error = block._extract_declared_error(exc_info.value, context)
    assert error is not None
    assert error.reasoning == "contains [redacted]"


@pytest.mark.asyncio
async def test_inline_caught_error_is_inert_and_undeclared_fails_closed() -> None:
    caught = _make_code_block(
        code="try:\n    raise ErrorCode('caught', 'reason')\nexcept ErrorCode:\n    value = 'ok'",
        error_code_mapping={"caught": "Caught"},
    )
    result = await caught.generate_async_user_function(caught.code, MagicMock())()
    assert result["value"] == "ok"

    undeclared = _make_code_block(
        code="raise ErrorCode('missing', 'reason')",
        error_code_mapping={"other": "Other"},
    )
    function = undeclared.generate_async_user_function(undeclared.code, MagicMock())
    with pytest.raises(ErrorCode) as exc_info:
        await function()
    assert undeclared._extract_declared_error(exc_info.value, _make_context()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_scope", ["block", "workflow"])
async def test_inline_declared_error_renders_effective_manifest(manifest_scope: str) -> None:
    mapping = {"ERR_{{ id }}": "Failure for {{ id }}"}
    block = _make_code_block(
        code="raise ErrorCode('ERR_{{ id }}', 'region {{ region }} failed')",
        error_code_mapping=mapping if manifest_scope == "block" else None,
    )
    context = _make_context()
    context.values["id"] = 42
    context.values["region"] = "EU"
    if manifest_scope == "workflow":
        context.workflow = SimpleNamespace(workflow_definition=SimpleNamespace(error_code_mapping=mapping))

    block.format_potential_template_parameters(context)
    function = block.generate_async_user_function(block.code, MagicMock())
    with pytest.raises(ErrorCode) as exc_info:
        await function()

    error = block._extract_declared_error(exc_info.value, context)
    assert error is not None
    assert error.error_code == "ERR_42"
    assert error.reasoning == "region EU failed"


@pytest.mark.asyncio
async def test_inline_templated_secret_error_code_fails_closed() -> None:
    block = _make_code_block(
        code="raise ErrorCode('{{ secret }}', 'must stay generic')",
        error_code_mapping={"{{ secret }}": "Secret-derived code"},
    )
    context = _make_context(with_secret=True)
    context.values["secret"] = SECRET_VALUE

    block.format_potential_template_parameters(context)
    function = block.generate_async_user_function(block.code, MagicMock())
    with pytest.raises(ErrorCode) as exc_info:
        await function()

    assert block.error_code_mapping is None
    assert block._extract_declared_error(exc_info.value, context) is None


@pytest.mark.asyncio
async def test_inline_extraction_rejects_secret_code_even_if_manifest_contains_it() -> None:
    block = _make_code_block(
        code=f"raise ErrorCode('{SECRET_VALUE}', 'must stay generic')",
        error_code_mapping={SECRET_VALUE: "Persisted unsafe manifest"},
    )
    context = _make_context(with_secret=True)
    function = block.generate_async_user_function(block.code, MagicMock())
    with pytest.raises(ErrorCode) as exc_info:
        await function()

    assert block._extract_declared_error(exc_info.value, context) is None


@pytest.mark.asyncio
async def test_inline_extraction_rejects_embedded_short_secret_code() -> None:
    block = _make_code_block(
        code="raise ErrorCode('ERR_abc', 'must stay generic')",
        error_code_mapping={"ERR_abc": "Persisted unsafe manifest"},
    )
    context = _make_context()
    context.secrets["k_short"] = SHORT_SECRET_VALUE
    function = block.generate_async_user_function(block.code, MagicMock())
    with pytest.raises(ErrorCode) as exc_info:
        await function()

    assert block._extract_declared_error(exc_info.value, context) is None


@pytest.mark.asyncio
async def test_inline_extraction_redacts_embedded_short_secret_reasoning() -> None:
    block = _make_code_block(
        code="raise ErrorCode('ERR_safe', 'contains abc value')",
        error_code_mapping={"ERR_safe": "Safe manifest"},
    )
    context = _make_context()
    context.secrets["k_short"] = SHORT_SECRET_VALUE
    function = block.generate_async_user_function(block.code, MagicMock())
    with pytest.raises(ErrorCode) as exc_info:
        await function()

    error = block._extract_declared_error(exc_info.value, context)
    assert error is not None
    assert error.reasoning == "contains [redacted] value"


def test_inline_rendered_manifest_redacts_description_and_discards_secret_key() -> None:
    block = _make_code_block(
        error_code_mapping={"ERR_safe": "Failure for {{ secret }}", "ERR_{{ secret }}": "Unsafe key"}
    )
    context = _make_context()
    context.secrets["k_short"] = SHORT_SECRET_VALUE
    context.values["secret"] = SHORT_SECRET_VALUE

    block.format_potential_template_parameters(context)

    assert block.error_code_mapping == {"ERR_safe": "Failure for [redacted]"}


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_value", ["x" * 129, "ERR_safe\n"])
async def test_inline_rendered_manifest_key_fails_closed_when_invalid(invalid_value: str) -> None:
    block = _make_code_block(
        code="raise ErrorCode('ERR_safe', 'must stay generic')",
        error_code_mapping={"{{ invalid_key }}": "Description"},
    )
    context = _make_context()
    context.values["invalid_key"] = invalid_value

    block.format_potential_template_parameters(context)
    function = block.generate_async_user_function(block.code, MagicMock())
    with pytest.raises(ErrorCode) as exc_info:
        await function()

    assert block._extract_declared_error(exc_info.value, context) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_value", ["x" * 2001, "unsafe\u0000value", "", "   "])
async def test_inline_rendered_manifest_description_fails_closed_when_invalid(invalid_value: str) -> None:
    block = _make_code_block(
        code="raise ErrorCode('ERR_safe', 'must stay generic')",
        error_code_mapping={"ERR_safe": "{{ invalid_description }}"},
    )
    context = _make_context()
    context.values["invalid_description"] = invalid_value

    block.format_potential_template_parameters(context)
    function = block.generate_async_user_function(block.code, MagicMock())
    with pytest.raises(ErrorCode) as exc_info:
        await function()

    assert block._extract_declared_error(exc_info.value, context) is None


def test_rendered_manifest_discards_aggregate_overflow_in_insertion_order() -> None:
    mapping = {f"ERR_{index}": f"{{{{ descriptions[{index}] }}}}" for index in range(17)}
    block = _make_code_block(error_code_mapping=mapping)
    context = _make_context()
    context.values["descriptions"] = ["x" * 2000] * len(mapping)

    block.format_potential_template_parameters(context)

    assert block.error_code_mapping is not None
    assert list(block.error_code_mapping) == [f"ERR_{index}" for index in range(16)]


@pytest.mark.asyncio
async def test_user_defined_failure_records_dedicated_skip_without_healing(monkeypatch: pytest.MonkeyPatch) -> None:
    block = _make_code_block()
    context = _make_context()
    recorder = SimpleNamespace(finalize=AsyncMock())
    monkeypatch.setattr(block, "_capture_failure_evidence", AsyncMock())
    write_episode = AsyncMock()
    monkeypatch.setattr(block, "_write_heal_episode_safe", write_episode)
    attempt_heal = AsyncMock()
    monkeypatch.setattr(block, "_attempt_self_heal", attempt_heal)

    async def build_failure() -> BlockResult:
        return await block.build_block_result(success=False, failure_reason="typed", status=BlockStatus.failed)

    result = await block._resolve_failure_with_heal(
        authored_code=None,
        exception=ErrorCode("typed", "reason"),
        failing_line=1,
        build_failure_result=build_failure,
        classification=HealClassification(healable=False, skip_reason=HealSkipReason.user_defined_error),
        recorder=recorder,
        workflow_run_context=context,
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id=None,
    )
    assert result.success is False
    attempt_heal.assert_not_awaited()
    assert write_episode.await_args.kwargs["skip_reason"] is HealSkipReason.user_defined_error


@pytest.mark.asyncio
@pytest.mark.parametrize("flag_on", [True, False])
async def test_chokepoint_gate_follows_the_org_flag(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None], flag_on: bool
) -> None:
    ai_fallback_flag("o_test" if flag_on else None)
    block = _make_code_block()
    context = _make_context()
    recorder = SimpleNamespace(finalize=AsyncMock(), recording_page=_recording_page(None))
    monkeypatch.setattr(block, "_capture_failure_evidence", AsyncMock())
    monkeypatch.setattr(block, "_write_heal_episode_safe", AsyncMock())
    attempt_heal = AsyncMock(return_value=None)
    monkeypatch.setattr(block, "_attempt_self_heal", attempt_heal)

    async def build_failure() -> BlockResult:
        return await block.build_block_result(success=False, failure_reason="boom", status=BlockStatus.failed)

    result = await block._resolve_failure_with_heal(
        authored_code=None,
        exception=PlaywrightTimeoutError("timeout"),
        failing_line=1,
        build_failure_result=build_failure,
        classification=HealClassification(healable=True, skip_reason=None),
        recorder=recorder,
        workflow_run_context=context,
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id=None,
    )

    assert result.success is False
    assert attempt_heal.await_count == (1 if flag_on else 0)


def _workflow_run_row(
    *,
    copilot_session_id: str | None = None,
    debug_session_id: str | None = None,
    parent_workflow_run_id: str | None = None,
) -> WorkflowRun:
    return WorkflowRun.model_construct(
        copilot_session_id=copilot_session_id,
        debug_session_id=debug_session_id,
        parent_workflow_run_id=parent_workflow_run_id,
    )


_AUTHORING_RUN_EPISODE = [(HealStatus.skipped, HealSkipReason.authoring_run)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag_on", "runs", "expected_episodes", "recovery_runs"),
    [
        (True, {"wr_test": _workflow_run_row(copilot_session_id="cs_1")}, _AUTHORING_RUN_EPISODE, False),
        (True, {"wr_test": _workflow_run_row(debug_session_id="ds_1")}, _AUTHORING_RUN_EPISODE, False),
        (
            True,
            {
                "wr_test": _workflow_run_row(parent_workflow_run_id="wr_parent"),
                "wr_parent": _workflow_run_row(debug_session_id="ds_1"),
            },
            _AUTHORING_RUN_EPISODE,
            False,
        ),
        (True, RuntimeError("db down"), [], False),
        (True, {}, [], False),
        (True, {"wr_test": _workflow_run_row()}, [(HealStatus.fired_failed, None)], True),
        (
            True,
            {
                "wr_test": _workflow_run_row(parent_workflow_run_id="wr_parent"),
                "wr_parent": _workflow_run_row(),
            },
            [(HealStatus.fired_failed, None)],
            True,
        ),
        (False, {"wr_test": _workflow_run_row(copilot_session_id="cs_1")}, [], False),
    ],
    ids=[
        "copilot_run",
        "editor_run",
        "child_of_editor_run",
        "read_raises",
        "run_missing",
        "unmarked_run",
        "child_of_unmarked_run",
        "flag_off",
    ],
)
async def test_ai_fallback_never_rescues_an_authoring_run(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
    flag_on: bool,
    runs: dict[str, WorkflowRun] | Exception,
    expected_episodes: list[tuple[HealStatus, HealSkipReason | None]],
    recovery_runs: bool,
) -> None:
    ai_fallback_flag("o_test" if flag_on else None)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.failed)

    async def _get_workflow_run(*, workflow_run_id: str, organization_id: str | None = None) -> WorkflowRun | None:
        if isinstance(runs, Exception):
            raise runs
        return runs.get(workflow_run_id)

    get_workflow_run = AsyncMock(side_effect=_get_workflow_run)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run", get_workflow_run)
    block = _make_code_block()
    exception = PlaywrightTimeoutError("Timeout 5000ms exceeded waiting for #missing")
    recorder = SimpleNamespace(recording_page=_recording_page(exception), finalize=AsyncMock())

    async def _build_failure() -> BlockResult:
        return await block.build_block_result(
            success=False,
            failure_reason="Timeout 5000ms exceeded waiting for #missing",
            output_parameter_value=None,
            status=BlockStatus.failed,
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
        )

    result = await block._resolve_failure_with_heal(
        authored_code=None,
        exception=exception,
        failing_line=2,
        build_failure_result=_build_failure,
        classification=HealClassification(healable=True, skip_reason=None),
        recorder=recorder,
        workflow_run_context=_make_context(),
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id=None,
    )

    assert result.success is False
    assert [(episode["status"], episode["skip_reason"]) for episode in state["heal_episodes"]] == expected_episodes
    assert (state["create_task_kwargs"] is not None) is recovery_runs
    assert (state["recovery_block_kwargs"] is not None) is recovery_runs
    if not recovery_runs:
        assert result.failure_reason == "Timeout 5000ms exceeded waiting for #missing"
    if not flag_on:
        get_workflow_run.assert_not_awaited()


def test_workflow_error_merge_does_not_replace_cross_provenance_task_error() -> None:
    typed = {
        "error_code": "same",
        "reasoning": "typed",
        "confidence_float": 1.0,
        "error_type": "USER_DEFINED_ERROR",
    }
    malformed = {**typed, "error_code": "extra", "unexpected": True}
    result = _merge_workflow_run_errors(
        [{"error_code": "same", "reasoning": "legacy", "confidence_float": 1.0}],
        [("wrb_code", ["same", "diagnostic"], "failure", {"errors": [typed, malformed]}, "code")],
    )
    assert result == [
        {"error_code": "same", "reasoning": "legacy", "confidence_float": 1.0},
        typed,
        {"error_code": "diagnostic", "reasoning": "failure", "confidence_float": 1.0},
    ]


def test_workflow_error_merge_upgrades_only_same_block_legacy_entry() -> None:
    first = {
        "error_code": "same",
        "reasoning": "first typed",
        "confidence_float": 1.0,
        "error_type": "USER_DEFINED_ERROR",
    }
    second = {**first, "reasoning": "second typed"}

    assert _merge_workflow_run_errors(
        [],
        [
            ("wrb_1", ["same"], "first legacy", {"errors": [first]}, "code"),
            ("wrb_2", ["same"], "second legacy", {"errors": [second]}, "code"),
        ],
    ) == [first, second]


def test_workflow_error_merge_dedupes_only_within_provenance_and_kind() -> None:
    typed = {
        "error_code": "same",
        "reasoning": "same reason",
        "confidence_float": 1.0,
        "error_type": "USER_DEFINED_ERROR",
    }
    task = dict(typed)

    assert _merge_workflow_run_errors(
        [
            {"error_code": "task_same", "reasoning": "first", "confidence_float": 1.0},
            {"error_code": "task_same", "reasoning": "second", "confidence_float": 1.0},
            task,
        ],
        [
            ("wrb_1", ["legacy_same", "legacy_same"], "legacy reason", {"errors": [typed, typed]}, "code"),
            ("wrb_2", ["legacy_same"], "legacy reason", {"errors": [typed]}, "code"),
        ],
    ) == [
        {"error_code": "task_same", "reasoning": "first", "confidence_float": 1.0},
        {"error_code": "task_same", "reasoning": "second", "confidence_float": 1.0},
        task,
        {"error_code": "legacy_same", "reasoning": "legacy reason", "confidence_float": 1.0},
        typed,
        {"error_code": "legacy_same", "reasoning": "legacy reason", "confidence_float": 1.0},
        typed,
    ]


def test_workflow_error_merge_at_cap_skips_appends_but_allows_later_same_block_upgrade() -> None:
    typed = {
        "error_code": "upgrade",
        "reasoning": "typed",
        "confidence_float": 1.0,
        "error_type": "USER_DEFINED_ERROR",
    }
    task_errors = [{"error_code": f"task_{index}", "reasoning": "task", "confidence_float": 1.0} for index in range(99)]

    errors = _merge_workflow_run_errors(
        task_errors,
        [
            ("wrb_upgrade", ["upgrade"], "legacy", None, "code"),
            ("wrb_skipped", ["not_appended"], "later", {"errors": [typed]}, "code"),
            ("wrb_upgrade", [], None, {"errors": [typed]}, "code"),
        ],
    )

    assert len(errors) == 100
    assert errors[-1] == typed
    assert all(error["error_code"] != "not_appended" for error in errors)


def test_workflow_error_merge_caps_duplicate_legacy_entries_per_row_and_processes_later_blocks() -> None:
    errors = _merge_workflow_run_errors(
        [],
        [
            ("wrb_duplicates", ["duplicate"] * 1000, "first", None, "code"),
            ("wrb_later", ["later"], "second", None, "code"),
        ],
    )

    assert [error["error_code"] for error in errors] == ["duplicate", "later"]


def test_workflow_error_merge_ignores_typed_payload_beyond_per_row_candidate_cap() -> None:
    typed = {
        "error_code": "declared",
        "reasoning": "typed",
        "confidence_float": 1.0,
        "error_type": "USER_DEFINED_ERROR",
    }
    errors = _merge_workflow_run_errors(
        [],
        [("wrb_code", ["declared"], "legacy", {"errors": [None] * 150 + [typed]}, "code")],
    )

    assert errors == [{"error_code": "declared", "reasoning": "legacy", "confidence_float": 1.0}]


def test_workflow_error_merge_upgrades_from_early_typed_payload_after_invalid_candidates() -> None:
    typed = {
        "error_code": "declared",
        "reasoning": "typed",
        "confidence_float": 1.0,
        "error_type": "USER_DEFINED_ERROR",
    }
    errors = _merge_workflow_run_errors(
        [],
        [("wrb_code", ["declared"], "legacy", {"errors": [None, {"bad": "shape"}, typed]}, "code")],
    )

    assert errors == [typed]


def test_workflow_error_merge_preserves_ordinary_and_file_parser_failures() -> None:
    result = _merge_workflow_run_errors(
        [],
        [
            ("wrb_code", ["code_failed"], "code reason", None, "code"),
            ("wrb_parser", ["file_parser_error"], "parser reason", {"errors": []}, "file_url_parser"),
        ],
    )
    assert result == [
        {"error_code": "code_failed", "reasoning": "code reason", "confidence_float": 1.0},
        {"error_code": "file_parser_error", "reasoning": "parser reason", "confidence_float": 1.0},
    ]


def test_workflow_error_merge_matches_status_and_webhook_top_level_shape() -> None:
    typed = {
        "error_code": "declared",
        "reasoning": f"contains {SECRET_VALUE}",
        "confidence_float": 1.0,
        "error_type": "USER_DEFINED_ERROR",
    }
    context = _make_context(with_secret=True)
    errors = _merge_workflow_run_errors(
        [],
        [("wrb_code", ["diagnostic", "declared"], "failure", {"errors": [typed]}, "code")],
        mask_reasoning=context.mask_secrets_in_data,
    )
    expected = [
        {"error_code": "diagnostic", "reasoning": "failure", "confidence_float": 1.0},
        {**typed, "reasoning": "contains *****"},
    ]
    assert {"errors": errors} == {"errors": expected}


def test_workflow_error_merge_skips_typed_secret_code_when_masker_is_available() -> None:
    typed = {
        "error_code": SECRET_VALUE,
        "reasoning": "typed",
        "confidence_float": 1.0,
        "error_type": "USER_DEFINED_ERROR",
    }
    context = _make_context(with_secret=True)

    errors = _merge_workflow_run_errors(
        [],
        [("wrb_code", [], "generic failure", {"errors": [typed]}, "code")],
        mask_reasoning=context.mask_secrets_in_data,
    )

    assert errors == []


def test_workflow_error_merge_skips_legacy_secret_code_when_masker_is_available() -> None:
    context = _make_context(with_secret=True)

    errors = _merge_workflow_run_errors(
        [],
        [("wrb_code", ["first", SECRET_VALUE, "last"], "generic failure", None, "code")],
        mask_reasoning=context.mask_secrets_in_data,
    )

    assert [error["error_code"] for error in errors] == ["first", "last"]


def test_workflow_error_merge_without_masker_preserves_legacy_and_typed_codes() -> None:
    typed = {
        "error_code": SECRET_VALUE,
        "reasoning": "typed",
        "confidence_float": 1.0,
        "error_type": "USER_DEFINED_ERROR",
    }

    errors = _merge_workflow_run_errors(
        [],
        [("wrb_legacy", [SECRET_VALUE], "legacy", None, "code"), ("wrb_typed", [], None, {"errors": [typed]}, "code")],
        mask_reasoning=None,
    )

    assert errors == [
        {"error_code": SECRET_VALUE, "reasoning": "legacy", "confidence_float": 1.0},
        typed,
    ]


def test_workflow_error_merge_only_promotes_typed_payloads_from_code_blocks() -> None:
    typed = {
        "error_code": "spoofed",
        "reasoning": "typed",
        "confidence_float": 1.0,
        "error_type": "USER_DEFINED_ERROR",
    }
    assert _merge_workflow_run_errors(
        [],
        [("wrb_parser", ["diagnostic"], "failure", {"errors": [typed]}, "file_url_parser")],
    ) == [{"error_code": "diagnostic", "reasoning": "failure", "confidence_float": 1.0}]


def test_workflow_error_merge_without_context_preserves_masked_content_with_bounds() -> None:
    task_errors = [
        {"error_code": f"task_{index}", "reasoning": "already ***** masked" + ("x" * 2100)} for index in range(105)
    ]
    task_errors.extend(
        [
            {"error_code": "", "reasoning": "invalid"},
            {"error_code": "x" * 129, "reasoning": "invalid"},
            {"error_code": 123, "reasoning": "invalid"},
        ]
    )

    errors = _merge_workflow_run_errors(task_errors, [], mask_reasoning=None)

    assert len(errors) == 100
    assert errors[0]["error_code"] == "task_0"
    assert errors[-1]["error_code"] == "task_99"
    assert errors[0]["reasoning"].startswith("already ***** masked")
    assert len(errors[0]["reasoning"]) == 2000


@pytest.mark.asyncio
async def test_workflow_run_block_errors_query_has_stable_multi_block_order() -> None:
    rows = [
        SimpleNamespace(
            workflow_run_block_id="wrb_1",
            error_codes=["first"],
            failure_reason="first reason",
            output=None,
            block_type="code",
        ),
        SimpleNamespace(
            workflow_run_block_id="wrb_2",
            error_codes=["second"],
            failure_reason="second reason",
            output=None,
            block_type="code",
        ),
    ]
    result = MagicMock()
    result.all.return_value = rows
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session_factory = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=session)))
    repository = WorkflowRunsRepository(session_factory)

    assert await repository.get_workflow_run_block_errors("wr_test") == [
        ("wrb_1", ["first"], "first reason", None, "code"),
        ("wrb_2", ["second"], "second reason", None, "code"),
    ]
    query = session.execute.await_args.args[0]
    assert [clause.name for clause in query._order_by_clauses] == ["created_at", "workflow_run_block_id"]


def _make_context(
    *,
    with_secret: bool = False,
    with_workflow: bool = True,
    created_by: str | None = "copilot",
    edited_by: str | None = None,
    organization_id: str = "o_test",
) -> WorkflowRunContext:
    context = WorkflowRunContext(
        workflow_title="wf",
        workflow_id="w_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=MagicMock(),
        mask_secrets=with_secret,
    )
    if with_secret:
        context.secrets["k_secret"] = SECRET_VALUE
        context.include_secrets_in_templates = True
    if with_workflow:
        context.workflow = SimpleNamespace(
            enable_self_healing=False,
            workflow_definition=None,
            created_by=created_by,
            edited_by=edited_by,
            workflow_permanent_id="wpid_test",
            organization_id=organization_id,
        )
    return context


class _FakeTask:
    def __init__(
        self,
        task_id: str,
        status: TaskStatus,
        extracted_information: ExtractedInformation | _Unset = _UNSET,
    ) -> None:
        self.task_id = task_id
        self.status = status
        self.extracted_information: ExtractedInformation = (
            {"report": "ok"} if extracted_information is _UNSET else extracted_information
        )
        self.failure_reason: str | None = None if status == TaskStatus.completed else "agent gave up"
        self.errors: list[dict[str, Any]] = []
        self.failure_category: list[dict[str, Any]] | None = None
        self.order = 7
        self.retry = 0


def _install_db_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    final_status: TaskStatus,
    organization: object | None = SimpleNamespace(organization_id="o_test", max_steps_per_run=None),
    downloaded_files: list[FileInfo] | None = None,
    extracted_information: ExtractedInformation | _Unset = _UNSET,
    copilot_lineage: bool = False,
) -> dict[str, Any]:
    created_task = _FakeTask("tsk_escalation", TaskStatus.running)
    updated_after_run = _FakeTask("tsk_escalation", final_status, extracted_information=extracted_information)
    state: dict[str, Any] = {
        "create_task_kwargs": None,
        "execute_step_calls": 0,
        "created_actions": [],
        "recovery_block_kwargs": None,
        "recovery_block_updates": [],
        "workflow_run_block_updates": [],
        "heal_episodes": [],
        "task_actions": [],
        "updated_task": updated_after_run,
    }

    async def _create_task(**kwargs: object) -> _FakeTask:
        state["create_task_kwargs"] = kwargs
        return created_task

    async def _update_task(*args: object, **kwargs: object) -> _FakeTask:
        return created_task

    async def _create_step(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(step_id="stp_1", order=0, retry_index=0)

    async def _get_task(*args: object, **kwargs: object) -> _FakeTask:
        return updated_after_run

    async def _get_organization(*args: object, **kwargs: object) -> object | None:
        return organization

    async def _get_task_order(*args: object, **kwargs: object) -> tuple[int, int]:
        return 8, 0

    async def _execute_step(*args: object, **kwargs: object) -> tuple[None, None, None]:
        state["execute_step_calls"] += 1
        state["execute_step_kwargs"] = kwargs
        return None, None, None

    async def _get_current_attempt_downloaded_files(*args: object, **kwargs: object) -> list[FileInfo]:
        return list(downloaded_files or [])

    async def _create_action(action: Action) -> Action:
        state["created_actions"].append(action)
        return action

    async def _get_task_actions(*args: object, **kwargs: object) -> list[Any]:
        return list(state["task_actions"])

    monkeypatch.setattr(app.DATABASE.tasks, "create_task", AsyncMock(side_effect=_create_task))
    monkeypatch.setattr(app.DATABASE.tasks, "update_task", AsyncMock(side_effect=_update_task))
    monkeypatch.setattr(app.DATABASE.tasks, "update_step", AsyncMock(return_value=None))
    monkeypatch.setattr(app.DATABASE.tasks, "create_step", AsyncMock(side_effect=_create_step))
    monkeypatch.setattr(app.DATABASE.tasks, "get_task", AsyncMock(side_effect=_get_task))
    monkeypatch.setattr(app.DATABASE.tasks, "get_task_actions", AsyncMock(side_effect=_get_task_actions))
    monkeypatch.setattr(app.DATABASE.organizations, "get_organization", AsyncMock(side_effect=_get_organization))
    monkeypatch.setattr(app.DATABASE.workflows, "is_workflow_copilot_authored", AsyncMock(return_value=copilot_lineage))
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.block.BaseTaskBlock.get_task_order",
        AsyncMock(side_effect=_get_task_order),
    )
    monkeypatch.setattr(app.agent, "execute_step", AsyncMock(side_effect=_execute_step))
    monkeypatch.setattr(app.DATABASE.workflow_params, "create_action", AsyncMock(side_effect=_create_action))

    async def _create_workflow_run_block(**kwargs: object) -> SimpleNamespace:
        state["recovery_block_kwargs"] = kwargs
        return SimpleNamespace(workflow_run_block_id="wrb_recovery")

    async def _update_workflow_run_block(**kwargs: object) -> None:
        state["workflow_run_block_updates"].append(kwargs)
        if kwargs.get("workflow_run_block_id") == "wrb_recovery":
            state["recovery_block_updates"].append(kwargs)

    monkeypatch.setattr(
        app.DATABASE.observer, "create_workflow_run_block", AsyncMock(side_effect=_create_workflow_run_block)
    )
    monkeypatch.setattr(
        app.DATABASE.observer, "update_workflow_run_block", AsyncMock(side_effect=_update_workflow_run_block)
    )
    monkeypatch.setattr(
        app.STORAGE,
        "get_current_attempt_downloaded_files",
        AsyncMock(side_effect=_get_current_attempt_downloaded_files),
    )
    monkeypatch.setattr(
        app.DATABASE.workflow_runs, "create_or_update_workflow_run_output_parameter", AsyncMock(return_value=None)
    )

    async def _create_heal_episode(**kwargs: object) -> None:
        state["heal_episodes"].append(kwargs)

    monkeypatch.setattr(app.DATABASE.self_heal, "create_heal_episode", AsyncMock(side_effect=_create_heal_episode))
    return state


def _recording_page(exception: Exception | None, *, url: object = "http://example.test/home") -> MagicMock:
    page = MagicMock()
    page.last_recorded_exception = MagicMock(return_value=exception)
    page.failure_nav_error_code = MagicMock(return_value=None)
    page.failure_document_receipt = MagicMock(return_value=None)
    page.url = url
    return page


def _browser_state() -> MagicMock:
    browser_state = MagicMock()
    browser_state.navigate_to_url = AsyncMock(return_value=None)
    return browser_state


class FakeRecorder:
    instances: list[FakeRecorder] = []
    _next_last_exception: Exception | None = None
    _next_last_failed_nav_error_code: str | None = None

    @classmethod
    def reset(
        cls, *, last_recorded_exception: Exception | None = None, last_failed_nav_error_code: str | None = None
    ) -> None:
        cls.instances = []
        cls._next_last_exception = last_recorded_exception
        cls._next_last_failed_nav_error_code = last_failed_nav_error_code

    def __init__(self, **kwargs: Any) -> None:
        self.recording_page = MagicMock()
        self.recording_page.last_recorded_exception = MagicMock(return_value=self._next_last_exception)
        self.recording_page.failure_nav_error_code = MagicMock(return_value=None)
        self.recording_page.failure_document_receipt = MagicMock(return_value=None)
        self.recording_page.last_failed_nav_error_code = MagicMock(return_value=self._next_last_failed_nav_error_code)
        self._actions: list[Any] = []
        self.finalized_success: bool | None = None
        self.__class__.instances.append(self)

    async def create_task_and_step(self) -> None:
        return None

    async def link_block(self) -> None:
        return None

    def recorded_actions(self) -> list[Any]:
        return list(self._actions)

    def last_recorded_exception(self) -> Exception | None:
        return self._next_last_exception

    async def persist(self, actions: list[Any]) -> None:
        return None

    async def finalize(self, success: bool) -> None:
        if self.finalized_success is None:
            self.finalized_success = success


async def _heal(
    block: CodeBlock,
    context: WorkflowRunContext,
    exception: Exception,
    recording_page: MagicMock,
    *,
    failing_line: int | None = 1,
    browser_state: MagicMock | None = None,
    page: MagicMock | None = None,
    authored_code: str | None = None,
) -> BlockResult | None:
    with skyvern_context.scoped(SkyvernContext(organization_id="o_test", workflow_run_id="wr_test")):
        return await block._attempt_self_heal(
            exception=exception,
            failing_line=failing_line,
            recording_page=recording_page,
            workflow_run_context=context,
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            browser_session_id=None,
            browser_state=browser_state if browser_state is not None else _browser_state(),
            page=page if page is not None else MagicMock(),
            authored_code=authored_code,
        )


def _statuses_for_block(
    state: dict[str, Any],
    workflow_run_block_id: str,
) -> list[BlockStatus]:
    statuses: list[BlockStatus] = []
    for update in state["workflow_run_block_updates"]:
        if update.get("workflow_run_block_id") != workflow_run_block_id:
            continue
        status = update.get("status")
        if isinstance(status, BlockStatus):
            statuses.append(status)
    return statuses


def _patch_execute_chokepoint_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    context: WorkflowRunContext,
    fake_browser_state: object,
    use_codeblock_runner: bool,
    format_templates: bool = False,
) -> None:
    monkeypatch.setattr(CodeBlock, "get_workflow_run_context", MagicMock(return_value=context))
    monkeypatch.setattr(CodeBlock, "get_or_create_browser_state", AsyncMock(return_value=fake_browser_state))
    monkeypatch.setattr(CodeBlock, "_ensure_run_recording_artifact", AsyncMock(return_value=None))
    if not format_templates:
        monkeypatch.setattr(CodeBlock, "format_potential_template_parameters", MagicMock(return_value=None))
    monkeypatch.setattr(app.AGENT_FUNCTION, "validate_code_block", AsyncMock(return_value=None))
    monkeypatch.setattr(app.AGENT_FUNCTION, "should_use_codeblock_runner", AsyncMock(return_value=use_codeblock_runner))
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_for_workflow_run", MagicMock(return_value=fake_browser_state))


async def _execute_inline_failure_with_download_output(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exception: Exception,
    download_output: dict[str, Any] | None,
    error_code_mapping: dict[str, str] | None = None,
    context: WorkflowRunContext | None = None,
    format_templates: bool = False,
    capture_failure_output: bool = False,
) -> tuple[BlockResult, AsyncMock | None, AsyncMock]:
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    code = (
        f"raise ErrorCode({exception.error_code!r}, {exception.reasoning!r})"
        if type(exception) is ErrorCode
        else "raise RuntimeError('processing failed')"
    )
    block = _make_code_block(code=code, error_code_mapping=error_code_mapping)
    context = context or _make_context()
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=fake_page), browser_artifacts=BrowserArtifacts()
    )
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=False,
        format_templates=format_templates,
    )
    if type(exception) is not ErrorCode:
        monkeypatch.setattr(CodeBlock, "execute_user_function_with_timeout", AsyncMock(side_effect=exception))
    monkeypatch.setattr(app.AGENT_FUNCTION, "execute_code_block_override", AsyncMock(return_value=None))
    monkeypatch.setattr(CodeBlock, "_capture_failure_evidence", AsyncMock(return_value=None))
    monkeypatch.setattr(CodeBlock, "_write_heal_episode_safe", AsyncMock(return_value=None))
    record_output = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record_output)

    failure_output_with_downloads = None
    if capture_failure_output:
        monkeypatch.setattr(
            "skyvern.forge.sdk.workflow.models.block.local_download_dir_file_identities",
            MagicMock(side_effect=[set(), {("failure-evidence.txt", 1, 1)}]),
        )
        monkeypatch.setattr(CodeBlock, "_read_back_downloaded_files", AsyncMock(return_value=[]))
        monkeypatch.setattr(CodeBlock, "_register_downloaded_files", AsyncMock(return_value=([], set())))
        monkeypatch.setattr(
            CodeBlock,
            "_bind_and_grade_downloads",
            AsyncMock(return_value=(download_output, None)),
        )
    else:
        failure_output_with_downloads = AsyncMock(return_value=download_output)
        monkeypatch.setattr(CodeBlock, "_failure_output_with_downloads", failure_output_with_downloads)
    FakeRecorder.reset(last_recorded_exception=exception)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )
    return result, failure_output_with_downloads, record_output


@pytest.mark.asyncio
async def test_rendered_error_code_misuse_returns_and_records_failed_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(code="raise ErrorCode({{ arguments }})")
    context = _make_context()
    context.values["arguments"] = "code='A', reasoning='why'"
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=fake_page), browser_artifacts=BrowserArtifacts()
    )
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=False,
        format_templates=True,
    )
    monkeypatch.setattr(app.AGENT_FUNCTION, "execute_code_block_override", AsyncMock(return_value=None))
    monkeypatch.setattr(CodeBlock, "_capture_failure_evidence", AsyncMock(return_value=None))
    monkeypatch.setattr(CodeBlock, "_write_heal_episode_safe", AsyncMock(return_value=None))
    monkeypatch.setattr(CodeBlock, "_attempt_self_heal", AsyncMock(return_value=None))
    record_output = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record_output)
    FakeRecorder.reset()
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )

    assert result.success is False
    recorded_output = record_output.await_args.args[2]
    assert recorded_output["status"] == "failed"
    record_output.assert_awaited_once_with(context, "wr_test", recorded_output)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "mapping", "secret", "template_value", "format_templates"),
    [
        ("{{ credential }}", {"{{ credential }}": "Credential failure"}, SECRET_VALUE, SECRET_VALUE, True),
        ("ERR_abc", {"ERR_abc": "PIN failure"}, SHORT_SECRET_VALUE, None, False),
    ],
    ids=["templated-long-credential", "embedded-short-credential"],
)
async def test_inline_secret_error_code_is_generic_in_persisted_failure_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    mapping: dict[str, str],
    secret: str,
    template_value: str | None,
    format_templates: bool,
) -> None:
    context = _make_context()
    context.secrets["credential"] = secret
    context.include_secrets_in_templates = True
    if template_value is not None:
        context.values["credential"] = template_value

    result, _, record_output = await _execute_inline_failure_with_download_output(
        monkeypatch,
        exception=ErrorCode(error_code, "must stay generic"),
        error_code_mapping=mapping,
        context=context,
        format_templates=format_templates,
        capture_failure_output=True,
        download_output={
            "errors": [
                {
                    "error_code": "user_code_error",
                    "reasoning": "CodeBlock failed while running user code.",
                    "confidence_float": 1.0,
                }
            ],
            "failure_reason": "CodeBlock failed while running user code.",
            "status": "failed",
        },
    )

    persisted_output = record_output.await_args.args[2]
    record_output.assert_awaited_once_with(context, "wr_test", result.output_parameter_value)
    assert result.failure_reason == "Failed to execute code block. Reason: ErrorCode: CodeBlock raised a declared error"
    assert result.error_codes == []
    assert persisted_output == result.output_parameter_value
    assert persisted_output["errors"][0]["error_code"] == "user_code_error"
    assert all(error.get("error_type") != "USER_DEFINED_ERROR" for error in persisted_output["errors"])
    protected_strings = _string_values(
        [result.failure_reason, result.error_codes, result.output_parameter_value, persisted_output]
    )
    if len(secret) < 5:
        # Short secrets can collide with timestamps/IDs in repr; inspect only attacker-visible strings.
        assert all(secret not in text for text in protected_strings)
    else:
        assert secret not in repr(result)
        assert secret not in repr(persisted_output)
        assert secret not in repr(result.error_codes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("secret", "reasoning", "extra_secrets"),
    [
        (SECRET_VALUE, f"credential {SECRET_VALUE} failed", {}),
        ("587", "credential 587 failed", {}),
        ("12", "user retried 12 times", {"card_cvv": "587"}),
    ],
    ids=["templated-long-credential", "short-credential", "short-card-expiry"],
)
async def test_inline_secret_reasoning_is_redacted_in_persisted_failure_artifacts(
    monkeypatch: pytest.MonkeyPatch, secret: str, reasoning: str, extra_secrets: dict[str, str]
) -> None:
    context = _make_context()
    context.secrets["credential"] = secret
    context.secrets.update(extra_secrets)

    result, _, record_output = await _execute_inline_failure_with_download_output(
        monkeypatch,
        exception=ErrorCode("credential_failed", reasoning),
        error_code_mapping={"credential_failed": "Credential failure"},
        context=context,
        download_output=None,
    )

    record_output.assert_awaited_once_with(context, "wr_test", result.output_parameter_value)
    persisted_output = record_output.await_args.args[2]
    expected_reasoning = reasoning.replace(secret, "[redacted]")
    assert result.failure_reason == expected_reasoning
    assert result.error_codes == ["credential_failed"]
    assert persisted_output == result.output_parameter_value
    assert persisted_output["errors"] == [
        {
            "error_code": "credential_failed",
            "reasoning": expected_reasoning,
            "confidence_float": 1.0,
            "error_type": "USER_DEFINED_ERROR",
        }
    ]
    protected_strings = _string_values(
        [result.failure_reason, result.error_codes, result.output_parameter_value, persisted_output]
    )
    if len(secret) < 5:
        # Short secrets can collide with timestamps/IDs in repr; inspect only attacker-visible strings.
        assert all(secret not in text for text in protected_strings)
    else:
        assert secret not in repr(result)
        assert secret not in repr(result.failure_reason)
        assert secret not in repr(persisted_output)
        assert secret not in repr(result.error_codes)
    # Short extra secrets can collide with timestamps/IDs in repr; inspect only attacker-visible strings.
    assert all(value not in text for value in extra_secrets.values() for text in protected_strings)


@pytest.mark.asyncio
async def test_inline_declared_error_failure_preserves_download_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    typed_error = {
        "error_code": "report_unavailable",
        "reasoning": "report generation failed",
        "confidence_float": 1.0,
        "error_type": "USER_DEFINED_ERROR",
    }
    download_output = {
        "errors": [typed_error],
        "downloaded_file_urls": ["https://files.test/report.pdf"],
        "downloaded_file_artifact_ids": ["art_report"],
        "failure_reason": "report generation failed",
        "status": "failed",
    }

    result, failure_output_with_downloads, record_output = await _execute_inline_failure_with_download_output(
        monkeypatch,
        exception=ErrorCode("report_unavailable", "report generation failed"),
        download_output=download_output,
        error_code_mapping={"report_unavailable": "The report could not be generated"},
    )

    assert result.output_parameter_value == download_output
    assert result.output_parameter_value["errors"] == [typed_error]
    assert result.error_codes == ["report_unavailable"]
    assert failure_output_with_downloads.await_args.kwargs["result"]["errors"] == [typed_error]
    record_output.assert_not_awaited()


@pytest.mark.asyncio
async def test_inline_declared_error_without_download_keeps_typed_output(monkeypatch: pytest.MonkeyPatch) -> None:
    result, failure_output_with_downloads, record_output = await _execute_inline_failure_with_download_output(
        monkeypatch,
        exception=ErrorCode("report_unavailable", "report generation failed"),
        download_output=None,
        error_code_mapping={"report_unavailable": "The report could not be generated"},
    )

    expected_output = {
        "errors": [
            {
                "error_code": "report_unavailable",
                "reasoning": "report generation failed",
                "confidence_float": 1.0,
                "error_type": "USER_DEFINED_ERROR",
            }
        ],
        "failure_reason": "report generation failed",
        "status": "failed",
        # SKY-15564: the run inherits the declared code instead of the classifier's UNKNOWN.
        "failure_category": [
            {
                "category": "report_unavailable",
                "confidence_float": 1.0,
                "reasoning": "report generation failed",
            }
        ],
        # Names the code as the author's own, so a reader can tell it from a driver's verdict.
        "declared_error_code": "report_unavailable",
    }
    assert result.output_parameter_value == expected_output
    assert "downloaded_files" not in result.output_parameter_value
    assert failure_output_with_downloads.await_args.kwargs["result"]["errors"] == expected_output["errors"]
    record_output.assert_awaited_once()
    assert record_output.await_args.args[1:] == ("wr_test", expected_output)


@pytest.mark.asyncio
async def test_inline_ordinary_exception_failure_still_preserves_download_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    download_output = {
        "downloaded_file_urls": ["https://files.test/report.pdf"],
        "downloaded_file_artifact_ids": ["art_report"],
    }

    result, failure_output_with_downloads, _ = await _execute_inline_failure_with_download_output(
        monkeypatch,
        exception=RuntimeError("processing failed"),
        download_output=download_output,
    )

    assert result.output_parameter_value == download_output
    assert failure_output_with_downloads.await_args.kwargs.get("result") is None


@pytest.mark.asyncio
async def test_everything_off_is_no_op(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag(None)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is None
    assert state["execute_step_calls"] == 0


@pytest.mark.asyncio
async def test_legacy_workflow_toggle_does_not_enable_heal_when_org_flag_off(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag(None)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, copilot_lineage=True)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")
    context = _make_context()
    context.workflow.enable_self_healing = True

    result = await _heal(block, context, exc, _recording_page(exc))

    assert result is None
    assert state["execute_step_calls"] == 0


@pytest.mark.asyncio
async def test_hand_authored_workflow_runs_the_ai_fallback_on_task_v3(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, extracted_information={})
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")
    context = _make_context(created_by="user@example.com", edited_by="user@example.com")

    result = await _heal(block, context, exc, _recording_page(exc))

    assert result is not None
    assert result.output_parameter_value == {}
    assert state["execute_step_calls"] == 1
    assert state["execute_step_kwargs"]["engine"] is RunEngine.skyvern_v3
    # Without this the engine sizes the run like a bare task: the org's caps stop binding it and
    # it stops following popups and reaching into child frames.
    assert state["execute_step_kwargs"]["workflow_owned_recovery"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("flag_on", [True, False])
@pytest.mark.parametrize("legacy_toggle", [True, False])
@pytest.mark.parametrize("copilot_authored", [True, False])
async def test_ai_fallback_enabled_follows_only_the_org_flag(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
    flag_on: bool,
    legacy_toggle: bool,
    copilot_authored: bool,
) -> None:
    ai_fallback_flag("o_test" if flag_on else None)
    monkeypatch.setattr(
        app.DATABASE.workflows, "is_workflow_copilot_authored", AsyncMock(return_value=copilot_authored)
    )
    created_by = "copilot" if copilot_authored else "user@example.com"
    context = _make_context(created_by=created_by, edited_by=created_by)
    context.workflow.enable_self_healing = legacy_toggle

    assert await _make_code_block()._ai_fallback_enabled(context) is flag_on


@pytest.mark.asyncio
@pytest.mark.parametrize(("workflow_org", "expected"), [("o_test", False), ("o_other", True)])
async def test_ai_fallback_enabled_flag_follows_the_workflow_org(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
    workflow_org: str,
    expected: bool,
) -> None:
    ai_fallback_flag("o_other")
    context = _make_context(organization_id=workflow_org)

    assert await _make_code_block()._ai_fallback_enabled(context) is expected


@pytest.mark.asyncio
async def test_ai_fallback_enabled_flag_provider_raises_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", AsyncMock(side_effect=RuntimeError("posthog down"))
    )

    assert await _make_code_block()._ai_fallback_enabled(_make_context()) is False


@pytest.mark.asyncio
async def test_lineage_lookup_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.DATABASE.workflows,
        "is_workflow_copilot_authored",
        AsyncMock(side_effect=RuntimeError("db unavailable")),
    )
    context = _make_context(created_by="user@example.com", edited_by="user@example.com")

    assert await _make_code_block()._workflow_is_copilot_authored(context) is False


@pytest.mark.asyncio
async def test_missing_workflow_on_context_never_heals(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(with_workflow=False), exc, _recording_page(exc))

    assert result is None
    assert state["execute_step_calls"] == 0


# --- The spine fix: a rotted page failure heals on block.prompt even with no covering step. ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "steps, failing_line",
    [
        pytest.param(None, 1, id="no_steps"),
        pytest.param([CodeBlockStep(description="d", line_start=50, line_end=60)], 1, id="unmatched_step"),
        pytest.param([CodeBlockStep(description="d", line_start=1, line_end=1)], None, id="null_failing_line"),
        pytest.param([CodeBlockStep(description=None, line_start=1, line_end=1)], 1, id="step_without_description"),
    ],
)
async def test_prompt_only_heal_fires_without_a_matched_step(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
    steps: list[CodeBlockStep] | None,
    failing_line: int | None,
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=steps)
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=failing_line)

    assert result is not None and result.success is True
    assert state["execute_step_calls"] == 1
    assert state["create_task_kwargs"]["navigation_goal"] == DEFAULT_PROMPT
    assert state["execute_step_kwargs"]["recovery_code_progress"] is None


@pytest.mark.asyncio
async def test_matched_step_is_a_record_beside_the_goal_not_part_of_it(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="click the export button", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=1)

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["navigation_goal"] == DEFAULT_PROMPT
    assert state["execute_step_kwargs"]["recovery_code_progress"] == CodeProgressRecord(
        before=(), failed_step="click the export button", failed_line=1, after=()
    )


_FORM_CODE = (
    "await page.goto('https://form.example.test/contact')\n"
    "await page.locator('#contact-form').wait_for(timeout=5000)\n"
    "await page.get_by_label('Full name').fill('Ada Lovelace')\n"
    "await page.fill('#contact-email', 'ada@example.test')\n"
    "await page.locator('#phone').fill('')\n"
)
_FORM_TYPED = [
    (3, "page.get_by_label('Full name')", "Ada Lovelace"),
    (4, "#contact-email", "ada@example.test"),
    (5, "page.locator('#phone')", ""),
]
_CONTROL_CODE = (
    "await page.goto('https://form.example.test/booking')\n"
    "await page.locator('#booking-form').wait_for(timeout=5000)\n"
    "await page.get_by_label('Passenger name').fill('Grace Hopper')\n"
    "await page.fill('#passenger-email', 'grace@example.test')\n"
    "await page.locator('#mobile').fill('')\n"
)
_CARD_CODE = (
    "await page.goto('https://form.example.test/pay')\n"
    "await page.locator('#pay-form').wait_for(timeout=5000)\n"
    "await page.get_by_label('Cardholder').fill('Grace Hopper')\n"
    f"await page.get_by_label('Password').fill('{SECRET_VALUE}')\n"
    f"await page.fill('#cvv', '{CVV_VALUE}')\n"
    f"await page.fill('#note', 'card code {CVV_VALUE}')\n"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "authored_code", "failing_line", "typed", "expect_record", "expect_failed_step"),
    [
        pytest.param(_FORM_CODE, None, 3, _FORM_TYPED, True, True, id="matched_fill_line"),
        pytest.param(_FORM_CODE, None, 2, _FORM_TYPED, True, False, id="unmatched_wait_line"),
        pytest.param(
            _CONTROL_CODE,
            None,
            2,
            [
                (3, "page.get_by_label('Passenger name')", "Grace Hopper"),
                (4, "#passenger-email", "grace@example.test"),
                (5, "page.locator('#mobile')", ""),
            ],
            True,
            False,
            id="renamed_control",
        ),
        pytest.param(
            _CARD_CODE, None, 2, [(3, "page.get_by_label('Cardholder')", "Grace Hopper")], True, False, id="secrets"
        ),
        pytest.param(
            _FORM_CODE.replace("'Ada Lovelace'", "'{{ contact_name }}'"),
            None,
            2,
            _FORM_TYPED[1:],
            True,
            False,
            id="jinja_slot",
        ),
        pytest.param(_FORM_CODE, "\n" + _FORM_CODE, 2, [], False, False, id="line_count_mismatch"),
    ],
)
async def test_typed_values_reach_the_code_outline_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
    code: str,
    authored_code: str | None,
    failing_line: int,
    typed: list[tuple[int, str, str]],
    expect_record: bool,
    expect_failed_step: bool,
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(code=code, steps=[CodeBlockStep(**step) for step in derive_code_block_steps(code)])
    context = _make_context(with_secret=True)
    context.secrets["k_cvv"] = CVV_VALUE
    context.values["contact_name"] = "Ada Lovelace"
    exc = RuntimeError("rotted selector")

    result = await _heal(
        block,
        context,
        exc,
        _recording_page(exc),
        failing_line=failing_line,
        authored_code=authored_code or block.render_code_with_inert_slots(code, context),
    )

    assert result is not None
    kwargs = state["create_task_kwargs"]
    assert kwargs["navigation_goal"] == DEFAULT_PROMPT
    assert kwargs["navigation_payload"] == {}
    record = state["execute_step_kwargs"]["recovery_code_progress"]
    assert (record is not None) == expect_record
    assert (record is not None and record.failed_step is not None) == expect_failed_step
    typed_values = record.typed_values if record is not None else ()
    assert [(value.line, value.target, value.value) for value in typed_values] == typed
    outline = (
        "\n".join([render_code_progress_section(record), *typed_value_rows(typed_values, token_budget=10**9)])
        if record is not None
        else ""
    )
    for _, target, value in typed:
        assert f"{json.dumps(value)} into {json.dumps(target)}" in outline
    assert '"*****"' not in outline
    for secret in (SECRET_VALUE, CVV_VALUE):
        assert secret not in outline and secret not in str(kwargs)


@pytest.mark.asyncio
async def test_a_credential_parameter_typed_by_the_code_never_reaches_the_outline(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    code = (
        "await page.locator('#login-form').wait_for(timeout=5000)\n"
        "await page.fill('#username', 'ada@example.test')\n"
        "await page.fill('#password', '{{ portal_credential.password }}')\n"
    )
    block = _make_code_block(code=code, steps=[CodeBlockStep(**step) for step in derive_code_block_steps(code)])
    credential = _credential_id_workflow_parameter()
    block.parameters = [credential]
    context = _make_context(with_secret=True)
    context.parameters[credential.key] = credential
    context.values[credential.key] = {"username": "secret_1_username", "password": "k_secret"}
    authored_code = block.render_code_with_inert_slots(code, context)
    exc = RuntimeError("rotted selector")

    await _heal(block, context, exc, _recording_page(exc), failing_line=1, authored_code=authored_code)

    kwargs = state["create_task_kwargs"]
    record = state["execute_step_kwargs"]["recovery_code_progress"]
    assert [(value.line, value.target, value.value) for value in record.typed_values] == [
        (2, "#username", "ada@example.test")
    ]
    assert kwargs["navigation_payload"][credential.key]["password"] == "k_secret"
    assert SECRET_VALUE not in str(kwargs)
    assert SECRET_VALUE not in render_code_progress_section(record)
    assert "*****" not in render_code_progress_section(record)


_TEMPLATED_FORM_CODE = (
    "await page.fill('#contact-email', 'ada@example.test')\nawait page.fill('#contact-name', '{{ contact_name }}')\n"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["inline", "secure"])
async def test_executed_block_lists_its_literal_fill_but_never_a_rendered_parameter_value(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None], engine: str
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.failed)
    block = _make_code_block(code=_TEMPLATED_FORM_CODE)
    context = _make_context()
    context.values["contact_name"] = "Grace Param"
    fake_browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=MagicMock()),
        browser_artifacts=BrowserArtifacts(),
        navigate_to_url=AsyncMock(return_value=None),
    )
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=engine == "secure",
        format_templates=True,
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "execute_code_block_override",
        AsyncMock(
            return_value=CodeBlockEngineResult(
                block_result=None,
                failure=CodeBlockEngineFailure(
                    error_code="user_code_error",
                    safe_message=None,
                    failure_reason="CodeBlock failed while running user code.",
                    exception_class="playwright._impl._errors.TimeoutError",
                    failing_line=1,
                    healability_hint=True,
                ),
            )
            if engine == "secure"
            else None
        ),
    )
    monkeypatch.setattr(CodeBlock, "_is_healable_page_failure", MagicMock(return_value=True))
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    FakeRecorder.reset(last_recorded_exception=None)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)

    with skyvern_context.scoped(SkyvernContext(organization_id="o_test", workflow_run_id="wr_test")):
        await block.execute(
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            browser_session_id="pbs_test",
        )

    assert "Grace Param" in block.code
    record = state["execute_step_kwargs"]["recovery_code_progress"]
    assert record is not None
    assert [(value.line, value.target, value.value) for value in record.typed_values] == [
        (1, "#contact-email", "ada@example.test")
    ]
    assert "Grace Param" not in render_code_progress_section(record)


@pytest.mark.parametrize("prompt", [None, ""], ids=["absent_goal", "empty_goal"])
def test_steps_alone_never_manufacture_a_heal_goal(prompt: str | None) -> None:
    # The harness heal path composes its goal without the floor path's `if not self.prompt`
    # gate, so a goal-less block carrying a code-derived step outline (what the MCP seam and
    # the recording converter both emit) would otherwise send the recovery agent at a live
    # page with a bare "click the export button" and no context.
    block = _make_code_block(
        prompt=prompt,
        steps=[CodeBlockStep(description="click the export button", line_start=1, line_end=1)],
    )
    context = _make_context()

    assert block._compose_heal_goal(workflow_run_context=context) == ""


def test_failing_goto_heals_toward_its_own_url_not_an_address_in_the_step_outline() -> None:
    block = _make_code_block(
        code="await page.goto('https://example.com/a')\nawait page.goto('https://example.com/b')\n",
        steps=[
            CodeBlockStep(description="Open https://example.com/a", line_start=1, line_end=1),
            CodeBlockStep(description="Open https://example.com/b", line_start=2, line_end=2),
        ],
    )

    assert block._derive_escalation_navigation_url(2, _recording_page(None)) == "https://example.com/b"


@pytest.mark.asyncio
async def test_failing_static_goto_sets_escalation_url(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    target_url = "https://dead-nav.example.com/login"
    block = _make_code_block(code=f'await page.goto("{target_url}")')
    exc = RuntimeError("navigation failed")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == target_url


@pytest.mark.asyncio
async def test_failing_goto_with_variable_keeps_empty_escalation_url(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(
        code="""
target = "https://dead-nav.example.com/login"
await page.goto(target)
""".strip()
    )
    exc = RuntimeError("navigation failed")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=2)

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == ""


@pytest.mark.asyncio
async def test_failing_goto_with_keyword_url_sets_escalation_url(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    target_url = "https://dead-nav.example.com/login"
    block = _make_code_block(code=f'await page.goto(url="{target_url}")')
    exc = RuntimeError("navigation failed")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == target_url


@pytest.mark.asyncio
async def test_failing_goto_wrapped_across_lines_sets_escalation_url(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    target_url = "https://dead-nav.example.com/login"
    block = _make_code_block(
        code=f'''await page.goto(
    "{target_url}",
    wait_until="domcontentloaded",
)'''
    )
    exc = RuntimeError("navigation failed")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=1)

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == target_url


@pytest.mark.asyncio
async def test_failing_goto_wrapped_with_url_keyword_sets_escalation_url(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    target_url = "https://dead-nav.example.com/login"
    block = _make_code_block(
        code=f'''await page.goto(
    url="{target_url}",
    wait_until="domcontentloaded",
)'''
    )
    exc = RuntimeError("navigation failed")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=1)

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == target_url


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_line", [5, 6, 8], ids=["call_line", "url_line", "closing_line"])
async def test_two_wrapped_gotos_uses_second_when_failing(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None], failing_line: int
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    first_url = "https://first.example.com/"
    second_url = "https://second.example.com/"
    block = _make_code_block(
        code=f'''await page.goto(
    "{first_url}",
    wait_until="domcontentloaded",
)
await page.goto(
    "{second_url}",
    wait_until="domcontentloaded",
)'''
    )
    exc = RuntimeError("navigation failed")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=failing_line)

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == second_url


@pytest.mark.asyncio
async def test_error_page_seat_uses_wrapped_preceding_goto(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    target_url = "https://wrapped-goto.example.com/login"
    block = _make_code_block(
        code=f'''await page.goto("https://stale-first.example.com/old")
await page.goto(
    "{target_url}",
    wait_until="domcontentloaded",
)
await page.click("#download")'''
    )
    exc = RuntimeError("click failed after dead nav")

    result = await _heal(
        block,
        _make_context(),
        exc,
        _recording_page(exc, url="chrome-error://chromewebdata/"),
        failing_line=6,
    )

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == target_url


@pytest.mark.asyncio
async def test_failing_goto_wrapped_with_variable_keeps_empty_escalation_url(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(
        code="""
target = "https://dead-nav.example.com/login"
await page.goto(
    target,
    wait_until="domcontentloaded",
)
""".strip()
    )
    exc = RuntimeError("navigation failed")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=3)

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == ""


@pytest.mark.asyncio
async def test_failing_goto_with_dynamic_keyword_url_keeps_empty_escalation_url(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(
        code="""
target = "https://dead-nav.example.com/login"
await page.goto(url=target)
""".strip()
    )
    exc = RuntimeError("navigation failed")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=2)

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target_url", ["ftp://files.example.com/report.csv", "https:///no-host", "/relative/path", "https://[bad"]
)
async def test_failing_wrapped_goto_without_an_http_host_keeps_empty_escalation_url(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None], target_url: str
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(
        code=f'''await page.goto(
    "{target_url}",
    wait_until="domcontentloaded",
)'''
    )
    exc = RuntimeError("navigation failed")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=2)

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "failing_line"),
    [
        ('    await page.goto("https://dead-nav.example.com/login")', 1),
        (
            """    await page.goto(
        "https://dead-nav.example.com/login",
        wait_until="domcontentloaded",
    )""",
            2,
        ),
    ],
    ids=["single_line", "wrapped"],
)
async def test_indented_block_goto_sets_escalation_url(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None], code: str, failing_line: int
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(code=code)
    exc = RuntimeError("navigation failed")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=failing_line)

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == "https://dead-nav.example.com/login"


@pytest.mark.asyncio
async def test_malformed_port_goto_with_failed_navigation_still_escalates(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    target_url = "https://example.com:bad/x?token=secret"
    block = _make_code_block(code=f'await page.goto("{target_url}")')
    exc = RuntimeError("navigation failed")
    browser_state = _browser_state()
    browser_state.navigate_to_url = AsyncMock(side_effect=RuntimeError("net::ERR_NAME_NOT_RESOLVED"))

    with capture_logs() as logs:
        result = await _heal(block, _make_context(), exc, _recording_page(exc), browser_state=browser_state)

    assert result is not None
    assert state["create_task_kwargs"]["url"] == target_url
    nav_failed = [e for e in logs if e["event"].startswith("Self-heal dead-nav escalation navigation failed")]
    assert [e["escalation_host"] for e in nav_failed] == ["example.com"]
    assert "secret" not in json.dumps(nav_failed, default=str)


@pytest.mark.asyncio
async def test_element_rot_failure_never_sets_escalation_url(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(code="await page.click('#download')")
    exc = RuntimeError("click failed")

    result = await _heal(
        block,
        _make_context(),
        exc,
        _recording_page(exc, url="https://app.example.com/dashboard"),
    )

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == ""


@pytest.mark.asyncio
async def test_error_page_seat_uses_first_static_goto_url(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    target_url = "https://dead-nav.example.com/login"
    block = _make_code_block(
        code=f"""
await page.goto("{target_url}")
await page.click("#download")
""".strip()
    )
    exc = RuntimeError("click failed after dead nav")

    result = await _heal(
        block,
        _make_context(),
        exc,
        _recording_page(exc, url="chrome-error://chromewebdata/"),
        failing_line=2,
    )

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == target_url


@pytest.mark.asyncio
async def test_error_page_seat_skips_dynamic_goto_to_reach_later_static_url(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    target_url = "https://dead-nav.example.com/login"
    block = _make_code_block(
        code=f"""
target = build_url()
await page.goto(target)
await page.click("#step1")
await page.goto("{target_url}")
await page.click("#download")
""".strip()
    )
    exc = RuntimeError("click failed after dead nav")

    result = await _heal(
        block,
        _make_context(),
        exc,
        _recording_page(exc, url="chrome-error://chromewebdata/"),
        failing_line=5,
    )

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == target_url


@pytest.mark.asyncio
async def test_error_page_seat_uses_nearest_preceding_goto_not_first_in_block(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
) -> None:
    """A multi-navigation block must seat recovery on the page the failure actually landed on
    (the nearest preceding goto), not the first static goto anywhere in the block — an earlier
    stale goto and a not-yet-executed later goto are both wrong answers here."""
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    stale_url = "https://stale-first.example.com/old"
    correct_url = "https://correct-recovery.example.com/target"
    never_executed_url = "https://never-executed.example.com/later"
    block = _make_code_block(
        code=f"""
await page.goto("{stale_url}")
await page.click("#step1")
await page.goto("{correct_url}")
await page.click("#step2")
await page.goto("{never_executed_url}")
""".strip()
    )
    exc = RuntimeError("click failed after dead nav")

    result = await _heal(
        block,
        _make_context(),
        exc,
        _recording_page(exc, url="chrome-error://chromewebdata/"),
        failing_line=4,
    )

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == correct_url


@pytest.mark.asyncio
async def test_dead_nav_seat_navigates_live_page_before_escalation_runs(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    """escalation_task.url alone does not reliably trigger navigation (BrowserManager can early-return
    a cached browser state without reading it), so the heal must drive the live browser_state/page
    directly for a dead-nav seat."""
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    target_url = "https://dead-nav.example.com/login"
    block = _make_code_block(code=f'await page.goto("{target_url}")')
    exc = RuntimeError("navigation failed")
    browser_state = _browser_state()
    live_page = MagicMock(name="live_page")

    result = await _heal(
        block,
        _make_context(),
        exc,
        _recording_page(exc),
        browser_state=browser_state,
        page=live_page,
    )

    assert result is not None and result.success is True
    browser_state.navigate_to_url.assert_awaited_once_with(page=live_page, url=target_url)


@pytest.mark.asyncio
async def test_dead_host_goto_leaves_the_goal_url_to_the_recovery_model(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    real_url = "http://localhost:8900/telco_billing/northwind/"
    block = _make_code_block(
        code='await page.goto("http://localhost:65531/")',
        prompt=f"Open the billing portal at {real_url} and sign in",
    )
    exc = RuntimeError("net::ERR_CONNECTION_REFUSED")
    browser_state = _browser_state()
    live_page = MagicMock(name="live_page")

    result = await _heal(
        block,
        _make_context(),
        exc,
        _recording_page(exc),
        browser_state=browser_state,
        page=live_page,
    )

    assert result is not None and result.success is True
    browser_state.navigate_to_url.assert_awaited_once_with(page=live_page, url="http://localhost:65531/")
    assert state["create_task_kwargs"]["navigation_goal"] == block.prompt


@pytest.mark.asyncio
async def test_element_rot_seat_never_navigates_live_page(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    """Element-rot heals must preserve same-session SPA state (gauntlet H8 invariant); the direct
    navigate call added for dead-nav seats must not fire when no recovery URL was derived."""
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(code="await page.click('#download')")
    exc = RuntimeError("click failed")
    browser_state = _browser_state()

    result = await _heal(
        block,
        _make_context(),
        exc,
        _recording_page(exc, url="https://app.example.com/dashboard"),
        browser_state=browser_state,
    )

    assert result is not None and result.success is True
    browser_state.navigate_to_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_derived_escalation_url_is_not_added_to_goal_text(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    target_url = "https://dead-nav.example.com/login"
    block = _make_code_block(code=f'await page.goto("{target_url}")')
    exc = RuntimeError("navigation failed")

    state_with_url = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    result_with_url = await _heal(block, _make_context(), exc, _recording_page(exc))
    goal_with_url = state_with_url["create_task_kwargs"]["navigation_goal"]

    assert result_with_url is not None and result_with_url.success is True
    assert target_url not in goal_with_url

    element_rot_block = _make_code_block(code="await page.click('#missing')")
    state_no_url = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    result_no_url = await _heal(
        element_rot_block,
        _make_context(),
        exc,
        _recording_page(exc, url="https://app.example.com/dashboard"),
    )

    assert result_no_url is not None and result_no_url.success is True
    assert state_no_url["create_task_kwargs"]["url"] == ""
    assert goal_with_url == state_no_url["create_task_kwargs"]["navigation_goal"]


@pytest.mark.asyncio
async def test_goto_inside_comment_or_string_never_sets_escalation_url(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    multi_line_string = '''note = """
await page.goto("https://evil.example.com")
"""
await page.click("#download")'''
    for code, failing_line, page_url in (
        ('await page.click("#download")  # retry via .goto("https://evil.example.com")', 1, "http://example.test/home"),
        ("await page.click('.goto(\"https://evil.example.com\")')", 1, "http://example.test/home"),
        (multi_line_string, 2, "http://example.test/home"),
        (multi_line_string, 4, "chrome-error://chromewebdata/"),
    ):
        block = _make_code_block(code=code)
        exc = RuntimeError("click failed")
        state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)

        result = await _heal(block, _make_context(), exc, _recording_page(exc, url=page_url), failing_line=failing_line)

        assert result is not None and result.success is True
        assert state["create_task_kwargs"]["url"] == ""


@pytest.mark.asyncio
async def test_unmapped_playwright_error_is_healed(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    # An unmapped Playwright call's exception is never registered as last_recorded_exception, but a
    # Playwright page error is still genuine page drift the type-classifier must catch (CORR-10).
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = PlaywrightTimeoutError("locator.click: Timeout 30000ms exceeded")

    result = await _heal(block, _make_context(), exc, _recording_page(None))

    assert result is not None and result.success is True
    assert state["execute_step_calls"] == 1


@pytest.mark.asyncio
async def test_deliberate_raise_is_not_healed(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    raised = ValueError("business logic refused")

    result = await _heal(block, _make_context(), raised, _recording_page(None))

    assert result is None
    assert state["execute_step_calls"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, organization",
    [
        pytest.param({"prompt": None}, SimpleNamespace(organization_id="o_test"), id="no_prompt"),
        pytest.param({}, None, id="no_organization"),
    ],
)
async def test_no_op_guards_skip_escalation(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
    kwargs: dict[str, Any],
    organization: object | None,
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, organization=organization)
    block = _make_code_block(steps=[CodeBlockStep(description="d", line_start=1, line_end=1)], **kwargs)
    exc = RuntimeError("rotted selector")

    assert await _heal(block, _make_context(), exc, _recording_page(exc)) is None
    assert state["execute_step_calls"] == 0


@pytest.mark.asyncio
async def test_completed_heal_maps_to_success(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record)
    block = _make_code_block(steps=[CodeBlockStep(description="download the report", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None
    assert result.success is True
    assert result.status == BlockStatus.completed
    assert state["execute_step_calls"] == 1
    record.assert_awaited_once()


@pytest.mark.asyncio
async def test_completed_heal_records_extracted_information(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record)
    state = _install_db_fakes(
        monkeypatch,
        final_status=TaskStatus.completed,
        extracted_information={"order_total": "42.50", "currency": "USD"},
    )
    schema = {"type": "object", "properties": {"order_total": {"type": "string"}, "currency": {"type": "string"}}}
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the order total", line_start=1, line_end=1)],
        prompt="Read the order total and currency",
        data_schema=schema,
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None
    assert result.output_parameter_value == {"order_total": "42.50", "currency": "USD"}
    assert not {"task_id", "status"} & set(result.output_parameter_value)
    assert record.await_args.args[2] == {"order_total": "42.50", "currency": "USD"}
    create_task_kwargs = state["create_task_kwargs"]
    assert create_task_kwargs["extracted_information_schema"] == schema
    goal = create_task_kwargs["data_extraction_goal"]
    assert "Read the order total and currency" in goal
    assert "order_total" in goal
    assert "currency" in goal
    # The block fails on an absent key, so the goal must offer the route a legitimate miss takes.
    assert "empty value" in goal
    assert "empty object" not in goal


@pytest.mark.parametrize("extracted", [{"total": "1"}, "oops", 3])
@pytest.mark.asyncio
async def test_a_non_list_answer_to_an_array_schema_fails_the_block(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None], extracted: object
) -> None:
    ai_fallback_flag("o_test")
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record)
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, extracted_information=extracted)
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the invoice rows", line_start=1, line_end=1)],
        prompt="Read every invoice row",
        data_schema={"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}}},
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is False
    assert result.status is BlockStatus.failed
    record.assert_not_awaited()


_NESTED_REQUIRED_SCHEMA = {
    "type": "object",
    "properties": {
        "invoice": {
            "type": "object",
            "properties": {"total": {"type": "string"}, "note": {"type": "string"}},
            "required": ["total"],
        }
    },
    "required": ["invoice"],
}


@pytest.mark.asyncio
async def test_a_required_key_behind_a_ref_is_still_demanded(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, extracted_information={"invoice": {}})
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the invoice", line_start=1, line_end=1)],
        prompt="Read the invoice total",
        data_schema={
            "type": "object",
            "$defs": {
                "invoice": {
                    "type": "object",
                    "properties": {"total": {"type": "string"}},
                    "required": ["total"],
                }
            },
            "properties": {"invoice": {"$ref": "#/$defs/invoice"}},
            "required": ["invoice"],
        },
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is False
    record.assert_not_awaited()
    assert "invoice.total" in state["create_task_kwargs"]["data_extraction_goal"]


@pytest.mark.asyncio
async def test_a_required_nested_key_left_absent_fails_the_block(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, extracted_information={"invoice": {}})
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the invoice", line_start=1, line_end=1)],
        prompt="Read the invoice total",
        data_schema=_NESTED_REQUIRED_SCHEMA,
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is False
    assert result.status is BlockStatus.failed
    record.assert_not_awaited()
    # The route to a passing answer has to be named, or the rejection ships without its satisfaction path.
    assert "invoice.total" in state["create_task_kwargs"]["data_extraction_goal"]


@pytest.mark.parametrize("extracted", [{"invoice": {"total": ""}}, {"invoice": {"total": "$8.10"}}])
@pytest.mark.asyncio
async def test_a_required_nested_key_answered_even_emptily_completes(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None], extracted: object
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, extracted_information=extracted)
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the invoice", line_start=1, line_end=1)],
        prompt="Read the invoice total",
        data_schema=_NESTED_REQUIRED_SCHEMA,
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    # `note` is optional and absent; only what the author marked required is demanded.
    assert result is not None and result.success is True
    assert result.output_parameter_value == extracted


@pytest.mark.parametrize("extracted", [[], {"invoice": {}}, {"invoice": None}])
@pytest.mark.asyncio
async def test_an_empty_answer_at_the_declared_level_still_completes(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None], extracted: object
) -> None:
    """An empty list, an empty nested object and a null nested value are answers, not absences.

    These schemas mark nothing `required`, so nothing below the root is demanded: a property the
    author left optional stays optional.
    """
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, extracted_information=extracted)
    schema = (
        {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}}}
        if isinstance(extracted, list)
        else {
            "type": "object",
            "properties": {"invoice": {"type": "object", "properties": {"total": {"type": "string"}}}},
        }
    )
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the invoice", line_start=1, line_end=1)],
        prompt="Read the invoice",
        data_schema=schema,
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    assert result.output_parameter_value == extracted


@pytest.mark.asyncio
async def test_a_list_returning_block_keeps_its_array_schema(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    rows = [{"name": "ACME", "amount": "$8.10"}]
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, extracted_information=rows)
    array_schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "amount": {"type": "string"}},
            "required": ["name"],
        },
    }
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the invoice rows", line_start=1, line_end=1)],
        prompt="Read every invoice row",
        data_schema=array_schema,
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    sent_schema = state["create_task_kwargs"]["extracted_information_schema"]
    assert sent_schema is not None and sent_schema["type"] == "array"
    assert "required" not in json.dumps(sent_schema)
    goal = state["create_task_kwargs"]["data_extraction_goal"]
    assert "list" in goal
    assert "empty object" not in goal
    assert result is not None and result.success is True
    assert result.output_parameter_value == rows


@pytest.mark.asyncio
async def test_a_ref_to_a_definition_named_required_still_resolves(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, extracted_information={"signature": "ok"})
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the signature", line_start=1, line_end=1)],
        prompt="Read the signature",
        data_schema={
            "type": "object",
            "$defs": {"required": {"type": "string"}},
            "properties": {"signature": {"$ref": "#/$defs/required"}},
            "required": ["signature"],
        },
    )
    exc = RuntimeError("rotted selector")

    await _heal(block, _make_context(), exc, _recording_page(exc))

    sent_schema = state["create_task_kwargs"]["extracted_information_schema"]
    # Deleting the definition would strand the $ref and make validation fall back to the raw value.
    assert sent_schema["$defs"] == {"required": {"type": "string"}}
    assert validate_schema(sent_schema) is True
    assert "required" not in sent_schema


@pytest.mark.asyncio
async def test_a_declared_property_named_required_survives_the_strip(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(
        monkeypatch, final_status=TaskStatus.completed, extracted_information={"required": "signature"}
    )
    block = _make_code_block(
        steps=[CodeBlockStep(description="read what the form requires", line_start=1, line_end=1)],
        prompt="Read what the form requires",
        data_schema={
            "type": "object",
            "properties": {"required": {"type": "string"}},
            "required": ["required"],
        },
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    sent_schema = state["create_task_kwargs"]["extracted_information_schema"]
    assert sent_schema["properties"] == {"required": {"type": "string"}}
    assert "required" not in sent_schema
    assert result is not None and result.success is True


_ORDERS_ROWS_REQUIRE_SKU = {
    "type": "object",
    "properties": {
        "orders": {
            "type": "array",
            "items": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]},
        }
    },
    "required": ["orders"],
}
_OPTIONAL_PARENT_REQUIRES_TOTAL = {
    "type": "object",
    "properties": {"invoice": {"type": "object", "properties": {"total": {"type": "string"}}, "required": ["total"]}},
}
_NULLABLE_ARRAY_ROWS_REQUIRE_SKU = {
    "type": ["array", "null"],
    "items": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]},
}


async def _heal_with_schema(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None], schema: object, extracted: object
) -> tuple[BlockResult | None, dict]:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, extracted_information=extracted)
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the page", line_start=1, line_end=1)],
        prompt="Read the page",
        data_schema=schema,
    )
    exc = RuntimeError("rotted selector")
    return await _heal(block, _make_context(), exc, _recording_page(exc)), state


@pytest.mark.asyncio
async def test_a_row_under_an_object_property_owes_its_required_field(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    result, state = await _heal_with_schema(monkeypatch, ai_fallback_flag, _ORDERS_ROWS_REQUIRE_SKU, {"orders": [{}]})

    assert result is not None and result.success is False
    # A row owes a field, not a position, so the goal names the field without naming the row.
    assert "orders.sku" in state["create_task_kwargs"]["data_extraction_goal"]


@pytest.mark.asyncio
async def test_a_present_optional_parent_owes_its_required_child(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    unmet, _ = await _heal_with_schema(monkeypatch, ai_fallback_flag, _OPTIONAL_PARENT_REQUIRES_TOTAL, {"invoice": {}})
    assert unmet is not None and unmet.success is False

    met, state = await _heal_with_schema(
        monkeypatch, ai_fallback_flag, _OPTIONAL_PARENT_REQUIRES_TOTAL, {"invoice": {"total": ""}}
    )
    assert met is not None and met.success is True
    assert "invoice.total" in state["create_task_kwargs"]["data_extraction_goal"]


@pytest.mark.parametrize("extracted", [[{}], "junk"])
@pytest.mark.asyncio
async def test_a_nullable_array_type_is_still_an_array_declaration(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None], extracted: object
) -> None:
    result, state = await _heal_with_schema(monkeypatch, ai_fallback_flag, _NULLABLE_ARRAY_ROWS_REQUIRE_SKU, extracted)

    assert result is not None and result.success is False
    # A list-form type must still carry its schema to the recovery task, not fall through as declaring nothing.
    assert state["create_task_kwargs"]["extracted_information_schema"] is not None


@pytest.mark.asyncio
async def test_an_absent_optional_parent_owes_nothing(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    schema = {
        "type": "object",
        "properties": {
            "a": {
                "type": "object",
                "properties": {"b": {"type": "object", "properties": {"c": {}}, "required": ["c"]}},
            }
        },
        "required": ["a"],
    }

    result, _ = await _heal_with_schema(monkeypatch, ai_fallback_flag, schema, {"a": {}})

    # `b` was never claimed, so nothing under it is owed.
    assert result is not None and result.success is True


_ROWS_REQUIRE_NAME = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "note": {"type": "string"}},
        "required": ["name"],
    },
}


async def _heal_list_block(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None], extracted: object
) -> tuple[BlockResult | None, dict]:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, extracted_information=extracted)
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the rows", line_start=1, line_end=1)],
        prompt="Read every row",
        data_schema=_ROWS_REQUIRE_NAME,
    )
    exc = RuntimeError("rotted selector")
    return await _heal(block, _make_context(), exc, _recording_page(exc)), state


@pytest.mark.parametrize("extracted", [[{}], [{"name": "x"}, {}], [{"name": "x"}, "a"]])
@pytest.mark.asyncio
async def test_a_row_missing_a_required_item_field_fails_the_block(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None], extracted: object
) -> None:
    result, state = await _heal_list_block(monkeypatch, ai_fallback_flag, extracted)

    assert result is not None and result.success is False
    assert result.status is BlockStatus.failed
    # Naming the field every row owes is the route a legitimate miss takes; the row needs no name.
    assert "name" in state["create_task_kwargs"]["data_extraction_goal"]


@pytest.mark.parametrize(
    "extracted",
    [[], [{"name": ""}], [{"name": None}], [{"name": "x"}], [{"name": "x"}, {"name": "y"}]],
)
@pytest.mark.asyncio
async def test_rows_that_answer_the_required_item_field_complete(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None], extracted: object
) -> None:
    """An empty list, and an empty or null value a model wrote, are answers. `note` is optional and
    absent throughout — only what the author marked required is demanded of a row."""
    result, _ = await _heal_list_block(monkeypatch, ai_fallback_flag, extracted)

    assert result is not None and result.success is True
    assert result.output_parameter_value == extracted


_MULTI_KEY_SCHEMA = {
    "type": "object",
    "properties": {"subtotal": {"type": "string"}, "tax": {"type": "string"}},
}


@pytest.mark.asyncio
async def test_every_declared_key_answered_completes_even_when_one_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    answered = {"subtotal": "$8.10", "tax": ""}
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, extracted_information=answered)
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the totals", line_start=1, line_end=1)],
        prompt="Read the subtotal and tax",
        data_schema=_MULTI_KEY_SCHEMA,
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    assert result.output_parameter_value == answered


@pytest.mark.asyncio
async def test_partially_answered_declaration_fails_the_block(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record)
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, extracted_information={"subtotal": "$8.10"})
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the totals", line_start=1, line_end=1)],
        prompt="Read the subtotal and tax",
        data_schema=_MULTI_KEY_SCHEMA,
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is False
    assert result.status is BlockStatus.failed
    assert result.output_parameter_value is None
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_nested_required_is_stripped_so_no_nested_default_is_fabricated(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(
        monkeypatch, final_status=TaskStatus.completed, extracted_information={"invoice": {"total": "$8.10"}}
    )
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the invoice", line_start=1, line_end=1)],
        prompt="Read the invoice total",
        data_schema={
            "type": "object",
            "properties": {
                "invoice": {
                    "type": "object",
                    "properties": {"total": {"type": "string"}},
                    "required": ["total"],
                }
            },
            "required": ["invoice"],
        },
    )
    exc = RuntimeError("rotted selector")

    await _heal(block, _make_context(), exc, _recording_page(exc))

    sent_schema = state["create_task_kwargs"]["extracted_information_schema"]
    assert "required" not in json.dumps(sent_schema)
    # The real filler must invent nothing at any depth for a property the model left empty.
    assert validate_and_fill_extraction_result({"invoice": {}}, sent_schema) == {"invoice": {}}


@pytest.mark.asyncio
async def test_declared_schema_is_sent_without_required_so_no_default_is_fabricated(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(
        monkeypatch, final_status=TaskStatus.completed, extracted_information={"order_total": "$8.10"}
    )
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the order total", line_start=1, line_end=1)],
        prompt="Read the order total",
        data_schema={
            "type": "object",
            "properties": {"order_total": {"type": "string"}},
            "required": ["order_total"],
        },
    )
    exc = RuntimeError("rotted selector")

    await _heal(block, _make_context(), exc, _recording_page(exc))

    sent_schema = state["create_task_kwargs"]["extracted_information_schema"]
    assert "required" not in sent_schema
    assert sent_schema["properties"] == {"order_total": {"type": "string"}}
    assert validate_and_fill_extraction_result({}, sent_schema) == {}


@pytest.mark.parametrize("extracted", [{"order_total": ""}, {"order_total": []}, {"order_total": 0}])
@pytest.mark.asyncio
async def test_declared_schema_answered_with_an_empty_value_still_completes(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None], extracted: object
) -> None:
    ai_fallback_flag("o_test")
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record)
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, extracted_information=extracted)
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the order total", line_start=1, line_end=1)],
        prompt="Read the order total",
        data_schema={"type": "object", "properties": {"order_total": {"type": "string"}}},
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    assert result.output_parameter_value == extracted


@pytest.mark.parametrize("extracted", [{}, {"other_key": "42.50"}, "42.50", None])
@pytest.mark.asyncio
async def test_declared_schema_unmet_by_the_extraction_fails_the_block(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None], extracted: object
) -> None:
    ai_fallback_flag("o_test")
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record)
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, extracted_information=extracted)
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the order total", line_start=1, line_end=1)],
        prompt="Read the order total",
        data_schema={"type": "object", "properties": {"order_total": {"type": "string"}}},
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is False
    assert result.status is BlockStatus.failed
    assert result.output_parameter_value is None
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_schema_less_heal_records_the_extracted_object_unvalidated(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record)
    state = _install_db_fakes(
        monkeypatch,
        final_status=TaskStatus.completed,
        extracted_information={"amount_due": "$84.20"},
    )
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the amount due", line_start=1, line_end=1)],
        prompt="Read the amount due",
    )
    assert block.data_schema is None
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    assert result.output_parameter_value == {"amount_due": "$84.20"}
    assert "task_id" not in result.output_parameter_value
    assert record.await_args.args[2] == {"amount_due": "$84.20"}
    assert state["create_task_kwargs"]["extracted_information_schema"] is None
    goal = state["create_task_kwargs"]["data_extraction_goal"]
    assert "Read the amount due" in goal
    assert "empty object" in goal


@pytest.mark.asyncio
async def test_schema_less_action_only_heal_records_empty_object(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record)
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, extracted_information={})
    block = _make_code_block(steps=[CodeBlockStep(description="log in", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    assert result.output_parameter_value == {}
    assert record.await_args.args[2] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data_schema",
    [{}, {"properties": {}}, "", [], {"properties": [1]}, {"properties": "total"}],
)
async def test_degenerate_schema_is_treated_as_schema_less(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
    data_schema: Any,
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, extracted_information={})
    block = _make_code_block(
        steps=[CodeBlockStep(description="read the amount due", line_start=1, line_end=1)],
        data_schema=data_schema,
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["extracted_information_schema"] is None
    assert "empty object" in state["create_task_kwargs"]["data_extraction_goal"]


@pytest.mark.asyncio
async def test_completed_heal_leaves_download_binding_to_the_recorder(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    downloaded = [FileInfo(url="https://files.test/report.pdf", checksum="abc123", artifact_id="art_1")]
    _install_db_fakes(
        monkeypatch,
        final_status=TaskStatus.completed,
        downloaded_files=downloaded,
        extracted_information={"report": "ok"},
    )
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record)
    block = _make_code_block(steps=[CodeBlockStep(description="download the report", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    assert result.output_parameter_value == {"report": "ok"}
    assert not set(result.output_parameter_value) & set(REGISTERED_DOWNLOAD_OUTPUT_KEYS)
    app.STORAGE.get_current_attempt_downloaded_files.assert_not_awaited()


@pytest.mark.parametrize(
    "final_status, expected_status",
    [
        (TaskStatus.terminated, BlockStatus.terminated),
        (TaskStatus.timed_out, BlockStatus.timed_out),
        (TaskStatus.canceled, BlockStatus.canceled),
        (TaskStatus.failed, BlockStatus.failed),
    ],
)
@pytest.mark.asyncio
async def test_non_completed_heal_maps_status_without_collapsing(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
    final_status: TaskStatus,
    expected_status: BlockStatus,
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=final_status)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None
    assert result.success is False
    assert result.status == expected_status
    assert state["execute_step_calls"] == 1


@pytest.mark.asyncio
async def test_escalation_runs_its_own_task(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    assert state["execute_step_kwargs"]["task"].task_id == "tsk_escalation"
    assert state["created_actions"] == []


@pytest.mark.asyncio
async def test_secret_value_never_leaks_into_goal_or_task(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    block = _make_code_block(
        steps=[CodeBlockStep(description=f"submit token {SECRET_VALUE}", line_start=1, line_end=1)],
        prompt=f"Sign in with {SECRET_VALUE} and download",
        data_schema={"type": "object", "properties": {f"receipt_{SECRET_VALUE}": {"type": "string"}}},
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(with_secret=True), exc, _recording_page(exc))

    assert result is not None
    goal = state["create_task_kwargs"]["navigation_goal"]
    assert SECRET_VALUE not in goal
    assert "*****" in goal
    assert state["execute_step_kwargs"]["recovery_code_progress"].failed_step == "submit token *****"
    assert _string_values(state["create_task_kwargs"]["extracted_information_schema"])
    assert "receipt_*****" in state["create_task_kwargs"]["data_extraction_goal"]
    for value in state["create_task_kwargs"].values():
        assert SECRET_VALUE not in str(value)


@pytest.mark.asyncio
async def test_completed_heal_masks_secret_in_recorded_output(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record)
    _install_db_fakes(
        monkeypatch,
        final_status=TaskStatus.completed,
        extracted_information={"token": SECRET_VALUE},
    )
    block = _make_code_block(steps=[CodeBlockStep(description="read the token", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(with_secret=True), exc, _recording_page(exc))

    assert result is not None
    recorded = record.await_args.args[2]
    assert SECRET_VALUE not in str(recorded)
    assert SECRET_VALUE not in str(result.output_parameter_value)


@pytest.mark.asyncio
async def test_max_steps_and_model_and_running_status_forwarded(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr("skyvern.config.settings.MAX_STEPS_PER_RUN", 7, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    update_calls: list[dict[str, object]] = []
    original_update = app.DATABASE.tasks.update_task

    async def _track_update(*args: object, **kwargs: object) -> object:
        update_calls.append(kwargs)
        return await original_update(*args, **kwargs)

    monkeypatch.setattr(app.DATABASE.tasks, "update_task", AsyncMock(side_effect=_track_update))
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    block.model = {"model_name": "gpt-5.5"}
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None
    assert state["create_task_kwargs"]["max_steps_per_run"] == 7
    assert state["create_task_kwargs"]["model"] == {"model_name": "gpt-5.5"}
    assert state["create_task_kwargs"]["order"] == 8
    assert any(call.get("status") == TaskStatus.running for call in update_calls)
    assert state["execute_step_kwargs"]["task_block"] is None


@pytest.mark.asyncio
async def test_heal_internal_exception_degrades_without_raising(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    monkeypatch.setattr(app.DATABASE.tasks, "create_task", AsyncMock(side_effect=RuntimeError("db down")))
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is None


@pytest.mark.asyncio
async def test_escalation_task_finalized_when_execute_step_raises(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    update_calls: list[dict[str, object]] = []
    original_update = app.DATABASE.tasks.update_task

    async def _track_update(*args: object, **kwargs: object) -> object:
        update_calls.append(kwargs)
        return await original_update(*args, **kwargs)

    monkeypatch.setattr(app.DATABASE.tasks, "update_task", AsyncMock(side_effect=_track_update))
    monkeypatch.setattr(app.agent, "execute_step", AsyncMock(side_effect=RuntimeError("agent boom")))
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is None
    # The escalation task was set running, then finalized to failed on cleanup — never left stranded.
    assert any(call.get("status") == TaskStatus.failed for call in update_calls)


@pytest.mark.asyncio
async def test_escalation_task_finalized_when_not_final(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    _install_db_fakes(monkeypatch, final_status=TaskStatus.running)
    update_calls: list[dict[str, object]] = []
    original_update = app.DATABASE.tasks.update_task

    async def _track_update(*args: object, **kwargs: object) -> object:
        update_calls.append(kwargs)
        return await original_update(*args, **kwargs)

    monkeypatch.setattr(app.DATABASE.tasks, "update_task", AsyncMock(side_effect=_track_update))
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is False
    assert result.status == BlockStatus.failed
    assert any(call.get("status") == TaskStatus.failed for call in update_calls)


@pytest.mark.asyncio
async def test_lone_line_start_step_is_matched(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    block = _make_code_block(steps=[CodeBlockStep(description="open the menu", line_start=3, line_end=None)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=9)

    assert result is not None
    assert state["execute_step_calls"] == 1
    assert state["execute_step_kwargs"]["recovery_code_progress"].failed_step == "open the menu"


def test_match_step_picks_largest_preceding_start() -> None:
    block = _make_code_block(
        steps=[
            CodeBlockStep(description="first", line_start=1, line_end=3),
            CodeBlockStep(description="second", line_start=5, line_end=None),
            CodeBlockStep(description="third", line_start=8, line_end=12),
        ]
    )
    assert block._match_step_for_failing_line(2).description == "first"
    assert block._match_step_for_failing_line(6).description == "second"
    assert block._match_step_for_failing_line(10).description == "third"
    assert block._match_step_for_failing_line(4) is None


@pytest.mark.asyncio
async def test_heal_max_steps_capped_by_org(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr("skyvern.config.settings.MAX_STEPS_PER_RUN", 25, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    # Org caps runs at 4 steps; the heal must not exceed it even though the global default is 25.
    state = _install_db_fakes(
        monkeypatch,
        final_status=TaskStatus.completed,
        organization=SimpleNamespace(organization_id="o_test", max_steps_per_run=4),
    )
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["max_steps_per_run"] == 4


@pytest.mark.asyncio
async def test_cancellation_finalizes_escalation_and_reraises(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    update_calls: list[dict[str, object]] = []
    original_update = app.DATABASE.tasks.update_task

    async def _track_update(*args: object, **kwargs: object) -> object:
        update_calls.append(kwargs)
        return await original_update(*args, **kwargs)

    monkeypatch.setattr(app.DATABASE.tasks, "update_task", AsyncMock(side_effect=_track_update))
    monkeypatch.setattr(app.agent, "execute_step", AsyncMock(side_effect=asyncio.CancelledError()))
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    with pytest.raises(asyncio.CancelledError):
        await _heal(block, _make_context(), exc, _recording_page(exc))

    # CancelledError (BaseException) must not strand the escalation task: finalized failed, then re-raised.
    assert any(call.get("status") == TaskStatus.failed for call in update_calls)


@pytest.mark.asyncio
async def test_recovery_child_block_created_and_finalized(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    # blocker #2: the heal takes over the live half-mutated page — the escalation task carries no url.
    assert state["create_task_kwargs"]["url"] == ""
    # blocker #1: a child block parented to the code block surfaces the recovery on the run timeline.
    rb = state["recovery_block_kwargs"]
    assert rb["parent_workflow_run_block_id"] == "wrb_test"
    assert rb["task_id"] == "tsk_escalation"
    assert rb["block_type"] == BlockType.TASK
    assert rb["label"]
    # the recovery block is finalized to the heal outcome, not left dangling in `running`.
    assert any(u.get("status") == BlockStatus.completed for u in state["recovery_block_updates"])


@pytest.mark.asyncio
async def test_recovery_block_finalized_to_non_completed_status(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.terminated)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is False
    assert any(u.get("status") == BlockStatus.terminated for u in state["recovery_block_updates"])


@pytest.mark.asyncio
async def test_mid_block_failure_keeps_the_goal_verbatim_and_records_the_outline(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(
        steps=[
            CodeBlockStep(description="open the portal", line_start=1, line_end=1),
            CodeBlockStep(description="click the invoices tab", line_start=2, line_end=2),
            CodeBlockStep(description="download the latest invoice", line_start=3, line_end=3),
        ]
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=2)

    assert result is not None and result.success is True
    goal = state["create_task_kwargs"]["navigation_goal"]
    assert goal == DEFAULT_PROMPT
    assert "mini goal" not in goal
    assert "Then:" not in goal
    record = state["execute_step_kwargs"]["recovery_code_progress"]
    assert record == CodeProgressRecord(
        before=("open the portal",),
        failed_step="click the invoices tab",
        failed_line=2,
        after=("download the latest invoice",),
    )
    message = compose_goal(goal, GoalDirectives(code_progress=record))
    assert message.startswith(DEFAULT_PROMPT + "\n\n")
    assert message.count("Code outline") == 1
    assert "- Earlier in the code: open the portal" in message
    assert "- Raised an error at line 2: click the invoices tab" in message
    assert "- Later in the code: download the latest invoice" in message


@pytest.mark.parametrize(
    ("failing_line", "expected"),
    [
        pytest.param(
            3,
            CodeProgressRecord(
                before=("Open https://example.com/list",),
                failed_step='Click "Open row"',
                failed_line=3,
                after=('Click "Dismiss"', 'Click "Skip"', 'Click "Export"'),
            ),
            id="inside_loop",
        ),
        pytest.param(
            7,
            CodeProgressRecord(
                before=("Open https://example.com/list", 'Click "Open row"', 'Click "Dismiss"'),
                failed_step='Click "Skip"',
                failed_line=7,
                after=('Click "Export"',),
            ),
            id="else_branch",
        ),
    ],
)
def test_outline_is_source_order_and_never_claims_what_ran(failing_line: int, expected: CodeProgressRecord) -> None:
    code = (
        'await page.goto("https://example.com/list")\n'
        "for row in rows:\n"
        '    await page.get_by_role("button", name="Open row").click()\n'
        "if banner:\n"
        '    await page.get_by_role("button", name="Dismiss").click()\n'
        "else:\n"
        '    await page.get_by_role("button", name="Skip").click()\n'
        'await page.get_by_role("button", name="Export").click()\n'
    )
    block = _make_code_block(
        code=code, steps=[CodeBlockStep.model_validate(step) for step in derive_code_block_steps(code)]
    )

    record = block._code_progress_record(
        workflow_run_context=_make_context(), failing_line=failing_line, authored_code=code
    )

    assert record == expected
    section = compose_goal(DEFAULT_PROMPT, GoalDirectives(code_progress=record)).removeprefix(DEFAULT_PROMPT).lower()
    for claim in ("completed", "not run", " ran", "already", "done"):
        assert claim not in section


@pytest.mark.asyncio
async def test_last_step_failure_keeps_single_step_goal(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(
        steps=[
            CodeBlockStep(description="open the portal", line_start=1, line_end=1),
            CodeBlockStep(description="download the latest invoice", line_start=2, line_end=2),
        ]
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=2)

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["navigation_goal"] == DEFAULT_PROMPT
    assert state["execute_step_kwargs"]["recovery_code_progress"] == CodeProgressRecord(
        before=("open the portal",), failed_step="download the latest invoice", failed_line=2, after=()
    )


@pytest.mark.asyncio
async def test_remaining_steps_without_descriptions_are_skipped(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(
        steps=[
            CodeBlockStep(description="click the invoices tab", line_start=1, line_end=1),
            CodeBlockStep(description=None, line_start=2, line_end=2),
        ]
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=1)

    assert result is not None and result.success is True
    assert state["execute_step_kwargs"]["recovery_code_progress"] == CodeProgressRecord(
        before=(), failed_step="click the invoices tab", failed_line=1, after=()
    )


@pytest.mark.asyncio
async def test_remaining_step_descriptions_are_masked(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(
        steps=[
            CodeBlockStep(description="open the portal", line_start=1, line_end=1),
            CodeBlockStep(description=f"submit token {SECRET_VALUE}", line_start=2, line_end=2),
        ]
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(with_secret=True), exc, _recording_page(exc), failing_line=1)

    assert result is not None and result.success is True
    record = state["execute_step_kwargs"]["recovery_code_progress"]
    assert record.after == ("submit token *****",)
    message = compose_goal(state["create_task_kwargs"]["navigation_goal"], GoalDirectives(code_progress=record))
    assert SECRET_VALUE not in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "steps",
    [
        pytest.param([CodeBlockStep(description="download", line_start=1, line_end=1)], id="matched_step"),
        pytest.param(None, id="bare_prompt"),
    ],
)
async def test_escalation_task_verifies_with_action_history(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None], steps: list[CodeBlockStep] | None
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=steps)
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["include_action_history_in_verification"] is True


@pytest.mark.parametrize(
    ("error_code", "healable", "skip_reason"),
    [
        ("unsupported_page_operation", True, None),
        ("browser_operation_failed", True, None),
        ("timeout", False, HealSkipReason.timeout_class),
        ("insecure_code_detected", False, HealSkipReason.insecure_code),
        ("browser_disconnected", False, HealSkipReason.unclassifiable),
        ("user_code_error", False, HealSkipReason.unclassifiable),
        ("busy", False, HealSkipReason.unclassifiable),
    ],
)
def test_secure_runner_degraded_classification_from_error_code(
    error_code: str, healable: bool, skip_reason: HealSkipReason | None
) -> None:
    block = _make_code_block()
    classification = block._classify_secure_runner_failure(
        CodeBlockEngineFailure(
            error_code=error_code,
            safe_message=None,
            failure_reason=None,
            exception_class=None,
            failing_line=None,
            healability_hint=None,
        )
    )
    assert classification.healable is healable
    assert classification.skip_reason == skip_reason


def test_secure_runner_classification_uses_playwright_class_when_hint_is_unknown() -> None:
    block = _make_code_block()
    classification = block._classify_secure_runner_failure(
        CodeBlockEngineFailure(
            error_code="busy",
            safe_message=None,
            failure_reason=None,
            exception_class="playwright._impl._errors.TimeoutError",
            failing_line=3,
            healability_hint=None,
        )
    )
    assert classification.healable is True
    assert classification.skip_reason is None


def test_secure_runner_classification_treats_browser_disconnected_as_non_healable() -> None:
    block = _make_code_block()
    classification = block._classify_secure_runner_failure(
        CodeBlockEngineFailure(
            error_code="browser_disconnected",
            safe_message=None,
            failure_reason=None,
            exception_class="codeblock.page_operation_broker.BrowserDisconnectedError",
            failing_line=3,
            healability_hint=None,
        )
    )
    assert classification.healable is False
    assert classification.skip_reason == HealSkipReason.unclassifiable


def test_secure_runner_does_not_heal_native_playwright_disconnect_when_hint_is_false() -> None:
    block = _make_code_block()
    classification = block._classify_secure_runner_failure(
        CodeBlockEngineFailure(
            error_code="browser_disconnected",
            safe_message=None,
            failure_reason=None,
            exception_class="playwright._impl._errors.TargetClosedError",
            failing_line=3,
            healability_hint=False,
        )
    )
    assert classification.healable is False
    assert classification.skip_reason == HealSkipReason.unclassifiable


@pytest.mark.asyncio
async def test_self_heal_passes_pre_resolved_browser_state_by_identity(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    browser_state = object()
    exc = RuntimeError("rotted selector")

    with skyvern_context.scoped(SkyvernContext(organization_id="o_test", workflow_run_id="wr_test")):
        result = await block._attempt_self_heal(
            authored_code=None,
            exception=exc,
            failing_line=1,
            recording_page=_recording_page(exc),
            workflow_run_context=_make_context(),
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            browser_session_id=None,
            browser_state=browser_state,
        )

    assert result is not None
    assert state["execute_step_kwargs"]["pre_resolved_browser_state"] is browser_state


@pytest.mark.asyncio
async def test_legacy_heal_success_skips_failed_write_and_ends_completed(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    context = _make_context()
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=fake_page), browser_artifacts=BrowserArtifacts()
    )
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=False,
    )
    raised = RuntimeError("rotted selector")
    monkeypatch.setattr(CodeBlock, "execute_user_function_with_timeout", AsyncMock(side_effect=raised))
    monkeypatch.setattr(app.AGENT_FUNCTION, "execute_code_block_override", AsyncMock(return_value=None))
    FakeRecorder.reset(last_recorded_exception=raised)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)

    async def _heal_success(**kwargs: Any) -> BlockResult:
        return await block.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value={"task_id": "tsk_escalation", "status": "completed"},
            status=BlockStatus.completed,
            workflow_run_block_id=kwargs["workflow_run_block_id"],
            organization_id=kwargs["organization_id"],
        )

    heal_mock = AsyncMock(side_effect=_heal_success)
    monkeypatch.setattr(block, "_attempt_self_heal", heal_mock)
    record_output_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record_output_mock)

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )

    block_statuses = _statuses_for_block(state, "wrb_test")
    assert result.success is True
    assert heal_mock.await_count == 1
    assert BlockStatus.failed not in block_statuses
    assert block_statuses[-1] == BlockStatus.completed
    assert record_output_mock.await_count == 1
    assert len(FakeRecorder.instances) == 1
    assert FakeRecorder.instances[0].finalized_success is True


@pytest.mark.asyncio
async def test_heal_output_write_failure_does_not_finalize_completed(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    context = _make_context()
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=fake_page), browser_artifacts=BrowserArtifacts()
    )
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=False,
    )
    raised = RuntimeError("rotted selector")
    monkeypatch.setattr(CodeBlock, "execute_user_function_with_timeout", AsyncMock(side_effect=raised))
    monkeypatch.setattr(app.AGENT_FUNCTION, "execute_code_block_override", AsyncMock(return_value=None))
    FakeRecorder.reset(last_recorded_exception=raised)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)

    async def _heal_success(**kwargs: Any) -> BlockResult:
        return await block.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value={"task_id": "tsk_escalation", "status": "completed"},
            status=BlockStatus.completed,
            workflow_run_block_id=kwargs["workflow_run_block_id"],
            organization_id=kwargs["organization_id"],
        )

    monkeypatch.setattr(block, "_attempt_self_heal", AsyncMock(side_effect=_heal_success))
    write_error = RuntimeError("output parameter write failed")
    record_output_mock = AsyncMock(side_effect=write_error)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record_output_mock)

    with pytest.raises(RuntimeError) as exc_info:
        await block.execute(
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            browser_session_id="pbs_test",
        )
    assert exc_info.value.args == ()

    assert record_output_mock.await_count == 1
    assert len(FakeRecorder.instances) == 1
    assert FakeRecorder.instances[0].finalized_success is not True


@pytest.mark.asyncio
async def test_secure_heal_success_skips_failed_write_and_ends_completed(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    context = _make_context()
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=fake_page), browser_artifacts=BrowserArtifacts()
    )
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=True,
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "execute_code_block_override",
        AsyncMock(
            return_value=CodeBlockEngineResult(
                block_result=None,
                failure=CodeBlockEngineFailure(
                    error_code="user_code_error",
                    safe_message=None,
                    failure_reason="CodeBlock failed while running user code.",
                    exception_class="playwright._impl._errors.TimeoutError",
                    failing_line=2,
                    healability_hint=True,
                ),
            )
        ),
    )
    FakeRecorder.reset(last_recorded_exception=None)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)

    async def _heal_success(**kwargs: Any) -> BlockResult:
        return await block.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value={"task_id": "tsk_escalation", "status": "completed"},
            status=BlockStatus.completed,
            workflow_run_block_id=kwargs["workflow_run_block_id"],
            organization_id=kwargs["organization_id"],
        )

    heal_mock = AsyncMock(side_effect=_heal_success)
    monkeypatch.setattr(block, "_attempt_self_heal", heal_mock)
    record_output_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record_output_mock)

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )

    block_statuses = _statuses_for_block(state, "wrb_test")
    assert result.success is True
    assert heal_mock.await_count == 1
    assert heal_mock.await_args.kwargs["page"] is fake_page
    assert heal_mock.await_args.kwargs["browser_state"] is fake_browser_state
    assert BlockStatus.failed not in block_statuses
    assert block_statuses[-1] == BlockStatus.completed
    assert record_output_mock.await_count == 1
    assert len(FakeRecorder.instances) == 1
    assert FakeRecorder.instances[0].finalized_success is True


@pytest.mark.asyncio
async def test_legacy_heal_declined_writes_failed_once(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    context = _make_context()
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=fake_page), browser_artifacts=BrowserArtifacts()
    )
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=False,
    )
    raised = RuntimeError("rotted selector")
    monkeypatch.setattr(CodeBlock, "execute_user_function_with_timeout", AsyncMock(side_effect=raised))
    monkeypatch.setattr(app.AGENT_FUNCTION, "execute_code_block_override", AsyncMock(return_value=None))
    FakeRecorder.reset(last_recorded_exception=raised)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)
    monkeypatch.setattr(block, "_attempt_self_heal", AsyncMock(return_value=None))
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )

    block_statuses = _statuses_for_block(state, "wrb_test")
    assert result.success is False
    assert block_statuses.count(BlockStatus.failed) == 1
    assert len(FakeRecorder.instances) == 1
    assert FakeRecorder.instances[0].finalized_success is False


@pytest.mark.asyncio
async def test_secure_heal_declined_writes_failed_once(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    context = _make_context()
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=fake_page), browser_artifacts=BrowserArtifacts()
    )
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=True,
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "execute_code_block_override",
        AsyncMock(
            return_value=CodeBlockEngineResult(
                block_result=None,
                failure=CodeBlockEngineFailure(
                    error_code="user_code_error",
                    safe_message=None,
                    failure_reason="CodeBlock failed while running user code.",
                    exception_class="playwright._impl._errors.TimeoutError",
                    failing_line=2,
                    healability_hint=True,
                ),
            )
        ),
    )
    FakeRecorder.reset(last_recorded_exception=None)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)
    monkeypatch.setattr(block, "_attempt_self_heal", AsyncMock(return_value=None))
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )

    block_statuses = _statuses_for_block(state, "wrb_test")
    assert result.success is False
    assert block_statuses.count(BlockStatus.failed) == 1
    assert len(FakeRecorder.instances) == 1
    assert FakeRecorder.instances[0].finalized_success is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "receiver_url", "worker_saw_sign_in_form", "expected"),
    [
        pytest.param("user_code_error", None, None, True, id="block_page_checked_here"),
        pytest.param("user_code_error", None, False, False, id="worker_answer_wins"),
        pytest.param(
            "user_code_error", "https://example.com/detail", True, True, id="opened_tab_checked_by_the_worker"
        ),
        pytest.param("user_code_error", "https://example.com/detail", None, False, id="opened_tab_already_closed"),
        # The runner's own budget timeout names no operation, so the tab the code waited on is unknown.
        pytest.param("timeout", None, None, False, id="timeout_tab_unknown"),
    ],
)
async def test_secure_failure_records_a_sign_in_form_only_on_the_page_it_failed_on(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
    error_code: str,
    receiver_url: str | None,
    worker_saw_sign_in_form: bool | None,
    expected: bool,
) -> None:
    ai_fallback_flag("o_test")
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    fake_page = MagicMock()
    fake_page.evaluate = AsyncMock(return_value=True)
    fake_browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=fake_page), browser_artifacts=BrowserArtifacts()
    )
    _patch_execute_chokepoint_environment(
        monkeypatch, context=_make_context(), fake_browser_state=fake_browser_state, use_codeblock_runner=True
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "execute_code_block_override",
        AsyncMock(
            return_value=CodeBlockEngineResult(
                block_result=None,
                failure=CodeBlockEngineFailure(
                    error_code=error_code,
                    safe_message=None,
                    failure_reason="CodeBlock failed while running user code.",
                    exception_class="playwright._impl._errors.TimeoutError",
                    failing_line=2,
                    healability_hint=True,
                    receiver_url=receiver_url,
                    sign_in_form_visible=worker_saw_sign_in_form,
                ),
            )
        ),
    )
    FakeRecorder.reset(last_recorded_exception=None)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)
    monkeypatch.setattr(block, "_attempt_self_heal", AsyncMock(return_value=None))
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )

    assert result.success is False
    assert result.sign_in_form_visible is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner_code", "recorder_code", "healability_hint", "expected_episodes"),
    [
        pytest.param(
            "net::ERR_TUNNEL_CONNECTION_FAILED",
            "net::ERR_TUNNEL_CONNECTION_FAILED",
            True,
            [(HealStatus.skipped, HealSkipReason.proxy_transport)],
            id="proxy_tunnel",
        ),
        pytest.param(
            "net::ERR_TUNNEL_CONNECTION_FAILED",
            "net::ERR_TUNNEL_CONNECTION_FAILED",
            False,
            [],
            id="proxy_tunnel_already_unhealable",
        ),
        pytest.param(
            "net::ERR_TUNNEL_CONNECTION_FAILED",
            NO_ADDRESS_RECORD_NAV_ERROR_CODE,
            True,
            [(HealStatus.fired_failed, None)],
            id="dead_host_behind_proxy",
        ),
        pytest.param(
            "net::ERR_TUNNEL_CONNECTION_FAILED",
            None,
            True,
            [(HealStatus.fired_failed, None)],
            id="recorder_saw_no_code",
        ),
        pytest.param(
            "net::ERR_NAME_NOT_RESOLVED",
            "net::ERR_NAME_NOT_RESOLVED",
            True,
            [(HealStatus.fired_failed, None)],
            id="dns_failure",
        ),
        pytest.param(
            None,
            "net::ERR_TUNNEL_CONNECTION_FAILED",
            True,
            [(HealStatus.fired_failed, None)],
            id="code_only_in_the_message",
        ),
    ],
)
async def test_secure_proxy_transport_failure_skips_the_ai_fallback(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
    runner_code: str | None,
    recorder_code: str | None,
    healability_hint: bool,
    expected_episodes: list[tuple[HealStatus, HealSkipReason | None]],
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block()
    context = _make_context()
    fake_browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=MagicMock()), browser_artifacts=BrowserArtifacts()
    )
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=True,
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "execute_code_block_override",
        AsyncMock(
            return_value=CodeBlockEngineResult(
                block_result=None,
                failure=CodeBlockEngineFailure(
                    error_code="browser_operation_failed",
                    safe_message=None,
                    failure_reason="Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://x.test/",
                    exception_class="playwright._impl._errors.Error",
                    failing_line=1,
                    healability_hint=healability_hint,
                    nav_error_code=runner_code,
                ),
            )
        ),
    )
    FakeRecorder.reset(last_failed_nav_error_code=recorder_code)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)
    monkeypatch.setattr(block, "_attempt_self_heal", AsyncMock(return_value=None))
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )

    assert result.success is False
    assert [(episode["status"], episode["skip_reason"]) for episode in state["heal_episodes"]] == expected_episodes
    if expected_episodes == [(HealStatus.skipped, HealSkipReason.proxy_transport)]:
        assert result.error_codes == ["browser_operation_failed", "net::ERR_TUNNEL_CONNECTION_FAILED"]


@pytest.mark.asyncio
async def test_secure_accepted_typed_failure_preserves_adapter_block_result(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block()
    context = _make_context()
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=fake_page), browser_artifacts=BrowserArtifacts()
    )
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=True,
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "execute_code_block_override",
        AsyncMock(
            return_value=CodeBlockEngineResult(
                block_result=BlockResult(
                    success=False,
                    output_parameter=block.output_parameter,
                    output_parameter_value={
                        "errors": [
                            UserDefinedError(error_code="declared", reasoning="typed", confidence_float=1.0).model_dump(
                                mode="json"
                            )
                        ]
                    },
                    status=BlockStatus.failed,
                    failure_reason="typed",
                    error_codes=["user_code_error", "declared"],
                ),
                failure=CodeBlockEngineFailure(
                    error_code="user_code_error",
                    safe_message="typed",
                    failure_reason="typed",
                    exception_class="codeblock.codeblock_runtime.ErrorCode",
                    failing_line=None,
                    healability_hint=False,
                    accepted_user_defined_error=UserDefinedError(
                        error_code="declared", reasoning="typed", confidence_float=1.0
                    ),
                ),
            )
        ),
    )
    FakeRecorder.reset(last_recorded_exception=None)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)
    heal_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(block, "_attempt_self_heal", heal_mock)
    write_episode = AsyncMock()
    monkeypatch.setattr(block, "_write_heal_episode_safe", write_episode)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )

    assert result.success is False
    assert result.error_codes == ["user_code_error", "declared"]
    assert result.output_parameter_value == {
        "errors": [
            UserDefinedError(error_code="declared", reasoning="typed", confidence_float=1.0).model_dump(mode="json")
        ]
    }
    assert heal_mock.await_count == 0
    assert write_episode.await_args.kwargs["skip_reason"] is HealSkipReason.user_defined_error
    assert _statuses_for_block(state, "wrb_test") == []
    assert len(FakeRecorder.instances) == 1
    assert FakeRecorder.instances[0].finalized_success is False


@pytest.mark.asyncio
async def test_secure_runner_missing_block_result_returns_generic_failure(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block()
    context = _make_context()
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=fake_page), browser_artifacts=BrowserArtifacts()
    )
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=True,
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "execute_code_block_override",
        AsyncMock(return_value=CodeBlockEngineResult(block_result=None, failure=None)),
    )
    FakeRecorder.reset(last_recorded_exception=None)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )

    block_statuses = _statuses_for_block(state, "wrb_test")
    assert result.success is False
    assert result.failure_reason == "Secure code block runner returned no result"
    assert block_statuses.count(BlockStatus.failed) == 1
    assert len(FakeRecorder.instances) == 1
    assert FakeRecorder.instances[0].finalized_success is False


@pytest.mark.asyncio
async def test_secure_infra_failure_without_metadata_finalizes_failed_and_records_no_output(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block()
    context = _make_context()
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=fake_page), browser_artifacts=BrowserArtifacts()
    )
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=True,
    )
    failed_result = await block.build_block_result(
        success=False,
        failure_reason="infra failure",
        output_parameter_value={"raw": "not-recorded"},
        status=BlockStatus.failed,
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "execute_code_block_override",
        AsyncMock(return_value=CodeBlockEngineResult(block_result=failed_result, failure=None)),
    )
    FakeRecorder.reset(last_recorded_exception=None)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)
    heal_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(block, "_attempt_self_heal", heal_mock)
    record_output_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record_output_mock)

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )

    block_statuses = _statuses_for_block(state, "wrb_test")
    assert result.success is False
    assert len(FakeRecorder.instances) == 1
    assert FakeRecorder.instances[0].finalized_success is False
    assert record_output_mock.await_count == 0
    assert BlockStatus.completed not in block_statuses
    heal_mock.assert_not_awaited()


def _credential_parameter() -> Parameter:
    return CredentialParameter(
        credential_parameter_id="cp_test",
        workflow_id="w_test",
        key="portal_credential",
        credential_id="cred_1",
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )


def _credential_id_workflow_parameter() -> Parameter:
    return WorkflowParameter(
        workflow_parameter_id="wp_test",
        workflow_id="w_test",
        key="portal_credential",
        workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_recovery_error_codes_reach_the_block_result(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    # The recovery task is given the block's error_code_mapping; a code it selects has to reach the
    # block so retry policies see the business outcome, as they do for a task block.
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.terminated)
    state["updated_task"].errors = [{"error_code": "account_locked", "reasoning": "locked", "confidence_float": 1.0}]
    block = _make_code_block(error_code_mapping={"account_locked": "The account is locked"})
    context = _make_context()

    result = await _heal(block, context, PlaywrightTimeoutError("timeout"), _recording_page(None))

    assert result is not None and result.success is False
    assert result.error_codes == ["account_locked"]


async def _resolve_failed_heal(
    block: CodeBlock, context: WorkflowRunContext, failed_nav_error_code: str | None, state: dict[str, Any]
) -> tuple[BlockResult, list[str]]:
    exception = PlaywrightTimeoutError("timeout")
    recorder = SimpleNamespace(recording_page=_recording_page(exception), finalize=AsyncMock())

    async def _build_failure() -> BlockResult:
        raise AssertionError("a heal that fired reports its own result")

    with skyvern_context.scoped(SkyvernContext(organization_id="o_test", workflow_run_id="wr_test")):
        result = await block._resolve_failure_with_heal(
            authored_code=None,
            exception=exception,
            failing_line=1,
            build_failure_result=_build_failure,
            classification=HealClassification(healable=True, skip_reason=None),
            recorder=recorder,
            workflow_run_context=context,
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            browser_session_id=None,
            browser_state=_browser_state(),
            page=MagicMock(),
            failed_nav_error_code=failed_nav_error_code,
        )
    # The recovery block is persisted after the code block, so the reader sees it as the newer one.
    recovery_block: dict[str, Any] = {}
    for update in state["recovery_block_updates"]:
        recovery_block.update({key: update.get(key) for key in ("failure_reason", "error_codes")})
    blocks = [{"failure_reason": result.failure_reason, "error_codes": result.error_codes}, recovery_block]
    return result, block_nav_error_codes({"data": {"blocks": blocks}}, result.failure_reason)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "driver_code", ["net::ERR_NAME_NOT_RESOLVED", "net::ERR_CERT_DATE_INVALID", NO_ADDRESS_RECORD_NAV_ERROR_CODE]
)
async def test_failed_recovery_keeps_the_driver_nav_code_behind_its_own_codes(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
    driver_code: str,
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.failed)
    state["updated_task"].errors = [{"error_code": "account_locked", "reasoning": "x", "confidence_float": 1.0}]
    state["updated_task"].failure_reason = "The site is unreachable"
    block = _make_code_block(error_code_mapping={"account_locked": "The account is locked"})

    result, codes = await _resolve_failed_heal(block, _make_context(), driver_code, state)

    assert result.success is False
    assert state["heal_episodes"][0]["status"] == HealStatus.fired_failed
    assert result.error_codes == ["account_locked", driver_code]
    assert codes == [driver_code]
    assert target_owns_nav_codes(codes) is True
    assert proxy_owns_nav_codes(codes) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("block_code", [None, "net::ERR_NAME_NOT_RESOLVED"])
async def test_failed_recovery_reports_the_code_of_its_own_failed_navigation(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None], block_code: str | None
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.failed)
    state["updated_task"].failure_reason = "The site is unreachable"

    async def _recovery_navigates_into_the_proxy(*args: object, task: Task, **kwargs: object) -> None:
        context = skyvern_context.current()
        assert context is not None
        context.task_nav_error_codes[task.task_id] = "net::ERR_TUNNEL_CONNECTION_FAILED"

    monkeypatch.setattr(app.agent, "execute_step", AsyncMock(side_effect=_recovery_navigates_into_the_proxy))

    result, codes = await _resolve_failed_heal(_make_code_block(), _make_context(), block_code, state)

    # The recovery's navigation is what the heal ended on, so an earlier target code does not veto it.
    assert result.error_codes == ["net::ERR_TUNNEL_CONNECTION_FAILED"]
    assert codes == ["net::ERR_TUNNEL_CONNECTION_FAILED"]
    assert proxy_owns_nav_codes(codes) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hop_refused", "expected_codes"), [(True, ["net::ERR_TUNNEL_CONNECTION_FAILED"]), (False, None)]
)
async def test_the_escalation_hop_before_the_recovery_runs_decides_what_the_heal_reports(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
    hop_refused: bool,
    expected_codes: list[str] | None,
) -> None:
    ai_fallback_flag("o_test")
    monkeypatch.setattr(navigation_module, "host_has_no_address_record", lambda host: False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.failed)
    state["updated_task"].failure_reason = "The site is unreachable"
    target_url = "https://dead-nav.example.com/login"
    block = _make_code_block(code=f'await page.goto("{target_url}")')
    exc = RuntimeError("navigation failed")
    browser_state = _browser_state()
    if hop_refused:
        browser_state.navigate_to_url = AsyncMock(
            side_effect=RuntimeError(f"Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at {target_url}")
        )

    with skyvern_context.scoped(SkyvernContext(organization_id="o_test", workflow_run_id="wr_test")):
        result = await block._attempt_self_heal(
            authored_code=None,
            exception=exc,
            failing_line=1,
            recording_page=_recording_page(exc),
            workflow_run_context=_make_context(),
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            browser_session_id=None,
            browser_state=browser_state,
            page=MagicMock(),
            failed_nav_error_code="net::ERR_NAME_NOT_RESOLVED",
        )

    # A hop that loaded got past the block's DNS failure, so the recovery does not inherit that code.
    assert result is not None and result.success is False
    assert result.error_codes == (expected_codes or [])
    assert state["recovery_block_updates"][-1]["error_codes"] == expected_codes


@pytest.mark.asyncio
@pytest.mark.parametrize("final_status", [TaskStatus.failed, TaskStatus.completed])
@pytest.mark.parametrize(
    ("recovery", "expected_codes"),
    [
        ("acts_past_it", None),
        ("reads_the_error_page", ["net::ERR_NAME_NOT_RESOLVED"]),
        ("navigates_into_the_proxy", ["net::ERR_TUNNEL_CONNECTION_FAILED"]),
    ],
)
async def test_a_failed_heal_reports_the_navigation_its_recovery_ended_on(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
    final_status: TaskStatus,
    recovery: str,
    expected_codes: list[str] | None,
) -> None:
    ai_fallback_flag("o_test")
    # A completed recovery that misses the declared values fails the block through its own exit.
    state = _install_db_fakes(monkeypatch, final_status=final_status, extracted_information={})
    state["updated_task"].failure_reason = "The site is unreachable"

    async def _recovery(*args: object, task: Task, **kwargs: object) -> None:
        if recovery == "acts_past_it":
            clear_task_nav_error_code(task.task_id)
        elif recovery == "navigates_into_the_proxy":
            await record_task_nav_error_code(
                task.task_id, RuntimeError("Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://x.test/")
            )

    monkeypatch.setattr(app.agent, "execute_step", AsyncMock(side_effect=_recovery))
    block = _make_code_block(
        data_schema={"type": "object", "properties": {"order_total": {"type": "string"}}, "required": ["order_total"]}
    )

    result, codes = await _resolve_failed_heal(block, _make_context(), "net::ERR_NAME_NOT_RESOLVED", state)

    assert result.success is False
    assert result.error_codes == (expected_codes or [])
    assert state["recovery_block_updates"][-1]["error_codes"] == expected_codes
    assert target_owns_nav_codes(codes) is (recovery == "reads_the_error_page")


@pytest.mark.asyncio
async def test_recovery_without_a_final_status_keeps_the_driver_nav_code(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.running)
    block = _make_code_block()

    result, codes = await _resolve_failed_heal(block, _make_context(), "net::ERR_NAME_NOT_RESOLVED", state)

    assert result.success is False
    assert result.error_codes == ["net::ERR_NAME_NOT_RESOLVED"]
    assert state["recovery_block_updates"][-1]["error_codes"] == ["net::ERR_NAME_NOT_RESOLVED"]
    assert target_owns_nav_codes(codes) is True


@pytest.mark.asyncio
async def test_a_net_code_named_only_in_the_recovery_sentence_is_not_carried(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.failed)
    state["updated_task"].failure_reason = "Navigation failed with net::ERR_TUNNEL_CONNECTION_FAILED"
    block = _make_code_block()

    result, codes = await _resolve_failed_heal(block, _make_context(), None, state)

    assert result.success is False
    assert result.error_codes == []
    assert proxy_owns_nav_codes(codes) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("engine", "driver_code", "recorder_code", "expected_codes"),
    [
        ("inline", "net::ERR_NAME_NOT_RESOLVED", None, ["net::ERR_NAME_NOT_RESOLVED"]),
        ("inline", SECRET_VALUE, None, []),
        ("secure", "net::ERR_NAME_NOT_RESOLVED", "net::ERR_NAME_NOT_RESOLVED", ["net::ERR_NAME_NOT_RESOLVED"]),
        ("secure", SECRET_VALUE, SECRET_VALUE, []),
        ("secure", None, "net::ERR_NAME_NOT_RESOLVED", []),
        # The runner blames the tunnel; the worker's resolver found no address, so the site owns it.
        (
            "secure",
            "net::ERR_TUNNEL_CONNECTION_FAILED",
            NO_ADDRESS_RECORD_NAV_ERROR_CODE,
            [NO_ADDRESS_RECORD_NAV_ERROR_CODE],
        ),
    ],
)
async def test_executed_block_keeps_its_screened_driver_nav_code_through_a_failed_recovery(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
    engine: str,
    driver_code: str | None,
    recorder_code: str | None,
    expected_codes: list[str],
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.failed)
    block = _make_code_block(code="raise RuntimeError('navigation failed')")
    context = _make_context(with_secret=True)
    fake_browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=MagicMock()),
        browser_artifacts=BrowserArtifacts(),
        navigate_to_url=AsyncMock(return_value=None),
    )
    _patch_execute_chokepoint_environment(
        monkeypatch, context=context, fake_browser_state=fake_browser_state, use_codeblock_runner=engine == "secure"
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "execute_code_block_override",
        AsyncMock(
            return_value=CodeBlockEngineResult(
                block_result=None,
                failure=CodeBlockEngineFailure(
                    error_code="user_code_error",
                    safe_message=None,
                    failure_reason="CodeBlock failed while running user code.",
                    exception_class="playwright._impl._errors.Error",
                    failing_line=1,
                    healability_hint=True,
                    nav_error_code=driver_code,
                ),
            )
            if engine == "secure"
            else None
        ),
    )
    monkeypatch.setattr(CodeBlock, "_is_healable_page_failure", MagicMock(return_value=True))
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    FakeRecorder.reset(last_recorded_exception=None, last_failed_nav_error_code=recorder_code)

    class _NavFailedRecorder(FakeRecorder):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            self.recording_page.failure_nav_error_code = MagicMock(return_value=driver_code)
            self.recording_page.failure_document_receipt = MagicMock(return_value=None)

    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", _NavFailedRecorder)

    with skyvern_context.scoped(SkyvernContext(organization_id="o_test", workflow_run_id="wr_test")):
        result = await block.execute(
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            browser_session_id="pbs_test",
        )

    assert result.success is False
    assert state["heal_episodes"][0]["status"] == HealStatus.fired_failed
    assert result.failure_reason == "agent gave up"
    assert [code for code in result.error_codes if code != "user_code_error"] == expected_codes


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["inline", "secure"])
@pytest.mark.parametrize(
    ("fallback_on", "final_status"),
    [(False, TaskStatus.failed), (True, TaskStatus.failed), (True, TaskStatus.running)],
    ids=["unhealed", "fired_failed", "never_final"],
)
async def test_no_persisted_block_row_keeps_a_nav_code_that_is_a_parameter_value(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
    engine: str,
    fallback_on: bool,
    final_status: TaskStatus,
) -> None:
    parameter_code = "net::ERR_NAME_NOT_RESOLVED"
    ai_fallback_flag("o_test" if fallback_on else None)
    state = _install_db_fakes(monkeypatch, final_status=final_status)
    block = _make_code_block(code="raise RuntimeError('navigation failed')")
    fake_browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=MagicMock()),
        browser_artifacts=BrowserArtifacts(),
        navigate_to_url=AsyncMock(return_value=None),
    )
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=_make_context(),
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=engine == "secure",
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "serialize_codeblock_parameters", lambda parameters: {"site": parameter_code}
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "redact_codeblock_parameter_values",
        lambda value, parameters: (
            [item.replace(parameter_code, "[redacted]") for item in value]
            if isinstance(value, list)
            else value.replace(parameter_code, "[redacted]")
            if isinstance(value, str)
            else value
        ),
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "execute_code_block_override",
        AsyncMock(
            return_value=CodeBlockEngineResult(
                block_result=None,
                failure=CodeBlockEngineFailure(
                    error_code="user_code_error",
                    safe_message=None,
                    failure_reason="CodeBlock failed while running user code.",
                    exception_class="playwright._impl._errors.Error",
                    failing_line=1,
                    healability_hint=True,
                    nav_error_code=parameter_code,
                ),
            )
            if engine == "secure"
            else None
        ),
    )
    monkeypatch.setattr(CodeBlock, "_is_healable_page_failure", MagicMock(return_value=True))
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    FakeRecorder.reset(last_recorded_exception=None, last_failed_nav_error_code=parameter_code)

    class _NavFailedRecorder(FakeRecorder):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            self.recording_page.failure_nav_error_code = MagicMock(return_value=parameter_code)
            self.recording_page.failure_document_receipt = MagicMock(return_value=None)

    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", _NavFailedRecorder)

    with skyvern_context.scoped(SkyvernContext(organization_id="o_test", workflow_run_id="wr_test")):
        result = await block.execute(
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            browser_session_id="pbs_test",
        )

    persisted = {
        update["workflow_run_block_id"]: update["error_codes"]
        for update in state["workflow_run_block_updates"]
        if update.get("error_codes")
    }
    assert result.success is False
    assert bool(state["heal_episodes"]) is fallback_on
    assert "[redacted]" in persisted["wrb_test"]
    if final_status == TaskStatus.failed and fallback_on:
        assert persisted["wrb_recovery"] == ["[redacted]"]
    assert all(parameter_code not in codes for codes in persisted.values())


@pytest.mark.asyncio
async def test_a_recovery_error_code_that_is_a_run_secret_is_not_reported(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.terminated)
    state["updated_task"].errors = [{"error_code": SECRET_VALUE, "reasoning": "x", "confidence_float": 1.0}]
    block = _make_code_block(error_code_mapping={"whatever": "x"})
    context = _make_context(with_secret=True)

    result = await _heal(block, context, PlaywrightTimeoutError("timeout"), _recording_page(None))

    assert result is not None and result.success is False
    assert result.error_codes == []


@pytest.mark.asyncio
async def test_recovery_pins_the_login_key_on_a_block_that_also_carries_a_card(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block()
    credential = _credential_id_workflow_parameter()
    card = BitwardenCreditCardDataParameter(
        bitwarden_credit_card_data_parameter_id="bccd_test",
        workflow_id="w_test",
        key="portal_card",
        bitwarden_client_id_aws_secret_key="client_id",
        bitwarden_client_secret_aws_secret_key="client_secret",
        bitwarden_master_password_aws_secret_key="master_password",
        bitwarden_collection_id="col_1",
        bitwarden_item_id="item_1",
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )
    block.parameters = [credential, card]
    context = _make_context()
    for parameter in block.parameters:
        context.parameters[parameter.key] = parameter
        context.values[parameter.key] = {"username": "secret_1_username", "password": "secret_1_password"}

    result = await _heal(block, context, PlaywrightTimeoutError("timeout"), _recording_page(None))

    assert result is not None and result.success is True
    assert state["execute_step_kwargs"]["recovery_credential_parameter_keys"] == [credential.key]
    # The pin is the login credential alone, but the payload exposes the card too, so the release
    # guard has to be armed for both or the card number can be typed on any site.
    assert state["execute_step_kwargs"]["recovery_release_parameter_keys"] == [credential.key, card.key]


@pytest.mark.asyncio
@pytest.mark.parametrize("make_parameter", [_credential_parameter, _credential_id_workflow_parameter])
async def test_recovery_task_is_built_from_the_block_fields(
    monkeypatch: pytest.MonkeyPatch,
    make_parameter: Callable[[], Parameter],
    ai_fallback_flag: Callable[[str | None], None],
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(error_code_mapping={"missing": "Report missing"})
    credential = make_parameter()
    block.parameters = [credential]
    context = _make_context()
    context.parameters[credential.key] = credential
    context.values[credential.key] = {"username": "secret_1_username", "password": "secret_1_password"}
    exception = PlaywrightTimeoutError("timeout")
    recorder = SimpleNamespace(recording_page=_recording_page(exception), finalize=AsyncMock())

    async def _build_failure() -> BlockResult:
        return await block.build_block_result(
            success=False,
            failure_reason="Failed to execute code block.",
            output_parameter_value=None,
            status=BlockStatus.failed,
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
        )

    with capture_logs() as logs:
        with skyvern_context.scoped(SkyvernContext(organization_id="o_test", workflow_run_id="wr_test")):
            result = await block._resolve_failure_with_heal(
                authored_code=None,
                exception=exception,
                failing_line=1,
                build_failure_result=_build_failure,
                classification=HealClassification(healable=True, skip_reason=None),
                recorder=recorder,
                workflow_run_context=context,
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_test",
                organization_id="o_test",
                browser_session_id=None,
                browser_state=_browser_state(),
                page=MagicMock(),
            )

    assert result.success is True
    create_kwargs = state["create_task_kwargs"]
    assert create_kwargs["navigation_payload"] == {
        "portal_credential": {"username": "secret_1_username", "password": "secret_1_password"}
    }
    assert create_kwargs["error_code_mapping"] == {"missing": "Report missing"}
    step_kwargs = state["execute_step_kwargs"]
    assert step_kwargs["engine"] is RunEngine.skyvern_v3
    assert step_kwargs["workflow_owned_recovery"] is True
    assert step_kwargs["recovery_credential_parameter_keys"] == ["portal_credential"]
    outcome_lines = [log for log in logs if log.get("event") == "codeblock.ai_fallback_outcome"]
    assert len(outcome_lines) == 1
    assert outcome_lines[0]["credential_parameter_key"] == "portal_credential"


@pytest.mark.asyncio
async def test_block_without_prompt_skips_recovery_without_an_llm_call(
    monkeypatch: pytest.MonkeyPatch, ai_fallback_flag: Callable[[str | None], None]
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(prompt=None)
    context = _make_context()
    exception = PlaywrightTimeoutError("timeout")
    recorder = SimpleNamespace(recording_page=_recording_page(exception), finalize=AsyncMock())

    async def _build_failure() -> BlockResult:
        return await block.build_block_result(
            success=False,
            failure_reason="Failed to execute code block.",
            output_parameter_value=None,
            status=BlockStatus.failed,
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
        )

    with capture_logs() as logs:
        result = await block._resolve_failure_with_heal(
            authored_code=None,
            exception=exception,
            failing_line=2,
            build_failure_result=_build_failure,
            classification=HealClassification(healable=True, skip_reason=None),
            recorder=recorder,
            workflow_run_context=context,
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            browser_session_id=None,
        )

    assert result.success is False
    assert state["create_task_kwargs"] is None
    assert state["execute_step_calls"] == 0
    assert len(state["heal_episodes"]) == 1
    episode = state["heal_episodes"][0]
    assert episode["status"] == HealStatus.skipped
    assert episode["skip_reason"] == HealSkipReason.no_goal
    outcome_lines = [log for log in logs if log.get("event") == "codeblock.ai_fallback_outcome"]
    assert len(outcome_lines) == 1
    assert outcome_lines[0]["skip_reason"] == HealSkipReason.no_goal.value
    assert set(outcome_lines[0]) >= {
        "workflow_run_id",
        "workflow_run_block_id",
        "block_label",
        "status",
        "skip_reason",
        "task_id",
        "action_count",
    }


@pytest.mark.asyncio
async def test_failed_recovery_outcome_carries_task_id_and_action_count(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
) -> None:
    ai_fallback_flag("o_test")
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.failed)
    state["task_actions"] = [
        MagicMock(action_type=ActionType.CLICK),
        MagicMock(action_type=ActionType.INPUT_TEXT),
        MagicMock(action_type=ActionType.COMPLETE),
    ]
    block = _make_code_block()
    context = _make_context()
    exception = PlaywrightTimeoutError("timeout")
    recorder = SimpleNamespace(recording_page=_recording_page(exception), finalize=AsyncMock())

    async def _build_failure() -> BlockResult:
        return await block.build_block_result(
            success=False,
            failure_reason="Failed to execute code block.",
            output_parameter_value=None,
            status=BlockStatus.failed,
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
        )

    with capture_logs() as logs:
        result = await block._resolve_failure_with_heal(
            authored_code=None,
            exception=exception,
            failing_line=2,
            build_failure_result=_build_failure,
            classification=HealClassification(healable=True, skip_reason=None),
            recorder=recorder,
            workflow_run_context=context,
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            browser_session_id=None,
        )

    assert result.success is False
    episode = state["heal_episodes"][0]
    assert episode["engine"] == "floor"
    assert episode["status"] == HealStatus.fired_failed
    assert episode["escalation_task_id"] == "tsk_escalation"
    # Decision rows (COMPLETE/TERMINATE) are not agent interactions.
    assert episode["action_count"] == 2
    outcome_lines = [log for log in logs if log.get("event") == "codeblock.ai_fallback_outcome"]
    assert len(outcome_lines) == 1
    assert outcome_lines[0]["task_id"] == "tsk_escalation"
    assert outcome_lines[0]["action_count"] == 2


@pytest.mark.asyncio
async def test_heal_episode_persistence_failure_does_not_change_heal_outcome(
    monkeypatch: pytest.MonkeyPatch,
    ai_fallback_flag: Callable[[str | None], None],
) -> None:
    ai_fallback_flag("o_test")
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    monkeypatch.setattr(app.DATABASE.self_heal, "create_heal_episode", AsyncMock(side_effect=RuntimeError("missing")))
    block = _make_code_block()
    context = _make_context()
    exception = RuntimeError("boom")
    recorder = SimpleNamespace(recording_page=_recording_page(exception), finalize=AsyncMock())
    floor_result = await block.build_block_result(
        success=True,
        failure_reason=None,
        output_parameter_value={"ok": True},
        status=BlockStatus.completed,
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
    )
    monkeypatch.setattr(block, "_attempt_self_heal", AsyncMock(return_value=floor_result))
    monkeypatch.setattr(block, "_ai_fallback_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_for_workflow_run", MagicMock(return_value=object()))

    async def _build_failure() -> BlockResult:
        raise AssertionError("should not use failure result when floor succeeds")

    with capture_logs() as logs:
        result = await block._resolve_failure_with_heal(
            authored_code=None,
            exception=exception,
            failing_line=2,
            build_failure_result=_build_failure,
            classification=HealClassification(healable=True, skip_reason=None),
            recorder=recorder,
            workflow_run_context=context,
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            browser_session_id=None,
        )

    assert result.success is True
    assert any(entry.get("event") == "self-heal episode persistence failed; continuing" for entry in logs)
