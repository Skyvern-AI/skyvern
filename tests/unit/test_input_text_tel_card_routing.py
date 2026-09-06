from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import (
    CaptchaSolveError,
    ImaginarySecretValue,
    InvalidElementForTextInput,
    MissingElement,
    MultipleElementsFound,
    PhoneNumberInputBrowserInteractionFailed,
    PhoneNumberInputBrowserValidityMismatch,
    PhoneNumberInputMismatch,
)
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants
from skyvern.webeye.actions.actions import (
    Action,
    ActionType,
    InputOrSelectContext,
    InputTextAction,
    TelInputOutcome,
    TelInputStrategy,
)
from skyvern.webeye.actions.handler import ActionHandler, handle_input_text_action_direct
from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess
from tests.unit.conftest import make_input_element_mock
from tests.unit.helpers import make_organization, make_step, make_task

_NOW = datetime.now(UTC)
_ORG = make_organization(_NOW)
_TASK = make_task(_NOW, _ORG, navigation_payload={}, navigation_goal="Fill checkout contact fields")
_STEP = make_step(_NOW, _TASK, step_id="stp-tel-card-routing", status=StepStatus.created, order=0, output=None)

VISA_16 = "4539578763621486"


def _mock_input(attrs: dict[str, str | None]) -> MagicMock:
    el = make_input_element_mock(attrs=attrs)
    el.is_raw_input = AsyncMock(return_value=True)
    return el


async def _run_input_text(
    el: MagicMock,
    text: str,
    *,
    resolved: str | None = None,
    tel_fix_enabled: bool = True,
    tel_verify_side_effect: list[Exception | int] | None = None,
    tag_name: str = "input",
    blocker: MagicMock | None = None,
    input_or_select_context: InputOrSelectContext | None = None,
    current_value: str = "",
) -> tuple[list, AsyncMock, AsyncMock, AsyncMock, MagicMock, AsyncMock]:
    # Production always parses a real InputOrSelectContext (the parse never returns None), so default to an
    # ordinary all-unset context here -- a None default would exercise a branch that cannot occur in prod.
    resolved_context = input_or_select_context if input_or_select_context is not None else InputOrSelectContext()
    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=el)
    if blocker is not None:
        # find_blocking_element() retargets the fill from `el` to this editable blocker.
        el.find_blocking_element = AsyncMock(return_value=(blocker, True))

    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    inc.get_incremental_element_tree = AsyncMock(return_value=[])

    skyvern_frame = MagicMock()
    skyvern_frame.safe_wait_for_animation_end = AsyncMock()

    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"AADC": {"tagName": tag_name}}

    card_readback = AsyncMock(return_value=None)
    tel_verify = (
        AsyncMock(return_value=10) if tel_verify_side_effect is None else AsyncMock(side_effect=tel_verify_side_effect)
    )
    phone_format = AsyncMock(return_value=text)
    warning_log = MagicMock()
    secret_readback = AsyncMock(return_value=None)
    # A resolved secret differs from the action's placeholder text; when equal, the value is not a secret.
    secret_return = text if resolved is None else resolved

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new=AsyncMock(return_value=skyvern_frame)),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value=current_value)),
        patch(
            "skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task",
            return_value=secret_return,
        ),
        patch(
            "skyvern.webeye.actions.handler._get_input_or_select_context",
            new=AsyncMock(return_value=resolved_context),
        ),
        patch("skyvern.webeye.actions.handler._is_tel_digit_fix_enabled", new=AsyncMock(return_value=tel_fix_enabled)),
        patch("skyvern.webeye.actions.handler.check_phone_number_format", new=phone_format),
        patch("skyvern.webeye.actions.handler._fill_card_number_with_readback", new=card_readback),
        patch("skyvern.webeye.actions.handler._fill_secret_with_readback", new=secret_readback),
        patch("skyvern.webeye.actions.handler._verify_tel_input_after_fill", new=tel_verify),
        patch("skyvern.webeye.actions.handler.LOG.warning", new=warning_log),
    ):
        results = await handle_input_text_action_direct(
            action=InputTextAction(element_id="AADC", text=text, reasoning="fill field"),
            page=MagicMock(),
            scraped_page=scraped_page,
            task=_TASK,
            step=_STEP,
        )

    return results, card_readback, tel_verify, phone_format, warning_log, secret_readback


def test_tel_input_outcome_is_excluded_from_action_serialization() -> None:
    action = Action(action_type=ActionType.INPUT_TEXT)
    action.tel_input_outcome = TelInputOutcome(
        flag_enabled=True,
        final_element_id="AADC",
        strategy=TelInputStrategy.sequential_national,
        expected_digit_count=10,
        actual_digit_count=10,
        browser_valid=True,
        attempt_count=1,
        retargeted=False,
    )

    assert "tel_input_outcome" not in action.model_dump()
    assert "tel_input_outcome" not in action.model_dump(mode="json")


@pytest.mark.asyncio
async def test_action_handler_logs_tel_outcome_once_without_phone_value() -> None:
    synthetic_phone = "2245550199"
    action = InputTextAction(element_id="AADC", text=synthetic_phone, reasoning="fill phone")
    outcome = TelInputOutcome(
        flag_enabled=True,
        final_element_id="AADC",
        strategy=TelInputStrategy.sequential_national,
        expected_digit_count=10,
        actual_digit_count=10,
        browser_valid=True,
        attempt_count=1,
        retargeted=False,
    )

    async def patched_input_handler(action: InputTextAction, **_kwargs: object) -> list[ActionSuccess]:
        action.tel_input_outcome = outcome
        return [ActionSuccess()]

    with (
        patch("skyvern.webeye.actions.handler._handle_input_text_action", new=patched_input_handler),
        patch("skyvern.webeye.actions.handler.LOG.info") as log_info,
    ):
        results = await handle_input_text_action_direct(
            action=action,
            page=MagicMock(),
            scraped_page=MagicMock(),
            task=_TASK,
            step=_STEP,
        )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    assert action.tel_input_outcome is None
    terminal_logs = [call for call in log_info.call_args_list if call.args and call.args[0] == "tel_input_outcome"]
    assert len(terminal_logs) == 1
    terminal_kwargs = terminal_logs[0].kwargs
    assert terminal_kwargs["sampling"] is False
    assert terminal_kwargs["terminal_result"] == "completed"
    assert terminal_kwargs["exception_type"] is None
    assert synthetic_phone not in str(terminal_kwargs)


@pytest.mark.asyncio
async def test_dispatcher_logs_final_failure_after_successful_tel_fill() -> None:
    action = InputTextAction(element_id="AADC", text="224-555-0199", reasoning="fill phone")
    outcome = TelInputOutcome(
        flag_enabled=True,
        final_element_id="AADC",
        strategy=TelInputStrategy.sequential_national,
        expected_digit_count=10,
        actual_digit_count=10,
        browser_valid=True,
        attempt_count=1,
        retargeted=False,
    )

    async def successful_input_handler(action: InputTextAction, *_args: object) -> list[ActionSuccess]:
        action.tel_input_outcome = outcome
        return [ActionSuccess()]

    app_mock = MagicMock()
    app_mock.AGENT_FUNCTION.wait_for_challenge_solver = AsyncMock(
        side_effect=[None, RuntimeError("post-handler failure")]
    )
    with (
        patch("skyvern.webeye.actions.handler.app", app_mock),
        patch("skyvern.webeye.actions.handler.check_for_invalid_web_action", return_value=None),
        patch.dict(ActionHandler._handled_action_types, {ActionType.INPUT_TEXT: successful_input_handler}, clear=True),
        patch.dict(ActionHandler._setup_action_types, {}, clear=True),
        patch.dict(ActionHandler._teardown_action_types, {}, clear=True),
        patch("skyvern.webeye.actions.handler.LLMCallerManager.get_llm_caller", return_value=None),
        patch("skyvern.webeye.actions.handler.LOG.info") as log_info,
    ):
        results = await ActionHandler._handle_action(
            scraped_page=MagicMock(),
            task=_TASK,
            step=_STEP,
            page=MagicMock(),
            action=action,
        )

    assert isinstance(results[-1], ActionFailure)
    terminal_logs = [call for call in log_info.call_args_list if call.args and call.args[0] == "tel_input_outcome"]
    assert len(terminal_logs) == 1
    assert terminal_logs[0].kwargs["terminal_result"] == "failed"
    assert terminal_logs[0].kwargs["exception_type"] == "RuntimeError"
    assert action.tel_input_outcome is None


@pytest.mark.asyncio
async def test_dispatcher_tel_outcome_emission_failure_preserves_success() -> None:
    action = InputTextAction(element_id="AADC", text="224-555-0199", reasoning="fill phone")
    outcome = TelInputOutcome(
        flag_enabled=True,
        final_element_id="AADC",
        strategy=TelInputStrategy.sequential_national,
        expected_digit_count=10,
        actual_digit_count=10,
        browser_valid=True,
        attempt_count=1,
        retargeted=False,
    )
    decided_result = ActionSuccess()

    async def successful_input_handler(action: InputTextAction, *_args: object) -> list[ActionSuccess]:
        action.tel_input_outcome = outcome
        return [decided_result]

    def fail_terminal_log(event: object, *_args: object, **_kwargs: object) -> None:
        if event == "tel_input_outcome":
            raise RuntimeError("synthetic telemetry failure")

    app_mock = MagicMock()
    app_mock.AGENT_FUNCTION.wait_for_challenge_solver = AsyncMock(return_value=None)
    with (
        patch("skyvern.webeye.actions.handler.app", app_mock),
        patch("skyvern.webeye.actions.handler.check_for_invalid_web_action", return_value=None),
        patch.dict(ActionHandler._handled_action_types, {ActionType.INPUT_TEXT: successful_input_handler}, clear=True),
        patch.dict(ActionHandler._setup_action_types, {}, clear=True),
        patch.dict(ActionHandler._teardown_action_types, {}, clear=True),
        patch("skyvern.webeye.actions.handler.LLMCallerManager.get_llm_caller", return_value=None),
        patch("skyvern.webeye.actions.handler.LOG.info", side_effect=fail_terminal_log),
    ):
        results = await ActionHandler._handle_action(
            scraped_page=MagicMock(),
            task=_TASK,
            step=_STEP,
            page=MagicMock(),
            action=action,
        )

    assert len(results) == 1
    assert results[0] is decided_result
    assert action.status.value == "completed"
    assert action.tel_input_outcome is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["log_info", "model_dump"])
async def test_direct_tel_outcome_emission_fails_open(failure_mode: str) -> None:
    action = InputTextAction(element_id="AADC", text="224-555-0199", reasoning="fill phone")
    outcome = TelInputOutcome(
        flag_enabled=True,
        final_element_id="AADC",
        strategy=TelInputStrategy.sequential_national,
        expected_digit_count=10,
        actual_digit_count=10,
        browser_valid=True,
        attempt_count=1,
        retargeted=False,
    )
    decided_result = ActionSuccess()
    original_exception = RuntimeError("synthetic direct failure")

    if failure_mode == "log_info":

        async def patched_input_handler(action: InputTextAction, **_kwargs: object) -> list[ActionSuccess]:
            action.tel_input_outcome = outcome
            return [decided_result]

        def fail_terminal_log(event: object, *_args: object, **_kwargs: object) -> None:
            if event == "tel_input_outcome":
                raise RuntimeError("synthetic telemetry failure")

        with (
            patch("skyvern.webeye.actions.handler._handle_input_text_action", new=patched_input_handler),
            patch("skyvern.webeye.actions.handler.LOG.info", side_effect=fail_terminal_log),
        ):
            results = await handle_input_text_action_direct(
                action=action,
                page=MagicMock(),
                scraped_page=MagicMock(),
                task=_TASK,
                step=_STEP,
            )

        assert len(results) == 1
        assert results[0] is decided_result
    else:
        serialized_outcome = MagicMock()
        serialized_outcome.model_dump.side_effect = RuntimeError("synthetic serialization failure")

        async def patched_input_handler(action: InputTextAction, **_kwargs: object) -> list[ActionSuccess]:
            action.tel_input_outcome = serialized_outcome
            raise original_exception

        with patch("skyvern.webeye.actions.handler._handle_input_text_action", new=patched_input_handler):
            with pytest.raises(RuntimeError) as raised:
                await handle_input_text_action_direct(
                    action=action,
                    page=MagicMock(),
                    scraped_page=MagicMock(),
                    task=_TASK,
                    step=_STEP,
                )

        assert raised.value is original_exception

    assert action.tel_input_outcome is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("blocker_attrs", "value", "blocker_id"),
    [
        pytest.param(
            {"type": "text", "autocomplete": None, "name": "plain"},
            "synthetic text",
            "TEXT-BLOCKER",
            id="tel-to-text",
        ),
        pytest.param(
            {"type": "tel", "autocomplete": "cc-number", "name": "card.number"},
            VISA_16,
            "CARD-BLOCKER",
            id="tel-to-card",
        ),
    ],
)
async def test_tel_retarget_does_not_relabel_non_phone_error(
    blocker_attrs: dict[str, str | None],
    value: str,
    blocker_id: str,
) -> None:
    original = _mock_input({"type": "tel", "autocomplete": None, "name": "phone"})
    blocker = _mock_input(blocker_attrs)
    blocker.get_id.return_value = blocker_id
    browser_error = RuntimeError("synthetic post-retarget browser error")
    blocker.is_auto_completion_input = AsyncMock(side_effect=browser_error)

    with pytest.raises(RuntimeError) as raised:
        await _run_input_text(original, value, blocker=blocker)

    assert raised.value is browser_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "final_error",
    [
        pytest.param(MissingElement(element_id="synthetic"), id="missing-element"),
        pytest.param(MultipleElementsFound(2, element_id="synthetic"), id="multiple-elements"),
        pytest.param(LLMProviderError("synthetic-provider"), id="llm-provider"),
        pytest.param(ImaginarySecretValue("synthetic-secret"), id="imaginary-secret"),
        pytest.param(CaptchaSolveError(), id="captcha-solve"),
        pytest.param(asyncio.TimeoutError("synthetic-timeout"), id="asyncio-timeout"),
        pytest.param(RuntimeError("synthetic-browser-error"), id="generic"),
    ],
)
async def test_phone_final_catch_preserves_typed_errors_and_wraps_generic(final_error: Exception) -> None:
    el = _mock_input({"type": "tel", "autocomplete": None, "name": "phone"})
    el.is_auto_completion_input = AsyncMock(side_effect=final_error)

    if type(final_error) is RuntimeError:
        results, *_ = await _run_input_text(el, "224-555-0199")
        assert len(results) == 1 and isinstance(results[0], ActionFailure)
        assert results[0].exception_type == PhoneNumberInputBrowserInteractionFailed.__name__
    else:
        with pytest.raises(type(final_error)) as raised:
            await _run_input_text(el, "224-555-0199")
        assert raised.value is final_error


@pytest.mark.asyncio
async def test_already_filled_invalid_treatment_tel_fails_closed() -> None:
    el = _mock_input({"type": "tel", "autocomplete": None, "name": "phone"})
    with patch("skyvern.webeye.actions.handler._probe_tel_browser_validity", new=AsyncMock(return_value=False)):
        results, _, _, _, _, _ = await _run_input_text(
            el,
            "224-555-0199",
            current_value="224-555-0199",
        )

    assert len(results) == 1 and isinstance(results[0], ActionFailure)
    assert results[0].exception_type == PhoneNumberInputBrowserValidityMismatch.__name__


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "tel_fix_enabled"),
    [
        ("+44 20 7946 0958", True),
        ("224-555-0199", False),
    ],
)
async def test_already_filled_invalid_ineligible_or_control_tel_preserves_success(
    text: str,
    tel_fix_enabled: bool,
) -> None:
    el = _mock_input({"type": "tel", "autocomplete": None, "name": "phone"})
    with patch("skyvern.webeye.actions.handler._probe_tel_browser_validity", new=AsyncMock(return_value=False)):
        results, _, _, _, _, _ = await _run_input_text(
            el,
            text,
            current_value=text,
            tel_fix_enabled=tel_fix_enabled,
        )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)


@pytest.mark.asyncio
async def test_non_tel_wrapper_to_tel_blocker_evaluates_and_plans_treatment() -> None:
    original = _mock_input({"type": "text", "autocomplete": None, "name": "phone-wrapper"})
    blocker = _mock_input({"type": "tel", "autocomplete": None, "name": "phone"})
    blocker.get_id.return_value = "BLOCKING"

    with patch("skyvern.webeye.actions.handler.LOG.info") as log_info:
        results, _, tel_verify, _, _, _ = await _run_input_text(
            original,
            "224-555-0199",
            blocker=blocker,
        )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    blocker.input_sequentially.assert_awaited_once_with(text="2245550199")
    tel_verify.assert_awaited_once()
    terminal_logs = [call for call in log_info.call_args_list if call.args and call.args[0] == "tel_input_outcome"]
    assert len(terminal_logs) == 1
    terminal_kwargs = terminal_logs[0].kwargs
    assert terminal_kwargs["flag_enabled"] is True
    assert terminal_kwargs["final_element_id"] == "BLOCKING"
    assert terminal_kwargs["strategy"] == TelInputStrategy.sequential_national.value
    assert terminal_kwargs["retargeted"] is True


@pytest.mark.asyncio
async def test_blinking_cursor_tel_treatment_enforces_browser_validity() -> None:
    el = _mock_input({"type": "tel", "autocomplete": None, "name": "phone", "class": "blinking-cursor"})
    with patch("skyvern.webeye.actions.handler._probe_tel_browser_validity", new=AsyncMock(return_value=False)):
        results, _, tel_verify, _, _, _ = await _run_input_text(el, "224-555-0199")
    assert len(results) == 1 and isinstance(results[0], ActionFailure)
    assert results[0].exception_type == PhoneNumberInputBrowserValidityMismatch.__name__
    assert tel_verify.await_count == 2
    el.press_fill.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attrs",
    [
        {"type": "tel", "autocomplete": "cc-number", "name": None},
        {"type": "tel", "autocomplete": None, "name": "card.number"},
    ],
)
async def test_tel_card_number_field_uses_card_readback_not_phone_format(attrs: dict[str, str | None]) -> None:
    el = _mock_input(attrs)

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(el, VISA_16)

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    card_readback.assert_awaited_once_with(
        skyvern_element=el,
        tag_name="input",
        text=VISA_16,
        expected_digits=VISA_16,
        engine_selection=None,
    )
    phone_format.assert_not_awaited()
    tel_verify.assert_not_awaited()
    secret_readback.assert_not_awaited()
    el.input_sequentially.assert_not_awaited()


@pytest.mark.asyncio
async def test_ten_digit_tel_phone_uses_tel_readback_not_card_readback() -> None:
    el = _mock_input({"type": "tel", "autocomplete": None, "name": "phone"})

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(el, "224-555-0199")

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="2245550199")
    tel_verify.assert_awaited_once_with(
        skyvern_element=el,
        tag_name="input",
        expected_value="2245550199",
        allow_nanp_country_prefix=False,
        pattern=None,
        maxlength=None,
        engine_selection=None,
    )
    card_readback.assert_not_awaited()
    phone_format.assert_not_awaited()
    secret_readback.assert_not_awaited()


@pytest.mark.asyncio
async def test_tel_flag_off_preserves_legacy_format_and_sequential_fill() -> None:
    el = _mock_input({"type": "tel", "autocomplete": None, "name": "phone"})

    results, _, tel_verify, phone_format, _, _ = await _run_input_text(
        el,
        "224-555-0199",
        tel_fix_enabled=False,
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    phone_format.assert_awaited_once()
    el.input_sequentially.assert_awaited_once_with(text="224-555-0199")
    tel_verify.assert_not_awaited()
    el.input_clear.assert_not_awaited()
    el.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_separator_only_tel_never_forces_nanp_country_code() -> None:
    el = _mock_input({"type": "tel", "autocomplete": None, "name": "phone"})
    mismatches = [
        PhoneNumberInputMismatch(expected_digit_count=10, actual_digit_count=12),
        PhoneNumberInputMismatch(expected_digit_count=10, actual_digit_count=12),
    ]

    results, _, tel_verify, _, warning_log, _ = await _run_input_text(
        el,
        "224-555-0199",
        tel_verify_side_effect=mismatches,
    )

    assert len(results) == 1 and isinstance(results[0], ActionFailure)
    assert tel_verify.await_count == 2
    assert all(call.kwargs["allow_nanp_country_prefix"] is False for call in tel_verify.await_args_list)
    el.input_clear.assert_awaited_once()
    el.input_fill.assert_awaited_once_with(text="2245550199")
    warning_log.assert_called_once_with(
        "Phone input read-back mismatch after retry",
        element_id="AADC",
        expected_digit_count=10,
        actual_digit_count=12,
    )


@pytest.mark.asyncio
async def test_explicit_nanp_tel_keeps_constraint_safe_e164_fallback() -> None:
    el = _mock_input({"type": "tel", "autocomplete": None, "name": "phone"})
    mismatches_then_success = [
        PhoneNumberInputMismatch(expected_digit_count=10, actual_digit_count=12),
        PhoneNumberInputMismatch(expected_digit_count=10, actual_digit_count=12),
        10,
    ]

    results, _, tel_verify, _, _, _ = await _run_input_text(
        el,
        "+1 (224) 555-0199",
        tel_verify_side_effect=mismatches_then_success,
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    assert tel_verify.await_count == 3
    assert all(call.kwargs["allow_nanp_country_prefix"] is True for call in tel_verify.await_args_list)
    assert el.input_clear.await_count == 2
    assert [await_call.kwargs["text"] for await_call in el.input_fill.await_args_list] == [
        "2245550199",
        "+12245550199",
    ]


@pytest.mark.asyncio
async def test_blocking_tel_input_rechecks_constraints_before_readback() -> None:
    original = _mock_input({"type": "tel", "autocomplete": None, "name": "phone"})
    blocking = _mock_input(
        {"type": "tel", "autocomplete": None, "name": "phone", "pattern": "[0-9]{10}", "maxlength": "10"}
    )
    blocking.get_id.return_value = "BLOCKING"

    results, _, tel_verify, _, _, _ = await _run_input_text(original, "+1 (224) 555-0199", blocker=blocking)

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    blocking.input_sequentially.assert_awaited_once_with(text="2245550199")
    # The blocker's mask both rejects the E.164 retry and governs the read-back constraint check.
    tel_verify.assert_awaited_once_with(
        skyvern_element=blocking,
        tag_name="input",
        expected_value="2245550199",
        allow_nanp_country_prefix=False,
        pattern="[0-9]{10}",
        maxlength="10",
        engine_selection=None,
    )


@pytest.mark.asyncio
async def test_secret_tel_value_uses_tel_verifier_not_secret_readback() -> None:
    # A resolved secret that is a NANP phone number must keep the digit-normalized tel verification
    # (a type=tel field renders punctuation), not the exact secret read-back which would false-mismatch
    # the bare digits against the formatted value and fail a correct fill.
    el = _mock_input({"type": "tel", "autocomplete": None, "name": "phone"})

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "{{ phone }}", resolved="224-555-0199"
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="2245550199")
    tel_verify.assert_awaited_once_with(
        skyvern_element=el,
        tag_name="input",
        expected_value="2245550199",
        allow_nanp_country_prefix=False,
        pattern=None,
        maxlength=None,
        engine_selection=None,
    )
    secret_readback.assert_not_awaited()
    card_readback.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_character_secret_skips_readback() -> None:
    # A one-character secret cannot be order-scrambled, so even a password input skips the read-back
    # (e.g. a multi-field TOTP digit routed into a masked box: is_secret_value True, is_totp_value False).
    # It is an ordinary native input, so it is populated with one atomic fill.
    el = _mock_input({"type": "password", "autocomplete": None, "name": "otp-digit"})

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "{{ digit }}", resolved="5"
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_fill.assert_awaited_once_with("5")
    el.input_sequentially.assert_not_awaited()
    secret_readback.assert_not_awaited()
    tel_verify.assert_not_awaited()
    card_readback.assert_not_awaited()


@pytest.mark.asyncio
async def test_secret_in_non_input_element_skips_readback() -> None:
    # A non-native editable sink (a plain <div> with no contenteditable attribute) trims/normalizes its
    # read-back, so the exact-value read-back is skipped. It is not an explicit contenteditable, so it keeps
    # the per-key sequential fill; only an explicit contenteditable takes the atomic path.
    el = _mock_input({"type": None, "autocomplete": None, "name": "note"})
    el.get_tag_name.return_value = "div"

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "{{ sec }}", resolved="mysecretvalue", tag_name="div"
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="mysecretvalue")
    el.input_fill.assert_not_awaited()
    secret_readback.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_contenteditable_url_uses_atomic_fill_not_sequential_seam() -> None:
    # SKY-13014: a Quill-style rich-text editor carries contenteditable="true". Filling it with the
    # input_sequentially fill(prefix)+type(tail) split lets a URL auto-linkifier wrap the prefix before the
    # tail arrives, corrupting the link. An explicit contenteditable must fill atomically via input_fill and
    # keep the stale-locator refresh locally (input_fill alone skips it); input_sequentially must not run.
    el = _mock_input({"type": None, "autocomplete": None, "name": "key-take-aways", "contenteditable": "true"})
    el.get_tag_name.return_value = "div"
    url_value = "https://example.com/shared/call/CUPRZqEEAWnXLTaMSGhrnR5UW5RZVthv8b6MZTiDGtmY"

    results, _, _, _, _, secret_readback = await _run_input_text(el, url_value, tag_name="div")

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_fill.assert_awaited_once_with(url_value)
    el.refresh_locator_if_stale.assert_awaited_once()
    el.input_sequentially.assert_not_awaited()
    secret_readback.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_contenteditable_custom_sink_url_keeps_sequential_fill() -> None:
    # Scope regression (SKY-13014): the atomic path is reserved for an explicit contenteditable. A non-native
    # custom sink that is editable but has no contenteditable attribute (here a role=textbox div) must keep
    # the per-key sequential fill even for a URL value -- it must not be widened onto the atomic branch, and
    # the contenteditable-only stale-locator refresh must not run for it.
    el = _mock_input({"type": None, "autocomplete": None, "name": "widget", "role": "textbox"})
    el.get_tag_name.return_value = "div"
    url_value = "https://example.com/shared/call/CUPRZqEEAWnXLTaMSGhrnR5UW5RZVthv8b6MZTiDGtmY"

    results, _, _, _, _, secret_readback = await _run_input_text(el, url_value, tag_name="div")

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text=url_value)
    el.input_fill.assert_not_awaited()
    el.refresh_locator_if_stale.assert_not_awaited()
    secret_readback.assert_not_awaited()


@pytest.mark.asyncio
async def test_editing_host_child_without_attribute_uses_atomic_fill() -> None:
    # Only the editing host carries the contenteditable attribute; a block inside it inherits editability and
    # must take the same atomic path, or the per-character seam re-resolves its selector onto split blocks.
    el = _mock_input({"type": None, "autocomplete": None, "name": None})
    el.get_tag_name.return_value = "p"
    el.is_content_editable = AsyncMock(return_value=True)
    text = "first line\nsecond line\nthird line"

    results, _, _, _, _, secret_readback = await _run_input_text(el, text, tag_name="p")

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_fill.assert_awaited_once_with(text)
    el.refresh_locator_if_stale.assert_awaited_once()
    el.input_sequentially.assert_not_awaited()
    secret_readback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("input_type", ["password", "text", "email", "search", "url", None])
async def test_secret_in_exact_value_input_uses_readback(input_type: str | None) -> None:
    # Every native exact-value input type (password/text/email/search/url and an untyped input) round-trips
    # its .value exactly, so the credential read-back verifier runs and is told the live type.
    el = _mock_input({"type": input_type, "autocomplete": None, "name": "credential"})

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "{{ sec }}", resolved="mysecretvalue", tag_name="input"
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    secret_readback.assert_awaited_once_with(
        skyvern_element=el,
        tag_name="input",
        text="mysecretvalue",
        input_type=input_type or "",
        maxlength=None,
        engine_selection=None,
        sequential_first=False,
    )
    el.input_sequentially.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("input_type", ["number", "datetime-local", "month", "week"])
async def test_secret_in_non_exact_value_input_skips_readback(input_type: str) -> None:
    # number/date-like inputs normalize or reformat their value, so an exact read-back is not meaningful; the
    # read-back is skipped. They also hard-throw in locator.fill() on a non-canonical value, so they keep the
    # per-character seam rather than an atomic fill (SKY-13821). (type=date has its own dedicated fill path
    # earlier and never reaches this gate.)
    el = _mock_input({"type": input_type, "autocomplete": None, "name": "field"})

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "{{ sec }}", resolved="mysecretvalue", tag_name="input"
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="mysecretvalue")
    el.input_fill.assert_not_awaited()
    secret_readback.assert_not_awaited()


@pytest.mark.asyncio
async def test_secret_readback_skips_when_retargeted_to_out_of_scope_blocker() -> None:
    # find_blocking_element() can retarget the fill to an editable blocker; the credential read-back gate
    # must be re-evaluated on the actual (blocker) element. A number blocker is out of the exact-value
    # scope, so no read-back runs even though the original element was in scope; it also stays on the
    # per-character seam because number hard-throws in locator.fill() (SKY-13821).
    el = _mock_input({"type": "text", "autocomplete": None, "name": "credential"})
    blocker = _mock_input({"type": "number", "autocomplete": None, "name": "overlay"})

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "{{ sec }}", resolved="mysecretvalue", tag_name="input", blocker=blocker
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    blocker.input_sequentially.assert_awaited_once_with(text="mysecretvalue")
    blocker.input_fill.assert_not_awaited()
    secret_readback.assert_not_awaited()
    el.input_sequentially.assert_not_awaited()


@pytest.mark.asyncio
async def test_secret_readback_runs_on_retargeted_element_type() -> None:
    # The read-back gate reads the live type of the actual (blocker) element: retargeting an in-scope text
    # element to a password blocker still runs the read-back, keyed on the blocker's type.
    el = _mock_input({"type": "text", "autocomplete": None, "name": "overlay"})
    blocker = _mock_input({"type": "password", "autocomplete": None, "name": "password"})

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "{{ sec }}", resolved="mysecretvalue", tag_name="input", blocker=blocker
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    secret_readback.assert_awaited_once_with(
        skyvern_element=blocker,
        tag_name="input",
        text="mysecretvalue",
        input_type="password",
        maxlength=None,
        engine_selection=None,
        sequential_first=False,
    )


@pytest.mark.asyncio
async def test_non_secret_exact_value_input_skips_readback() -> None:
    # A non-secret value in an exact-value input is not a credential, so the read-back verifier never runs;
    # only secrets are read back. It is an ordinary native input, so it is populated with one atomic fill.
    el = _mock_input({"type": "text", "autocomplete": None, "name": "search"})

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "not a secret value"
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_fill.assert_awaited_once_with("not a secret value")
    el.input_sequentially.assert_not_awaited()
    secret_readback.assert_not_awaited()


@pytest.mark.asyncio
async def test_totp_value_short_circuits_before_secret_readback() -> None:
    # A resolved TOTP value is recognized as TOTP and short-circuits in the TOTP path before the credential
    # read-back is ever reached (this fixture's task has no valid TOTP secret, so it fails closed with
    # NoTOTPSecretFound -- whatever the TOTP outcome, the credential read-back is never invoked). The gate's
    # `not is_totp_value` conjunct is a defensive backstop for this invariant.
    el = _mock_input({"type": "password", "autocomplete": None, "name": "otp"})

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "{{ totp }}", resolved=str(BitwardenConstants.TOTP)
    )

    assert len(results) == 1 and isinstance(results[0], ActionFailure)
    assert results[0].exception_type == "NoTOTPSecretFound"
    secret_readback.assert_not_awaited()
    card_readback.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_native_freetext_uses_atomic_fill() -> None:
    # SKY-13821 fill-first: an ordinary native input (non-secret, non-tel, no select context) is populated with
    # a single atomic fill instead of the per-character fill/type seam, so the caret race cannot reorder it.
    el = _mock_input({"type": "text", "autocomplete": None, "name": "full-name"})

    results, _, tel_verify, _, _, secret_readback = await _run_input_text(el, "Ada Lovelace")

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_fill.assert_awaited_once_with("Ada Lovelace")
    el.input_sequentially.assert_not_awaited()
    tel_verify.assert_not_awaited()
    secret_readback.assert_not_awaited()


@pytest.mark.asyncio
async def test_residual_sequential_path_runs_truncation_heal() -> None:
    # SKY-13631 coverage preserved after the fill-first flip: a path still typed character-by-character (here a
    # tel field with the digit fix off) runs the observational truncation heal right after input_sequentially.
    el = _mock_input({"type": "tel", "autocomplete": None, "name": "phone"})

    with patch("skyvern.webeye.actions.handler._heal_truncated_freetext_input", new=AsyncMock()) as heal:
        results, *_ = await _run_input_text(el, "224-555-0199", tel_fix_enabled=False)

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="224-555-0199")
    heal.assert_awaited_once()


@pytest.mark.asyncio
async def test_atomic_fill_path_skips_truncation_heal() -> None:
    # The atomic fill has no per-character seam to lose a prefix, so the truncation heal must not run after it.
    el = _mock_input({"type": "text", "autocomplete": None, "name": "full-name"})

    with patch("skyvern.webeye.actions.handler._heal_truncated_freetext_input", new=AsyncMock()) as heal:
        results, *_ = await _run_input_text(el, "Ada Lovelace")

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_fill.assert_awaited_once_with("Ada Lovelace")
    heal.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_bar_input_keeps_sequential_typing() -> None:
    # A search-bar surfaces its options only as the value is typed, so it keeps the per-character seam.
    el = _mock_input({"type": "text", "autocomplete": None, "name": "q"})

    results, *_ = await _run_input_text(
        el, "engineer", input_or_select_context=InputOrSelectContext(is_search_bar=True)
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="engineer")
    el.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_location_input_keeps_sequential_typing() -> None:
    # A location/address field is an autocomplete widget, so it keeps the per-character seam.
    el = _mock_input({"type": "text", "autocomplete": None, "name": "address"})

    results, *_ = await _run_input_text(
        el, "123 Main", input_or_select_context=InputOrSelectContext(is_location_input=True)
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="123 Main")
    el.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_autocomplete_input_keeps_sequential_typing() -> None:
    # An is_auto_completion_input() field surfaces suggestions as the value is typed, so it keeps the seam.
    el = _mock_input({"type": "text", "autocomplete": None, "name": "skill"})
    el.is_auto_completion_input = AsyncMock(return_value=True)

    results, *_ = await _run_input_text(el, "engineer")

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="engineer")
    el.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_gate_recomputes_is_tel_after_blocking_retarget() -> None:
    # find_blocking_element can retarget the fill to a different element; the fill/type decision must read the
    # retargeted element's tel-ness, not the original's stale value. A plain text field fronting a tel blocker
    # must keep the tel blocker on the per-character seam, not atomically fill it.
    original = _mock_input({"type": "text", "autocomplete": None, "name": "phone-wrapper"})
    blocker = _mock_input({"type": "tel", "autocomplete": None, "name": "phone"})
    blocker.get_id.return_value = "BLOCKING"

    results, *_ = await _run_input_text(original, "sometext", blocker=blocker)

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    blocker.input_sequentially.assert_awaited_once_with(text="sometext")
    blocker.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_required_combobox_keeps_sequential_typing() -> None:
    # A role=combobox field that is still aria-invalid after typing commits only by picking a rendered option,
    # so it keeps the per-character seam that surfaces those options.
    el = _mock_input({"type": "text", "role": "combobox", "aria-invalid": "true", "name": "title"})

    results, *_ = await _run_input_text(el, "engineer")

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="engineer")
    el.input_fill.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "combobox_attrs",
    [
        {"role": "combobox"},  # role=combobox, aria-invalid absent -> still keyboard-driven
        {"aria-autocomplete": "both"},  # aria-autocomplete=both (is_auto_completion_input only matches "list")
        {"aria-autocomplete": "inline"},  # role-less inline completion still depends on keyboard events
        {"role": "combobox", "aria-invalid": "false"},  # explicitly valid combobox before input
    ],
)
async def test_combobox_identity_keeps_seam_even_when_valid(combobox_attrs: dict[str, str | None]) -> None:
    # A role=combobox / aria-autocomplete=both control opens or filters its options only via key events; its
    # pre-input aria-invalid state must not pick the write strategy. Keep the per-character seam by structural
    # identity, or an atomic fill emits no keys and no option is ever surfaced (SKY-13821).
    el = _mock_input({"type": "text", "name": "job-title", **combobox_attrs})

    results, *_ = await _run_input_text(el, "Backend Engineer")

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="Backend Engineer")
    el.input_fill.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("input_type", ["number", "time", "month", "week", "datetime-local"])
async def test_non_text_native_input_types_keep_the_seam(input_type: str) -> None:
    # locator.fill() hard-throws on a non-canonical value for these native input types ("Cannot type text into
    # input[type=number]" / "Malformed value"); the per-character seam tolerated them. Keep them off the
    # atomic branch (SKY-13821).
    el = _mock_input({"type": input_type, "autocomplete": None, "name": "when"})

    results, *_ = await _run_input_text(el, "3")

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="3")
    el.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_element_for_text_input_is_not_swallowed_into_success() -> None:
    # If the live node disagrees with the scraped tag, input_fill raises InvalidElementForTextInput -- a
    # SkyvernException, not a PlaywrightError -- so the broad incremental handler falls to its swallow arm and
    # returns ActionSuccess with the credential never written. It must fail closed, matching the explicit
    # re-raise already added for SkyvernPageAnalysisTimeout (SKY-13821).
    el = _mock_input({"type": "text", "autocomplete": None, "name": "field"})
    el.input_fill = AsyncMock(side_effect=InvalidElementForTextInput(element_id="AADC", tag_name="input"))

    with pytest.raises(InvalidElementForTextInput):
        await _run_input_text(el, "some value")


@pytest.mark.asyncio
@pytest.mark.parametrize("typed_widget_attrs", [{"role": "combobox"}, {"aria-autocomplete": "inline"}])
async def test_secret_valued_typed_widget_keeps_keyboard_path_with_readback(
    typed_widget_attrs: dict[str, str],
) -> None:
    el = _mock_input({"type": "text", "name": "title", **typed_widget_attrs})

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "{{ sec }}", resolved="mysecretvalue"
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    secret_readback.assert_awaited_once_with(
        skyvern_element=el,
        tag_name="input",
        text="mysecretvalue",
        input_type="text",
        maxlength=None,
        engine_selection=None,
        sequential_first=True,
    )


@pytest.mark.asyncio
async def test_secret_valued_search_bar_keeps_keyboard_path() -> None:
    # Same for a secret entered into a search-bar context: select sequential transport inside the verifier.
    el = _mock_input({"type": "text", "name": "q"})

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "{{ sec }}", resolved="mysecretvalue", input_or_select_context=InputOrSelectContext(is_search_bar=True)
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    secret_readback.assert_awaited_once_with(
        skyvern_element=el,
        tag_name="input",
        text="mysecretvalue",
        input_type="text",
        maxlength=None,
        engine_selection=None,
        sequential_first=True,
    )


@pytest.mark.asyncio
async def test_secret_valued_plain_native_input_still_uses_atomic_readback() -> None:
    # Contrast (must keep working): a secret in a plain native input with NO typed-widget signal still takes
    # the atomic secret read-back path -- the fix narrows only the typed-widget cases.
    el = _mock_input({"type": "text", "name": "credential"})

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "{{ sec }}", resolved="mysecretvalue"
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    secret_readback.assert_awaited_once_with(
        skyvern_element=el,
        tag_name="input",
        text="mysecretvalue",
        input_type="text",
        maxlength=None,
        engine_selection=None,
        sequential_first=False,
    )
    el.input_sequentially.assert_not_awaited()


@pytest.mark.asyncio
async def test_secret_in_maxlength_short_input_uses_sequential_not_atomic_readback() -> None:
    # An ordinary secret whose value exceeds a positive maxlength (an auto-advancing split field, e.g. SSN /
    # account boxes) must type sequentially so the per-key focus advance carries the remaining characters to
    # the sibling boxes. The atomic read-back would leave only a truncated prefix in the first box and, since
    # the value cannot round-trip, report success without verification (SKY-13821).
    el = _mock_input({"type": "text", "maxlength": "4", "name": "ssn"})

    results, card_readback, tel_verify, phone_format, warning_log, secret_readback = await _run_input_text(
        el, "{{ sec }}", resolved="123456789"
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="123456789")
    secret_readback.assert_not_awaited()
    el.input_fill.assert_not_awaited()
    # no secret leakage: the resolved value never reaches the warning log
    assert all("123456789" not in str(call) for call in warning_log.call_args_list)


@pytest.mark.asyncio
async def test_secret_in_capacity_fitting_input_still_uses_atomic_readback() -> None:
    # Contrast (must keep working): a secret that fits its capacity (maxlength >= value length) keeps the
    # atomic read-back path -- only truncating capacity reroutes to the seam.
    el = _mock_input({"type": "text", "maxlength": "20", "name": "credential"})

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "{{ sec }}", resolved="123456789"
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    secret_readback.assert_awaited_once()
    el.input_sequentially.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_value_in_maxlength_short_input_uses_sequential() -> None:
    # The same auto-advance routing for a non-secret ordinary value into a positive-maxlength split field:
    # atomic fill would truncate it, the per-character seam distributes it across the boxes.
    el = _mock_input({"type": "text", "maxlength": "1", "name": "digit"})

    results, *_ = await _run_input_text(el, "123456")

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="123456")
    el.input_fill.assert_not_awaited()
