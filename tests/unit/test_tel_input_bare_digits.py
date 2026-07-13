from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.actions import handler_utils
from skyvern.webeye.actions.handler import _is_tel_digit_fix_enabled, _nanp_national_digits, _plan_tel_text
from tests.unit.helpers import make_organization, make_task


@pytest.mark.parametrize(
    "value,expected",
    [
        ("+1 (224) 555-0199", "2245550199"),
        ("+12245550199", "2245550199"),
        ("1-224-555-0199", "2245550199"),
        ("1 (224) 555-0199", "2245550199"),
        ("(224) 555-0199", "2245550199"),
        ("224-555-0199", "2245550199"),
        ("224.555.0199", "2245550199"),
        ("224 555 0199", "2245550199"),
        ("12245550199", None),  # bare leading 1 is not written as a country code
        ("13912345678", None),  # 11-digit non-NANP mobile whose first digit is 1
        ("15012345678", None),
        ("13987654321", None),
        ("(13) 98765-4321", None),  # 2-digit area code + 9-digit subscriber, strips to 1 + 10
        ("0412345678", None),
        ("0412 345 678", None),
        ("1234567890", None),
        ("2245550199", None),
        ("4155550123", None),
        ("(024) 555-0199", None),
        ("(224) 155-0199", None),
        ("+44 20 7946 0958", None),
        ("224-555-0199 x123", None),
        ("224-555-019", None),
        ("", None),
    ],
)
def test_nanp_national_digits(value: str, expected: str | None) -> None:
    assert _nanp_national_digits(value) == expected


def test_plan_tel_text_strips_secret_resolved_formatted_nanp() -> None:
    # A secret with an affirmative NANP country code is typed as national digits and is never sent to
    # the format-check LLM.
    text, used_bare, run_format_check = _plan_tel_text(
        is_tel=True, is_secret=True, value="+1 (224) 555-0199", pattern=None
    )
    assert text == "2245550199"
    assert used_bare is True
    assert run_format_check is False


@pytest.mark.parametrize(
    "is_secret,expected_format_check",
    [(False, True), (True, False)],
)
def test_plan_tel_text_ineligible_value_falls_back(is_secret: bool, expected_format_check: bool) -> None:
    value = "0412 345 678"
    assert _plan_tel_text(is_tel=True, is_secret=is_secret, value=value, pattern=None) == (
        value,
        False,
        expected_format_check,
    )


def test_plan_tel_text_self_formatting_field_uses_bare_digits() -> None:
    # A permissive pattern that accepts bare digits keeps the bare-digit fast path.
    text, used_bare, run_format_check = _plan_tel_text(
        is_tel=True, is_secret=False, value="(224) 555-0199", pattern=r"[+0-9().\- ]{7,20}"
    )
    assert text == "2245550199"
    assert used_bare is True
    assert run_format_check is False


def test_plan_tel_text_masked_field_keeps_format_check() -> None:
    # A field whose pattern requires a specific mask (bare digits don't match) is not stripped; the
    # non-secret format-check path still runs.
    text, used_bare, run_format_check = _plan_tel_text(
        is_tel=True, is_secret=False, value="(224) 555-0199", pattern=r"\(\d{3}\) \d{3}-\d{4}"
    )
    assert text == "(224) 555-0199"
    assert used_bare is False
    assert run_format_check is True


def test_plan_tel_text_masked_secret_skips_llm() -> None:
    # A masked field carrying a secret: not stripped, and the LLM is never called for secrets.
    assert _plan_tel_text(is_tel=True, is_secret=True, value="(224) 555-0199", pattern=r"\(\d{3}\) \d{3}-\d{4}") == (
        "(224) 555-0199",
        False,
        False,
    )


def test_plan_tel_text_non_tel_passthrough() -> None:
    assert _plan_tel_text(is_tel=False, is_secret=False, value="(224) 555-0199", pattern=None) == (
        "(224) 555-0199",
        False,
        False,
    )


@pytest.mark.asyncio
async def test_input_sequentially_does_not_fill_split_ten_digits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare 10-digit values are typed in one pass — no fill()-split that an auto-formatting tel field
    would mangle into a dropped digit."""
    typed: list[str] = []
    monkeypatch.setattr(
        handler_utils.EventStrategyFactory,
        "type_text",
        AsyncMock(side_effect=lambda page, locator, text: typed.append(text)),
    )
    locator = MagicMock()
    locator.fill = AsyncMock()
    locator.page = MagicMock()

    await handler_utils.input_sequentially(locator, "2245550199")

    locator.fill.assert_not_called()
    assert typed == ["2245550199"]


@pytest.mark.asyncio
async def test_input_sequentially_fill_splits_separator_formatted_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 14-char separator-formatted value gets fill()-split into the half-open '(224', which a live
    AsYouType formatter collapses — the failure mode the bare-digit path avoids."""
    typed: list[str] = []
    monkeypatch.setattr(
        handler_utils.EventStrategyFactory,
        "type_text",
        AsyncMock(side_effect=lambda page, locator, text: typed.append(text)),
    )
    locator = MagicMock()
    locator.fill = AsyncMock()
    locator.page = MagicMock()

    await handler_utils.input_sequentially(locator, "(224) 555-0199")

    locator.fill.assert_awaited_once()
    assert locator.fill.await_args.args[0] == "(224"
    assert typed == [") 555-0199"]


@pytest.mark.asyncio
async def test_is_tel_digit_fix_enabled_uses_org_keyed_rollout(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    provider = SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=True))
    monkeypatch.setattr("skyvern.webeye.actions.handler.app.EXPERIMENTATION_PROVIDER", provider)

    assert await _is_tel_digit_fix_enabled(task) is True
    provider.is_feature_enabled_cached.assert_awaited_once_with(
        "FIX_TEL_INPUT_DIGIT_DROP",
        organization.organization_id,
        properties={"organization_id": organization.organization_id},
    )
