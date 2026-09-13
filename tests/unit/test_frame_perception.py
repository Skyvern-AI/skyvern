"""Tests for the per-run resolution of TASK_V3_FRAME_PERCEPTION.

Mirrors `tests/unit/test_planner_levers.py`'s conventions (patch `settings` and the
provider's `is_feature_enabled_cached` directly) and `tests/unit/test_workflow_block_engine.py`'s
NoOp-provider pattern (a real `NoOpExperimentationProvider` instance, spied so the test can prove
it was never queried, since the provider itself would otherwise return False regardless).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.experimentation.providers import NoOpExperimentationProvider
from skyvern.forge.taskv3 import frame_perception
from skyvern.forge.taskv3.frame_perception import (
    FRAME_PERCEPTION_FLAG,
    frame_perception_enabled,
    resolve_frame_perception,
)
from skyvern.forge.taskv3.tools import build_browser_tools
from tests.unit.test_taskv3_tools import _FakePage, _fixed_page_provider, _tool


@pytest.fixture
def scoped_context() -> Iterator[SkyvernContext]:
    context = SkyvernContext()
    skyvern_context.set(context)
    try:
        yield context
    finally:
        skyvern_context.reset()


def test_env_true_forces_on_without_consulting_a_pinned_false_context(
    scoped_context: SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The context is explicitly pinned OFF for this run; the env term must still win, because it
    # short-circuits before the pin is read at all.
    scoped_context.frame_perception_flag = False
    scoped_context.frame_perception_resolved_run_id = "tsk_pinned_off"
    provider = AsyncMock(return_value=False)
    monkeypatch.setattr(settings, "TASK_V3_FRAME_PERCEPTION", True)
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", provider)

    assert frame_perception_enabled() is True
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_env_false_and_provider_resolves_true(
    scoped_context: SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "TASK_V3_FRAME_PERCEPTION", False)
    provider = AsyncMock(return_value=True)
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", provider)

    await resolve_frame_perception(scoped_context, distinct_id="tsk_true", organization_id="org_1")

    assert frame_perception_enabled() is True
    provider.assert_awaited_once_with(FRAME_PERCEPTION_FLAG, "tsk_true", properties={"organization_id": "org_1"})


def test_env_false_and_context_never_resolved_is_off(
    scoped_context: SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No resolve_frame_perception call at all: a fresh context's resolved_run_id stays None.
    monkeypatch.setattr(settings, "TASK_V3_FRAME_PERCEPTION", False)

    assert scoped_context.frame_perception_resolved_run_id is None
    assert frame_perception_enabled() is False


def test_no_context_at_all_is_off_and_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TASK_V3_FRAME_PERCEPTION", False)
    skyvern_context.reset()

    assert skyvern_context.current() is None
    assert frame_perception_enabled() is False


@pytest.mark.asyncio
async def test_same_distinct_id_pins_and_does_not_re_resolve(
    scoped_context: SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The anti-re-evaluation guarantee: a second resolve for the same run must not re-query the
    # provider, even if the provider would now answer differently. A cache-expiry or a mid-run ramp
    # must not be able to flip an already-pinned run.
    monkeypatch.setattr(settings, "TASK_V3_FRAME_PERCEPTION", False)
    provider = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", provider)

    await resolve_frame_perception(scoped_context, distinct_id="tsk_pin", organization_id="org_1")
    assert frame_perception_enabled() is True

    await resolve_frame_perception(scoped_context, distinct_id="tsk_pin", organization_id="org_1")
    assert frame_perception_enabled() is True

    provider.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_different_distinct_id_re_resolves(
    scoped_context: SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "TASK_V3_FRAME_PERCEPTION", False)
    provider = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", provider)

    await resolve_frame_perception(scoped_context, distinct_id="tsk_a", organization_id="org_1")
    assert frame_perception_enabled() is True

    await resolve_frame_perception(scoped_context, distinct_id="tsk_b", organization_id="org_1")
    assert frame_perception_enabled() is False

    assert provider.await_count == 2


@pytest.mark.asyncio
async def test_provider_exception_resolves_off_and_still_pins(
    scoped_context: SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "TASK_V3_FRAME_PERCEPTION", False)
    provider = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", provider)

    await resolve_frame_perception(scoped_context, distinct_id="tsk_err", organization_id="org_1")

    assert frame_perception_enabled() is False
    assert scoped_context.frame_perception_resolved_run_id == "tsk_err"
    provider.assert_awaited_once()


@pytest.mark.asyncio
async def test_noop_provider_is_off_without_ever_being_queried(
    scoped_context: SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "TASK_V3_FRAME_PERCEPTION", False)
    provider = NoOpExperimentationProvider()
    spy = AsyncMock(wraps=provider.is_feature_enabled_cached)
    monkeypatch.setattr(provider, "is_feature_enabled_cached", spy)
    monkeypatch.setattr(frame_perception.app, "EXPERIMENTATION_PROVIDER", provider)

    await resolve_frame_perception(scoped_context, distinct_id="tsk_noop", organization_id="org_1")

    assert frame_perception_enabled() is False
    spy.assert_not_awaited()


def test_the_pin_reaches_the_module_level_iframe_reach_clause(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.taskv3.tools import _iframe_reach_clause
    from skyvern.forge.taskv3.tools import _Observation as Obs

    monkeypatch.setattr(settings, "TASK_V3_FRAME_PERCEPTION", False)
    observation = Obs({}, [], [], {}, {}, 0, 0)

    context = SkyvernContext(frame_perception_flag=True, frame_perception_resolved_run_id="tsk_reach")
    skyvern_context.set(context)
    try:
        assert "actionable by ref" in _iframe_reach_clause(observation)
    finally:
        skyvern_context.reset()

    # Complement: same shape of context, but the run was never resolved -- must read as off.
    unresolved = SkyvernContext()
    skyvern_context.set(unresolved)
    try:
        assert "NOT reachable by selector" in _iframe_reach_clause(observation)
    finally:
        skyvern_context.reset()


def test_the_pin_reaches_the_build_time_observe_tool_description(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TASK_V3_FRAME_PERCEPTION", False)

    pinned = SkyvernContext(frame_perception_flag=True, frame_perception_resolved_run_id="tsk_build")
    skyvern_context.set(pinned)
    try:
        tools = build_browser_tools(_fixed_page_provider(_FakePage()))
        description = _tool(tools, "observe").description
    finally:
        skyvern_context.reset()
    assert "acted on by ref" in description
    assert "cannot be observed or reached" not in description

    # Complement: an unresolved run baked at build time must get the no-reach description.
    unresolved = SkyvernContext()
    skyvern_context.set(unresolved)
    try:
        tools = build_browser_tools(_fixed_page_provider(_FakePage()))
        description = _tool(tools, "observe").description
    finally:
        skyvern_context.reset()
    assert "cannot be observed or reached" in description
    assert "acted on by ref" not in description


@pytest.mark.asyncio
async def test_parallel_branches_sharing_one_context_resolve_the_arm_once(
    scoped_context: SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Parallel branches of one run share a SkyvernContext, so both reach resolve with the same
    # distinct_id before either has pinned. Unless the first is single-flighted, the second queries
    # the provider too and overwrites the arm the first already baked into its observe description
    # -- and the provider's failure path answers False, so that overwrite is an ordinary network
    # blip rather than a rare straddled ramp.
    monkeypatch.setattr(settings, "TASK_V3_FRAME_PERCEPTION", False)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    answers = iter([True, False])

    async def _racing_provider(flag: str, distinct_id: str, properties: dict | None = None) -> bool:
        first_entered.set()
        await release_first.wait()
        return next(answers)

    provider = AsyncMock(side_effect=_racing_provider)
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", provider)

    winner = asyncio.create_task(
        resolve_frame_perception(scoped_context, distinct_id="tsk_race", organization_id="org_1")
    )
    await first_entered.wait()
    loser = asyncio.create_task(
        resolve_frame_perception(scoped_context, distinct_id="tsk_race", organization_id="org_1")
    )
    for _ in range(5):
        await asyncio.sleep(0)
    release_first.set()
    await asyncio.gather(winner, loser)

    assert provider.await_count == 1
    assert frame_perception_enabled() is True


def test_no_module_outside_the_accessor_reads_the_env_setting_directly() -> None:
    """The migration's own invariant, made mechanical rather than remembered.

    Twelve sites moved from `settings.TASK_V3_FRAME_PERCEPTION` to `frame_perception_enabled()`, and
    reverting any ONE of them is invisible to a behaviour test: with the flag at 0% the two
    expressions agree everywhere except under a context pinned against the env setting, which only a
    few sites are exercised under. Several of the rest -- the page fingerprint, the realm document
    id, the frame-text tail -- have no on-arm observable at all without a real browser, so there is no
    behaviour test to write for them in this suite.

    This checks the property a revert actually breaks, for every site at once and for the thirteenth
    site nobody has written yet. It is the enforcement the runbook's removal section says does not
    exist.
    """
    import ast  # noqa: PLC0415
    import pathlib  # noqa: PLC0415

    import skyvern  # noqa: PLC0415

    repo_root = pathlib.Path(skyvern.__file__).resolve().parent.parent
    accessor = "skyvern/forge/taskv3/frame_perception.py"
    reads: set[str] = set()
    for root in (repo_root / "skyvern", repo_root / "cloud"):
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8", errors="replace")
            # A cheap prefilter, not the test: the attribute name is the thing being searched for, so
            # any spelling that reaches it -- including through an aliased `settings` import -- still
            # contains this literal.
            if "TASK_V3_FRAME_PERCEPTION" not in source:
                continue
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Attribute) and node.attr == "TASK_V3_FRAME_PERCEPTION":
                    reads.add(path.relative_to(repo_root).as_posix())

    # Anti-vacuity: a walk that matched nothing would satisfy the check below in silence. The
    # accessor itself reads the setting three times and must always appear here.
    assert accessor in reads, "the scan found no reads at all; it is not looking at the source"
    assert reads == {accessor}, (
        f"these read the env setting instead of frame_perception_enabled(): {sorted(reads - {accessor})}"
    )
