import asyncio
import json
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.failure_classifier import HAR_ANTI_BOT_DIAGNOSTIC_REASON_CODE
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, TaskV2Status
from skyvern.services import task_v2_service
from tests.unit.conftest import cancel_once_parked

PRIMARY = {"category": "WEBSITE_ERROR", "confidence_float": 0.9, "reasoning": "x"}
DIAGNOSTIC = {
    "category": "ANTI_BOT_DETECTION",
    "confidence_float": 0.8,
    "reason_code": HAR_ANTI_BOT_DIAGNOSTIC_REASON_CODE,
    "reasoning": "datadome enforcement was recorded during the failed attempt; it is not known to have caused it",
}
ORGANIZATION = SimpleNamespace(
    organization_id="o_1",
    organization_name="o",
    default_llm_key=None,
    default_secondary_llm_key=None,
    created_at=datetime(2026, 1, 1, tzinfo=UTC),
)


ROOT_MAPPING = {"ACCESS_DENIED": "The site shows an unusual-activity page"}
FINISHED = datetime(2026, 8, 1, 0, 10)


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, callback: str | None = "https://example.com/hook") -> None:
        self.order: list[str] = []
        self.payloads: list[dict[str, Any]] = []
        self.task_v2 = TaskV2(
            task_id="oc_1",
            status=TaskV2Status.failed,
            organization_id="o_1",
            workflow_run_id="wr_1",
            workflow_id="w_1",
            webhook_callback_url=callback,
            error_code_mapping=ROOT_MAPPING,
            failure_category=[PRIMARY],
            created_at=datetime(2026, 8, 1),
            modified_at=datetime(2026, 8, 1),
        )
        self.run_categories: list[dict[str, Any]] = [PRIMARY]
        # The snapshot the run's own terminal write returns; None models a refused write.
        self.owner: Any = SimpleNamespace(workflow_run_id="wr_1", organization_id="o_1", finished_at=FINISHED)
        db = task_v2_service.app.DATABASE
        monkeypatch.setattr(db.observer, "get_task_v2", AsyncMock(side_effect=self._reload))
        monkeypatch.setattr(db.observer, "set_final_failure_diagnostic", AsyncMock(side_effect=self._mirror))
        self.park_at: str | None = None
        self.parked = asyncio.Event()
        self.released = asyncio.Event()
        monkeypatch.setattr(
            db.workflow_runs,
            "get_workflow_run",
            AsyncMock(
                side_effect=lambda *a, **k: SimpleNamespace(failure_category=self.run_categories, finished_at=FINISHED)
            ),
        )
        monkeypatch.setattr(
            db.organizations, "get_valid_org_auth_token", AsyncMock(return_value=SimpleNamespace(token="k"))
        )
        monkeypatch.setattr(
            task_v2_service,
            "build_task_v2_run_response",
            AsyncMock(return_value=SimpleNamespace(model_dump_json=lambda **_: "{}")),
        )
        monkeypatch.setattr(task_v2_service, "generate_skyvern_webhook_signature", self._sign)
        monkeypatch.setattr(task_v2_service, "deliver_webhook_with_retries", AsyncMock(side_effect=self._deliver))
        monkeypatch.setattr(task_v2_service, "_validate_task_v2_model_for_org", AsyncMock())
        monkeypatch.setattr(task_v2_service, "acquire_run_lease", AsyncMock(return_value=True))
        monkeypatch.setattr(task_v2_service, "run_lease_lost", AsyncMock(return_value=False))
        monkeypatch.setattr(task_v2_service, "track_run", lambda *a, **k: nullcontext())
        monkeypatch.setattr(task_v2_service, "run_task_v2_helper", self._helper)
        monkeypatch.setattr(
            task_v2_service.app.WORKFLOW_SERVICE, "clean_up_workflow", AsyncMock(side_effect=self._cleanup)
        )
        self.cleanup_error: Exception | None = None

    async def _park(self, point: str) -> None:
        if self.park_at == point:
            self.parked.set()
            await self.released.wait()

    async def _reload(self, *_: Any, **__: Any) -> TaskV2:
        if "cleanup" in self.order:
            await self._park("reload")
        return self.task_v2

    async def _mirror(self, task_v2_id: str, organization_id: str, workflow_run_id: str, entry: dict, _: Any) -> TaskV2:
        await self._park("mirror")
        self.task_v2 = self.task_v2.model_copy(
            update={"failure_category": [*(self.task_v2.failure_category or []), entry]}
        )
        return self.task_v2

    def _sign(self, payload: dict[str, Any], api_key: str) -> SimpleNamespace:
        self.payloads.append(payload)
        return SimpleNamespace(headers={}, signed_payload=json.dumps(payload, default=str))

    async def _deliver(self, **_: Any) -> SimpleNamespace:
        self.order.append("webhook")
        return SimpleNamespace(status_code=200, text="")

    async def _helper(self, **_: Any) -> tuple[Any, Any, TaskV2]:
        # The terminal writers send from inside the helper, before run_task_v2's cleanup.
        if self.owner is not None:
            task_v2_service._hold_terminal_write("oc_1", "wr_1")(self.owner)
        await task_v2_service.send_task_v2_webhook(self.task_v2)
        self.order.append("terminal_write")
        return (
            SimpleNamespace(workflow_id="w_1"),
            SimpleNamespace(parent_workflow_run_id=None, browser_address=None),
            self.task_v2,
        )

    async def _cleanup(self, **kwargs: Any) -> None:
        self.order.append("cleanup")
        self.cleanup_kwargs = kwargs
        await self._park("annotation")
        self.run_categories = [PRIMARY, DIAGNOSTIC]
        if self.cleanup_error is not None:
            raise self.cleanup_error


@pytest.mark.asyncio
async def test_run_task_v2_sends_its_terminal_webhook_once_cleanup_recorded_the_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(monkeypatch)

    await task_v2_service.run_task_v2(ORGANIZATION, "oc_1")  # type: ignore[arg-type]

    assert harness.order == ["terminal_write", "cleanup", "webhook"]
    [payload] = harness.payloads
    assert payload["failure_category"] == [PRIMARY, DIAGNOSTIC]


@pytest.mark.asyncio
async def test_a_failing_cleanup_still_sends_the_held_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(monkeypatch)
    harness.cleanup_error = RuntimeError("browser teardown failed")

    with pytest.raises(RuntimeError):
        await task_v2_service.run_task_v2(ORGANIZATION, "oc_1")  # type: ignore[arg-type]

    assert harness.order == ["terminal_write", "cleanup", "webhook"]


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_error", [RuntimeError("database unavailable"), asyncio.CancelledError()])
async def test_a_failing_workflow_lookup_after_a_helper_error_still_sends_the_held_webhook(
    monkeypatch: pytest.MonkeyPatch, lookup_error: BaseException
) -> None:
    harness = Harness(monkeypatch)

    async def crash(**_: Any) -> None:
        raise RuntimeError("planner crashed")

    async def mark_failed(*_: Any, **__: Any) -> TaskV2:
        await task_v2_service.send_task_v2_webhook(harness.task_v2)
        harness.order.append("terminal_write")
        return harness.task_v2

    monkeypatch.setattr(task_v2_service, "run_task_v2_helper", crash)
    monkeypatch.setattr(task_v2_service, "mark_task_v2_as_failed", mark_failed)
    monkeypatch.setattr(task_v2_service.app.WORKFLOW_SERVICE, "get_workflow", AsyncMock(side_effect=lookup_error))

    with pytest.raises(type(lookup_error)):
        await task_v2_service.run_task_v2(ORGANIZATION, "oc_1")  # type: ignore[arg-type]

    assert harness.order == ["terminal_write", "webhook"]
    assert task_v2_service._deferred_task_v2_webhooks.get() is None


@pytest.mark.asyncio
async def test_a_sender_outside_run_task_v2_delivers_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(monkeypatch)

    await task_v2_service.send_task_v2_webhook(harness.task_v2)

    assert harness.order == ["webhook"] and harness.payloads[0]["failure_category"] == [PRIMARY]


@pytest.mark.asyncio
@pytest.mark.parametrize("callback", ["https://example.com/hook", None])
async def test_the_task_v2_row_mirrors_the_diagnostic_with_or_without_a_callback(
    monkeypatch: pytest.MonkeyPatch, callback: str | None
) -> None:
    harness = Harness(monkeypatch, callback=callback)

    returned = await task_v2_service.run_task_v2(ORGANIZATION, "oc_1")  # type: ignore[arg-type]

    assert harness.cleanup_kwargs["final_diagnostic_error_code_mapping"] == ROOT_MAPPING
    assert harness.task_v2.failure_category == [PRIMARY, DIAGNOSTIC]
    assert returned.model_dump(by_alias=True)["failure_category"] == [PRIMARY, DIAGNOSTIC]
    assert harness.order == (["terminal_write", "cleanup", "webhook"] if callback else ["terminal_write", "cleanup"])
    assert [p["failure_category"] for p in harness.payloads] == ([[PRIMARY, DIAGNOSTIC]] if callback else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("park_at", ["annotation", "mirror", "reload"])
@pytest.mark.parametrize("callback", ["https://example.com/hook", None])
async def test_cancelling_the_optional_diagnostic_work_still_sends_the_held_webhook_then_propagates(
    monkeypatch: pytest.MonkeyPatch, park_at: str, callback: str | None
) -> None:
    harness = Harness(monkeypatch, callback=callback)
    harness.park_at = park_at
    deferral_at_exit: list[object] = []

    async def owned() -> None:
        try:
            await task_v2_service.run_task_v2(ORGANIZATION, "oc_1")  # type: ignore[arg-type]
        finally:
            # Read inside the task that ran it: run_task_v2 must have restored its own deferral context.
            deferral_at_exit.append(task_v2_service._deferred_task_v2_webhooks.get())

    run = await cancel_once_parked(owned(), harness.parked, harness.released)

    assert run.cancelled()
    assert harness.order == (["terminal_write", "cleanup", "webhook"] if callback else ["terminal_write", "cleanup"])
    assert deferral_at_exit == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize("callback", ["https://example.com/hook", None])
@pytest.mark.parametrize("park_at", [None, "mirror"], ids=["completes", "cancelled-mirror"])
async def test_a_terminated_task_v2_returns_its_mirrored_row(
    monkeypatch: pytest.MonkeyPatch, callback: str | None, park_at: str | None
) -> None:
    harness = Harness(monkeypatch, callback=callback)
    harness.park_at = park_at

    async def terminate(**_: Any) -> None:
        raise task_v2_service.TaskTerminationError(reason="the goal cannot be reached")

    async def mark_terminated(**_: Any) -> TaskV2:
        # The real writer's own conditional terminal write is what hands run_task_v2 its diagnostic owner.
        task_v2_service._hold_terminal_write("oc_1", "wr_1")(harness.owner)
        await task_v2_service.send_task_v2_webhook(harness.task_v2)
        harness.order.append("terminal_write")
        return harness.task_v2

    monkeypatch.setattr(task_v2_service, "run_task_v2_helper", terminate)
    monkeypatch.setattr(task_v2_service, "mark_task_v2_as_terminated", mark_terminated)
    monkeypatch.setattr(
        task_v2_service.app.WORKFLOW_SERVICE, "get_workflow", AsyncMock(return_value=SimpleNamespace(workflow_id="w_1"))
    )
    monkeypatch.setattr(
        task_v2_service.app.WORKFLOW_SERVICE,
        "get_workflow_run",
        AsyncMock(return_value=SimpleNamespace(parent_workflow_run_id=None, browser_address=None)),
    )
    order = ["terminal_write", "cleanup", *(["webhook"] if callback else [])]

    if park_at is None:
        returned = await task_v2_service.run_task_v2(ORGANIZATION, "oc_1")  # type: ignore[arg-type]
        # A nested Task V2 block persists what this returns, so it must be the row the diagnostic was mirrored onto.
        assert returned.failure_category == [PRIMARY, DIAGNOSTIC]
    else:
        run = await cancel_once_parked(
            task_v2_service.run_task_v2(ORGANIZATION, "oc_1"),  # type: ignore[arg-type]
            harness.parked,
            harness.released,
        )
        assert run.cancelled()
    assert harness.order == order
    assert [p["failure_category"] for p in harness.payloads] == (
        [[PRIMARY, DIAGNOSTIC] if park_at is None else [PRIMARY]] if callback else []
    )


WRITERS = {
    "failed": ("mark_task_v2_as_failed", "mark_workflow_run_as_failed", TaskV2Status.failed),
    "terminated": ("mark_task_v2_as_terminated", "mark_workflow_run_as_terminated", TaskV2Status.terminated),
    "timed_out": ("mark_task_v2_as_timed_out", "mark_workflow_run_as_timed_out", TaskV2Status.timed_out),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", list(WRITERS))
@pytest.mark.parametrize("won", [True, False], ids=["own-write", "same-status-refusal"])
@pytest.mark.parametrize("callback", ["https://example.com/hook", None])
@pytest.mark.parametrize("lease", [True, False], ids=["lease", "no-lease"])
async def test_a_task_v2_terminal_writer_owns_the_diagnostic_only_through_its_own_conditional_write(
    monkeypatch: pytest.MonkeyPatch, writer: str, won: bool, callback: str | None, lease: bool
) -> None:
    """The real task v2 writer inside run_task_v2, with no attempt rows and a nonterminal helper snapshot."""
    harness = Harness(monkeypatch, callback=callback)
    harness.owner = None
    task_writer, run_marker, status = WRITERS[writer]
    harness.task_v2 = harness.task_v2.model_copy(update={"status": status})
    written = SimpleNamespace(workflow_run_id="wr_1", organization_id="o_1", finished_at=FINISHED)
    newer = SimpleNamespace(workflow_run_id="wr_1", organization_id="o_1", finished_at=datetime(2026, 8, 1, 0, 20))

    async def marker(*_: Any, _on_terminal_write: Any = None, **__: Any) -> Any:
        if won:
            _on_terminal_write(written)
            return written
        return newer

    monkeypatch.setattr(task_v2_service, "run_lease_tasks_enabled", lambda: lease)
    monkeypatch.setattr(task_v2_service, "_update_task_v2_status", AsyncMock(return_value=harness.task_v2))
    monkeypatch.setattr(task_v2_service.app.WORKFLOW_SERVICE, run_marker, AsyncMock(side_effect=marker))

    async def helper(**_: Any) -> tuple[Any, Any, TaskV2]:
        await getattr(task_v2_service, task_writer)("oc_1", workflow_run_id="wr_1", organization_id="o_1")
        harness.order.append("terminal_write")
        return (
            SimpleNamespace(workflow_id="w_1"),
            SimpleNamespace(parent_workflow_run_id=None, browser_address=None, finished_at=None),
            harness.task_v2,
        )

    monkeypatch.setattr(task_v2_service, "run_task_v2_helper", helper)

    returned = await task_v2_service.run_task_v2(ORGANIZATION, "oc_1")  # type: ignore[arg-type]

    assert harness.cleanup_kwargs["final_diagnostic_owner"] is (written if won else None)
    assert returned.failure_category == ([PRIMARY, DIAGNOSTIC] if won else [PRIMARY])
    assert harness.order == ["terminal_write", "cleanup", *(["webhook"] if callback else [])]
