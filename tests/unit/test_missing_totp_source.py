from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.errors.errors import (
    MissingTOTPSourceError,
    TimeoutGetTOTPVerificationCodeError,
)
from skyvern.exceptions import SkyvernException
from skyvern.forge.agent import _build_totp_timeout_reasoning
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import TerminateAction, VerificationCodeAction
from skyvern.webeye.actions.parse_actions import generate_cua_fallback_actions


def _make_task(
    *,
    totp_verification_url: str | None = None,
    totp_identifier: str | None = None,
    organization_id: str | None = "o_test",
    workflow_run_id: str | None = "wr_test",
) -> MagicMock:
    task = MagicMock()
    task.task_id = "tsk_test"
    task.organization_id = organization_id
    task.workflow_run_id = workflow_run_id
    task.workflow_permanent_id = "wpid_test"
    task.totp_verification_url = totp_verification_url
    task.totp_identifier = totp_identifier
    task.navigation_payload = None
    task.navigation_goal = "log in"
    return task


def _make_step() -> MagicMock:
    step = MagicMock()
    step.step_id = "stp_test"
    step.order = 0
    return step


def _patch_cua_fallback_common(monkeypatch: pytest.MonkeyPatch, action_type: str) -> None:
    """Patch the LLM handler and credential helpers common to most tests."""

    async def _fake_llm(*args, **kwargs):  # type: ignore[no-untyped-def]
        return {"action": action_type}

    monkeypatch.setattr("skyvern.webeye.actions.parse_actions.app.LLM_API_HANDLER", AsyncMock(side_effect=_fake_llm))
    monkeypatch.setattr(
        "skyvern.webeye.actions.parse_actions.extract_totp_from_navigation_inputs",
        lambda *_: None,
    )
    monkeypatch.setattr(
        "skyvern.webeye.actions.parse_actions.try_generate_totp_from_credential",
        lambda *_: None,
    )


# ---------- get_verification_code branch ----------


@pytest.mark.asyncio
async def test_get_verification_code_with_no_source_emits_missing_totp_source(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _make_task()
    step = _make_step()
    _patch_cua_fallback_common(monkeypatch, "get_verification_code")
    monkeypatch.setattr(
        "skyvern.webeye.actions.parse_actions._has_credential_totp_candidate",
        lambda *_: False,
    )

    actions = await generate_cua_fallback_actions(task, step, assistant_message=None, reasoning=None)

    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, TerminateAction)
    assert action.action_type == ActionType.TERMINATE
    assert action.errors
    assert action.errors[0].error_code == MissingTOTPSourceError().error_code
    assert "no TOTP source is configured" in action.reasoning


@pytest.mark.asyncio
async def test_get_verification_code_with_url_configured_emits_configured_but_empty_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # URL set, poll returns None (silent delivery or org-token-missing) → OTP_ERROR
    # so programmatic consumers can distinguish from wrong-type case.
    task = _make_task(totp_verification_url="https://example.invalid/totp?secret=abc123")
    step = _make_step()
    _patch_cua_fallback_common(monkeypatch, "get_verification_code")
    monkeypatch.setattr(
        "skyvern.webeye.actions.parse_actions._has_credential_totp_candidate",
        lambda *_: False,
    )

    async def _no_poll(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr("skyvern.webeye.actions.parse_actions.poll_otp_value", _no_poll)

    actions = await generate_cua_fallback_actions(task, step, assistant_message=None, reasoning=None)

    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, TerminateAction)
    assert all(err.error_code != MissingTOTPSourceError().error_code for err in action.errors)
    assert action.errors
    assert action.errors[0].error_code == "OTP_ERROR"
    # Reasoning surfaces the polled URL, with the secret-bearing query string stripped.
    assert "Configured TOTP source" in action.reasoning
    assert "totp_verification_url=https://example.invalid/totp" in action.reasoning
    assert "secret=abc123" not in action.reasoning


@pytest.mark.asyncio
async def test_get_verification_code_with_only_credential_configured_but_failing_emits_otp_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When the ONLY configured source is a credential and generation fails (returns
    # None), the customer must get OTP_ERROR with a credential-specific reasoning —
    # not silently fall into the wrong-type case with no errors.
    task = _make_task()
    step = _make_step()
    _patch_cua_fallback_common(monkeypatch, "get_verification_code")
    monkeypatch.setattr(
        "skyvern.webeye.actions.parse_actions._has_credential_totp_candidate",
        lambda *_: True,
    )

    actions = await generate_cua_fallback_actions(task, step, assistant_message=None, reasoning=None)

    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, TerminateAction)
    assert action.errors
    assert action.errors[0].error_code == "OTP_ERROR"
    assert "credential parameter with TOTP" in action.reasoning


@pytest.mark.asyncio
async def test_get_verification_code_with_credential_totp_returns_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Guards against the diagnostic branches swallowing valid runs.
    task = _make_task()
    step = _make_step()
    _patch_cua_fallback_common(monkeypatch, "get_verification_code")

    fake_otp_value = MagicMock()
    fake_otp_value.value = "123456"
    fake_otp_value.get_otp_type.return_value = OTPType.TOTP

    monkeypatch.setattr(
        "skyvern.webeye.actions.parse_actions.try_generate_totp_from_credential",
        lambda *_: fake_otp_value,
    )

    actions = await generate_cua_fallback_actions(task, step, assistant_message=None, reasoning=None)

    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, VerificationCodeAction)
    assert action.verification_code == "123456"


@pytest.mark.asyncio
async def test_get_verification_code_with_multiple_unselected_credentials_emits_missing_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Multi-no-active: try_generate_totp_from_credential returns None per
    # otp_service.py's selection logic, and the helper reports no usable source.
    # MISSING_TOTP_SOURCE must fire — not OTP_ERROR — because nothing was actually
    # configured for THIS run.
    task = _make_task()
    step = _make_step()
    _patch_cua_fallback_common(monkeypatch, "get_verification_code")
    monkeypatch.setattr(
        "skyvern.webeye.actions.parse_actions._has_credential_totp_candidate",
        lambda *_: False,  # mirroring multi-no-active selection result
    )

    actions = await generate_cua_fallback_actions(task, step, assistant_message=None, reasoning=None)

    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, TerminateAction)
    assert action.errors
    assert action.errors[0].error_code == MissingTOTPSourceError().error_code


# ---------- get_magic_link branch ----------


@pytest.mark.asyncio
async def test_get_magic_link_with_no_source_emits_missing_totp_source(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _make_task()
    step = _make_step()
    _patch_cua_fallback_common(monkeypatch, "get_magic_link")

    actions = await generate_cua_fallback_actions(task, step, assistant_message=None, reasoning=None)

    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, TerminateAction)
    assert action.errors
    assert action.errors[0].error_code == MissingTOTPSourceError().error_code


@pytest.mark.asyncio
async def test_get_magic_link_with_credential_candidate_still_emits_missing_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Magic-link cannot be satisfied by credential TOTP (credentials emit only
    # OTPType.TOTP). The classifier must not consult the credential probe here.
    task = _make_task()
    step = _make_step()
    _patch_cua_fallback_common(monkeypatch, "get_magic_link")

    # Even if _has_credential_totp_candidate would return True, the magic-link
    # branch must NOT consult it. Patch to True to prove the branch ignores it.
    monkeypatch.setattr(
        "skyvern.webeye.actions.parse_actions._has_credential_totp_candidate",
        lambda *_: True,
    )

    actions = await generate_cua_fallback_actions(task, step, assistant_message=None, reasoning=None)

    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, TerminateAction)
    assert action.errors
    assert action.errors[0].error_code == MissingTOTPSourceError().error_code


@pytest.mark.asyncio
async def test_get_magic_link_with_url_but_poll_returns_none_emits_otp_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Symmetry with get_verification_code: configured-but-empty must attach
    # OTP_ERROR so webhook consumers can branch failures the same way across
    # both OTP-type paths.
    task = _make_task(totp_verification_url="https://example.invalid/magic?secret=abc")
    step = _make_step()
    _patch_cua_fallback_common(monkeypatch, "get_magic_link")

    async def _no_poll(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr("skyvern.webeye.actions.parse_actions.poll_otp_value", _no_poll)

    actions = await generate_cua_fallback_actions(task, step, assistant_message=None, reasoning=None)

    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, TerminateAction)
    assert action.errors
    assert action.errors[0].error_code == "OTP_ERROR"
    assert "No magic link found" in action.reasoning


@pytest.mark.asyncio
async def test_get_magic_link_with_failed_delivery_emits_otp_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # FailedToGetTOTPVerificationCode -> broken delivery endpoint, not silent
    # timeout. Must attach OTP_ERROR with the underlying reason surfaced.
    from skyvern.exceptions import FailedToGetTOTPVerificationCode

    task = _make_task(totp_verification_url="https://example.invalid/magic")
    step = _make_step()
    _patch_cua_fallback_common(monkeypatch, "get_magic_link")

    async def _fail_poll(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise FailedToGetTOTPVerificationCode(reason="upstream 502")

    monkeypatch.setattr("skyvern.webeye.actions.parse_actions.poll_otp_value", _fail_poll)

    actions = await generate_cua_fallback_actions(task, step, assistant_message=None, reasoning=None)

    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, TerminateAction)
    assert action.errors
    assert action.errors[0].error_code == "OTP_ERROR"
    assert "upstream 502" in action.reasoning


# ---------- agent.py _build_totp_timeout_reasoning helper ----------


def test_non_cua_timeout_reasoning_uses_url_when_present() -> None:
    # URL precedence mirrors poll_otp_value's if/elif. Query string stripped to
    # avoid leaking auth tokens into webhook payloads.
    task = _make_task(
        totp_verification_url="https://example.invalid/totp?token=secret",
        totp_identifier="user@example.com",
    )
    reasoning = _build_totp_timeout_reasoning(task)
    assert "totp_verification_url=https://example.invalid/totp" in reasoning
    assert "token=secret" not in reasoning
    # Identifier is suppressed — only the URL was actually polled.
    assert "totp_identifier=" not in reasoning


def test_non_cua_timeout_reasoning_uses_identifier_when_url_absent() -> None:
    task = _make_task(totp_identifier="user@example.com")
    reasoning = _build_totp_timeout_reasoning(task)
    assert "totp_identifier=user@example.com" in reasoning
    assert "totp_verification_url=" not in reasoning


def test_non_cua_timeout_reasoning_raises_when_called_without_source() -> None:
    # SkyvernException (not assert) so the guard survives python -O.
    task = _make_task(totp_verification_url=None, totp_identifier=None)
    with pytest.raises(SkyvernException):
        _build_totp_timeout_reasoning(task)


# ---------- wire-format pins ----------


def test_missing_totp_source_error_to_user_defined_error_shape() -> None:
    # Pins the wire format. Customers may have alerts keyed on the exact string.
    err = MissingTOTPSourceError().to_user_defined_error()
    assert err.error_code == "MISSING_TOTP_SOURCE"
    assert err.confidence_float == 1.0
    assert "totp_verification_url" in err.reasoning or "totp_identifier" in err.reasoning


def test_timeout_error_unchanged_for_backcompat() -> None:
    # OTP_TIMEOUT wire string must remain stable — customers may key alerting on it.
    err = TimeoutGetTOTPVerificationCodeError().to_user_defined_error()
    assert err.error_code == "OTP_TIMEOUT"
