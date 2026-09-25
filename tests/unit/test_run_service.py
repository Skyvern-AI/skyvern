import asyncio
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from skyvern.constants import SKYVERN_UI_USER_AGENT
from skyvern.forge.sdk.routes import agent_protocol
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.schemas.runs import RunResponse, RunStatus, RunType, WorkflowRunResponse
from skyvern.services import run_service

OWNER_ORG = "o_owner"
OTHER_ORG = "o_other"
RUN_ID = "wr_1"


def _org(organization_id: str) -> Organization:
    now = datetime.now(UTC)
    return Organization(
        organization_id=organization_id, organization_name=organization_id, created_at=now, modified_at=now
    )


class _GatedBuild:
    """Stands in for get_run_response: org-scoped like the DB reads, and parked until released."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str, bool]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.status = RunStatus.running
        self.error: Exception | None = None

    async def __call__(
        self, run_id: str, organization_id: str | None = None, cap_output_values: bool = False
    ) -> WorkflowRunResponse | None:
        self.calls.append((organization_id, run_id, cap_output_values))
        self.entered.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        if organization_id != OWNER_ORG:
            return None
        now = datetime.now(UTC)
        return WorkflowRunResponse(
            run_id=run_id, run_type=RunType.workflow_run, status=self.status, created_at=now, modified_at=now
        )


@pytest.fixture
def build(monkeypatch: pytest.MonkeyPatch) -> _GatedBuild:
    fake = _GatedBuild()
    monkeypatch.setattr(run_service, "get_run_response", fake)
    monkeypatch.setattr(run_service, "_IN_FLIGHT", {})
    return fake


async def _get(run_id: str = RUN_ID, org: str = OWNER_ORG, user_agent: str | None = None) -> RunResponse:
    return await agent_protocol.get_run(run_id=run_id, current_org=_org(org), x_user_agent=user_agent)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_identical_overlapping_polls_share_one_build(build: _GatedBuild) -> None:
    polls = [asyncio.create_task(_get()) for _ in range(20)]
    await asyncio.wait_for(build.entered.wait(), timeout=5)
    await _settle()
    build.release.set()
    results = await asyncio.gather(*polls)

    assert len(build.calls) == 1
    assert all(result == results[0] for result in results)
    assert run_service._IN_FLIGHT == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant",
    [
        {"run_id": "wr_2"},
        {"user_agent": SKYVERN_UI_USER_AGENT},
    ],
)
async def test_different_run_or_cap_builds_separately(build: _GatedBuild, variant: dict[str, str]) -> None:
    first = asyncio.create_task(_get())
    second = asyncio.create_task(_get(**variant))
    await _settle()
    build.release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert len(build.calls) == 2
    assert first_result.run_id == RUN_ID
    assert second_result.run_id == variant.get("run_id", RUN_ID)


@pytest.mark.asyncio
async def test_other_org_overlapping_poll_gets_404_not_the_shared_payload(build: _GatedBuild) -> None:
    owner = asyncio.create_task(_get())
    await asyncio.wait_for(build.entered.wait(), timeout=5)
    intruder = asyncio.create_task(_get(org=OTHER_ORG))
    await _settle()
    build.release.set()

    assert (await owner).run_id == RUN_ID
    with pytest.raises(HTTPException) as exc_info:
        await intruder
    assert exc_info.value.status_code == 404
    assert [call[0] for call in build.calls] == [OWNER_ORG, OTHER_ORG]


@pytest.mark.asyncio
async def test_build_error_reaches_every_waiter_and_next_poll_rebuilds(build: _GatedBuild) -> None:
    build.error = RuntimeError("db down")
    polls = [asyncio.create_task(_get()) for _ in range(3)]
    await _settle()
    build.release.set()
    results = await asyncio.gather(*polls, return_exceptions=True)

    assert all(isinstance(result, RuntimeError) for result in results)
    assert run_service._IN_FLIGHT == {}

    build.error = None
    assert (await _get()).status == RunStatus.running
    assert len(build.calls) == 2


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_the_shared_build(build: _GatedBuild) -> None:
    leaver = asyncio.create_task(_get())
    stayer = asyncio.create_task(_get())
    await _settle()
    leaver.cancel()
    await _settle()
    build.release.set()

    assert (await stayer).run_id == RUN_ID
    assert leaver.cancelled()
    assert len(build.calls) == 1
    assert run_service._IN_FLIGHT == {}


@pytest.mark.asyncio
async def test_completed_build_is_not_reused_by_the_next_poll(build: _GatedBuild) -> None:
    build.release.set()
    assert (await _get()).status == RunStatus.running
    build.status = RunStatus.completed

    assert (await _get()).status == RunStatus.completed
    assert len(build.calls) == 2


@pytest.mark.asyncio
async def test_poll_after_build_finishes_but_before_cleanup_rebuilds(build: _GatedBuild) -> None:
    async def poll_on_release() -> RunResponse:
        await build.release.wait()
        return await _get()

    first = asyncio.create_task(_get())
    await asyncio.wait_for(build.entered.wait(), timeout=5)
    late = asyncio.create_task(poll_on_release())
    await _settle()
    build.release.set()
    await asyncio.gather(first, late)

    assert len(build.calls) == 2
    assert run_service._IN_FLIGHT == {}
