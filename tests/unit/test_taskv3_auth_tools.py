"""Unit tests for the Task V3 auth tools (verification-code handling)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.forge.sdk.workflow import context_manager as cm
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.forge.taskv3 import auth_tools
from skyvern.services import otp_service
from skyvern.services.otp_service import OTPValue
from skyvern.utils.secret_redaction import redact_secrets_from_bytes


def _task(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "task_id": "tsk_1",
        "workflow_run_id": None,
        "totp_verification_url": None,
        "totp_identifier": None,
        "navigation_payload": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_auth_tools_absent_without_code_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_tools, "has_credential_totp_candidate", lambda *_a, **_k: False)
    tools, guidance = auth_tools.build_auth_tools(_task())
    assert tools == [] and guidance == ""


def test_build_auth_tools_bare_task_no_workflow_lookup() -> None:
    # Unmocked: a bare task (workflow_run_id=None) with no code source returns no tool without any
    # workflow-run-context lookup — has_credential_totp_candidate short-circuits on the falsy run id and
    # never reaches the getter that raises when a context isn't registered.
    tools, guidance = auth_tools.build_auth_tools(_task())
    assert tools == [] and guidance == ""


def test_has_credential_totp_candidate_unregistered_context_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-None workflow_run_id with no registered context returns False without raising: the getter
    # raises WorkflowRunContextNotInitialized, so the gate checks has_workflow_run_context first.
    monkeypatch.setattr(otp_service.app, "WORKFLOW_CONTEXT_MANAGER", WorkflowContextManager())
    assert otp_service.has_credential_totp_candidate("wr_unregistered") is False


def test_try_generate_totp_from_credential_unregistered_context_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same dead-guard class as has_credential_totp_candidate, and it fires first inside resolve_otp_value:
    # an unregistered context must yield None, not raise (the raise would escape into the v1/CUA
    # get_verification_code path, whose callers don't catch WorkflowRunContextNotInitialized).
    monkeypatch.setattr(otp_service.app, "WORKFLOW_CONTEXT_MANAGER", WorkflowContextManager())
    assert otp_service.try_generate_totp_from_credential("wr_unregistered") is None


def test_build_auth_tools_absent_for_verification_url_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # A totp_verification_url task stays on the step engine (the v3 dispatch gate excludes it), so it
    # never reaches this builder — the tool must not be offered on that source alone.
    monkeypatch.setattr(auth_tools, "has_credential_totp_candidate", lambda *_a, **_k: False)
    tools, guidance = auth_tools.build_auth_tools(_task(totp_verification_url="https://totp.example"))
    assert tools == [] and guidance == ""


def test_build_auth_tools_present_with_totp_identifier() -> None:
    tools, guidance = auth_tools.build_auth_tools(_task(totp_identifier="user@example.com"))
    assert [t.name for t in tools] == ["get_verification_code"]
    assert "verification code" in guidance.lower()


def test_build_auth_tools_present_with_payload_only_totp_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # navigation_payload is resolve_otp_value's first waterfall source; the tool must be offered
    # from it alone, with no totp_identifier and no credential candidate.
    monkeypatch.setattr(auth_tools, "has_credential_totp_candidate", lambda *_a, **_k: False)
    tools, guidance = auth_tools.build_auth_tools(_task(navigation_payload={"mfa_code": "123456"}))
    assert [t.name for t in tools] == ["get_verification_code"]
    assert "verification code" in guidance.lower()


def test_build_auth_tools_absent_with_no_code_source_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_tools, "has_credential_totp_candidate", lambda *_a, **_k: False)
    tools, guidance = auth_tools.build_auth_tools(_task(navigation_payload={"unrelated_field": "value"}))
    assert tools == [] and guidance == ""


def test_build_auth_tools_absent_with_magic_link_only_payload_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # A payload-embedded URL resolves to a magic link, not a TOTP code; get_verification_code hard-rejects
    # non-TOTP values, so offering the tool here would be guaranteed to error.
    monkeypatch.setattr(auth_tools, "has_credential_totp_candidate", lambda *_a, **_k: False)
    tools, guidance = auth_tools.build_auth_tools(
        _task(navigation_payload={"verification_link": "https://example.test/x"})
    )
    assert tools == [] and guidance == ""


@pytest.mark.asyncio
async def test_get_verification_code_resolves_and_registers_for_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth_tools, "resolve_otp_value", AsyncMock(return_value=OTPValue(value="123456", type=OTPType.TOTP))
    )
    tools, _ = auth_tools.build_auth_tools(_task(totp_identifier="user@example.com"))
    ctx = SkyvernContext(task_id="tsk_1")
    skyvern_context.set(ctx)
    try:
        result = await tools[0].handler({})
    finally:
        skyvern_context.reset()
    assert result.status == "ok" and "123456" in result.content
    # Registered for redaction on the task context (task-scoped, so a bare task is covered).
    assert "123456" in ctx.runtime_secret_values


@pytest.mark.asyncio
async def test_get_verification_code_no_code_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=None))
    tools, _ = auth_tools.build_auth_tools(_task(totp_identifier="user@example.com"))
    ctx = SkyvernContext(task_id="tsk_1")
    skyvern_context.set(ctx)
    try:
        result = await tools[0].handler({})
    finally:
        skyvern_context.reset()
    assert result.status == "error"
    assert ctx.runtime_secret_values == set()


@pytest.mark.asyncio
async def test_get_verification_code_ignores_magic_link_value(monkeypatch: pytest.MonkeyPatch) -> None:
    # A URL (magic link) is not a code — Phase 1 handles codes only, so it must not be typed as one.
    monkeypatch.setattr(
        auth_tools,
        "resolve_otp_value",
        AsyncMock(return_value=OTPValue(value="https://example.com/signin?token=abc", type=None)),
    )
    tools, _ = auth_tools.build_auth_tools(_task(totp_identifier="user@example.com"))
    ctx = SkyvernContext(task_id="tsk_1")
    skyvern_context.set(ctx)
    try:
        result = await tools[0].handler({})
    finally:
        skyvern_context.reset()
    assert result.status == "error"
    assert ctx.runtime_secret_values == set()  # a rejected magic-link URL must not be registered


def test_registered_code_scrubbed_when_redaction_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    # The enabled global flag exercises the same gate used by bare-task artifact persistence.
    monkeypatch.setattr(cm.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    wcm = WorkflowContextManager()
    ctx = SkyvernContext(task_id="tsk_1")
    ctx.register_secret_value("482913")
    skyvern_context.set(ctx)
    try:
        secret_values = wcm.get_secret_values_for_run(None)
        payload = b'{"role": "tool", "content": "verification_code: 482913"}, {"type": "482913"}'
        redacted = redact_secrets_from_bytes(payload, secret_values)
    finally:
        skyvern_context.reset()
    assert b"482913" not in redacted


def test_get_secret_values_for_run_standalone_task_uses_global_artifact_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cm.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    wcm = WorkflowContextManager()
    ctx = SkyvernContext(task_id="tsk_1")
    ctx.register_secret_value("987654")
    ctx.register_secret_value("12")  # too short to redact
    skyvern_context.set(ctx)
    try:
        assert wcm.get_secret_values_for_run(None) == {"987654"}
        assert wcm.get_secret_values_for_run(None, exclude_runtime_otp=True) == set()
    finally:
        skyvern_context.reset()


def test_get_secret_values_for_run_standalone_task_respects_disabled_global_artifact_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cm.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", False)
    wcm = WorkflowContextManager()
    ctx = SkyvernContext(task_id="tsk_1")
    ctx.register_secret_value("987654")
    skyvern_context.set(ctx)
    try:
        assert wcm.get_secret_values_for_run(None) == set()
        assert wcm.get_secret_values_for_run(None, respect_artifact_redaction_flag=False) == {"987654"}
        assert (
            wcm.get_secret_values_for_run(
                None,
                exclude_runtime_otp=True,
                respect_artifact_redaction_flag=False,
            )
            == set()
        )
    finally:
        skyvern_context.reset()
