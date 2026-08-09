from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock

import pytest

from skyvern.services import planner_levers

BoolResolver = Callable[[str | None], Awaitable[bool]]

BOOL_LEVERS: list[tuple[str, BoolResolver]] = [
    (
        "TASK_V2_SKIP_COMPLETION_CHECK_AFTER_NAVIGATE",
        planner_levers.skip_completion_check_after_navigate,
    ),
    ("TASK_V2_CARRY_SUBGOALS", planner_levers.carry_subgoals),
    (
        "RESET_BROWSER_TABS_BETWEEN_LOOP_ITERATIONS",
        planner_levers.reset_browser_tabs_between_loop_iterations,
    ),
]
BOOL_LEVER_IDS = ["skip_completion_check_after_navigate", "carry_subgoals", "reset_browser_tabs"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("setting_name", "resolver"), BOOL_LEVERS, ids=BOOL_LEVER_IDS)
@pytest.mark.parametrize(
    ("settings_default", "provider_enabled", "expected", "provider_called"),
    [
        (True, False, True, False),
        (False, True, True, True),
        (False, False, False, True),
    ],
)
async def test_bool_lever_resolves_env_or_org_flag(
    setting_name: str,
    resolver: BoolResolver,
    settings_default: bool,
    provider_enabled: bool,
    expected: bool,
    provider_called: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AsyncMock(return_value=provider_enabled)
    monkeypatch.setattr(planner_levers.settings, setting_name, settings_default)
    monkeypatch.setattr(planner_levers.app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", provider)

    assert await resolver("org_test") is expected
    if provider_called:
        provider.assert_awaited_once_with(
            "TASK_V2_PLANNER_LEVERS",
            "org_test",
            properties={"organization_id": "org_test"},
        )
    else:
        provider.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("setting_name", "resolver"), BOOL_LEVERS, ids=BOOL_LEVER_IDS)
async def test_bool_lever_falls_back_to_env_when_provider_raises(
    setting_name: str,
    resolver: BoolResolver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    monkeypatch.setattr(planner_levers.settings, setting_name, False)
    monkeypatch.setattr(planner_levers.app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", provider)

    assert await resolver("org_test") is False
    provider.assert_awaited_once_with(
        "TASK_V2_PLANNER_LEVERS",
        "org_test",
        properties={"organization_id": "org_test"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("setting_name", "resolver"), BOOL_LEVERS, ids=BOOL_LEVER_IDS)
@pytest.mark.parametrize("settings_default", [False, True])
async def test_bool_lever_without_org_uses_env_only(
    setting_name: str,
    resolver: BoolResolver,
    settings_default: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AsyncMock()
    monkeypatch.setattr(planner_levers.settings, setting_name, settings_default)
    monkeypatch.setattr(planner_levers.app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", provider)

    assert await resolver(None) is settings_default
    provider.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings_default", "provider_enabled", "expected", "provider_called"),
    [
        (0, False, 0, True),
        (0, True, 20, True),
        (37, False, 37, False),
        (37, True, 37, False),
    ],
)
async def test_converge_pct_resolves_env_or_org_flag(
    settings_default: int,
    provider_enabled: bool,
    expected: int,
    provider_called: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AsyncMock(return_value=provider_enabled)
    monkeypatch.setattr(planner_levers.settings, "TASK_V2_CONVERGE_PCT", settings_default)
    monkeypatch.setattr(planner_levers.app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", provider)

    assert await planner_levers.converge_pct("org_test") == expected
    if provider_called:
        provider.assert_awaited_once_with(
            "TASK_V2_PLANNER_LEVERS",
            "org_test",
            properties={"organization_id": "org_test"},
        )
    else:
        provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_converge_pct_falls_back_to_env_when_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    monkeypatch.setattr(planner_levers.settings, "TASK_V2_CONVERGE_PCT", 0)
    monkeypatch.setattr(planner_levers.app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", provider)

    assert await planner_levers.converge_pct("org_test") == 0
    provider.assert_awaited_once_with(
        "TASK_V2_PLANNER_LEVERS",
        "org_test",
        properties={"organization_id": "org_test"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("settings_default", [0, 37])
async def test_converge_pct_without_org_uses_env_only(
    settings_default: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AsyncMock()
    monkeypatch.setattr(planner_levers.settings, "TASK_V2_CONVERGE_PCT", settings_default)
    monkeypatch.setattr(planner_levers.app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", provider)

    assert await planner_levers.converge_pct(None) == settings_default
    provider.assert_not_awaited()
