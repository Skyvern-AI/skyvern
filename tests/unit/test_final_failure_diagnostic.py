import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from skyvern.config import settings
from skyvern.errors.errors import anti_bot_system_errors
from skyvern.forge import final_failure_diagnostic as ffd
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.failure_classifier import (
    HAR_ANTI_BOT_DIAGNOSTIC_REASON_CODE,
    derive_failure_attribution,
    is_output_only_entry,
    with_root_user_errors,
    without_output_only_categories,
)
from skyvern.forge.sdk.copilot.challenge_evidence import is_carrier_backed_category_entry
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
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
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, TaskV2Status
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.service import WorkflowService, _with_diagnostic_system_errors
from skyvern.services import task_v2_service
from skyvern.webeye.utils.challenge_solve_record import ChallengeSolveReceipt
from tests.unit.conftest import cancel_once_parked
from tests.unit.test_output_parameter_size_guards import _build_status_response

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


def _context(failure_reason: str | None = "The page did not load") -> ffd.FailureContext:
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
        run_ids=("tsk_ffd",),
    )


class NoModel:
    """Every model handler the reporting could reach; any call is a contract break."""

    def __init__(self) -> None:
        self.prompt_names: list[str] = []

    async def __call__(self, *, prompt_name: str = "", **_: Any) -> Any:
        self.prompt_names.append(prompt_name)
        raise AssertionError(f"unexpected model call: {prompt_name}")


@pytest.fixture
def no_model(monkeypatch: pytest.MonkeyPatch) -> NoModel:
    fake = NoModel()
    for handler in ("LLM_API_HANDLER", "SECONDARY_LLM_API_HANDLER", "EXTRACTION_LLM_API_HANDLER"):
        monkeypatch.setattr(ffd.app, handler, fake, raising=False)
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
async def test_a_page_block_becomes_an_output_only_entry_naming_its_evidence(no_model: NoModel) -> None:
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
    # The entry is a label every runtime reader skips.
    assert no_model.prompt_names == []
    categories = [PRIMARY, entry]
    assert is_output_only_entry(entry) and without_output_only_categories(categories) == [PRIMARY]
    assert not is_carrier_backed_category_entry(entry)
    assert derive_failure_attribution(categories) == derive_failure_attribution([PRIMARY])


@pytest.mark.parametrize(
    "availability",
    [a for a in HarEvidenceAvailability if a != HarEvidenceAvailability.available],
)
@pytest.mark.asyncio
async def test_unavailable_evidence_reports_nothing(availability: HarEvidenceAvailability, no_model: NoModel) -> None:
    context = _context(failure_reason=None)
    diagnostic = await ffd.final_failure_diagnostic(HarEvidence.unavailable(availability), context)

    assert diagnostic.availability == availability and ffd.diagnostic_entry(diagnostic, context) is None


def _qualified(service: str | None, before: float = 10) -> HarEvidenceEvent:
    return _event(
        "explicit_block",
        before,
        vendor="cloudflare" if service and service.startswith("cloudflare") else service,
        http_status=403,
        url_class="document",
        failure_signal="block",
        service=service,
    )


def _receipt(
    service: str | None,
    outcome: str = "unsolved",
    before: float = 10,
    page_state: str = "unknown",
    run_id: str = "tsk_ffd",
    owner_id: str | None = None,
) -> ChallengeSolveReceipt:
    return ChallengeSolveReceipt(
        run_id=run_id,
        ended_at=_at(before),
        service=service,
        outcome=outcome,
        page_state=page_state,
        owner_id=owner_id or run_id,
    )


@pytest.fixture
def run_context() -> Iterator[SkyvernContext]:
    with skyvern_context.scoped(SkyvernContext(task_id="tsk_ffd")) as context:
        yield context


def _codes(diagnostic: ffd.FinalDiagnostic) -> list[str]:
    assert all(error["error_type"] == "SYSTEM_DEFINED_ERROR" for error in diagnostic.system_errors)
    return [error["error_code"] for error in diagnostic.system_errors]


@pytest.mark.parametrize(
    ("events", "receipts", "codes"),
    [
        pytest.param((_qualified("cloudflare"),), (), ["ANTI_BOT_ERROR", "CLOUDFLARE_ERROR"], id="cloudflare-block"),
        pytest.param((), (_receipt("cloudflare"),), ["ANTI_BOT_ERROR", "CLOUDFLARE_ERROR"], id="cloudflare-solve"),
        pytest.param((_qualified("akamai"),), (), ["ANTI_BOT_ERROR"], id="other-vendor-generic"),
        pytest.param((_qualified(None),), (), ["ANTI_BOT_ERROR"], id="unknown-service"),
        pytest.param((), (_receipt("recaptcha"),), ["ANTI_BOT_ERROR", "RECAPTCHA_ERROR"], id="recaptcha"),
        pytest.param((), (_receipt("hcaptcha"),), ["ANTI_BOT_ERROR", "HCAPTCHA_ERROR"], id="hcaptcha"),
        pytest.param(
            (), (_receipt("cloudflare_turnstile"),), ["ANTI_BOT_ERROR", "CLOUDFLARE_TURNSTILE_ERROR"], id="turnstile"
        ),
        pytest.param((), (_receipt("other"),), ["ANTI_BOT_ERROR"], id="presented-unknown-challenge"),
        pytest.param(
            (_qualified("cloudflare"), _qualified("cloudflare", before=5)),
            (_receipt("hcaptcha"), _receipt("recaptcha", before=4)),
            ["ANTI_BOT_ERROR", "CLOUDFLARE_ERROR", "HCAPTCHA_ERROR", "RECAPTCHA_ERROR"],
            id="mixed-each-once",
        ),
        # A success clears only the same owner's challenge of the same known service.
        pytest.param(
            (),
            (_receipt("hcaptcha"), _receipt("recaptcha", "solved", 5)),
            ["ANTI_BOT_ERROR", "HCAPTCHA_ERROR"],
            id="another-services-solve-keeps-it",
        ),
        pytest.param(
            (),
            (_receipt("hcaptcha"), _receipt(None, "solved", 5)),
            ["ANTI_BOT_ERROR", "HCAPTCHA_ERROR"],
            id="unidentified-solve-keeps-it",
        ),
        pytest.param(
            (),
            (_receipt("hcaptcha", owner_id="tsk_a"), _receipt("hcaptcha", "solved", 5, owner_id="tsk_b")),
            ["ANTI_BOT_ERROR", "HCAPTCHA_ERROR"],
            id="another-owners-solve-keeps-it",
        ),
    ],
)
@pytest.mark.asyncio
async def test_qualified_evidence_reports_the_general_and_supported_service_codes(
    run_context: SkyvernContext,
    no_model: NoModel,
    events: tuple[HarEvidenceEvent, ...],
    receipts: tuple[ChallengeSolveReceipt, ...],
    codes: list[str],
) -> None:
    run_context.challenge_solve_receipts.extend(receipts)

    diagnostic = await ffd.final_failure_diagnostic(_evidence(_visit(events)), _context())

    assert _codes(diagnostic) == codes and no_model.prompt_names == []


@pytest.mark.parametrize(
    ("events", "receipts"),
    [
        pytest.param((_event("recaptcha_observed", 10, vendor="recaptcha"),), (), id="passive-recaptcha-widget"),
        pytest.param((_event("hcaptcha_observed", 10, vendor="hcaptcha"),), (), id="passive-hcaptcha-widget"),
        pytest.param(
            (_event("challenge_issued", 10, vendor="cloudflare", challenge_type="turnstile"),), (), id="turnstile-asset"
        ),
        pytest.param(
            (_event("challenge_resource_failed", 10, vendor="cloudflare", resource_fail_class="canceled"),),
            (),
            id="aborted-challenge-resource",
        ),
        pytest.param((AKAMAI_BLOCK,), (), id="block-the-extractor-did-not-qualify"),
        pytest.param((_qualified("cloudflare", before=3600),), (), id="block-in-an-earlier-attempt"),
        pytest.param(
            (), (_receipt("recaptcha"), _receipt("recaptcha", outcome="solved", before=5)), id="later-solve-clears"
        ),
        pytest.param((), (_receipt(None),), id="unsolved-without-a-presented-challenge"),
        pytest.param((), (_receipt("hcaptcha", page_state="challenge_gone"),), id="challenge-gone"),
        pytest.param((), (_receipt("recaptcha", outcome="unknown"),), id="timed-out-solve"),
        pytest.param((), (_receipt("recaptcha", before=3600),), id="solve-in-an-earlier-attempt"),
        pytest.param((), (_receipt("recaptcha", run_id="tsk_other"),), id="another-runs-solve"),
    ],
)
@pytest.mark.asyncio
async def test_presence_and_out_of_scope_evidence_report_no_system_error(
    run_context: SkyvernContext,
    no_model: NoModel,
    events: tuple[HarEvidenceEvent, ...],
    receipts: tuple[ChallengeSolveReceipt, ...],
) -> None:
    run_context.challenge_solve_receipts.extend(receipts)
    context = _context()

    diagnostic = await ffd.final_failure_diagnostic(_evidence(_visit(events)), context)

    assert diagnostic.system_errors == () and no_model.prompt_names == []
    entry = ffd.diagnostic_entry(diagnostic, context)
    assert entry is None or entry["anti_bot"]["system_errors"] == []


@pytest.mark.asyncio
async def test_unresolved_solves_report_even_when_the_recording_is_unavailable(
    run_context: SkyvernContext, no_model: NoModel
) -> None:
    run_context.challenge_solve_receipts.append(_receipt("recaptcha"))
    context = _context()

    diagnostic = await ffd.final_failure_diagnostic(HarEvidence.unavailable(HarEvidenceAvailability.timeout), context)
    entry = ffd.diagnostic_entry(diagnostic, context)

    assert _codes(diagnostic) == ["ANTI_BOT_ERROR", "RECAPTCHA_ERROR"]
    assert entry is not None and entry["anti_bot"]["enforcement"] == "none_observed"
    assert ffd.diagnostic_system_errors([PRIMARY, entry], 1) == list(diagnostic.system_errors)
    assert ffd.diagnostic_system_errors([PRIMARY, entry], 2) == []


def test_the_projection_only_releases_code_authored_system_rows() -> None:
    entry = {
        "reason_code": HAR_ANTI_BOT_DIAGNOSTIC_REASON_CODE,
        "anti_bot": {
            "attempt_number": 1,
            "system_errors": [
                {"error_code": "ANTI_BOT_ERROR", "reasoning": "r", "error_type": "SYSTEM_DEFINED_ERROR", "x": 1},
                {"error_code": "ACCESS_DENIED", "reasoning": "r", "error_type": "SYSTEM_DEFINED_ERROR"},
                {"error_code": "RECAPTCHA_ERROR", "reasoning": "r", "error_type": "USER_DEFINED_ERROR"},
                "RECAPTCHA_ERROR",
            ],
        },
    }

    assert ffd.diagnostic_system_errors([PRIMARY, entry], 1) == [
        {"error_code": "ANTI_BOT_ERROR", "reasoning": "r", "error_type": "SYSTEM_DEFINED_ERROR"}
    ]


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


REACH_MAX_STEPS = {
    "error_code": "REACH_MAX_STEPS",
    "reasoning": "The agent has reached the maximum number of steps.",
    "error_type": "SYSTEM_DEFINED_ERROR",
}
# A customer's own code that happens to share a system code's name stays a separate user row.
USER_ANTI_BOT = {
    "error_code": "ANTI_BOT_ERROR",
    "reasoning": "The portal showed its bot page",
    "confidence_float": 0.9,
    "error_type": "USER_DEFINED_ERROR",
}
ANTI_BOT_ROWS = [error.model_dump() for error in anti_bot_system_errors(["recaptcha"])]


async def _set_errors(database: AgentDB, task_id: str, errors: list[dict[str, Any]]) -> None:
    async with database.Session() as session:
        await session.execute(TaskModel.__table__.update().where(TaskModel.task_id == task_id).values(errors=errors))
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("categories", [[PRIMARY], None], ids=["primary", "no-primary"])
async def test_system_errors_are_appended_once_after_every_existing_row(
    database: AgentDB, org: str, categories: list | None
) -> None:
    task_id, finished = await _task(database, org, "failed")
    await _set_errors(database, task_id, [REACH_MAX_STEPS, USER_ANTI_BOT])
    async with database.Session() as session:
        await session.execute(
            TaskModel.__table__.update().where(TaskModel.task_id == task_id).values(failure_category=categories)
        )
        await session.commit()

    first, repeat = [
        await database.tasks.set_final_failure_diagnostic(task_id, org, finished, _entry(), system_errors=ANTI_BOT_ROWS)
        for _ in range(2)
    ]
    stored = await database.tasks.get_task(task_id, organization_id=org)

    # A repeat with nothing left to add writes nothing.
    assert first is not None and (repeat is None) == (categories is None)
    assert stored is not None and stored.status.value == "failed"
    assert stored.errors == [REACH_MAX_STEPS, USER_ANTI_BOT, *ANTI_BOT_ROWS]
    assert [e["error_code"] for e in ANTI_BOT_ROWS] == ["ANTI_BOT_ERROR", "RECAPTCHA_ERROR"]
    # Errors do not need a classifier primary; the category entry still does.
    assert stored.failure_category == ([PRIMARY, _entry()] if categories else None)
    assert stored.to_task_response().errors == stored.errors


@pytest.mark.asyncio
async def test_a_stale_or_foreign_cleanup_appends_no_system_error(database: AgentDB, org: str) -> None:
    completed, finished = await _task(database, org, "completed")
    rerun, _ = await _task(database, org, "failed")
    write = database.tasks.set_final_failure_diagnostic

    assert await write(completed, org, finished, _entry(), system_errors=ANTI_BOT_ROWS) is None
    assert await write(rerun, org, finished - timedelta(hours=1), _entry(), system_errors=ANTI_BOT_ROWS) is None
    assert await write(rerun, f"{org}_other", finished, _entry(), system_errors=ANTI_BOT_ROWS) is None
    for task_id in (completed, rerun):
        assert (await database.tasks.get_task(task_id, organization_id=org)).errors == []  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_a_reset_drops_only_the_last_executions_anti_bot_system_errors(database: AgentDB, org: str) -> None:
    task_id, finished = await _task(database, org, "failed")
    await _set_errors(database, task_id, [REACH_MAX_STEPS, USER_ANTI_BOT])
    await database.tasks.set_final_failure_diagnostic(task_id, org, finished, _entry(), system_errors=ANTI_BOT_ROWS)

    reset = await database.tasks.reset_task_for_rerun(task_id, org)
    # The old execution's cleanup arriving after the reset cannot restore them.
    late = await database.tasks.set_final_failure_diagnostic(
        task_id, org, finished, _entry(), system_errors=ANTI_BOT_ROWS
    )

    assert reset.errors == [REACH_MAX_STEPS, USER_ANTI_BOT] and late is None
    stored = await database.tasks.get_task(task_id, organization_id=org)
    assert stored is not None and stored.errors == [REACH_MAX_STEPS, USER_ANTI_BOT]


@pytest.mark.asyncio
async def test_a_failed_standalone_task_reports_its_unresolved_solve_without_a_model(
    database: AgentDB, org: str, monkeypatch: pytest.MonkeyPatch, no_model: NoModel
) -> None:
    monkeypatch.setattr(ffd.app, "DATABASE", database, raising=False)
    task_id, _ = await _task(database, org, "failed")
    await _set_errors(database, task_id, [REACH_MAX_STEPS])
    async with database.Session() as session:
        await session.execute(
            TaskModel.__table__.update()
            .where(TaskModel.task_id == task_id)
            .values(error_code_mapping={"ACCESS_DENIED": "The portal refused access"})
        )
        await session.commit()
    task = await database.tasks.get_task(task_id, organization_id=org)
    assert task is not None
    receipts = [
        # Solved in an earlier execution of this task: before its start, so not this attempt's.
        ChallengeSolveReceipt(task_id, datetime(2026, 7, 31, 23, 0, tzinfo=UTC), "hcaptcha", "unsolved", "unknown"),
        ChallengeSolveReceipt(task_id, datetime(2026, 8, 1, 0, 5, tzinfo=UTC), "recaptcha", "unsolved", "unknown"),
    ]

    with skyvern_context.scoped(SkyvernContext(task_id=task_id)) as context:
        context.challenge_solve_receipts.extend(receipts)
        for _ in range(2):
            task = await ffd.annotate_failed_task(task, HarEvidence.unavailable(HarEvidenceAvailability.timeout))

    assert task.errors == [REACH_MAX_STEPS, *ANTI_BOT_ROWS] and no_model.prompt_names == []
    assert task.failure_category is not None and task.failure_category[0] == PRIMARY
    assert task.status.value == "failed"


@pytest.mark.asyncio
async def test_a_reset_after_real_annotation_serializes_no_trace_of_the_last_executions_anti_bot_report(
    database: AgentDB, org: str, monkeypatch: pytest.MonkeyPatch, no_model: NoModel
) -> None:
    monkeypatch.setattr(ffd.app, "DATABASE", database, raising=False)
    task_id, finished = await _task(database, org, "failed")
    await _set_errors(database, task_id, [REACH_MAX_STEPS, USER_ANTI_BOT])
    task = await database.tasks.get_task(task_id, organization_id=org)
    assert task is not None
    block = _event("explicit_block", 30, vendor="akamai", http_status=403, url_class="document")
    evidence = _evidence(_visit((block.model_copy(update={"failure_signal": "block", "service": "akamai"}),)))
    annotated = await ffd.annotate_failed_task(task, evidence)
    assert [c.get("reason_code") for c in annotated.failure_category or []] == [
        None,
        HAR_ANTI_BOT_DIAGNOSTIC_REASON_CODE,
    ]

    reset = await database.tasks.reset_task_for_rerun(task_id, org)
    late = await database.tasks.set_final_failure_diagnostic(
        task_id, org, finished, _entry(), system_errors=ANTI_BOT_ROWS
    )

    response = reset.to_task_response()
    assert response.errors == [REACH_MAX_STEPS, USER_ANTI_BOT] and late is None
    assert response.failure_category == [PRIMARY]


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
    database: AgentDB, org: str, monkeypatch: pytest.MonkeyPatch, no_model: NoModel
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
    assert no_model.prompt_names == []
    assert rows[completed].failure_category is None and rows[failing_block].status.value == "failed"  # type: ignore[union-attr]


@pytest.mark.asyncio
@pytest.mark.parametrize("categories", [[PRIMARY], None], ids=["primary", "no-primary"])
async def test_a_workflow_runs_system_errors_reach_its_response_errors_from_the_diagnostic_entry(
    database: AgentDB, org: str, monkeypatch: pytest.MonkeyPatch, no_model: NoModel, categories: list | None
) -> None:
    monkeypatch.setattr(ffd.app, "DATABASE", database, raising=False)
    run_id, _ = await _workflow_run(database, org, attempts=1)
    async with database.Session() as session:
        await session.execute(
            WorkflowRunModel.__table__.update()
            .where(WorkflowRunModel.workflow_run_id == run_id)
            .values(failure_category=categories)
        )
        await session.commit()
    run = await database.workflow_runs.get_workflow_run(run_id, organization_id=org)
    assert run is not None
    block = _event("repeat_block_after_challenge", 10, vendor="cloudflare", http_status=403, url_class="document")
    evidence = _evidence(_visit((block.model_copy(update={"failure_signal": "block", "service": "cloudflare"}),)))

    with skyvern_context.scoped(SkyvernContext(workflow_run_id=run_id)) as context:
        context.challenge_solve_receipts.append(
            ChallengeSolveReceipt(
                run_id, datetime(2026, 8, 1, 0, 5, tzinfo=UTC), "hcaptcha", "unsolved", "unknown", owner_id="tsk_w"
            )
        )
        await ffd.annotate_failed_workflow_run(run, evidence, attempt_number=1, attempt_started_at=None)
    stored = await database.workflow_runs.get_workflow_run(run_id, organization_id=org)
    assert stored is not None and no_model.prompt_names == []
    existing = [REACH_MAX_STEPS, USER_ANTI_BOT]

    projected = _with_diagnostic_system_errors(existing, stored.failure_category, 1)

    if categories is None:
        # A run-level failure with no classifier primary keeps abstaining: no carrier, so no system rows.
        assert stored.failure_category is None and projected == existing
        return
    assert [e["error_code"] for e in projected] == [
        "REACH_MAX_STEPS",
        "ANTI_BOT_ERROR",
        "ANTI_BOT_ERROR",
        "CLOUDFLARE_ERROR",
        "HCAPTCHA_ERROR",
    ]
    assert [e["error_type"] for e in projected[2:]] == ["SYSTEM_DEFINED_ERROR"] * 3
    assert _with_diagnostic_system_errors(projected, stored.failure_category, 1) == projected
    # Another attempt's response, and a response already at its row bound, gain nothing.
    assert _with_diagnostic_system_errors(existing, stored.failure_category, 2) == existing
    full = [{**USER_ANTI_BOT, "error_code": f"CODE_{n}"} for n in range(100)]
    assert _with_diagnostic_system_errors(full, stored.failure_category, 1) == full


@pytest.mark.asyncio
async def test_the_workflow_status_response_and_webhook_payload_carry_the_projected_system_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [error.model_dump() for error in anti_bot_system_errors(["cloudflare"])]
    entry = {**_entry(), "anti_bot": {"attempt_number": 1, "system_errors": rows}}

    response = await _build_status_response(
        monkeypatch, cap=1024, cap_output_values=False, failure_reason="blocked", failure_category=[PRIMARY, entry]
    )

    assert response.errors == rows


ROOT_MAPPING = {"BOT_WALL": "The site's bot wall blocked the run"}


@pytest.mark.asyncio
async def test_the_task_v2_roots_own_matched_codes_reach_the_response_as_user_rows_beside_system_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_rows = [
        {"error_code": "BOT_WALL", "reasoning": "blocked", "confidence_float": 0.8, "error_type": "USER_DEFINED_ERROR"},
        {"error_code": "NOT_MAPPED", "reasoning": "x", "confidence_float": 0.8, "error_type": "USER_DEFINED_ERROR"},
        {
            "error_code": "ANTI_BOT_ERROR",
            "reasoning": "x",
            "confidence_float": 1.0,
            "error_type": "SYSTEM_DEFINED_ERROR",
        },
    ]
    categories = with_root_user_errors([PRIMARY], user_rows)
    system_rows = [error.model_dump() for error in anti_bot_system_errors(["hcaptcha"])]
    diagnostic = {**_entry(), "anti_bot": {"attempt_number": 1, "system_errors": system_rows}}
    now = datetime.now(UTC)
    task_v2 = TaskV2(
        task_id="oc_ffd",
        status=TaskV2Status.failed,
        organization_id="o_1",
        workflow_run_id="wr_1",
        error_code_mapping=ROOT_MAPPING,
        created_at=now,
        modified_at=now,
    )

    response = await _build_status_response(
        monkeypatch,
        cap=1024,
        cap_output_values=False,
        failure_reason="blocked",
        failure_category=[*categories, diagnostic],
        task_v2=task_v2,
    )

    # The root entry never leads and carries no new category name, so primary-reading and category-set readers
    # (attribution, credential fallback) see the same run.
    assert categories[0] == PRIMARY and {c["category"] for c in categories} == {PRIMARY["category"]}
    assert is_output_only_entry(categories[1]) and derive_failure_attribution(categories) == derive_failure_attribution(
        [PRIMARY]
    )
    assert response.errors == [user_rows[0], *system_rows]


SECRET_VALUE = "pw-SYNTHETIC-4e1d9c"


@pytest.mark.asyncio
async def test_a_task_v2_roots_matched_code_is_stored_and_served_with_its_run_secret_masked(
    database: AgentDB, org: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ffd.app, "DATABASE", database, raising=False)
    service = WorkflowService()
    for name, value in (
        ("get_recent_workflow_screenshot_urls", []),
        ("get_output_parameter_workflow_run_output_parameter_tuples", []),
        ("_fetch_recording_urls", ([], False)),
        ("_fetch_downloaded_files", ([], None)),
    ):
        # Storage transports only; the repositories and terminal writers are real.
        monkeypatch.setattr(service, name, AsyncMock(return_value=value))
    monkeypatch.setattr(ffd.app, "WORKFLOW_SERVICE", service, raising=False)
    run_id, _ = await _workflow_run(database, org, attempts=1)
    task_v2_id = await _linked_task_v2(database, org, run_id)
    async with database.Session() as session:
        await session.execute(
            WorkflowRunModel.__table__.update()
            .where(WorkflowRunModel.workflow_run_id == run_id)
            .values(status="running", finished_at=None, failure_category=None)
        )
        await session.execute(
            TaskV2Model.__table__.update()
            .where(TaskV2Model.observer_cruise_id == task_v2_id)
            .values(status="running", failure_category=None, error_code_mapping=ROOT_MAPPING, prompt="goal")
        )
        await session.commit()
    run_context = WorkflowRunContext("ffd", f"w_{run_id}", f"wpid_{run_id}", run_id, aws_client=MagicMock())
    run_context.secrets["portal_password"] = SECRET_VALUE
    monkeypatch.setitem(ffd.app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts, run_id, run_context)
    calls: list[dict[str, Any]] = []

    async def handler(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        error = {"error_code": "BOT_WALL", "reasoning": f"blocked after {SECRET_VALUE}", "confidence_float": 0.9}
        return {"reasoning": "blocked", "failure_categories": [PRIMARY], "errors": [error]}

    monkeypatch.setattr(task_v2_service, "_get_task_v2_llm_api_handler", lambda task_v2: handler)
    monkeypatch.setattr(task_v2_service.SkyvernFrame, "take_split_screenshots", AsyncMock(return_value=[]))
    task_v2 = await database.observer.get_task_v2(task_v2_id, organization_id=org)
    assert task_v2 is not None
    browser = MagicMock(get_working_page=AsyncMock(return_value=MagicMock()), engine_selection=None)

    with skyvern_context.scoped(SkyvernContext(organization_id=org, workflow_run_id=run_id)):
        _, categories, user_errors = await task_v2_service._summarize_max_steps_failure_reason(task_v2, org, browser)
    # Built exactly as the max-steps call site builds it, then written by the real terminal writers.
    await task_v2_service.mark_task_v2_as_failed(
        task_v2_id,
        workflow_run_id=run_id,
        organization_id=org,
        failure_reason="Reached the max number of steps.",
        failure_category=with_root_user_errors(categories, [error.model_dump(mode="json") for error in user_errors]),
    )
    del ffd.app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[run_id]
    stored = await database.observer.get_task_v2(task_v2_id, organization_id=org)
    run = await database.workflow_runs.get_workflow_run(run_id, organization_id=org)
    assert stored is not None and run is not None
    api = await task_v2_service.build_task_v2_run_response(stored)
    await database.organizations.create_org_auth_token(org, OrganizationAuthTokenType.api, "tok_ffd")
    deliver = AsyncMock(return_value=MagicMock(status_code=200, text=""))
    monkeypatch.setattr(task_v2_service, "deliver_webhook_with_retries", deliver)
    await task_v2_service.send_task_v2_webhook(stored.model_copy(update={"webhook_callback_url": "https://h.test/x"}))

    assert len(calls) == 1 and run.failure_category == stored.failure_category
    assert stored.failure_category[0] == PRIMARY and stored.status.value == "failed"
    assert [(e["error_code"], e["error_type"]) for e in api.errors or []] == [("BOT_WALL", "USER_DEFINED_ERROR")]
    payload = deliver.await_args.kwargs["payload"]
    for served in (
        json.dumps(stored.failure_category),
        json.dumps(run.failure_category),
        api.model_dump_json(),
        payload,
    ):
        assert SECRET_VALUE not in served
    assert "BOT_WALL" in payload


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
async def test_a_vendor_seen_only_outside_this_attempt_reports_nothing(
    no_model: NoModel, evidence: HarEvidence
) -> None:
    # The recording abstains for want of current evidence; that is not a finding that protection was absent.
    context = _context()

    diagnostic = await ffd.final_failure_diagnostic(evidence, context)

    assert diagnostic.vendors_observed == () and diagnostic.system_errors == ()
    assert diagnostic.enforcement.value == "none_observed" and ffd.diagnostic_entry(diagnostic, context) is None


@pytest.mark.asyncio
async def test_only_the_vendor_this_attempt_saw_is_reported(no_model: NoModel) -> None:
    earlier = _event("challenge_issued", EARLIER, vendor="akamai")
    current = _event("explicit_block", 10, vendor="cloudflare", http_status=403, url_class="document")
    visit = _visit((earlier, current), start=EARLIER, vendors=("akamai", "cloudflare"))
    context = _context()

    diagnostic = await ffd.final_failure_diagnostic(_evidence(visit), context)

    assert diagnostic.vendors_observed == ("cloudflare",)
    assert diagnostic.enforcement.value == "explicit_block" and diagnostic.enforcing_vendor == "cloudflare"
    # The block keeps its index in the recorded events, so an earlier excluded event does not shift its reference.
    assert diagnostic.evidence_refs == ("har:t1/1/1",)


@pytest.mark.asyncio
@pytest.mark.parametrize("timed", [True, False], ids=["current-event", "aggregate-only"])
async def test_current_vendor_presence_is_observed_but_aggregate_presence_is_not(
    no_model: NoModel, timed: bool
) -> None:
    # Endpoints inside the attempt do not prove an untimed entry was too, so only a timed event counts.
    visit = _visit((AKAMAI_ASSET,) if timed else (), vendors=("akamai",))
    context = _context()

    diagnostic = await ffd.final_failure_diagnostic(_evidence(visit), context)

    assert diagnostic.vendors_observed == (("akamai",) if timed else ())
    assert diagnostic.enforcement.value == "none_observed" and diagnostic.system_errors == ()


@pytest.mark.asyncio
async def test_a_widget_vendor_outside_the_recorded_vendors_opens_nothing(no_model: NoModel) -> None:
    widget = _event("recaptcha_observed", 10, vendor="recaptcha")

    diagnostic = await ffd.final_failure_diagnostic(_evidence(_visit((widget,))), _context())

    assert diagnostic.vendors_observed == () and diagnostic.system_errors == ()


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
    database: AgentDB, org: str, monkeypatch: pytest.MonkeyPatch, no_model: NoModel, attempts: int, reset: bool
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
    database: AgentDB, org: str, monkeypatch: pytest.MonkeyPatch, no_model: NoModel
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
