from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright._impl._errors import Error as PlaywrightError

from skyvern.webeye.actions.handler import (
    _caret_readback_eligible,
    _exact_value_input_type,
    _fill_secret_with_readback,
    _maxlength_truncates_value,
    _secret_input_cannot_round_trip,
    _secret_readback_is_mismatch,
    _secret_readback_is_unreadable_mask,
    _secret_readback_matches,
)
from skyvern.webeye.actions.responses import ActionFailure

# Synthetic secret, not a real credential. It is longer than the fill+type split threshold so the entry
# exercises the fill(prefix) + type(tail) seam where the caret race rotates the value.
SECRET = "abcdefghijklmno"
# The fill+type caret race on a hardened field rotates the value by the filled-prefix length (len - 10).
ROTATED = SECRET[5:] + SECRET[:5]
# A controlled field can drop the typed tail, truncating the value.
TRUNCATED = SECRET[:-3]
# The shortest value the gate still verifies (a single character cannot be order-scrambled).
SHORT_SECRET = "ab"
# A secret that legitimately contains mask-like characters; a password input's .value returns them as-is.
GLYPH_SECRET = "ab*c•de"
GLYPH_SCRAMBLED = GLYPH_SECRET[3:] + GLYPH_SECRET[:3]


@pytest.mark.parametrize(
    "actual_value,is_mismatch",
    [
        (SECRET, False),  # correct value -> not a mismatch
        (ROTATED, True),  # rotated value, same length -> mismatch
        (TRUNCATED, True),  # dropped tail -> mismatch
        (SECRET[:-1], True),  # dropped character -> mismatch
        ("", True),  # empty read-back -> mismatch, must be re-entered atomically
        (None, True),  # unreadable read-back -> mismatch
    ],
)
def test_secret_readback_is_mismatch(actual_value: str | None, is_mismatch: bool) -> None:
    assert _secret_readback_is_mismatch(SECRET, actual_value) is is_mismatch


@pytest.mark.parametrize(
    "actual_value,matches",
    [
        (SECRET, True),  # exact match
        (ROTATED, False),  # rotated -> not a positive match
        (SECRET[:-1], False),  # dropped character -> not a positive match
        ("", False),  # empty -> not a positive match
        (None, False),  # unreadable -> not a positive match
    ],
)
def test_secret_readback_matches(actual_value: str | None, matches: bool) -> None:
    assert _secret_readback_matches(SECRET, actual_value) is matches


@pytest.mark.parametrize(
    "actual_value,is_masked",
    [
        ("••••••••", True),  # entirely bullets -> unreadable
        ("•••• ••••", True),  # bullets grouped by a space separator -> unreadable
        ("****-****", True),  # entirely asterisks with a hyphen separator -> unreadable
        ("*", True),  # a single mask glyph and nothing else -> unreadable
        ("ab•cde", False),  # a real value that merely contains one glyph -> readable (compared exactly)
        ("p*ssw0rd", False),  # a revealed password containing "*" -> readable, must NOT be skipped
        ("mysecretvalue", False),  # a real rendered value -> readable
        ("", False),  # empty is not "masked" -> handled as a mismatch, not a skip
        (None, False),  # unreadable/None -> handled as a mismatch, not a skip
    ],
)
def test_secret_readback_is_unreadable_mask_non_password(actual_value: str | None, is_masked: bool) -> None:
    assert _secret_readback_is_unreadable_mask(actual_value, is_password=False) is is_masked


def test_password_readback_is_never_masked() -> None:
    # A native password input's .value is the real typed value, so mask-like glyphs are real characters
    # and the value is always comparable -- never treated as an unreadable mask.
    assert _secret_readback_is_unreadable_mask("••••••••", is_password=True) is False
    assert _secret_readback_is_unreadable_mask(GLYPH_SECRET, is_password=True) is False


@pytest.mark.parametrize(
    "text,maxlength,cannot_round_trip",
    [
        (SECRET, None, False),  # no declared constraint -> round-trips
        (SECRET, "20", False),  # maxlength longer than the value -> round-trips
        (SECRET, "8", True),  # positive maxlength shorter than the value -> truncates
        (SECRET, "0", True),  # maxlength 0 cannot hold a >1 char value
        (SECRET, "abc", False),  # unparseable maxlength -> ignore, do not skip
        (SECRET, "", False),  # empty maxlength attr -> ignore, do not skip
        ("abc\ndef", None, True),  # a single-line input strips LF -> cannot round-trip
        ("abc\r\ndef", None, True),  # CRLF stripped -> cannot round-trip
    ],
)
def test_secret_input_cannot_round_trip(text: str, maxlength: str | None, cannot_round_trip: bool) -> None:
    assert _secret_input_cannot_round_trip(text, maxlength=maxlength) is cannot_round_trip


@pytest.mark.parametrize(
    "maxlength,truncates",
    [
        (None, False),  # unbounded -> fits
        ("", False),  # empty attr -> ignore
        ("abc", False),  # unparseable -> ignore
        ("20", False),  # larger than the 9-char value -> fits
        ("9", False),  # equal to the value length -> fits exactly
        ("4", True),  # positive maxlength shorter than the value -> truncates
        ("0", True),  # zero capacity -> truncates
    ],
)
def test_maxlength_truncates_value(maxlength: str | None, truncates: bool) -> None:
    # Routing gate for auto-advancing split fields: a positive maxlength shorter than the value keeps the field
    # on sequential entry (SKY-13821). Unlike _secret_input_cannot_round_trip this ignores CR/LF (a single-line
    # input strips those regardless, so they stay on the atomic path).
    assert _maxlength_truncates_value("123456789", maxlength=maxlength) is truncates


@pytest.mark.parametrize(
    "input_type,normalized",
    [
        ("password", "password"),
        ("TEXT", "text"),
        ("  email  ", "email"),
        (None, ""),
        ("", ""),
    ],
)
def test_exact_value_input_type_normalizes(input_type: str | None, normalized: str) -> None:
    assert _exact_value_input_type(input_type) == normalized


def _make_secret_element(readbacks: list[str | None]) -> MagicMock:
    locator = MagicMock()
    locator.input_value = AsyncMock(side_effect=list(readbacks))
    element = MagicMock()
    element.get_locator = MagicMock(return_value=locator)
    element.get_id = MagicMock(return_value="secret")
    element.input_sequentially = AsyncMock()
    element.input_clear = AsyncMock()
    element.input_fill = AsyncMock()
    element.refresh_locator_if_stale = AsyncMock()
    return element


async def _fill(
    element: MagicMock,
    *,
    text: str = SECRET,
    input_type: str = "password",
    maxlength: str | None = None,
    sequential_first: bool = False,
) -> ActionFailure | None:
    return await _fill_secret_with_readback(
        skyvern_element=element,
        tag_name="input",
        text=text,
        input_type=input_type,
        maxlength=maxlength,
        sequential_first=sequential_first,
    )


@pytest.mark.asyncio
async def test_fill_secret_recovers_mismatch_after_first_write() -> None:
    # A mismatched first read-back triggers a clear + retry that renders the value exactly; the recovery
    # succeeds and no failure is returned. The retry transport (sequential) is asserted separately.
    element = _make_secret_element([ROTATED, SECRET])

    result = await _fill(element)

    assert result is None
    element.input_clear.assert_awaited_once()
    element.input_fill.assert_awaited_once_with(text=SECRET)  # the single atomic first write


@pytest.mark.asyncio
async def test_fill_secret_recovers_text_truncation_via_readback() -> None:
    # SKY-12597/12579: a controlled text/email field drops the typed tail; the clear + retry recovers it.
    element = _make_secret_element([TRUNCATED, SECRET])

    result = await _fill(element, input_type="text")

    assert result is None
    element.input_clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_fill_secret_recovers_scramble_containing_mask_glyph_on_password() -> None:
    # A password secret containing "*"/"•" scrambled is caught by the exact .value comparison and recovered
    # by the clear + retry -- not skipped as "masked" (a password .value is the real value).
    element = _make_secret_element([GLYPH_SCRAMBLED, GLYPH_SECRET])

    result = await _fill(element, text=GLYPH_SECRET, input_type="password")

    assert result is None
    element.input_clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_fill_secret_first_write_is_atomic_fill() -> None:
    # SKY-13821 fill-first: the credential's first write is a single atomic fill, not the per-character seam.
    # A clean read-back confirms it with no re-entry; input_sequentially must never run.
    element = _make_secret_element([SECRET])

    result = await _fill(element)

    assert result is None
    element.input_fill.assert_awaited_once_with(text=SECRET)
    element.input_sequentially.assert_not_awaited()
    element.input_clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_typed_widget_secret_first_write_is_sequential_and_exact_readback_succeeds() -> None:
    element = _make_secret_element([SECRET])

    result = await _fill(element, sequential_first=True)

    assert result is None
    element.input_sequentially.assert_awaited_once_with(text=SECRET)
    element.input_fill.assert_not_awaited()
    element.get_locator.return_value.input_value.assert_awaited_once()
    element.input_clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_typed_widget_secret_persistent_mismatch_retries_once_and_fails_closed() -> None:
    element = _make_secret_element([ROTATED, ROTATED])

    result = await _fill(element, sequential_first=True)

    assert isinstance(result, ActionFailure)
    assert result.exception_type == "SecretInputMismatch"
    assert element.input_sequentially.await_count == 2
    element.input_fill.assert_not_awaited()
    element.input_clear.assert_awaited_once()
    assert element.get_locator.return_value.input_value.await_count == 2


@pytest.mark.asyncio
async def test_fill_secret_clean_first_try_skips_retry() -> None:
    element = _make_secret_element([SECRET])

    result = await _fill(element)

    assert result is None
    element.input_clear.assert_not_awaited()
    element.input_sequentially.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [SECRET, SHORT_SECRET])
async def test_fill_secret_recovers_drop_to_empty(text: str) -> None:
    # An empty first read-back (the fill was rejected/dropped) triggers the atomic recovery for both a long
    # and a short credential rather than silently submitting an empty field.
    element = _make_secret_element(["", text])

    result = await _fill(element, text=text, input_type="text")

    assert result is None
    element.input_clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_fill_secret_fails_after_second_mismatch() -> None:
    element = _make_secret_element([ROTATED, ROTATED])

    result = await _fill(element)

    assert isinstance(result, ActionFailure)
    assert result.success is False
    assert result.exception_type == "SecretInputMismatch"


@pytest.mark.asyncio
async def test_fill_secret_fails_when_retry_readback_empty() -> None:
    # After a confirmed first mismatch we cleared a known-bad value; an empty retry read-back is NOT a
    # positive confirmation, so fail loudly rather than proceed with an unverified secret.
    element = _make_secret_element([ROTATED, ""])

    result = await _fill(element)

    assert isinstance(result, ActionFailure)
    assert result.exception_type == "SecretInputMismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [SECRET, SHORT_SECRET])
async def test_fill_secret_fails_when_persistently_empty(text: str) -> None:
    element = _make_secret_element(["", ""])

    result = await _fill(element, text=text, input_type="text")

    assert isinstance(result, ActionFailure)
    assert result.exception_type == "SecretInputMismatch"


# --- caret-vulnerable eligibility gate + single-field TOTP read-back (SKY-13821) ---

# A synthetic six-digit TOTP; below the fill+type split boundary, so it is typed entirely character-by-
# character and, on a caret-resetting field, submits reordered with no verification today.
TOTP_CODE = "123456"
TOTP_REORDERED = TOTP_CODE[::-1]  # "654321": same digits, reordered


@pytest.mark.parametrize("input_type", ["text", "password", "search", "url", ""])
def test_caret_readback_eligible_for_vulnerable_input_types(input_type: str) -> None:
    assert _caret_readback_eligible(tag_name="input", input_type=input_type, text=TOTP_CODE) is True


@pytest.mark.parametrize("input_type", ["email", "number"])
def test_caret_readback_not_eligible_for_immune_input_types(input_type: str) -> None:
    # setSelectionRange raises InvalidStateError on email/number (Playwright swallows it), so they are immune.
    assert _caret_readback_eligible(tag_name="input", input_type=input_type, text=TOTP_CODE) is False


def test_caret_readback_excludes_tel_because_it_reformats() -> None:
    # tel is caret-vulnerable but reformats its value, so an exact read-back would false-fail a correctly
    # submitted code; a tel-formatted single-field TOTP stays a typed residual (SKY-13821).
    assert _caret_readback_eligible(tag_name="input", input_type="tel", text=TOTP_CODE) is False


@pytest.mark.parametrize("tag_name", ["textarea", "select", "a"])
def test_caret_readback_not_eligible_for_non_input_tags(tag_name: str) -> None:
    # Playwright's caret reset is <input>-only.
    assert _caret_readback_eligible(tag_name=tag_name, input_type="", text=TOTP_CODE) is False


def test_caret_readback_not_eligible_for_single_character() -> None:
    # A single character cannot be order-scrambled.
    assert _caret_readback_eligible(tag_name="input", input_type="text", text="1") is False


def test_caret_readback_not_eligible_when_type_unknown() -> None:
    assert _caret_readback_eligible(tag_name="input", input_type=None, text=TOTP_CODE) is False


@pytest.mark.parametrize(
    "maxlength,eligible",
    [
        (None, True),  # no declared capacity -> the whole code fits, atomic read-back is meaningful
        ("20", True),  # capacity longer than the code -> fits
        ("6", True),  # capacity equal to the code length -> still fits
        ("1", False),  # split-code first box: cannot hold the whole code, stay on the per-key seam
        ("0", False),  # zero capacity -> stay on the per-key seam
        ("abc", True),  # unparseable maxlength -> ignore the constraint, do not force the seam
        ("", True),  # empty maxlength attr -> ignore
    ],
)
def test_caret_readback_eligibility_respects_maxlength(maxlength: str | None, eligible: bool) -> None:
    # A split TOTP widget targets its first <input maxlength="1"> with the whole code. Routing that to the
    # atomic read-back would confine the code to the first box and report success; keep such a field on the
    # per-character seam so key events advance focus across the boxes (SKY-13821).
    assert (
        _caret_readback_eligible(tag_name="input", input_type="text", text=TOTP_CODE, maxlength=maxlength) is eligible
    )


@pytest.mark.asyncio
async def test_fill_secret_refreshes_stale_locator_before_write() -> None:
    # Parity with the ordinary atomic-fill branch (which calls refresh_locator_if_stale): a credential or
    # single-field TOTP input re-mounted between scrape and write must re-resolve its XPath before the atomic
    # fill, or the cached locator times out on a zero-match target (SKY-13821).
    element = _make_secret_element([SECRET])

    manager = MagicMock()
    manager.attach_mock(element.refresh_locator_if_stale, "refresh")
    manager.attach_mock(element.input_fill, "fill")

    result = await _fill(element)

    assert result is None
    element.refresh_locator_if_stale.assert_awaited_once()
    call_names = [c[0] for c in manager.mock_calls]
    assert call_names.index("refresh") < call_names.index("fill")


@pytest.mark.asyncio
async def test_fill_secret_refreshes_stale_locator_before_recovery_refill() -> None:
    # A re-mounting controlled field is a likely cause of the read-back mismatch that triggers recovery, so the
    # recovery clear/re-fill must also re-resolve the locator -- otherwise the single retry is spent raising on
    # a zero-match target (SKY-13821).
    element = _make_secret_element([ROTATED, SECRET])  # first read-back mismatches -> recovery clear + re-fill

    manager = MagicMock()
    manager.attach_mock(element.refresh_locator_if_stale, "refresh")
    manager.attach_mock(element.input_clear, "clear")

    result = await _fill(element)

    assert result is None
    assert element.refresh_locator_if_stale.await_count == 2  # before the first fill AND before recovery
    element.input_clear.assert_awaited_once()
    assert element.input_fill.await_count == 1  # first write is atomic; the retry is sequential (SKY-13821 @15)
    element.input_sequentially.assert_awaited_once_with(text=SECRET)
    assert [c[0] for c in manager.mock_calls] == ["refresh", "refresh", "clear"]  # recovery refresh precedes clear


@pytest.mark.asyncio
async def test_fill_secret_recovery_uses_sequential_retry() -> None:
    # On a JS-enforced auto-advancing widget (its per-box capacity is not a maxlength attr) the atomic fill is
    # reduced to one character and the read-back mismatches; repeating the same atomic fill can never emit the
    # key events that advance the siblings. The retry attempts the sequential transport instead. The first
    # write stays atomic (caret-immune); only the retry is sequential (SKY-13821 @15).
    element = _make_secret_element([ROTATED, SECRET])  # first atomic write mismatches -> sequential retry matches

    result = await _fill(element)

    assert result is None
    element.input_fill.assert_awaited_once_with(text=SECRET)  # the single atomic first write
    element.input_clear.assert_awaited_once()
    element.input_sequentially.assert_awaited_once_with(text=SECRET)  # the retry uses sequential transport


@pytest.mark.asyncio
async def test_fill_secret_sequential_retry_persistent_mismatch_fails_closed() -> None:
    # HARD acceptance: never return success just because the sequential retry did not raise. If the target
    # still cannot prove the intended value (an auto-advance box holds only a prefix, or the field keeps
    # mangling it), fail closed with SecretInputMismatch -- zero silent corruption (SKY-13821 @15).
    element = _make_secret_element([ROTATED, TRUNCATED])  # sequential retry read-back still mismatches

    result = await _fill(element)

    assert isinstance(result, ActionFailure)
    assert result.exception_type == "SecretInputMismatch"
    element.input_sequentially.assert_awaited_once_with(text=SECRET)


@pytest.mark.asyncio
async def test_fill_secret_sequential_retry_async_clear_fails_closed() -> None:
    # An async-cleared field (empty read-back after the sequential retry) is a mismatch, not a positive
    # confirmation -- fail closed rather than submit an unverified/empty credential (SKY-13821 @15).
    element = _make_secret_element([ROTATED, ""])

    result = await _fill(element)

    assert isinstance(result, ActionFailure)
    assert result.exception_type == "SecretInputMismatch"
    element.input_sequentially.assert_awaited_once_with(text=SECRET)


@pytest.mark.asyncio
async def test_fill_secret_recovery_never_logs_the_secret() -> None:
    # The sequential retry + fail-closed path must never log the secret, its length, or its characters.
    element = _make_secret_element([ROTATED, ROTATED])  # persistent mismatch -> fail closed

    with patch("skyvern.webeye.actions.handler.LOG") as mock_log:
        result = await _fill(element)

    assert isinstance(result, ActionFailure)
    logged = str(mock_log.mock_calls)
    assert SECRET not in logged and ROTATED not in logged


_NAV_ERROR = PlaywrightError("Execution context was destroyed, most likely because of a navigation")


@pytest.mark.asyncio
async def test_fill_secret_treats_navigation_after_fill_as_submitted() -> None:
    # A TOTP/credential field that auto-submits once the value completes tears down the execution context, so
    # the read-back raises a navigation error. The value was accepted and submitted, so treat it as success --
    # never re-raise out of the action or turn a correctly-entered code into SecretInputMismatch (SKY-13821).
    element = _make_secret_element([SECRET])

    with patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(side_effect=_NAV_ERROR)):
        result = await _fill(element)

    assert result is None
    element.input_fill.assert_awaited_once_with(text=SECRET)
    element.input_clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_secret_propagates_non_navigation_readback_error() -> None:
    # Guard: only navigation teardown is tolerated. Any other read-back driver error still propagates rather
    # than being swallowed into a false success.
    element = _make_secret_element([SECRET])

    with patch(
        "skyvern.webeye.actions.handler.get_input_value",
        new=AsyncMock(side_effect=PlaywrightError("some other driver failure")),
    ):
        with pytest.raises(PlaywrightError):
            await _fill(element)


@pytest.mark.asyncio
@pytest.mark.parametrize("input_type", ["text", "search"])
async def test_totp_shaped_reorder_recovered_via_retry(input_type: str) -> None:
    # A single-field TOTP typed across the seam and reordered (123456 -> 654321) is read back and re-entered
    # through the shared secret read-back path (a TOTP is a secret; the retry uses the sequential transport).
    # tel is excluded upstream by _caret_readback_eligible because it reformats, so the eligible types here are
    # exact-value ones.
    element = _make_secret_element([TOTP_REORDERED, TOTP_CODE])

    result = await _fill(element, text=TOTP_CODE, input_type=input_type)

    assert result is None
    element.input_clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_totp_shaped_reorder_fails_closed_on_persistent_mismatch() -> None:
    element = _make_secret_element([TOTP_REORDERED, TOTP_REORDERED])

    result = await _fill(element, text=TOTP_CODE, input_type="text")

    assert isinstance(result, ActionFailure)
    assert result.exception_type == "SecretInputMismatch"


@pytest.mark.asyncio
async def test_fill_secret_transforming_field_loud_fails() -> None:
    # An eligible text field that transforms the value (e.g. uppercases it) with no declared incompatibility
    # still mismatches after the clear + retry -> loud failure rather than submitting a known-different
    # credential (account-lockout-safe over silent-wrong-submit).
    transformed = SECRET.upper()
    element = _make_secret_element([transformed, transformed])

    result = await _fill(element, input_type="text")

    assert isinstance(result, ActionFailure)
    assert result.exception_type == "SecretInputMismatch"


@pytest.mark.asyncio
async def test_fill_secret_skips_fully_masked_non_password_field() -> None:
    # A non-password field that renders ONLY mask glyphs into .value for a real (non-glyph) secret cannot be
    # verified; leave it as filled rather than clearing a possibly-correct value and false-failing.
    element = _make_secret_element(["••••••••••••••••"])

    result = await _fill(element, input_type="text")

    assert result is None
    element.input_fill.assert_awaited_once_with(text=SECRET)
    element.input_sequentially.assert_not_awaited()
    element.input_clear.assert_not_awaited()


# A revealed (type=text) secret that contains mask-like characters mixed with real ones.
MIXED_MASK_SECRET = "aB*cd•eF*gh"
MIXED_MASK_SCRAMBLED = MIXED_MASK_SECRET[4:] + MIXED_MASK_SECRET[:4]


@pytest.mark.asyncio
async def test_fill_secret_recovers_mixed_mask_secret_on_text_field() -> None:
    # The any()->all() correctness fix: a revealed secret containing a "*"/"•" among real characters is
    # readable, so a scramble is a real mismatch that gets recovered -- it must NOT be skipped as masked
    # (which would silently reproduce the bug for exactly the "show password" text fields this covers).
    element = _make_secret_element([MIXED_MASK_SCRAMBLED, MIXED_MASK_SECRET])

    result = await _fill(element, text=MIXED_MASK_SECRET, input_type="text")

    assert result is None
    element.input_clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_fill_secret_mixed_mask_secret_exact_match_needs_no_recovery() -> None:
    element = _make_secret_element([MIXED_MASK_SECRET])

    result = await _fill(element, text=MIXED_MASK_SECRET, input_type="text")

    assert result is None
    element.input_clear.assert_not_awaited()
    element.input_fill.assert_awaited_once_with(text=MIXED_MASK_SECRET)
    element.input_sequentially.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_secret_all_glyph_secret_exact_match_is_not_skipped() -> None:
    # A secret that is legitimately all mask glyphs and round-trips exactly is a MATCH (confirmed), not an
    # unreadable-mask skip -- equality is checked before the mask heuristic.
    all_glyph = "******"
    element = _make_secret_element([all_glyph])

    result = await _fill(element, text=all_glyph, input_type="text")

    assert result is None
    element.input_clear.assert_not_awaited()
    element.input_fill.assert_awaited_once_with(text=all_glyph)
    element.input_sequentially.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_secret_skips_maxlength_truncating_field_without_reading_back() -> None:
    # A field whose positive maxlength is shorter than the value cannot hold it; skip the exact read-back
    # (no read, no clear, no fail) so a legacy truncate-at-signup-and-login site keeps succeeding.
    element = _make_secret_element([])

    result = await _fill(element, input_type="text", maxlength="8")

    assert result is None
    element.input_fill.assert_awaited_once_with(text=SECRET)
    element.input_sequentially.assert_not_awaited()
    element.get_locator.return_value.input_value.assert_not_awaited()
    element.input_clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_secret_skips_newline_bearing_single_line_field() -> None:
    # A single-line input strips CR/LF, so a stored secret containing a newline can never round-trip;
    # skip the read-back instead of deterministically loud-failing a correct-as-possible fill.
    text = "abcdefghij\nklmnop"
    element = _make_secret_element([])

    result = await _fill(element, text=text, input_type="text")

    assert result is None
    element.input_fill.assert_awaited_once_with(text=text)
    element.input_sequentially.assert_not_awaited()
    element.get_locator.return_value.input_value.assert_not_awaited()
    element.input_clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_secret_logs_carry_no_secret_material() -> None:
    # The loud-failure path must never log the secret, its length, or its character classes.
    element = _make_secret_element([ROTATED, ROTATED])

    with patch("skyvern.webeye.actions.handler.LOG") as mock_log:
        result = await _fill(element)

    assert isinstance(result, ActionFailure)
    logged = " ".join(
        repr(call.args) + repr(call.kwargs) for call in (*mock_log.warning.mock_calls, *mock_log.info.mock_calls)
    )
    assert SECRET not in logged
    assert ROTATED not in logged
    assert str(len(SECRET)) not in logged
    assert result.exception_type == "SecretInputMismatch"
    assert SECRET not in (result.exception_message or "")


@pytest.mark.asyncio
async def test_fill_secret_threads_engine_selection_to_both_readbacks() -> None:
    selection = object()
    element = _make_secret_element([])
    with patch(
        "skyvern.webeye.actions.handler.get_input_value",
        new=AsyncMock(side_effect=[ROTATED, SECRET]),
    ) as get_input_value:
        result = await _fill_secret_with_readback(
            skyvern_element=element,
            tag_name="input",
            text=SECRET,
            input_type="password",
            maxlength=None,
            engine_selection=selection,
        )

    assert result is None
    assert [call.kwargs["engine_selection"] for call in get_input_value.await_args_list] == [selection, selection]
