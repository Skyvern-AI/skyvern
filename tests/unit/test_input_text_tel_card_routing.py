from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import PhoneNumberInputMismatch
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants
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
    resolved: str | None = None,
    tel_fix_enabled: bool = True,
    tel_verify_side_effect: list[Exception | None] | None = None,
    tag_name: str = "input",
    blocker: MagicMock | None = None,
) -> tuple[list, AsyncMock, AsyncMock, AsyncMock, MagicMock, AsyncMock]:
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
    tel_verify = AsyncMock(side_effect=tel_verify_side_effect)
    phone_format = AsyncMock(return_value=text)
    warning_log = MagicMock()
    secret_readback = AsyncMock(return_value=None)
    # A resolved secret differs from the action's placeholder text; when equal, the value is not a secret.
    secret_return = text if resolved is None else resolved

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new=AsyncMock(return_value=skyvern_frame)),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value="")),
        patch(
            "skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task",
            return_value=secret_return,
        ),
        patch("skyvern.webeye.actions.handler._get_input_or_select_context", new=AsyncMock(return_value=None)),
        patch("skyvern.webeye.actions.handler._is_tel_digit_fix_enabled", new=AsyncMock(return_value=tel_fix_enabled)),
        patch("skyvern.webeye.actions.handler.check_phone_number_format", new=phone_format),
        patch("skyvern.webeye.actions.handler._fill_card_number_with_readback", new=card_readback),
        patch("skyvern.webeye.actions.handler._fill_secret_with_readback", new=secret_readback),
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

    return results, card_readback, tel_verify, phone_format, warning_log, secret_readback


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
        None,
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
    )
    secret_readback.assert_not_awaited()
    card_readback.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_character_secret_skips_readback() -> None:
    # A one-character secret cannot be order-scrambled, so even a password input skips the read-back
    # (e.g. a multi-field TOTP digit routed into a masked box: is_secret_value True, is_totp_value False).
    el = _mock_input({"type": "password", "autocomplete": None, "name": "otp-digit"})

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "{{ digit }}", resolved="5"
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="5")
    secret_readback.assert_not_awaited()
    tel_verify.assert_not_awaited()
    card_readback.assert_not_awaited()


@pytest.mark.asyncio
async def test_secret_in_non_input_element_skips_readback() -> None:
    # A contenteditable/div is not a native input (its read-back is trimmed/normalized), so the read-back
    # is skipped; it keeps the plain sequential fill and its pre-existing behavior.
    el = _mock_input({"type": None, "autocomplete": None, "name": "note"})
    el.get_tag_name.return_value = "div"

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "{{ sec }}", resolved="mysecretvalue", tag_name="div"
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="mysecretvalue")
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
    )
    el.input_sequentially.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("input_type", ["number", "datetime-local", "month", "week"])
async def test_secret_in_non_exact_value_input_skips_readback(input_type: str) -> None:
    # number/date-like inputs normalize or reformat their value, so an exact read-back is not meaningful;
    # they keep the plain sequential fill, not the exact read-back. (type=date has its own dedicated fill
    # path earlier and never reaches this gate.)
    el = _mock_input({"type": input_type, "autocomplete": None, "name": "field"})

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "{{ sec }}", resolved="mysecretvalue", tag_name="input"
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="mysecretvalue")
    secret_readback.assert_not_awaited()


@pytest.mark.asyncio
async def test_secret_readback_skips_when_retargeted_to_out_of_scope_blocker() -> None:
    # find_blocking_element() can retarget the fill to an editable blocker; the credential read-back gate
    # must be re-evaluated on the actual (blocker) element. A number blocker is out of the exact-value
    # scope, so no read-back runs even though the original element was in scope.
    el = _mock_input({"type": "text", "autocomplete": None, "name": "credential"})
    blocker = _mock_input({"type": "number", "autocomplete": None, "name": "overlay"})

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "{{ sec }}", resolved="mysecretvalue", tag_name="input", blocker=blocker
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    blocker.input_sequentially.assert_awaited_once_with(text="mysecretvalue")
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
    )


@pytest.mark.asyncio
async def test_non_secret_exact_value_input_skips_readback() -> None:
    # A non-secret value in an exact-value input is not a credential, so the read-back verifier never runs;
    # only secrets are read back.
    el = _mock_input({"type": "text", "autocomplete": None, "name": "search"})

    results, card_readback, tel_verify, phone_format, _, secret_readback = await _run_input_text(
        el, "not a secret value"
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    el.input_sequentially.assert_awaited_once_with(text="not a secret value")
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
