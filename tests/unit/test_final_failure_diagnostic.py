import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from skyvern.config import settings
from skyvern.forge import final_failure_diagnostic as ffd
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.failure_classifier import (
    HAR_ANTI_BOT_DIAGNOSTIC_REASON_CODE,
    derive_failure_attribution,
    is_output_only_entry,
    without_output_only_categories,
)
from skyvern.forge.sdk.copilot.challenge_evidence import is_carrier_backed_category_entry
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import (
    TaskModel,
    TaskV2Model,
    WorkflowModel,
    WorkflowRunAttemptModel,
    WorkflowRunBlockModel,
    WorkflowRunModel,
)
from skyvern.forge.sdk.schemas.har_evidence import (
    HarEvidence,
    HarEvidenceAvailability,
    HarEvidenceEvent,
    HarEvidenceVisit,
)
from tests.unit.conftest import cancel_once_parked

FAILED_AT = datetime(2026, 8, 1, 0, 10, tzinfo=UTC)
PRIMARY = {"category": "NAVIGATION_FAILURE", "confidence_float": 0.9, "reasoning": "page did not load"}


def _at(seconds_before_failure: float) -> datetime:
    return FAILED_AT - timedelta(seconds=seconds_before_failure)


def _event(event_type: str, before: float, **fields: Any) -> HarEvidenceEvent:
    return HarEvidenceEvent(event_type=event_type, event_at=_at(before), **fields)


def _visit(
    events: tuple[HarEvidenceEvent, ...] = (),
    *,
    start: float = 300,
    end: float = 1,
    domain: str = "shop.example.com",
    vendors: tuple[str, ...] = (),
    visit_n: int = 1,
) -> HarEvidenceVisit:
    return HarEvidenceVisit(
        tab_ref="t1",
        visit_n=visit_n,
        domain=domain,
        started_at=_at(start),
        ended_at=_at(end),
        vendors_seen=vendors,
        clearance_effective="unknown",
        events=events,
    )


def _evidence(*visits: HarEvidenceVisit) -> HarEvidence:
    return HarEvidence(availability=HarEvidenceAvailability.available, visits=visits)


def _context(
    mapping: dict[str, str] | None = None, failure_reason: str | None = "The page did not load"
) -> ffd.FailureContext:
    return ffd.FailureContext(
        organization_id="o_ffd",
        task_id="tsk_ffd",
        attempt_number=1,
        window_start=FAILED_AT - timedelta(minutes=30),
        finished_at=FAILED_AT,
        failure_reason=failure_reason,
        failed_action=ffd.FailedActionFacts(
            action_id="a_1",
            action_type="click",
            status="failed",
            step_id="stp_1",
            step_order=3,
            screenshot_artifact_id="art_1",
            agent_intention="Click the search button",
        ),
        error_code_mapping=mapping or {},
    )


class FakeMatcher:
    def __init__(self, response: Any = None, delay: float = 0) -> None:
        self.response = response
        self.delay = delay
        self.prompts: list[str] = []

    async def __call__(self, *, prompt: str, **_: Any) -> Any:
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.response


@pytest.fixture
def matcher(monkeypatch: pytest.MonkeyPatch) -> FakeMatcher:
    fake = FakeMatcher()
    monkeypatch.setattr(ffd.app, "SECONDARY_LLM_API_HANDLER", fake, raising=False)
    return fake


AKAMAI_BLOCK = _event("explicit_block", 10, vendor="akamai", http_status=403, url_class="document")
# A refused static asset names its vendor inside the attempt without being enforcement on the page.
AKAMAI_ASSET = _event("explicit_block", 20, vendor="akamai", http_status=403, url_class="static")


@pytest.mark.parametrize(
    ("visits", "enforcement"),
    [
        pytest.param((_visit((AKAMAI_BLOCK,), vendors=("akamai",)),), "explicit_block", id="page-block"),
        pytest.param(
            (_visit((_event("explicit_block", 10, vendor="akamai", http_status=403, url_class="api"),)),),
            "explicit_block",
            id="api-403",
        ),
        pytest.param(
            (_visit((_event("explicit_block", 10, vendor="akamai", http_status=403, url_class="static"),)),),
            "none_observed",
            id="static-403-irrelevant",
        ),
        pytest.param(
            (_visit((_event("explicit_block", 10, vendor="akamai", http_status=503, url_class="document"),)),),
            "none_observed",
            id="maintenance-503",
        ),
        pytest.param(
            (_visit((_event("challenge_issued", 20, vendor="cloudflare", challenge_type="managed"),)),),
            "unresolved_challenge",
            id="challenge",
        ),
        pytest.param(
            # Blocked early, then minutes of traffic that loaded: that neither clears nor erases the block.
            (_visit((_event("repeat_block_after_challenge", 280, vendor="cloudflare"),), start=300, end=1),),
            "repeat_block",
            id="later-traffic-keeps-the-observation",
        ),
        pytest.param(
            (
                _visit((AKAMAI_BLOCK.model_copy(update={"event_at": _at(900)}),), start=950, end=890),
                _visit(start=600, end=2, visit_n=2),
            ),
            "explicit_block",
            id="block-on-an-earlier-visit-of-the-attempt",
        ),
        pytest.param(
            # Recorded before this attempt started, so it belongs to an earlier one.
            (_visit((AKAMAI_BLOCK.model_copy(update={"event_at": _at(3600)}),), start=3700, end=3500),),
            "none_observed",
            id="earlier-attempt",
        ),
    ],
)
def test_enforcement_observed_in_the_failed_attempt(visits: tuple[HarEvidenceVisit, ...], enforcement: str) -> None:
    diagnostic, _ = ffd.diagnose(_evidence(*visits), _context())

    assert diagnostic.enforcement.value == enforcement


@pytest.mark.parametrize(
    "event",
    [
        AKAMAI_BLOCK,
        _event("challenge_resource_failed", 3, vendor="cloudflare", url_class="static"),
        _event("challenge_issued", 1, vendor="cloudflare"),
    ],
)
def test_no_enforcement_is_reported_as_the_cause_of_the_failure(event: HarEvidenceEvent) -> None:
    context = _context()
    diagnostic, _ = ffd.diagnose(_evidence(_visit((event,))), context)
    entry = ffd.diagnostic_entry(diagnostic, context)

    assert entry is not None and entry["anti_bot"]["contribution"] == "unknown"
    assert "not known to have caused" in entry["reasoning"]


@pytest.mark.asyncio
async def test_a_page_block_becomes_an_output_only_entry_naming_its_evidence(matcher: FakeMatcher) -> None:
    context = _context()
    diagnostic = await ffd.final_failure_diagnostic(_evidence(_visit((AKAMAI_BLOCK,), vendors=("akamai",))), context)
    entry = ffd.diagnostic_entry(diagnostic, context)

    assert entry is not None and entry["reason_code"] == HAR_ANTI_BOT_DIAGNOSTIC_REASON_CODE
    assert entry["anti_bot"] | {} == {
        **entry["anti_bot"],
        "vendors_observed": ["akamai"],
        "enforcement": "explicit_block",
        "enforcing_vendor": "akamai",
        "contribution": "unknown",
        "evidence_refs": ["har:t1/1/0"],
        "failed_task_id": "tsk_ffd",
        "failed_action_id": "a_1",
        "failed_screenshot_artifact_id": "art_1",
    }
    # No mapping, so no model call; the entry is a label every runtime reader skips.
    assert matcher.prompts == []
    categories = [PRIMARY, entry]
    assert is_output_only_entry(entry) and without_output_only_categories(categories) == [PRIMARY]
    assert not is_carrier_backed_category_entry(entry)
    assert derive_failure_attribution(categories) == derive_failure_attribution([PRIMARY])


@pytest.mark.parametrize(
    "availability",
    [a for a in HarEvidenceAvailability if a != HarEvidenceAvailability.available],
)
@pytest.mark.asyncio
async def test_unavailable_evidence_reports_nothing_without_a_grounded_match(
    availability: HarEvidenceAvailability, matcher: FakeMatcher
) -> None:
    context = _context({"BOT_BLOCK": "The site blocked the bot"}, failure_reason=None)
    diagnostic = await ffd.final_failure_diagnostic(HarEvidence.unavailable(availability), context)

    assert diagnostic.availability == availability and ffd.diagnostic_entry(diagnostic, context) is None


@pytest.mark.asyncio
async def test_a_loop_with_no_vendor_or_enforcement_does_not_ask_the_matcher(matcher: FakeMatcher) -> None:
    context = _context({"ACCESS_DENIED": "Akamai Access Denied page"}, failure_reason=None)
    context = ffd.FailureContext(**{**context.__dict__, "failed_action": None})
    diagnostic = await ffd.final_failure_diagnostic(_evidence(_visit(domain="login.example.net")), context)

    assert matcher.prompts == [] and ffd.diagnostic_entry(diagnostic, context) is None


MAPPING = {"ACCESS_DENIED": "The site shows an unusual-activity or access denied page", "BAD_LOGIN": "Wrong password"}
UNUSUAL = "The page says unusual activity was detected from this browser"


@pytest.mark.asyncio
async def test_an_unusual_page_with_no_known_marker_still_reaches_the_configured_descriptions(
    matcher: FakeMatcher,
) -> None:
    matcher.response = {"error_code": "ACCESS_DENIED", "evidence_refs": ["failure_reason", "vendors_present"]}
    context = _context(MAPPING, failure_reason=UNUSUAL)
    diagnostic = await ffd.final_failure_diagnostic(_evidence(_visit((AKAMAI_ASSET,), vendors=("akamai",))), context)
    entry = ffd.diagnostic_entry(diagnostic, context)

    assert len(matcher.prompts) == 1 and "BAD_LOGIN" in matcher.prompts[0] and "presence_only" in matcher.prompts[0]
    assert entry is not None and entry["error_code"] == "ACCESS_DENIED"
    assert entry["reasoning"] == f"ACCESS_DENIED: {MAPPING['ACCESS_DENIED']}"
    assert entry["anti_bot"]["enforcement"] == "none_observed" and entry["anti_bot"]["contribution"] == "unknown"
    assert entry["anti_bot"]["evidence_refs"] == ["failure_reason", "vendors_present"]


@pytest.mark.asyncio
async def test_an_ordinary_error_on_an_unprotected_site_never_reaches_the_matcher(matcher: FakeMatcher) -> None:
    matcher.response = {"error_code": "BAD_LOGIN", "evidence_refs": ["failure_reason"]}
    context = _context(MAPPING, failure_reason="The password was rejected")
    diagnostic = await ffd.final_failure_diagnostic(_evidence(_visit()), context)

    assert matcher.prompts == [] and ffd.diagnostic_entry(diagnostic, context) is None


@pytest.mark.parametrize(
    "response",
    [
        pytest.param({"error_code": "ACCESS_DENIED", "evidence_refs": ["vendors_present"]}, id="presence-only"),
        pytest.param({"error_code": "ACCESS_DENIED", "evidence_refs": ["made_up"]}, id="ungrounded-ref"),
        pytest.param({"error_code": "NOT_CONFIGURED", "evidence_refs": ["failure_reason"]}, id="unknown-code"),
        pytest.param({"error_code": None, "evidence_refs": []}, id="model-abstains"),
        pytest.param("not json", id="malformed"),
    ],
)
@pytest.mark.asyncio
async def test_a_configured_code_needs_a_grounded_non_presence_fact(matcher: FakeMatcher, response: Any) -> None:
    matcher.response = response
    context = _context(MAPPING, failure_reason=UNUSUAL)
    diagnostic = await ffd.final_failure_diagnostic(_evidence(_visit((AKAMAI_ASSET,), vendors=("akamai",))), context)

    assert diagnostic.error_code is None and ffd.diagnostic_entry(diagnostic, context) is None


@pytest.mark.asyncio
async def test_a_slow_matcher_times_out_and_leaves_the_deterministic_block(
    matcher: FakeMatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ffd, "MATCH_TIMEOUT_S", 0.01)
    matcher.delay = 1
    context = _context(MAPPING)
    diagnostic = await ffd.final_failure_diagnostic(_evidence(_visit((AKAMAI_BLOCK,))), context)

    assert diagnostic.enforcement.value == "explicit_block" and diagnostic.error_code is None


@pytest.mark.asyncio
async def test_cancellation_during_the_match_propagates(matcher: FakeMatcher) -> None:
    matcher.delay = 5
    run = asyncio.ensure_future(ffd.final_failure_diagnostic(_evidence(_visit((AKAMAI_BLOCK,))), _context(MAPPING)))
    await asyncio.sleep(0.01)
    run.cancel()

    with pytest.raises(asyncio.CancelledError):
        await run


# Persisted rows: guarded writes against a real database.


@pytest_asyncio.fixture
async def database() -> AsyncIterator[AgentDB]:
    if not settings.DATABASE_STRING.startswith("postgresql"):
        pytest.skip("PostgreSQL with the migrated schema is required")
    db = AgentDB(settings.DATABASE_STRING)
    yield db
    await db.engine.dispose()


@pytest_asyncio.fixture
async def org(database: AgentDB) -> str:
    organization_id = f"o_ffd_{uuid4().hex}"
    await database.organizations.create_organization("ffd", organization_id=organization_id)
    return organization_id


def _entry(enforcement: str = "explicit_block") -> dict[str, Any]:
    return {
        "category": "ANTI_BOT_DETECTION",
        "confidence_float": 0.8,
        "reason_code": HAR_ANTI_BOT_DIAGNOSTIC_REASON_CODE,
        "reasoning": "akamai enforcement was recorded during the failed attempt; it is not known to have caused it",
        "anti_bot": {"enforcement": enforcement, "contribution": "unknown"},
    }


async def _task(
    database: AgentDB, org: str, status: str, workflow_run_id: str | None = None, created_at: datetime | None = None
) -> tuple[str, datetime]:
    task_id, finished = f"tsk_{uuid4().hex}", datetime(2026, 8, 1, 0, 10)
    async with database.Session() as session:
        session.add(
            TaskModel(
                task_id=task_id,
                organization_id=org,
                status=status,
                workflow_run_id=workflow_run_id,
                attempt_number=1 if workflow_run_id else None,
                created_at=created_at or datetime(2026, 8, 1),
                url="https://shop.example.com",
                started_at=datetime(2026, 8, 1),
                finished_at=finished,
                failure_category=[PRIMARY] if status != "completed" else None,
            )
        )
        await session.commit()
    return task_id, finished


@pytest.mark.asyncio
async def test_the_task_row_gets_one_diagnostic_however_often_cleanup_runs(database: AgentDB, org: str) -> None:
    task_id, finished = await _task(database, org, "failed")

    first = await database.tasks.set_final_failure_diagnostic(task_id, org, finished, _entry("unresolved_challenge"))
    second = await database.tasks.set_final_failure_diagnostic(task_id, org, finished, _entry("explicit_block"))
    stored = await database.tasks.get_task(task_id, organization_id=org)

    assert first is not None and second is not None and stored is not None
    assert stored.failure_category == [PRIMARY, _entry("explicit_block")]
    assert stored.status.value == "failed" and stored.errors == []


@pytest.mark.asyncio
async def test_a_completed_reset_or_foreign_task_is_left_alone(database: AgentDB, org: str) -> None:
    completed, finished = await _task(database, org, "completed")
    rerun, _ = await _task(database, org, "failed")

    assert await database.tasks.set_final_failure_diagnostic(completed, org, finished, _entry()) is None
    # A cleanup holding an older execution's finish time must not write onto the newer one.
    assert (
        await database.tasks.set_final_failure_diagnostic(rerun, org, finished - timedelta(hours=1), _entry()) is None
    )
    assert await database.tasks.set_final_failure_diagnostic(rerun, f"{org}_other", finished, _entry()) is None
    assert (await database.tasks.get_task(completed, organization_id=org)).failure_category is None  # type: ignore[union-attr]
    assert (await database.tasks.get_task(rerun, organization_id=org)).failure_category == [PRIMARY]  # type: ignore[union-attr]


async def _workflow_run(database: AgentDB, org: str, attempts: int) -> tuple[str, datetime]:
    run_id, finished = f"wr_{uuid4().hex}", datetime(2026, 8, 1, 0, 10)
    async with database.Session() as session:
        session.add(
            WorkflowModel(
                workflow_id=f"w_{run_id}",
                workflow_permanent_id=f"wpid_{run_id}",
                organization_id=org,
                title="ffd",
                version=1,
                workflow_definition={"parameters": [], "blocks": []},
            )
        )
        session.add(
            WorkflowRunModel(
                workflow_run_id=run_id,
                workflow_id=f"w_{run_id}",
                workflow_permanent_id=f"wpid_{run_id}",
                organization_id=org,
                status="failed",
                started_at=datetime(2026, 8, 1),
                finished_at=finished,
                failure_category=[PRIMARY],
            )
        )
        await session.flush()
        session.add_all(
            WorkflowRunAttemptModel(workflow_run_id=run_id, organization_id=org, attempt_number=n, status="failed")
            for n in range(1, attempts + 1)
        )
        await session.commit()
    return run_id, finished


async def _block(
    database: AgentDB,
    org: str,
    run_id: str,
    block_type: str,
    minute: int,
    task_id: str | None = None,
    continue_on_failure: bool = False,
) -> None:
    async with database.Session() as session:
        session.add(
            WorkflowRunBlockModel(
                workflow_run_id=run_id,
                organization_id=org,
                attempt_number=1,
                task_id=task_id,
                block_type=block_type,
                status="failed",
                continue_on_failure=continue_on_failure,
                created_at=datetime(2026, 8, 1, 0, minute),
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_a_stale_attempt_cannot_annotate_the_run_but_the_current_one_can(database: AgentDB, org: str) -> None:
    run_id, finished = await _workflow_run(database, org, attempts=2)

    assert await database.workflow_runs.set_final_failure_diagnostic(run_id, org, finished, 1, _entry()) is None
    updated = await database.workflow_runs.set_final_failure_diagnostic(run_id, org, finished, 2, _entry())

    assert updated is not None and updated.failure_category == [PRIMARY, _entry()]
    assert updated.status.value == "failed"


@pytest.mark.asyncio
async def test_the_task_v2_row_mirrors_only_its_own_failed_run(database: AgentDB, org: str) -> None:
    run_id, finished = await _workflow_run(database, org, attempts=1)
    task_v2_id = await _linked_task_v2(database, org, run_id)

    assert await database.observer.set_final_failure_diagnostic(task_v2_id, org, "wr_other", _entry(), finished) is None
    mirrored = await database.observer.set_final_failure_diagnostic(task_v2_id, org, run_id, _entry(), finished)

    assert mirrored is not None and mirrored.failure_category == [PRIMARY, _entry()]
    # Once a reset has another execution finish the run, the old execution's copy is refused.
    await _reset_and_fail_again(database, org, run_id, attempts=1)
    assert await database.observer.set_final_failure_diagnostic(task_v2_id, org, run_id, _entry(), finished) is None


@pytest.mark.asyncio
async def test_a_failed_workflow_run_is_annotated_from_its_failing_block_only(
    database: AgentDB, org: str, monkeypatch: pytest.MonkeyPatch, matcher: FakeMatcher
) -> None:
    monkeypatch.setattr(ffd.app, "DATABASE", database, raising=False)
    evidence = _evidence(_visit((AKAMAI_BLOCK,), vendors=("akamai",)))
    failing_block, _ = await _workflow_run(database, org, attempts=1)
    failed_task, _ = await _task(database, org, "failed", workflow_run_id=failing_block)
    await _block(database, org, failing_block, "task", 0, task_id=failed_task)
    # An earlier block failed under continue_on_failure and a later block completed: no task failed last.
    continued, _ = await _workflow_run(database, org, attempts=1)
    await _task(database, org, "failed", workflow_run_id=continued, created_at=datetime(2026, 8, 1, 0, 1))
    await _task(database, org, "completed", workflow_run_id=continued, created_at=datetime(2026, 8, 1, 0, 2))
    # The continued task is still the attempt's last task, but a later non-task block is what failed the run.
    non_task, _ = await _workflow_run(database, org, attempts=1)
    continued_task, _ = await _task(
        database, org, "failed", workflow_run_id=non_task, created_at=datetime(2026, 8, 1, 0, 1)
    )
    async with database.Session() as session:
        await session.execute(
            TaskModel.__table__.update()
            .where(TaskModel.task_id == continued_task)
            .values(error_code_mapping={"ACCESS_DENIED": "The portal refused access"})
        )
        await session.commit()
    await _block(database, org, non_task, "task", 1, task_id=continued_task, continue_on_failure=True)
    await _block(database, org, non_task, "http_request", 3)
    completed, _ = await _workflow_run(database, org, attempts=1)
    async with database.Session() as session:
        await session.execute(
            WorkflowRunModel.__table__.update()
            .where(WorkflowRunModel.workflow_run_id == completed)
            .values(status="completed", failure_category=None)
        )
        await session.commit()

    for run_id in (failing_block, continued, non_task, completed):
        run = await database.workflow_runs.get_workflow_run(run_id, organization_id=org)
        assert run is not None
        await ffd.annotate_failed_workflow_run(run, evidence, attempt_number=1, attempt_started_at=None)

    rows = {
        r: await database.workflow_runs.get_workflow_run(r, organization_id=org)
        for r in (failing_block, continued, non_task, completed)
    }
    annotated = rows[failing_block].failure_category  # type: ignore[union-attr]
    assert [c["category"] for c in annotated] == ["NAVIGATION_FAILURE", "ANTI_BOT_DETECTION"]
    assert annotated[1]["anti_bot"]["failed_task_id"] == failed_task
    assert rows[continued].failure_category[1]["anti_bot"]["failed_task_id"] is None  # type: ignore[union-attr,index]
    non_task_diagnostic = rows[non_task].failure_category[1]  # type: ignore[union-attr,index]
    assert non_task_diagnostic["anti_bot"]["failed_task_id"] is None and "error_code" not in non_task_diagnostic
    assert matcher.prompts == []
    assert rows[completed].failure_category is None and rows[failing_block].status.value == "failed"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_a_root_mapping_reaches_the_matcher_when_the_generated_task_has_none(
    database: AgentDB, org: str, monkeypatch: pytest.MonkeyPatch, matcher: FakeMatcher
) -> None:
    monkeypatch.setattr(ffd.app, "DATABASE", database, raising=False)
    evidence = _evidence(_visit((AKAMAI_ASSET,), vendors=("akamai",)))
    annotated = {}
    for name, response in (
        ("matching", {"error_code": "ACCESS_DENIED", "evidence_refs": ["failure_reason", "vendors_present"]}),
        ("unrelated", {"error_code": None, "evidence_refs": []}),
    ):
        matcher.response = response
        run_id, _ = await _workflow_run(database, org, attempts=1)
        task_id, _ = await _task(database, org, "failed", workflow_run_id=run_id)
        await _block(database, org, run_id, "task", 0, task_id=task_id)
        async with database.Session() as session:
            await session.execute(
                WorkflowRunModel.__table__.update()
                .where(WorkflowRunModel.workflow_run_id == run_id)
                .values(failure_reason=UNUSUAL)
            )
            await session.commit()
        run = await database.workflow_runs.get_workflow_run(run_id, organization_id=org)
        assert run is not None
        await ffd.annotate_failed_workflow_run(
            run, evidence, attempt_number=1, attempt_started_at=None, error_code_mapping=MAPPING
        )
        annotated[name] = (await database.workflow_runs.get_workflow_run(run_id, organization_id=org)).failure_category  # type: ignore[union-attr]

    assert len(matcher.prompts) == 2 and all("BAD_LOGIN" in prompt for prompt in matcher.prompts)
    matching = annotated["matching"][1]
    assert matching["error_code"] == "ACCESS_DENIED" and matching["anti_bot"]["contribution"] == "unknown"
    # Vendor presence alone is not evidence, so an unmatched description leaves only the primary category.
    assert annotated["matching"][0] == PRIMARY and annotated["unrelated"] == [PRIMARY]


@pytest.mark.asyncio
@pytest.mark.parametrize("need_call_webhook", [True, False])
async def test_cancelling_the_standalone_diagnostic_still_runs_the_cleanup_tail_then_propagates(
    need_call_webhook: bool,
) -> None:
    agent = ForgeAgent()
    order: list[str] = []
    parked, release = asyncio.Event(), asyncio.Event()
    task = MagicMock(task_id="tsk_1", organization_id="o_1", workflow_run_id=None, browser_session_id=None)
    context = SkyvernContext(task_id="tsk_1")
    context.totp_codes["tsk_1"] = "123456"
    state_at_exit: list[bool] = []

    async def park(*_: Any) -> None:
        parked.set()
        await release.wait()

    def record(step: str) -> Any:
        return AsyncMock(side_effect=lambda *_, **__: order.append(step))

    async def owned() -> None:
        try:
            await agent.clean_up_task(
                task, last_step=MagicMock(), need_final_screenshot=False, need_call_webhook=need_call_webhook
            )
        finally:
            state_at_exit.append("tsk_1" in context.totp_codes)

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=context),
        patch("skyvern.forge.agent.drain_speculative_persist_tasks", AsyncMock()),
        patch("skyvern.forge.agent.annotate_failed_task", AsyncMock(side_effect=park)),
        patch("skyvern.forge.agent.uploaded_file_service.delete_files_attached_to_run", record("attachments")),
        patch.object(agent, "cleanup_browser_and_create_artifacts", AsyncMock(return_value=_evidence())),
        patch.object(agent, "execute_task_webhook", record("webhook")),
        patch.object(agent.async_operation_pool, "remove_task", AsyncMock()),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.BROWSER_MANAGER.get_for_task = MagicMock(return_value=None)
        mock_app.STORAGE.save_downloaded_files = AsyncMock()
        mock_app.ARTIFACT_MANAGER.wait_for_upload_aiotasks = record("artifacts")
        cleanup = await cancel_once_parked(owned(), parked, release)

    assert cleanup.cancelled()
    assert order == ["artifacts", "attachments", *(["webhook"] if need_call_webhook else [])]
    assert state_at_exit == [False]


SIGN_IN = {
    "category": "WRONG_PAGE_STATE",
    "confidence_float": 0.9,
    "reason_code": "sign_in_form_visible",
    "reasoning": "A sign-in form was showing on the page when the block failed",
}


async def _diagnosable_row(database: AgentDB, org: str, writer: str, categories: list | None) -> Any:
    """A failed row of ``writer``'s table holding ``categories``, and the call that writes its diagnostic."""
    run_id, finished = await _workflow_run(database, org, attempts=1)
    if writer == "tasks":
        task_id, finished = await _task(database, org, "failed")
        table, row = TaskModel.__table__, TaskModel.task_id == task_id

        async def write() -> Any:
            return await database.tasks.set_final_failure_diagnostic(task_id, org, finished, _entry())

        async def read() -> Any:
            return (await database.tasks.get_task(task_id, organization_id=org)).failure_category  # type: ignore[union-attr]

    elif writer == "workflow_runs":
        table, row = WorkflowRunModel.__table__, WorkflowRunModel.workflow_run_id == run_id

        async def write() -> Any:
            return await database.workflow_runs.set_final_failure_diagnostic(run_id, org, finished, 1, _entry())

        async def read() -> Any:
            return (await database.workflow_runs.get_workflow_run(run_id, organization_id=org)).failure_category  # type: ignore[union-attr]

    else:
        task_v2_id = f"oc_{uuid4().hex}"
        async with database.Session() as session:
            session.add(
                TaskV2Model(observer_cruise_id=task_v2_id, organization_id=org, workflow_run_id=run_id, status="failed")
            )
            await session.commit()
        table, row = TaskV2Model.__table__, TaskV2Model.observer_cruise_id == task_v2_id

        async def write() -> Any:
            return await database.observer.set_final_failure_diagnostic(task_v2_id, org, run_id, _entry(), finished)

        async def read() -> Any:
            return (await database.observer.get_task_v2(task_v2_id, organization_id=org)).failure_category  # type: ignore[union-attr]

    async with database.Session() as session:
        await session.execute(table.update().where(row).values(failure_category=categories))
        await session.commit()
    return write, read


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["tasks", "workflow_runs", "observer"])
@pytest.mark.parametrize(
    "categories", [None, [], [SIGN_IN], [_entry()]], ids=["null", "empty", "label-only", "diagnostic-only"]
)
async def test_a_row_without_a_classifier_primary_is_left_exactly_as_it_was(
    database: AgentDB, org: str, writer: str, categories: list | None
) -> None:
    # The diagnostic would otherwise become failure_category[0], which runs filters and API readers take as primary.
    write, read = await _diagnosable_row(database, org, writer, categories)

    assert await write() is None
    assert await read() == categories


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["tasks", "workflow_runs", "observer"])
@pytest.mark.parametrize("categories", [[PRIMARY], [PRIMARY, SIGN_IN]], ids=["primary", "primary-then-label"])
async def test_a_row_with_a_classifier_primary_gets_the_diagnostic_after_it(
    database: AgentDB, org: str, writer: str, categories: list
) -> None:
    write, read = await _diagnosable_row(database, org, writer, categories)

    assert await write() is not None
    assert await read() == [*categories, _entry()]


EARLIER = 2400  # seconds before the failure: before this attempt's 30-minute window, inside the same visit


def _shared_visit(*events: HarEvidenceEvent, start: float | None = EARLIER, truncated: bool = False) -> HarEvidence:
    """One visit spanning an earlier attempt and this one, as a same-site retry produces."""
    visit = _visit(events, start=EARLIER, vendors=("akamai",))
    if start is None:
        visit = visit.model_copy(update={"started_at": None})
    return HarEvidence(availability=HarEvidenceAvailability.available, visits=(visit,), truncated=truncated)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param(_shared_visit(_event("challenge_issued", EARLIER, vendor="akamai")), id="earlier-event-only"),
        pytest.param(_shared_visit(), id="aggregate-only"),
        pytest.param(_shared_visit(AKAMAI_ASSET.model_copy(update={"event_at": None})), id="untimed-event"),
        pytest.param(_shared_visit(start=None), id="unknown-start"),
        pytest.param(_shared_visit(truncated=True), id="truncated"),
    ],
)
async def test_a_vendor_seen_only_outside_this_attempt_never_reaches_the_matcher(
    matcher: FakeMatcher, evidence: HarEvidence
) -> None:
    # The recording abstains for want of current evidence; that is not a finding that protection was absent.
    matcher.response = {"error_code": "ACCESS_DENIED", "evidence_refs": ["failure_reason"]}
    context = _context(MAPPING, failure_reason=UNUSUAL)

    diagnostic = await ffd.final_failure_diagnostic(evidence, context)

    assert matcher.prompts == [] and diagnostic.vendors_observed == ()
    assert diagnostic.enforcement.value == "none_observed" and ffd.diagnostic_entry(diagnostic, context) is None


@pytest.mark.asyncio
async def test_only_the_vendor_this_attempt_saw_is_reported_and_offered_to_the_matcher(matcher: FakeMatcher) -> None:
    matcher.response = {"error_code": None, "evidence_refs": []}
    earlier = _event("challenge_issued", EARLIER, vendor="akamai")
    current = _event("explicit_block", 10, vendor="cloudflare", http_status=403, url_class="document")
    visit = _visit((earlier, current), start=EARLIER, vendors=("akamai", "cloudflare"))
    context = _context(MAPPING, failure_reason=UNUSUAL)

    diagnostic = await ffd.final_failure_diagnostic(_evidence(visit), context)

    assert diagnostic.vendors_observed == ("cloudflare",)
    assert diagnostic.enforcement.value == "explicit_block" and diagnostic.enforcing_vendor == "cloudflare"
    # The block keeps its index in the recorded events, so an earlier excluded event does not shift its reference.
    assert diagnostic.evidence_refs == ("har:t1/1/1",)
    [prompt] = matcher.prompts
    assert '"vendors_present": {"vendors": ["cloudflare"], "presence_only": true}' in prompt and "akamai" not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("timed", [True, False], ids=["current-event", "aggregate-only"])
async def test_current_vendor_presence_reaches_the_matcher_but_aggregate_presence_does_not(
    matcher: FakeMatcher, timed: bool
) -> None:
    matcher.response = {"error_code": None, "evidence_refs": []}
    # Endpoints inside the attempt do not prove an untimed entry was too, so only a timed event counts.
    visit = _visit((AKAMAI_ASSET,) if timed else (), vendors=("akamai",))
    context = _context(MAPPING, failure_reason=UNUSUAL)

    diagnostic = await ffd.final_failure_diagnostic(_evidence(visit), context)

    assert diagnostic.vendors_observed == (("akamai",) if timed else ())
    assert len(matcher.prompts) == (1 if timed else 0) and diagnostic.enforcement.value == "none_observed"


@pytest.mark.asyncio
async def test_a_widget_vendor_outside_the_recorded_vendors_opens_nothing(matcher: FakeMatcher) -> None:
    widget = _event("recaptcha_observed", 10, vendor="recaptcha")
    context = _context(MAPPING, failure_reason=UNUSUAL)

    diagnostic = await ffd.final_failure_diagnostic(_evidence(_visit((widget,))), context)

    assert diagnostic.vendors_observed == () and matcher.prompts == []


@pytest.mark.parametrize(
    ("event_type", "vendor"), [("recaptcha_observed", "recaptcha"), ("hcaptcha_observed", "hcaptcha")]
)
@pytest.mark.parametrize(
    ("refs", "code"),
    [
        pytest.param(["har:t1/1/1"], None, id="passive-only"),
        pytest.param(["har:t1/1/0", "har:t1/1/1"], "ACCESS_DENIED", id="with-block"),
    ],
)
@pytest.mark.asyncio
async def test_a_passive_widget_event_cannot_carry_a_configured_code_alone(
    matcher: FakeMatcher, event_type: str, vendor: str, refs: list[str], code: str | None
) -> None:
    block = _event("explicit_block", 20, vendor="cloudflare", http_status=403, url_class="document")
    visit = _visit((block, _event(event_type, 10, vendor=vendor)), vendors=("cloudflare", vendor))
    matcher.response = {"error_code": "ACCESS_DENIED", "evidence_refs": refs}
    context = _context(MAPPING, failure_reason=UNUSUAL)

    diagnostic = await ffd.final_failure_diagnostic(_evidence(visit), context)

    assert diagnostic.enforcement.value == "explicit_block" and diagnostic.error_code == code


NEW_FINISH = datetime(2026, 8, 1, 0, 20)


async def _linked_task_v2(database: AgentDB, org: str, run_id: str) -> str:
    task_v2_id = f"oc_{uuid4().hex}"
    async with database.Session() as session:
        session.add(
            TaskV2Model(
                observer_cruise_id=task_v2_id,
                organization_id=org,
                workflow_run_id=run_id,
                status="failed",
                failure_category=[PRIMARY],
            )
        )
        await session.commit()
    return task_v2_id


async def _reset_and_fail_again(database: AgentDB, org: str, run_id: str, attempts: int) -> None:
    """A reset reusing the row (attempt history deleted, attempt 1 recreated) and a newer execution failing it."""
    async with database.Session() as session:
        await session.execute(delete(WorkflowRunAttemptModel).where(WorkflowRunAttemptModel.workflow_run_id == run_id))
        await session.execute(
            update(WorkflowRunModel)
            .where(WorkflowRunModel.workflow_run_id == run_id)
            .values(finished_at=NEW_FINISH, failure_reason="a later execution failed", failure_category=[PRIMARY])
        )
        if attempts:
            session.add(
                WorkflowRunAttemptModel(workflow_run_id=run_id, organization_id=org, attempt_number=1, status="failed")
            )
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("attempts", [0, 1, 2])
@pytest.mark.parametrize("reset", [True, False])
async def test_only_the_execution_that_wrote_the_owner_snapshot_can_annotate_the_run(
    database: AgentDB, org: str, monkeypatch: pytest.MonkeyPatch, matcher: FakeMatcher, attempts: int, reset: bool
) -> None:
    monkeypatch.setattr(ffd.app, "DATABASE", database, raising=False)
    run_id, _ = await _workflow_run(database, org, attempts=attempts)
    owner = await database.workflow_runs.get_workflow_run(run_id, organization_id=org)
    assert owner is not None
    evidence = _evidence(_visit((AKAMAI_BLOCK,), vendors=("akamai",)))
    if reset:
        await _reset_and_fail_again(database, org, run_id, attempts)
    annotate = ffd.annotate_failed_workflow_run

    # Without the finish its own write recorded, the owner proves nothing and the optional diagnostic is skipped.
    await annotate(
        owner.model_copy(update={"finished_at": None}),
        evidence,
        attempt_number=max(attempts, 1),
        attempt_started_at=None,
    )
    skipped = await database.workflow_runs.get_workflow_run(run_id, organization_id=org)
    assert skipped is not None and [c["category"] for c in skipped.failure_category or []] == ["NAVIGATION_FAILURE"]
    await annotate(
        owner, evidence, attempt_number=1 if reset else max(attempts, 1), attempt_started_at=owner.started_at
    )

    run = await database.workflow_runs.get_workflow_run(run_id, organization_id=org)
    assert run is not None and run.status.value == "failed"
    assert [c["category"] for c in run.failure_category or []] == (
        ["NAVIGATION_FAILURE"] if reset else ["NAVIGATION_FAILURE", "ANTI_BOT_DETECTION"]
    )


@pytest.mark.asyncio
async def test_a_reset_after_the_owner_check_but_before_the_write_is_refused(
    database: AgentDB, org: str, monkeypatch: pytest.MonkeyPatch, matcher: FakeMatcher
) -> None:
    monkeypatch.setattr(ffd.app, "DATABASE", database, raising=False)
    run_id, _ = await _workflow_run(database, org, attempts=1)
    owner = await database.workflow_runs.get_workflow_run(run_id, organization_id=org)
    assert owner is not None
    real = ffd.final_failure_diagnostic

    async def reset_mid_diagnostic(evidence: HarEvidence, context: Any) -> Any:
        await _reset_and_fail_again(database, org, run_id, attempts=1)
        return await real(evidence, context)

    monkeypatch.setattr(ffd, "final_failure_diagnostic", reset_mid_diagnostic)
    evidence = _evidence(_visit((AKAMAI_BLOCK,), vendors=("akamai",)))
    await ffd.annotate_failed_workflow_run(owner, evidence, attempt_number=1, attempt_started_at=owner.started_at)

    run = await database.workflow_runs.get_workflow_run(run_id, organization_id=org)
    assert run is not None and run.failure_category == [PRIMARY] and run.finished_at == NEW_FINISH


async def _until_a_session_waits_on_a_lock(database: AgentDB) -> None:
    async with asyncio.timeout(10):
        while True:
            async with database.Session() as session:
                waiting = await session.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                    )
                )
            if waiting:
                return
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["reset", "mirror"])
async def test_the_mirror_and_a_reset_serialize_on_the_run_row(
    database: AgentDB, org: str, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    run_id, finished = await _workflow_run(database, org, attempts=1)
    task_v2_id = await _linked_task_v2(database, org, run_id)
    holding, release = asyncio.Event(), asyncio.Event()
    tasks: dict[str, asyncio.Task[Any]] = {}

    async def reset() -> None:
        async with database.Session() as session:
            await session.execute(select(WorkflowRunModel).filter_by(workflow_run_id=run_id).with_for_update())
            if first == "reset":
                holding.set()
                await release.wait()
            await session.execute(
                update(WorkflowRunModel)
                .where(WorkflowRunModel.workflow_run_id == run_id)
                .values(finished_at=NEW_FINISH)
            )
            await session.commit()

    real_commit = AsyncSession.commit

    async def gated_commit(self: AsyncSession) -> None:
        # Holds the mirror's transaction open, both locks taken, until the reset is seen waiting on them.
        if first == "mirror" and asyncio.current_task() is tasks.get("mirror"):
            holding.set()
            await release.wait()
        await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", gated_commit)

    async def mirror() -> Any:
        return await database.observer.set_final_failure_diagnostic(task_v2_id, org, run_id, _entry(), finished)

    try:
        order = ["reset", "mirror"] if first == "reset" else ["mirror", "reset"]
        tasks[order[0]] = asyncio.ensure_future(reset() if order[0] == "reset" else mirror())
        async with asyncio.timeout(10):
            await holding.wait()
        tasks[order[1]] = asyncio.ensure_future(reset() if order[1] == "reset" else mirror())
        await _until_a_session_waits_on_a_lock(database)
        release.set()
        async with asyncio.timeout(10):
            mirrored, _ = await asyncio.gather(tasks["mirror"], tasks["reset"])
    finally:
        release.set()
        for task in tasks.values():
            if not task.done():
                task.cancel()
        async with asyncio.timeout(10):
            await asyncio.gather(*tasks.values(), return_exceptions=True)

    task_v2 = await database.observer.get_task_v2(task_v2_id, organization_id=org)
    run = await database.workflow_runs.get_workflow_run(run_id, organization_id=org)
    assert run is not None and run.finished_at == NEW_FINISH
    if first == "reset":
        assert mirrored is None and task_v2 is not None and task_v2.failure_category == [PRIMARY]
    else:
        assert mirrored is not None and task_v2 is not None and task_v2.failure_category == [PRIMARY, _entry()]
