"""Tests for CodeBlock's error_code_mapping manifest + ErrorCode primitive (SKY-13668, stream 1).

Covers the inline (non-secure-runner) execution path only: a declared `raise ErrorCode(code,
reasoning)` is surfaced as a USER_DEFINED_ERROR and skips self-healing; an undeclared code (or a
raise with no manifest at all) fails closed exactly like an ordinary exception.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.schemas.self_heal import HealSkipReason
from skyvern.schemas.workflows import BlockStatus
from skyvern.webeye.browser_artifacts import BrowserArtifacts


class FakeBrowserState:
    def __init__(self) -> None:
        self.browser_artifacts = BrowserArtifacts()

    async def get_working_page(self) -> object:
        return object()


class FakeWorkflowRunContext:
    values: dict[str, object] = {}
    secrets: dict[str, object] = {}
    include_secrets_in_templates = False
    organization_id = None
    workflow_title = "Test Workflow"
    workflow_id = "w_test"
    workflow_permanent_id = "wpid_test"
    workflow_run_id = "wrid_test"
    browser_session_id = None
    workflow_run_outputs: list[object] = []
    workflow = None

    def get_block_metadata(self, label: str | None) -> dict[str, object]:
        return {}

    def build_workflow_run_summary(self) -> str:
        return ""

    def mask_secrets_in_data(self, data: object, mask: str = "*****") -> object:
        return data


def _output_parameter(key: str) -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        description="test output",
        output_parameter_id=f"op_{key}",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )


async def _run_code_block(monkeypatch: pytest.MonkeyPatch, block: CodeBlock, self_heal_mock: AsyncMock | None = None):
    async def validate_code_block(*args: object, **kwargs: object) -> None:
        return None

    async def get_browser_state(*args: object, **kwargs: object) -> FakeBrowserState:
        return FakeBrowserState()

    async def record_output(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.block.app.AGENT_FUNCTION.validate_code_block",
        validate_code_block,
    )
    monkeypatch.setattr(CodeBlock, "get_or_create_browser_state", get_browser_state)
    monkeypatch.setattr(CodeBlock, "get_workflow_run_context", lambda *args: FakeWorkflowRunContext())
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record_output)
    if self_heal_mock is not None:
        monkeypatch.setattr(CodeBlock, "_resolve_failure_with_heal", self_heal_mock)

    return await block.execute(workflow_run_id="wrid_test", workflow_run_block_id="")


@pytest.mark.asyncio
async def test_no_manifest_success_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: a block with no error_code_mapping behaves exactly as before on success."""
    block = CodeBlock(label="ok_block", code="value = 'ok'", output_parameter=_output_parameter("ok_output"))
    result = await _run_code_block(monkeypatch, block)

    assert result.success is True
    assert result.status == BlockStatus.completed
    assert result.output_parameter_value == {"value": "ok"}
    assert result.error_codes == []


@pytest.mark.asyncio
async def test_declared_error_code_surfaces_as_user_defined_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: a declared ErrorCode raise writes a USER_DEFINED_ERROR with the code/reasoning/confidence 1.0."""
    block = CodeBlock(
        label="login_block",
        code="raise ErrorCode('LOGIN_FAILED', 'could not find the login button')",
        output_parameter=_output_parameter("login_output"),
        error_code_mapping={"LOGIN_FAILED": "the login form could not be located"},
    )
    result = await _run_code_block(monkeypatch, block)

    assert result.success is False
    assert result.status == BlockStatus.failed
    assert result.error_codes == ["LOGIN_FAILED"]
    assert result.output_parameter_value["errors"] == [
        {
            "error_code": "LOGIN_FAILED",
            "reasoning": "could not find the login button",
            "confidence_float": 1.0,
            "error_type": "USER_DEFINED_ERROR",
        }
    ]


@pytest.mark.asyncio
async def test_undeclared_error_code_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4: raising a code absent from the manifest is NOT surfaced as a caller-defined error."""
    block = CodeBlock(
        label="undeclared_block",
        code="raise ErrorCode('NOT_IN_MANIFEST', 'should not be trusted')",
        output_parameter=_output_parameter("undeclared_output"),
        error_code_mapping={"LOGIN_FAILED": "the login form could not be located"},
    )
    result = await _run_code_block(monkeypatch, block)

    assert result.success is False
    assert result.status == BlockStatus.failed
    assert result.error_codes == []
    assert result.output_parameter_value is None
    assert result.failure_reason == (
        "Failed to execute code block. Reason: ErrorCode is not declared in the effective error_code_mapping"
    )
    assert "NOT_IN_MANIFEST" not in result.failure_reason
    assert "should not be trusted" not in result.failure_reason


@pytest.mark.asyncio
async def test_error_code_raise_with_no_manifest_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4 variant: raising ErrorCode with no manifest configured at all is an ordinary failure."""
    block = CodeBlock(
        label="no_manifest_block",
        code="raise ErrorCode('X', 'y')",
        output_parameter=_output_parameter("no_manifest_output"),
    )
    result = await _run_code_block(monkeypatch, block)

    assert result.success is False
    assert result.status == BlockStatus.failed
    assert result.error_codes == []
    assert result.output_parameter_value is None
    assert result.failure_reason == (
        "Failed to execute code block. Reason: ErrorCode is not declared in the effective error_code_mapping"
    )


@pytest.mark.asyncio
async def test_declared_error_code_skips_self_heal(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC5: a declared typed raise records a non-healable skip without attempting repair."""
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)

    async def resolve_failure_with_heal(**kwargs: object):
        return await kwargs["build_failure_result"]()  # type: ignore[operator]

    resolver_mock = AsyncMock(side_effect=resolve_failure_with_heal)
    heal_attempt_mock = AsyncMock(side_effect=AssertionError("self-heal attempt must not run"))
    monkeypatch.setattr(CodeBlock, "_attempt_self_heal", heal_attempt_mock)
    block = CodeBlock(
        label="healable_login_block",
        code="raise ErrorCode('LOGIN_FAILED', 'could not find the login button')",
        output_parameter=_output_parameter("heal_output"),
        error_code_mapping={"LOGIN_FAILED": "the login form could not be located"},
    )
    result = await _run_code_block(monkeypatch, block, self_heal_mock=resolver_mock)

    assert result.success is False
    # SKY-13668 fail-closed contract: use the resolver for skip bookkeeping, but never attempt heal.
    resolver_mock.assert_awaited_once()
    classification = resolver_mock.await_args.kwargs["classification"]
    assert classification.healable is False
    assert classification.skip_reason is HealSkipReason.user_defined_error
    heal_attempt_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_exception_still_eligible_for_self_heal(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC5 counterpart: an ordinary exception (or an undeclared ErrorCode) still reaches self-heal resolution."""
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    heal_mock = AsyncMock(return_value=None)
    block = CodeBlock(
        label="ordinary_failure_block",
        code="raise Exception('boom')",
        output_parameter=_output_parameter("ordinary_output"),
    )
    await _run_code_block(monkeypatch, block, self_heal_mock=heal_mock)

    heal_mock.assert_called_once()


@pytest.mark.asyncio
async def test_multiple_manifest_entries_resolve_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: a manifest with multiple entries lets different branches raise different declared codes."""
    manifest = {
        "LOGIN_FAILED": "the login form could not be located",
        "CAPTCHA_BLOCKED": "a captcha challenge blocked progress",
    }
    login_block = CodeBlock(
        label="branch_a",
        code="raise ErrorCode('LOGIN_FAILED', 'no login form')",
        output_parameter=_output_parameter("branch_a_output"),
        error_code_mapping=manifest,
    )
    captcha_block = CodeBlock(
        label="branch_b",
        code="raise ErrorCode('CAPTCHA_BLOCKED', 'captcha shown')",
        output_parameter=_output_parameter("branch_b_output"),
        error_code_mapping=manifest,
    )

    login_result = await _run_code_block(monkeypatch, login_block)
    captcha_result = await _run_code_block(monkeypatch, captcha_block)

    assert login_result.error_codes == ["LOGIN_FAILED"]
    assert captcha_result.error_codes == ["CAPTCHA_BLOCKED"]
