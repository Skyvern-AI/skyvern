from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import PhoneNumberInputMismatch
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import InputTextAction
from skyvern.webeye.actions.handler import handle_input_text_action
from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess
from tests.unit.helpers import make_organization, make_step, make_task

_NOW = datetime.now(UTC)
_ORG = make_organization(_NOW)
_TASK = make_task(_NOW, _ORG, navigation_payload={}, navigation_goal="Fill checkout contact fields")
_STEP = make_step(_NOW, _TASK, step_id="stp-tel-card-routing", status=StepStatus.created, order=0, output=None)

VISA_16 = "4539578763621486"


def _mock_input(attrs: dict[str, str | None]) -> MagicMock:
    el = MagicMock()
    el.get_id.return_value = "AADC"
    el.get_tag_name.return_value = "input"
    el.get_frame.return_value = MagicMock()
    locator = MagicMock()
    locator.focus = AsyncMock()
    el.get_locator.return_value = locator
    el.is_disabled = AsyncMock(return_value=False)
    el.get_selectable = AsyncMock(return_value=False)
    el.has_hidden_attr = AsyncMock(return_value=False)
    el.is_readonly = AsyncMock(return_value=False)
    el.get_attr = AsyncMock(side_effect=lambda name, **kwargs: attrs.get(name))
    el.is_spinbtn_input = AsyncMock(return_value=False)
    el.is_editable = AsyncMock(return_value=True)
    el.is_visible = AsyncMock(return_value=True)
    el.is_raw_input = AsyncMock(return_value=True)
    el.supports_text_input = AsyncMock(return_value=True)
    el.find_blocking_element = AsyncMock(return_value=(None, False))
    el.get_element_handler = AsyncMock(return_value=MagicMock())
    el.input_sequentially = AsyncMock()
    el.input_clear = AsyncMock()
    el.input_fill = AsyncMock()
    el.press_key = AsyncMock()
    return el


async def _run_input_text(
    el: MagicMock,
    text: str,
    *,
    tel_fix_enabled: bool = True,
    tel_verify_side_effect: list[Exception | None] | None = None,
) -> tuple[list, AsyncMock, AsyncMock, AsyncMock, MagicMock]:
    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=el)

    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    inc.get_incremental_element_tree = AsyncMock(return_value=[])

    skyvern_frame = MagicMock()
    skyvern_frame.safe_wait_for_animation_end = AsyncMock()

    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"AADC": {"tagName": "input"}}

    card_readback = AsyncMock(return_value=None)
    tel_verify = AsyncMock(side_effect=tel_verify_side_effect)
    phone_format = AsyncMock(return_value=text)
    warning_log = MagicMock()

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new=AsyncMock(return_value=skyvern_frame)),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value="")),
        patch("skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task", return_value=text),
        patch("skyvern.webeye.actions.handler._get_input_or_select_context", new=AsyncMock(return_value=None)),
        patch("skyvern.webeye.actions.handler._is_tel_digit_fix_enabled", new=AsyncMock(return_value=tel_fix_enabled)),
        patch("skyvern.webeye.actions.handler.check_phone_number_format", new=phone_format),
        patch("skyvern.webeye.actions.handler._fill_card_number_with_readback", new=card_readback),
        patch("skyvern.webeye.actions.handler._verify_tel_input_after_fill", new=tel_verify),
        patch("skyvern.webeye.actions.handler.LOG.warning", new=warning_log),
    ):
        results = await handle_input_text_action(
            action=InputTextAction(element_id="AADC", text=text, reasoning="fill field"),
            page=MagicMock(),
            scraped_page=scraped_page,
            task=_TASK,
            step=_STEP,
        )

    return results, card_readback, tel_verify, phone_format, warning_log


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

    results, card_readback, tel_verify, phone_format, _ = await _run_input_text(el, VISA_16)

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    card_readback.assert_awaited_once_with(
        skyvern_element=el,
        tag_name="input",
        text=VISA_16,
        expected_digits=VISA_16,
    )
    phone_format.assert_not_awaited()
    tel_verify.assert_not_awaited()
    el.input_sequentially.assert_not_awaited()


@pytest.mark.asyncio
async def test_ten_digit_tel_phone_uses_tel_readback_not_card_readback() -> None:
    el = _mock_input({"type": "tel", "autocomplete": None, "name": "phone"})

    results, card_readback, tel_verify, phone_format, _ = await _run_input_text(el, "224-555-0199")

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="2245550199")
    tel_verify.assert_awaited_once_with(
        skyvern_element=el,
        tag_name="input",
        expected_value="2245550199",
        allow_nanp_country_prefix=False,
    )
    card_readback.assert_not_awaited()
    phone_format.assert_not_awaited()


@pytest.mark.asyncio
async def test_tel_flag_off_preserves_legacy_format_and_sequential_fill() -> None:
    el = _mock_input({"type": "tel", "autocomplete": None, "name": "phone"})

    results, _, tel_verify, phone_format, _ = await _run_input_text(
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

    results, _, tel_verify, _, warning_log = await _run_input_text(
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
        None,
    ]

    results, _, tel_verify, _, _ = await _run_input_text(
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
