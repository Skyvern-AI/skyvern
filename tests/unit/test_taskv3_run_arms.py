from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.taskv3.run_arms import resolve_run_arm, run_arm_enabled

_ARM_LOG = "Resolved Task V3 run arm"
_FLAG = "TASK_V3_EXAMPLE_ARM"
_OTHER_FLAG = "TASK_V3_OTHER_ARM"


@pytest.fixture
def scoped_context() -> Iterator[SkyvernContext]:
    context = SkyvernContext()
    skyvern_context.set(context)
    try:
        yield context
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reader", "arm", "enabled"),
    [
        (AsyncMock(return_value="treatment"), "treatment", True),
        (AsyncMock(return_value="control"), "control", False),
        (AsyncMock(return_value=None), "unrandomized", False),
        (AsyncMock(return_value="true"), "unrandomized", False),
        (AsyncMock(side_effect=RuntimeError("flag service down")), "unrandomized", False),
    ],
)
async def test_arm_log_tells_randomized_control_from_never_randomized(
    scoped_context: SkyvernContext, monkeypatch: pytest.MonkeyPatch, reader: AsyncMock, arm: str, enabled: bool
) -> None:
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "get_value_cached", reader)

    with capture_logs() as logs:
        await resolve_run_arm(scoped_context, _FLAG, distinct_id="wr_1", organization_id="o_1", forced=False)

    assert run_arm_enabled(_FLAG, forced=False) is enabled
    [line] = [log for log in logs if log["event"] == _ARM_LOG]
    assert (line["flag"], line["distinct_id"], line["arm"], line["enabled"], line["forced_by_env"]) == (
        _FLAG,
        "wr_1",
        arm,
        enabled,
        False,
    )


@pytest.mark.asyncio
async def test_env_setting_forces_on_but_the_log_keeps_the_randomized_arm(
    scoped_context: SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "get_value_cached", AsyncMock(return_value="control"))

    with capture_logs() as logs:
        await resolve_run_arm(scoped_context, _FLAG, distinct_id="wr_1", organization_id="o_1", forced=True)

    assert run_arm_enabled(_FLAG, forced=True) is True
    assert run_arm_enabled(_FLAG, forced=False) is False
    [line] = [log for log in logs if log["event"] == _ARM_LOG]
    assert (line["arm"], line["enabled"], line["forced_by_env"]) == ("control", True, True)


@pytest.mark.asyncio
async def test_later_blocks_of_a_run_keep_the_first_resolution(
    scoped_context: SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = AsyncMock(side_effect=["treatment", "control", "control"])
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "get_value_cached", reader)

    with capture_logs() as logs:
        await resolve_run_arm(scoped_context, _FLAG, distinct_id="wr_1", organization_id="o_1", forced=False)
        await resolve_run_arm(scoped_context, _FLAG, distinct_id="wr_1", organization_id="o_1", forced=False)
        assert run_arm_enabled(_FLAG, forced=False) is True
        await resolve_run_arm(scoped_context, _FLAG, distinct_id="wr_2", organization_id="o_1", forced=False)

    assert run_arm_enabled(_FLAG, forced=False) is False
    assert [log["distinct_id"] for log in logs if log["event"] == _ARM_LOG] == ["wr_1", "wr_2"]


@pytest.mark.asyncio
async def test_each_flag_keeps_its_own_arm_within_one_run(
    scoped_context: SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = AsyncMock(side_effect=lambda flag, *_a, **_k: "treatment" if flag == _FLAG else "control")
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "get_value_cached", reader)

    await resolve_run_arm(scoped_context, _FLAG, distinct_id="wr_1", organization_id="o_1", forced=False)
    await resolve_run_arm(scoped_context, _OTHER_FLAG, distinct_id="wr_1", organization_id="o_1", forced=False)

    assert (run_arm_enabled(_FLAG, forced=False), run_arm_enabled(_OTHER_FLAG, forced=False)) == (True, False)
