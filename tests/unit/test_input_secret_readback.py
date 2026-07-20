from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye.actions.handler import (
    _exact_value_input_type,
    _fill_secret_with_readback,
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
    return element


async def _fill(
    element: MagicMock, *, text: str = SECRET, input_type: str = "password", maxlength: str | None = None
) -> ActionFailure | None:
    return await _fill_secret_with_readback(
        skyvern_element=element, tag_name="input", text=text, input_type=input_type, maxlength=maxlength
    )


@pytest.mark.asyncio
async def test_fill_secret_recovers_rotation_with_atomic_fill() -> None:
    # First (character-by-character) entry rotates the value; the atomic re-entry renders it exactly.
    element = _make_secret_element([ROTATED, SECRET])

    result = await _fill(element)

    assert result is None
    element.input_sequentially.assert_awaited_once_with(text=SECRET)
    element.input_clear.assert_awaited_once()
    element.input_fill.assert_awaited_once_with(text=SECRET)


@pytest.mark.asyncio
async def test_fill_secret_recovers_text_truncation_via_readback() -> None:
    # SKY-12597/12579: a controlled text/email field drops the typed tail; the atomic re-fill recovers it.
    element = _make_secret_element([TRUNCATED, SECRET])

    result = await _fill(element, input_type="text")

    assert result is None
    element.input_clear.assert_awaited_once()
    element.input_fill.assert_awaited_once_with(text=SECRET)


@pytest.mark.asyncio
async def test_fill_secret_recovers_scramble_containing_mask_glyph_on_password() -> None:
    # A password secret containing "*"/"•" scrambled is caught by the exact .value comparison and recovered
    # by the atomic re-entry -- not skipped as "masked" (a password .value is the real value).
    element = _make_secret_element([GLYPH_SCRAMBLED, GLYPH_SECRET])

    result = await _fill(element, text=GLYPH_SECRET, input_type="password")

    assert result is None
    element.input_clear.assert_awaited_once()
    element.input_fill.assert_awaited_once_with(text=GLYPH_SECRET)


@pytest.mark.asyncio
async def test_fill_secret_clean_first_try_skips_retry() -> None:
    element = _make_secret_element([SECRET])

    result = await _fill(element)

    assert result is None
    element.input_clear.assert_not_awaited()
    element.input_fill.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [SECRET, SHORT_SECRET])
async def test_fill_secret_recovers_drop_to_empty(text: str) -> None:
    # An empty first read-back (the fill was rejected/dropped) triggers the atomic recovery for both a long
    # and a short credential rather than silently submitting an empty field.
    element = _make_secret_element(["", text])

    result = await _fill(element, text=text, input_type="text")

    assert result is None
    element.input_clear.assert_awaited_once()
    element.input_fill.assert_awaited_once_with(text=text)


@pytest.mark.asyncio
async def test_fill_secret_fails_after_second_mismatch() -> None:
    element = _make_secret_element([ROTATED, ROTATED])

    result = await _fill(element)

    assert isinstance(result, ActionFailure)
    assert result.success is False
    assert result.exception_type == "SecretInputMismatch"
    element.input_fill.assert_awaited_once_with(text=SECRET)


@pytest.mark.asyncio
async def test_fill_secret_fails_when_retry_readback_empty() -> None:
    # After a confirmed first mismatch we cleared a known-bad value; an empty retry read-back is NOT a
    # positive confirmation, so fail loudly rather than proceed with an unverified secret.
    element = _make_secret_element([ROTATED, ""])

    result = await _fill(element)

    assert isinstance(result, ActionFailure)
    assert result.exception_type == "SecretInputMismatch"
    element.input_fill.assert_awaited_once_with(text=SECRET)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [SECRET, SHORT_SECRET])
async def test_fill_secret_fails_when_persistently_empty(text: str) -> None:
    element = _make_secret_element(["", ""])

    result = await _fill(element, text=text, input_type="text")

    assert isinstance(result, ActionFailure)
    assert result.exception_type == "SecretInputMismatch"
    element.input_fill.assert_awaited_once_with(text=text)


@pytest.mark.asyncio
async def test_fill_secret_transforming_field_loud_fails() -> None:
    # An eligible text field that transforms the value (e.g. uppercases it) with no declared incompatibility
    # still mismatches after the atomic re-fill -> loud failure rather than submitting a known-different
    # credential (account-lockout-safe over silent-wrong-submit).
    transformed = SECRET.upper()
    element = _make_secret_element([transformed, transformed])

    result = await _fill(element, input_type="text")

    assert isinstance(result, ActionFailure)
    assert result.exception_type == "SecretInputMismatch"


@pytest.mark.asyncio
async def test_fill_secret_skips_fully_masked_non_password_field() -> None:
    # A non-password field that renders ONLY mask glyphs into .value for a real (non-glyph) secret cannot be
    # verified; leave it as typed rather than clearing a possibly-correct value and false-failing.
    element = _make_secret_element(["••••••••••••••••"])

    result = await _fill(element, input_type="text")

    assert result is None
    element.input_sequentially.assert_awaited_once_with(text=SECRET)
    element.input_clear.assert_not_awaited()
    element.input_fill.assert_not_awaited()


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
    element.input_fill.assert_awaited_once_with(text=MIXED_MASK_SECRET)


@pytest.mark.asyncio
async def test_fill_secret_mixed_mask_secret_exact_match_needs_no_recovery() -> None:
    element = _make_secret_element([MIXED_MASK_SECRET])

    result = await _fill(element, text=MIXED_MASK_SECRET, input_type="text")

    assert result is None
    element.input_clear.assert_not_awaited()
    element.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_secret_all_glyph_secret_exact_match_is_not_skipped() -> None:
    # A secret that is legitimately all mask glyphs and round-trips exactly is a MATCH (confirmed), not an
    # unreadable-mask skip -- equality is checked before the mask heuristic.
    all_glyph = "******"
    element = _make_secret_element([all_glyph])

    result = await _fill(element, text=all_glyph, input_type="text")

    assert result is None
    element.input_clear.assert_not_awaited()
    element.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_secret_skips_maxlength_truncating_field_without_reading_back() -> None:
    # A field whose positive maxlength is shorter than the value cannot hold it; skip the exact read-back
    # (no read, no clear, no fail) so a legacy truncate-at-signup-and-login site keeps succeeding.
    element = _make_secret_element([])

    result = await _fill(element, input_type="text", maxlength="8")

    assert result is None
    element.input_sequentially.assert_awaited_once_with(text=SECRET)
    element.get_locator.return_value.input_value.assert_not_awaited()
    element.input_clear.assert_not_awaited()
    element.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_secret_skips_newline_bearing_single_line_field() -> None:
    # A single-line input strips CR/LF, so a stored secret containing a newline can never round-trip;
    # skip the read-back instead of deterministically loud-failing a correct-as-possible fill.
    text = "abcdefghij\nklmnop"
    element = _make_secret_element([])

    result = await _fill(element, text=text, input_type="text")

    assert result is None
    element.input_sequentially.assert_awaited_once_with(text=text)
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
