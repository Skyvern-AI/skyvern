from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, TypeVar
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog.testing

from skyvern.errors.errors import (
    MissingTOTPSourceError,
    TimeoutGetTOTPVerificationCodeError,
)
from skyvern.exceptions import NoTOTPVerificationCodeFound, SkyvernException
from skyvern.forge.agent import _build_totp_timeout_reasoning
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import TOTPCodeModel
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.services import otp_service
from skyvern.services.otp_service import poll_otp_value
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import TerminateAction, VerificationCodeAction
from skyvern.webeye.actions.parse_actions import generate_cua_fallback_actions
from tests.unit.conftest import SqliteEngineFactory
from tests.unit.scoped_asyncio import ScopedAsyncio


def _make_task(
    *,
    totp_verification_url: str | None = None,
    totp_identifier: str | None = None,
    organization_id: str | None = "o_test",
    workflow_run_id: str | None = "wr_test",
    otp_request_id: str | None = None,
) -> MagicMock:
    task = MagicMock()
    task.otp_request_id = otp_request_id
    task.task_id = "tsk_test"
    task.organization_id = organization_id
    task.workflow_run_id = workflow_run_id
    task.workflow_permanent_id = "wpid_test"
    task.totp_verification_url = totp_verification_url
    task.totp_identifier = totp_identifier
    task.navigation_payload = None
    task.navigation_goal = "log in"
    task.url = "https://app.example.com/login"
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
        "skyvern.services.otp_service.extract_totp_from_navigation_inputs",
        lambda *_: None,
    )
    monkeypatch.setattr(
        "skyvern.services.otp_service.try_generate_totp_from_credential",
        lambda *_: None,
    )


# ---------- get_verification_code branch ----------


@pytest.mark.asyncio
async def test_get_verification_code_with_no_source_emits_missing_totp_source(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _make_task()
    step = _make_step()
    _patch_cua_fallback_common(monkeypatch, "get_verification_code")
    monkeypatch.setattr(
        "skyvern.webeye.actions.parse_actions.has_credential_totp_candidate",
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
        "skyvern.webeye.actions.parse_actions.has_credential_totp_candidate",
        lambda *_: False,
    )

    async def _no_poll(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr("skyvern.services.otp_service.poll_otp_value", _no_poll)

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
        "skyvern.webeye.actions.parse_actions.has_credential_totp_candidate",
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
        "skyvern.services.otp_service.try_generate_totp_from_credential",
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
        "skyvern.webeye.actions.parse_actions.has_credential_totp_candidate",
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
        "skyvern.webeye.actions.parse_actions.has_credential_totp_candidate",
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

    captured: dict[str, object] = {}

    async def _no_poll(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return None

    monkeypatch.setattr("skyvern.webeye.actions.parse_actions.poll_otp_value", _no_poll)

    actions = await generate_cua_fallback_actions(task, step, assistant_message=None, reasoning=None)

    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, TerminateAction)
    assert action.errors
    assert action.errors[0].error_code == "OTP_ERROR"
    assert "No magic link found" in action.reasoning
    assert captured["task_url"] == "https://app.example.com/login"


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


# ---------- no code value in any diagnostic, reasoning or log ----------

_CODE = "482913"
_T = TypeVar("_T")
_IDENTIFIER = "otp-user@example.test"


class _FakeClock:
    def __init__(self) -> None:
        self.now = datetime.utcnow()

    def utcnow(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


async def _install_otp_store(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_engine_factory: SqliteEngineFactory,
    row_overrides: dict[str, Any] | None,
) -> datetime:
    """Real repository queries on SQLite, so the run and expiry filters the diagnostics describe actually apply.
    Returns the fake run start, one minute before the clock's start."""
    clock = _FakeClock()
    engine = sqlite_engine_factory()
    agent_db = AgentDB(database_string=str(engine.url), debug_enabled=True, db_engine=engine)
    if row_overrides is not None:
        expires_in = row_overrides.pop("expires_in", None)
        created_ago = row_overrides.pop("created_ago", None)
        lookup_hangs = row_overrides.pop("lookup_hangs", False)
        if created_ago is not None:
            row_overrides["created_at"] = clock.now - created_ago
        async with agent_db.Session() as session:
            session.add(
                TOTPCodeModel(
                    **{
                        "totp_code_id": "otp_row",
                        "organization_id": "o_test",
                        "totp_identifier": _IDENTIFIER,
                        "content": f"Your code is {_CODE}.",
                        "code": _CODE,
                        "otp_type": OTPType.TOTP,
                        "parse_status": "parsed",
                        "expired_at": clock.now + expires_in if expires_in is not None else None,
                        **row_overrides,
                    }
                )
            )
            await session.commit()
    if row_overrides is not None and lookup_hangs:

        async def _never_answers(**_kwargs: object) -> list[str]:
            await asyncio.Event().wait()
            return []

        monkeypatch.setattr(agent_db.otp, "get_other_run_otp_code_ids", _never_answers)
        monkeypatch.setattr(otp_service, "_OTHER_RUN_LOOKUP_TIMEOUT_SECONDS", 0.01)
    fake_app = MagicMock()
    fake_app.DATABASE = agent_db
    fake_app.AGENT_FUNCTION.enforce_totp_identifier_credential_use = AsyncMock()
    fake_app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts = {}
    monkeypatch.setattr(otp_service, "app", fake_app)
    monkeypatch.setattr(otp_service, "datetime", SimpleNamespace(utcnow=clock.utcnow))
    monkeypatch.setattr(otp_service, "asyncio", ScopedAsyncio(sleep=clock.sleep))
    monkeypatch.setattr(otp_service, "_get_otp_value_from_email", AsyncMock(return_value=None))
    return clock.now - timedelta(minutes=1)


async def _bounded(awaitable: Awaitable[_T]) -> _T:
    # A poll that waits on a stalled lookup fails here instead of at the suite's per-test timeout.
    return await asyncio.wait_for(awaitable, timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row_overrides, strict, rejected, expected_fact",
    [
        pytest.param({"workflow_run_id": "wr_test"}, False, False, None, id="cua_code_received"),
        pytest.param(
            {"workflow_run_id": "wr_other"},
            False,
            False,
            "1 stored OTP message belonged to another run",
            id="other_run",
        ),
        pytest.param(
            {"task_id": "tsk_other"}, False, False, "1 stored OTP message belonged to another run", id="other_task"
        ),
        pytest.param(
            {"workflow_run_id": "wr_other", "created_ago": timedelta(minutes=2)},
            False,
            False,
            "1 stored OTP message belonged to another run",
            id="other_run_before_run_start",
        ),
        # The skip counts cover every stored message type, so a code poll must not call a sign-in link a code.
        pytest.param(
            {"workflow_run_id": "wr_other", "otp_type": OTPType.MAGIC_LINK},
            False,
            False,
            "1 stored OTP message belonged to another run",
            id="other_run_magic_link_under_code_poll",
        ),
        # The other-run lookup runs after the deadline, so a stalled one is dropped rather than waited on.
        pytest.param(
            {"workflow_run_id": "wr_other", "lookup_hangs": True},
            False,
            False,
            "no code has been stored",
            id="other_run_lookup_hangs",
        ),
        pytest.param(
            {"expires_in": timedelta(minutes=-1)}, False, False, "1 stored OTP message had expired", id="expired"
        ),
        pytest.param(
            {"workflow_run_id": "wr_test"}, False, True, "1 stored OTP message was already tried", id="already_tried"
        ),
        pytest.param(None, True, False, "no eligible code bound to this run's request was found", id="strict_request"),
    ],
)
async def test_otp_diagnostics_never_carry_a_code_value(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_engine_factory: SqliteEngineFactory,
    row_overrides: dict[str, Any] | None,
    strict: bool,
    rejected: bool,
    expected_fact: str | None,
) -> None:
    run_started_at = await _install_otp_store(
        monkeypatch, sqlite_engine_factory, dict(row_overrides) if row_overrides else row_overrides
    )
    _patch_cua_fallback_common(monkeypatch, "get_verification_code")
    monkeypatch.setattr("skyvern.webeye.actions.parse_actions.has_credential_totp_candidate", lambda *_: False)
    task = _make_task(totp_identifier=_IDENTIFIER, otp_request_id="otpreq_test" if strict else None)
    texts: list[str] = []
    sources_checked: str | None = None

    with structlog.testing.capture_logs() as logs:
        if expected_fact is not None:
            with pytest.raises(NoTOTPVerificationCodeFound) as exc_info:
                await _bounded(
                    poll_otp_value(
                        organization_id="o_test",
                        task_id=task.task_id,
                        workflow_run_id=task.workflow_run_id,
                        otp_request_id=task.otp_request_id,
                        totp_identifier=_IDENTIFIER,
                        created_after=run_started_at,
                        expected_otp_type=OTPType.TOTP,
                        rejected_code_hash=hashlib.sha256(_CODE.encode()).hexdigest() if rejected else None,
                    )
                )
            sources_checked = exc_info.value.sources_checked
            assert sources_checked is not None
            assert expected_fact in sources_checked
            if "has been stored" not in expected_fact:
                assert "has been stored" not in sources_checked
            assert "stored code" not in sources_checked
            assert _IDENTIFIER not in sources_checked
            failure_reason = _build_totp_timeout_reasoning(
                task, exc_info.value.webhook_diagnostics, exc_info.value.sources_checked
            )
            assert expected_fact in failure_reason
            texts += [sources_checked, str(exc_info.value), failure_reason]
        # The CUA path never passes a rejected hash, so for that row it would simply deliver the code.
        if not rejected:
            (action,) = await _bounded(
                generate_cua_fallback_actions(task, _make_step(), assistant_message=None, reasoning=None)
            )
            if expected_fact is None:
                assert isinstance(action, VerificationCodeAction)
                assert action.verification_code == _CODE
            else:
                assert isinstance(action, TerminateAction)
                assert expected_fact in (action.reasoning or "")
            texts += [action.reasoning or "", action.intention or ""]

    timeouts = [record for record in logs if record["event"].startswith("Polling otp value timed out")]
    assert len(timeouts) == (0 if expected_fact is None else 1 if rejected else 2)
    assert all(record["otp_sources_checked"] == sources_checked for record in timeouts)
    for text in texts:
        assert _CODE not in text
    assert _CODE not in repr(logs)


# ---------- agent.py _build_totp_timeout_reasoning helper ----------


@pytest.mark.parametrize(
    "totp_verification_url, expected, absent",
    [
        # URL precedence mirrors poll_otp_value's if/elif. Query string stripped to
        # avoid leaking auth tokens into webhook payloads.
        (
            "https://example.invalid/totp?token=secret",
            "totp_verification_url=https://example.invalid/totp",
            ["token=secret", "totp_identifier="],
        ),
        (None, "totp_identifier=user@example.com", ["totp_verification_url="]),
    ],
)
def test_non_cua_timeout_reasoning_names_the_polled_source(
    totp_verification_url: str | None, expected: str, absent: list[str]
) -> None:
    task = _make_task(totp_verification_url=totp_verification_url, totp_identifier="user@example.com")
    reasoning = _build_totp_timeout_reasoning(task)
    assert expected in reasoning
    for text in absent:
        assert text not in reasoning


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
