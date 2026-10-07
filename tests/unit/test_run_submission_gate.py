import asyncio

import pytest
from fastapi import HTTPException

from skyvern.config import Settings
from skyvern.forge import app
from skyvern.forge.sdk.core import run_submission_gate
from skyvern.forge.sdk.core.run_submission_gate import (
    DISABLE_RUN_SUBMISSION_GATE_FLAG,
    RETRY_AFTER_SECONDS,
    RunSubmissionGate,
    run_submission_slot,
)
from tests.unit._workflow_block_engine_fakes import FakeExperimentationProvider

# A leaked slot must fail the test, not hang it: no pytest timeout is configured.
_BOUNDED_WAIT_SECONDS = 1


async def _hold_slot(gate: RunSubmissionGate, entered: asyncio.Event, release: asyncio.Event) -> None:
    async with gate.slot():
        entered.set()
        await release.wait()


@pytest.mark.asyncio
async def test_submission_over_the_cap_waits_for_a_slot_instead_of_failing() -> None:
    gate = RunSubmissionGate(limit=1, wait_seconds=5)
    entered, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_slot(gate, entered, release))
    await asyncio.wait_for(entered.wait(), _BOUNDED_WAIT_SECONDS)

    admitted = asyncio.Event()

    async def queued_submission() -> None:
        async with gate.slot():
            admitted.set()

    waiter = asyncio.create_task(queued_submission())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(admitted.wait()), timeout=0.05)
    assert (gate.in_flight, gate.waiting) == (1, 1)

    release.set()
    await asyncio.wait_for(asyncio.gather(holder, waiter), _BOUNDED_WAIT_SECONDS)
    assert admitted.is_set()
    assert (gate.in_flight, gate.waiting) == (0, 0)


@pytest.mark.asyncio
async def test_submission_that_gets_no_slot_in_time_is_rejected_before_it_dispatches() -> None:
    gate = RunSubmissionGate(limit=1, wait_seconds=0.05)
    dispatched = False

    async with gate.slot():
        with pytest.raises(HTTPException) as rejected:
            async with gate.slot():
                dispatched = True
        assert gate.waiting == 0

    assert not dispatched
    assert rejected.value.status_code == 503
    assert rejected.value.headers == {"Retry-After": str(RETRY_AFTER_SECONDS)}
    # The timed-out waiter must not have consumed the slot the holder then released.
    async with gate.slot():
        pass


@pytest.mark.asyncio
async def test_slot_is_released_when_dispatch_fails_or_the_request_is_cancelled() -> None:
    gate = RunSubmissionGate(limit=1, wait_seconds=0.05)

    with pytest.raises(RuntimeError):
        async with gate.slot():
            raise RuntimeError("dispatch failed")

    entered = asyncio.Event()
    cancelled = asyncio.create_task(_hold_slot(gate, entered, asyncio.Event()))
    await asyncio.wait_for(entered.wait(), _BOUNDED_WAIT_SECONDS)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert gate.in_flight == 0

    # A leaked slot would make this wait out its 50 ms and raise the 503.
    async with gate.slot():
        pass


@pytest.mark.parametrize(
    ("provider", "admitted"),
    [
        (FakeExperimentationProvider({DISABLE_RUN_SUBMISSION_GATE_FLAG: True}), True),
        (FakeExperimentationProvider(), False),
        (FakeExperimentationProvider(raise_error=True), False),
    ],
    ids=["kill_switch_on", "flag_absent", "flag_lookup_fails"],
)
@pytest.mark.asyncio
async def test_kill_switch_lets_submissions_past_a_full_gate_and_anything_else_keeps_the_setting(
    monkeypatch: pytest.MonkeyPatch, provider: FakeExperimentationProvider, admitted: bool
) -> None:
    full_gate = RunSubmissionGate(limit=1, wait_seconds=0.05)
    monkeypatch.setattr(run_submission_gate, "_GATE", full_gate)
    monkeypatch.setattr(app, "EXPERIMENTATION_PROVIDER", provider)
    dispatched = False

    async with full_gate.slot():
        try:
            async with run_submission_slot("o_123"):
                dispatched = True
        except HTTPException as rejected:
            assert rejected.status_code == 503

    assert dispatched is admitted


@pytest.mark.parametrize(
    ("limit", "provider"),
    [
        (Settings.model_fields["RUN_SUBMISSION_MAX_CONCURRENCY"].default, FakeExperimentationProvider()),
        (1, FakeExperimentationProvider({DISABLE_RUN_SUBMISSION_GATE_FLAG: True})),
    ],
    ids=["default_limit", "kill_switch_on"],
)
@pytest.mark.asyncio
async def test_gate_that_is_off_never_waits_or_rejects_but_still_counts_submissions_in_flight(
    monkeypatch: pytest.MonkeyPatch, limit: int, provider: FakeExperimentationProvider
) -> None:
    gate = RunSubmissionGate(limit=limit, wait_seconds=0.05)
    monkeypatch.setattr(run_submission_gate, "_GATE", gate)
    monkeypatch.setattr(app, "EXPERIMENTATION_PROVIDER", provider)
    # More submissions than the limit, so a gate that enforced it would leave some waiting.
    entered = [asyncio.Event() for _ in range(limit + 5)]
    release = asyncio.Event()

    async def submission(index: int) -> None:
        async with run_submission_slot("o_123"):
            entered[index].set()
            await release.wait()
            if index == 0:
                raise RuntimeError("dispatch failed")

    submissions = [asyncio.create_task(submission(index)) for index in range(len(entered))]
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), _BOUNDED_WAIT_SECONDS)
    assert (gate.in_flight, gate.waiting) == (len(entered), 0)

    submissions[1].cancel()
    with pytest.raises(asyncio.CancelledError):
        await submissions[1]
    assert gate.in_flight == len(entered) - 1

    release.set()
    outcomes = await asyncio.wait_for(
        asyncio.gather(submissions[0], *submissions[2:], return_exceptions=True), _BOUNDED_WAIT_SECONDS
    )
    assert isinstance(outcomes[0], RuntimeError)
    assert outcomes[1:] == [None] * (len(entered) - 2)
    assert (gate.in_flight, gate.waiting) == (0, 0)
