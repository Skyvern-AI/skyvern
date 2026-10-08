"""The repair turn's binding to the run it was opened about.

The cases that matter are the refusals. A binding that silently fell back to the chat's browser
would answer a question about the failed run with a different browser's contents, and the answer
would look exactly like a real one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from skyvern.constants import SCRUBBED_VALUE
from skyvern.exceptions import WorkflowParameterNotFound, WorkflowRunNotFound
from skyvern.forge import app
from skyvern.forge.sdk.copilot.agent import run_copilot_agent
from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm
from skyvern.forge.sdk.copilot.repair_origin_run import (
    OriginExecutionSettings,
    OriginOutputRefusal,
    OriginOutputSnapshot,
    RepairOriginRefusal,
    origin_block_outputs_from_rows,
    resolve_repair_origin_binding,
    seed_repair_origin_run,
)
from skyvern.forge.sdk.copilot.tools import _record_run_blocks_result, get_run_results_tool, run_execution
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.db.models import WorkflowModel, WorkflowRunModel
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowRun,
    WorkflowRunParameter,
    WorkflowRunStatus,
)
from skyvern.schemas.runs import ProxyLocation
from tests.unit.copilot_test_helpers import (
    HARNESS_RUN_CREATED_AT,
    INERT_APPROVAL_WORKFLOW_YAML,
    ORIGIN_OUTPUT_SENTINEL,
    harness_run,
    inert_approval_workflow,
    install_get_run_results_harness,
    merge_origin_rows,
    origin_block_rows,
    origin_run_input,
    origin_run_row,
    run_result_block_row,
    stub_copilot_agent_loop,
)
from tests.unit.helpers import unsolved_captcha_relabel_categories

ORG = "o_1"
WPID = "wpid_1"
RUN = "wr_1"
RUN_BROWSER = "pbs_run"
CHAT_BROWSER = "pbs_chat"


@dataclass
class _Ctx:
    organization_id: str = ORG
    workflow_permanent_id: str | None = WPID
    last_run_blocks_workflow_run_id: str | None = None
    last_run_blocks_browser_session_id: str | None = None
    last_run_binding_unavailable_reason: str | None = None
    repair_origin_input_values: tuple[tuple[WorkflowParameter, WorkflowRunParameter], ...] = field(default=())
    repair_origin_is_copilot_run: bool = False
    repair_origin_outputs: OriginOutputSnapshot | OriginOutputRefusal | None = None
    repair_origin_outputs_run_id: str | None = None


ORIGIN_VALUES = [origin_run_input("resume", "resume_run_value")]
STALE_VALUES = (origin_run_input("stale", "stale_run_value"),)


def _install_run(
    monkeypatch: pytest.MonkeyPatch,
    run: object | Exception,
    parameters: list[tuple[WorkflowParameter, WorkflowRunParameter]] | Exception = ORIGIN_VALUES,
) -> list[str]:
    loaded_run_ids: list[str] = []

    async def get_workflow_run(*, workflow_run_id: str, organization_id: str | None = None) -> object:
        if isinstance(run, Exception):
            raise run
        return run

    async def get_workflow_run_parameters(
        *, workflow_run_id: str
    ) -> list[tuple[WorkflowParameter, WorkflowRunParameter]]:
        loaded_run_ids.append(workflow_run_id)
        if isinstance(parameters, Exception):
            raise parameters
        return parameters

    monkeypatch.setattr(app, "WORKFLOW_SERVICE", SimpleNamespace(get_workflow_run=get_workflow_run), raising=False)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run_parameters", get_workflow_run_parameters)
    monkeypatch.setattr(app.DATABASE.workflows, "get_workflow", AsyncMock(return_value=None))
    return loaded_run_ids


def _run(**overrides: object) -> WorkflowRun:
    fields: dict[str, object] = {
        "workflow_run_id": RUN,
        "organization_id": ORG,
        "workflow_permanent_id": WPID,
        "browser_session_id": RUN_BROWSER,
        "status": WorkflowRunStatus.failed,
    }
    return origin_run_row(**{**fields, **overrides})


@pytest.mark.asyncio
async def test_seeds_the_browser_the_failed_run_used(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_run(monkeypatch, _run())
    ctx = _Ctx(last_run_blocks_browser_session_id=None)

    binding = await seed_repair_origin_run(ctx, workflow_run_id=RUN)

    assert binding.usable
    assert ctx.last_run_blocks_workflow_run_id == RUN
    assert ctx.last_run_blocks_browser_session_id == RUN_BROWSER


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (WorkflowRunNotFound(RUN), RepairOriginRefusal.RUN_NOT_FOUND),
        (_run(organization_id="o_other"), RepairOriginRefusal.FOREIGN_ORGANIZATION),
        (_run(workflow_permanent_id="wpid_other"), RepairOriginRefusal.WORKFLOW_MISMATCH),
        (_run(browser_session_id=None), RepairOriginRefusal.NO_RECORDED_BROWSER),
    ],
)
@pytest.mark.asyncio
async def test_a_run_it_cannot_vouch_for_leaves_the_target_unavailable(
    monkeypatch: pytest.MonkeyPatch, run: object, expected: RepairOriginRefusal
) -> None:
    _install_run(monkeypatch, run)
    # The chat's own browser is present and must not be substituted for the run's.
    ctx = _Ctx(last_run_blocks_browser_session_id=None)

    binding = await seed_repair_origin_run(ctx, workflow_run_id=RUN)

    assert binding.refusal is expected
    assert not binding.usable
    assert ctx.last_run_blocks_browser_session_id is None
    assert ctx.last_run_blocks_workflow_run_id is None


@pytest.mark.parametrize(
    ("run", "loads_values"),
    [
        (_run(), True),
        (_run(browser_session_id=None), True),
        (WorkflowRunNotFound(RUN), False),
        (_run(organization_id="o_other"), False),
        (_run(workflow_permanent_id="wpid_other"), False),
        (RuntimeError("the run store is unreachable"), False),
    ],
    ids=[
        "usable",
        "no_recorded_browser",
        "run_not_found",
        "foreign_organization",
        "workflow_mismatch",
        "lookup_failed",
    ],
)
@pytest.mark.asyncio
async def test_origin_input_values_load_only_after_the_ownership_checks(
    monkeypatch: pytest.MonkeyPatch, run: WorkflowRun | Exception, loads_values: bool
) -> None:
    loaded_run_ids = _install_run(monkeypatch, run)
    ctx = _Ctx(repair_origin_input_values=STALE_VALUES)

    await seed_repair_origin_run(ctx, workflow_run_id=RUN)

    assert loaded_run_ids == ([RUN] if loads_values else [])
    assert ctx.repair_origin_input_values == (tuple(ORIGIN_VALUES) if loads_values else ())


@pytest.mark.parametrize("browser_session_id", [RUN_BROWSER, None], ids=["usable", "no_recorded_browser"])
@pytest.mark.parametrize("copilot_session_id", ["wcc_1", None], ids=["copilot_test_run", "user_run"])
@pytest.mark.asyncio
async def test_the_seed_records_whether_the_origin_is_a_copilot_test_run(
    monkeypatch: pytest.MonkeyPatch, browser_session_id: str | None, copilot_session_id: str | None
) -> None:
    _install_run(monkeypatch, _run(browser_session_id=browser_session_id, copilot_session_id=copilot_session_id))
    ctx = _Ctx(repair_origin_is_copilot_run=copilot_session_id is None)

    await seed_repair_origin_run(ctx, workflow_run_id=RUN)

    assert ctx.repair_origin_input_values == tuple(ORIGIN_VALUES)
    assert ctx.repair_origin_is_copilot_run is (copilot_session_id is not None)


@pytest.mark.asyncio
async def test_origin_input_values_that_cannot_be_loaded_leave_the_turn_without_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_run(monkeypatch, _run(), parameters=WorkflowParameterNotFound(workflow_parameter_id="wp_gone"))
    ctx = _Ctx()

    binding = await seed_repair_origin_run(ctx, workflow_run_id=RUN)

    assert binding.usable
    assert ctx.repair_origin_input_values == ()


@pytest.mark.parametrize(
    ("stored_row", "reused"),
    [
        (SimpleNamespace(storage_uri="s3://bucket/o_1/resume.pdf", expires_at=None, run_id=None), True),
        (None, False),
        (SimpleNamespace(storage_uri="s3://bucket/o_1/resume.pdf", expires_at=None, run_id=RUN), False),
    ],
    ids=["unattached_live_file", "deleted_with_its_run", "still_attached_awaiting_sweep"],
)
@pytest.mark.asyncio
async def test_an_uploaded_file_id_is_reused_only_when_no_run_will_delete_it(
    monkeypatch: pytest.MonkeyPatch, stored_row: SimpleNamespace | None, reused: bool
) -> None:
    resume = origin_run_input("resume", "file_123")
    _install_run(monkeypatch, _run(), parameters=[resume, *ORIGIN_VALUES])

    async def get_uploaded_file(*, file_id: str, organization_id: str) -> SimpleNamespace | None:
        return stored_row

    monkeypatch.setattr(app.DATABASE.uploaded_files, "get_uploaded_file", get_uploaded_file)
    ctx = _Ctx()

    await seed_repair_origin_run(ctx, workflow_run_id=RUN)

    assert ctx.repair_origin_input_values == ((resume, *ORIGIN_VALUES) if reused else tuple(ORIGIN_VALUES))


@pytest.mark.asyncio
async def test_resolving_the_binding_alone_never_loads_input_values(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded_run_ids = _install_run(monkeypatch, _run())

    await resolve_repair_origin_binding(workflow_run_id=RUN, organization_id=ORG, workflow_permanent_id=WPID)

    assert loaded_run_ids == []


@pytest.mark.asyncio
async def test_a_turn_opened_about_no_run_seeds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_run(monkeypatch, _run())
    ctx = _Ctx()

    binding = await seed_repair_origin_run(ctx, workflow_run_id=None)

    assert binding.refusal is RepairOriginRefusal.NOT_REQUESTED
    assert ctx.last_run_blocks_browser_session_id is None


@pytest.mark.asyncio
async def test_a_run_in_this_turn_replaces_what_was_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seed is only a starting point: a run performed in this turn goes through the ordinary
    recording path, which must leave the turn looking at what it just did."""
    _install_run(monkeypatch, _run())
    ctx = MagicMock()
    ctx.organization_id = ORG
    ctx.workflow_permanent_id = WPID
    await seed_repair_origin_run(ctx, workflow_run_id=RUN)
    assert ctx.last_run_blocks_browser_session_id == RUN_BROWSER

    _record_run_blocks_result(
        ctx,
        {"ok": True, "data": {"workflow_run_id": "wr_2", "browser_session_id": "pbs_2", "blocks": []}},
    )

    assert ctx.last_run_blocks_workflow_run_id == "wr_2"
    assert ctx.last_run_blocks_browser_session_id == "pbs_2"


@pytest.mark.asyncio
async def test_the_binding_never_reads_the_chat_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """The only source is the run record; a request's own browser is the chat's."""
    _install_run(monkeypatch, _run(browser_session_id=None))

    binding = await resolve_repair_origin_binding(workflow_run_id=RUN, organization_id=ORG, workflow_permanent_id=WPID)

    assert binding.browser_session_id is None
    assert binding.browser_session_id != CHAT_BROWSER


@pytest.mark.asyncio
async def test_a_turn_opened_about_a_run_is_seeded_before_it_acts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Testing the binding alone leaves the hop unpinned: the turn could stop calling it and every
    direct test would stay green while a repair reached its first tool with no run to look at."""
    _install_run(monkeypatch, _run())
    seen: dict[str, object] = {}

    async def capture_turn(**kwargs: object) -> SimpleNamespace:
        ctx = kwargs["ctx"]
        seen["browser"] = ctx.last_run_blocks_browser_session_id
        seen["run"] = ctx.last_run_blocks_workflow_run_id
        return SimpleNamespace(final_output=json.dumps({"type": "REPLY", "user_response": "ok"}), new_items=[])

    stub_copilot_agent_loop(monkeypatch, capture_turn)

    await run_copilot_agent(
        stream=MagicMock(),
        organization_id=ORG,
        chat_request=WorkflowCopilotChatRequest(
            workflow_permanent_id=WPID,
            workflow_id="wf-1",
            workflow_copilot_chat_id="chat-1",
            message="fix the run",
            workflow_yaml="",
            workflow_run_id=RUN,
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=AsyncMock(
            return_value={"version": "1", "state": "clean", "handling": "none", "citations": []}
        ),
        api_key="sk-test",
    )

    assert seen["browser"] == RUN_BROWSER
    assert seen["run"] == RUN


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["", "   ", None, "wpid_other"])
async def test_a_run_from_another_workflow_never_binds_its_browser(
    monkeypatch: pytest.MonkeyPatch, requested: str | None
) -> None:
    """The field is required on the request, so a falsy one is a mismatch. Skipping the check on
    an empty string would let any run in the organization hand this turn its browser."""
    _install_run(monkeypatch, _run())

    binding = await resolve_repair_origin_binding(
        workflow_run_id=RUN, organization_id=ORG, workflow_permanent_id=requested
    )

    assert binding.refusal is RepairOriginRefusal.WORKFLOW_MISMATCH
    assert binding.browser_session_id is None


@pytest.mark.parametrize(
    ("status", "reads_the_run"),
    [
        (WorkflowRunStatus.failed, True),
        (WorkflowRunStatus.timed_out, True),
        (WorkflowRunStatus.terminated, True),
        (WorkflowRunStatus.completed, True),
        (WorkflowRunStatus.running, False),
        (WorkflowRunStatus.queued, False),
    ],
)
@pytest.mark.asyncio
async def test_a_finished_run_is_read_and_an_unfinished_one_is_not(
    monkeypatch: pytest.MonkeyPatch, status: WorkflowRunStatus, reads_the_run: bool
) -> None:
    """A completed run can still be the one the user is complaining about, so completion is
    reported to the model rather than used to withhold the record."""
    _install_run(monkeypatch, _run(status=status))

    binding = await resolve_repair_origin_binding(workflow_run_id=RUN, organization_id=ORG, workflow_permanent_id=WPID)

    assert binding.finished is reads_the_run


@pytest.mark.asyncio
async def test_hydrating_a_prior_run_never_reads_a_live_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wiring only: that the guard is honoured is pinned where the branch lives, in
    test_copilot_screenshot_handling."""
    seen: dict[str, object] = {}

    async def fake_get_run_results(  # type: ignore[no-untyped-def]
        params, ctx, *, read_live_page=True, admit_sensitive_origin_artifact=True
    ):
        seen["read_live_page"] = read_live_page
        seen["admit_sensitive_origin_artifact"] = admit_sensitive_origin_artifact
        return {"ok": False}

    monkeypatch.setattr(run_execution, "_get_run_results", fake_get_run_results)

    await run_execution.hydrate_prior_run_packet(SimpleNamespace(), workflow_run_id=RUN)  # type: ignore[arg-type]

    assert seen["read_live_page"] is False
    assert seen["admit_sensitive_origin_artifact"] is False


@pytest.mark.asyncio
async def test_a_packet_that_cannot_be_projected_leaves_the_turn_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn that cannot read its origin run still has to answer the user."""

    async def run_results(  # type: ignore[no-untyped-def]
        params, ctx, *, read_live_page=True, admit_sensitive_origin_artifact=True
    ):
        return {"ok": True, "data": {}}

    def explode(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("projection failed")

    monkeypatch.setattr(run_execution, "_get_run_results", run_results)
    monkeypatch.setattr(run_execution, "finalize_build_test_result", explode)

    assert await run_execution.hydrate_prior_run_packet(SimpleNamespace(), workflow_run_id=RUN) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_failed_run_lookup_leaves_the_turn_unseeded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn that inherits a run id must not die because that run could not be read."""
    _install_run(monkeypatch, RuntimeError("the run store is unreachable"))
    ctx = _Ctx()

    binding = await seed_repair_origin_run(ctx, workflow_run_id=RUN)

    assert binding.refusal is RepairOriginRefusal.LOOKUP_FAILED
    assert ctx.last_run_blocks_workflow_run_id is None
    assert ctx.last_run_blocks_browser_session_id is None
    assert ctx.last_run_binding_unavailable_reason == "The last run this chat recorded could not be looked up."


@pytest.mark.asyncio
async def test_a_refused_binding_names_the_fact_that_was_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_run(monkeypatch, _run(browser_session_id=None))
    ctx = _Ctx()

    await seed_repair_origin_run(ctx, workflow_run_id=RUN)

    assert (
        ctx.last_run_binding_unavailable_reason == "The last run this chat recorded did not record a browser session."
    )


@pytest.mark.asyncio
async def test_a_chat_with_no_recorded_run_carries_no_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_run(monkeypatch, _run())
    ctx = _Ctx()

    binding = await seed_repair_origin_run(ctx, workflow_run_id=None)

    assert binding.refusal is RepairOriginRefusal.NOT_REQUESTED
    assert ctx.last_run_binding_unavailable_reason is None


LATER = HARNESS_RUN_CREATED_AT + timedelta(hours=1)


def _newer_run_ids(data: dict[str, Any]) -> list[str]:
    return [entry["workflow_run_id"] for entry in data["newer_finished_runs"]]


@pytest.mark.asyncio
async def test_a_carried_run_is_returned_with_the_newer_scheduled_run_beside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = install_get_run_results_harness(
        monkeypatch,
        blocks=[],
        run_status="completed",
        carried_successful_run_id="wr-1",
        other_runs=[
            harness_run("wr-scheduled", created_at=LATER, trigger_type=WorkflowRunTriggerType.scheduled),
        ],
    )

    result = await run_execution._get_run_results({}, ctx, read_live_page=False)
    data = sanitize_tool_result_for_llm("get_run_results", result)["data"]

    assert data["workflow_run_id"] == "wr-1"
    assert data["selected_by"] == "carried_from_chat"
    assert data["created_at"] == "2026-04-21T12:00:00+00:00"
    assert data["trigger_type"] is None
    assert data["newer_finished_runs"] == [
        {
            "workflow_run_id": "wr-scheduled",
            "status": "completed",
            "created_at": "2026-04-21T13:00:00+00:00",
            "trigger_type": "scheduled",
        }
    ]


@pytest.mark.asyncio
async def test_an_explicit_run_id_returns_exactly_that_run(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = install_get_run_results_harness(
        monkeypatch,
        blocks=[],
        run_status="failed",
        carried_successful_run_id="wr-carried",
        other_runs=[
            harness_run("wr-carried", created_at=LATER, copilot_session_id="wcs-1"),
            harness_run("wr-scheduled", created_at=LATER, trigger_type=WorkflowRunTriggerType.scheduled),
        ],
    )

    data = (await run_execution._get_run_results({"workflow_run_id": "wr-1"}, ctx, read_live_page=False))["data"]

    assert data["workflow_run_id"] == "wr-1"
    assert data["selected_by"] == "explicit"
    assert _newer_run_ids(data) == ["wr-scheduled"]


@pytest.mark.parametrize(
    ("carried_run_id", "selected_run_id", "selected_by", "selected_trigger_type", "listed_run_ids"),
    [
        ("wr-1", "wr-1", "carried_from_chat", None, ["wr-scheduled"]),
        (None, "wr-scheduled", "latest_for_workflow", "scheduled", []),
    ],
)
@pytest.mark.asyncio
async def test_unfinished_and_same_instant_runs_are_never_selected_or_listed(
    monkeypatch: pytest.MonkeyPatch,
    carried_run_id: str | None,
    selected_run_id: str,
    selected_by: str,
    selected_trigger_type: str | None,
    listed_run_ids: list[str],
) -> None:
    decoys_at = LATER + timedelta(minutes=5)
    ctx = install_get_run_results_harness(
        monkeypatch,
        blocks=[],
        run_status="completed",
        carried_run_id=carried_run_id,
        other_runs=[
            harness_run("wr-scheduled", created_at=LATER, trigger_type=WorkflowRunTriggerType.scheduled),
            harness_run("wr-running", created_at=decoys_at, status="running"),
            harness_run("wr-queued", created_at=decoys_at, status="queued"),
            harness_run("wr-other-workflow", created_at=decoys_at, workflow_permanent_id="wpid-other"),
            harness_run("wr-other-org", created_at=decoys_at, organization_id="org-other"),
            harness_run("wr-same-instant", created_at=HARNESS_RUN_CREATED_AT),
        ],
    )

    data = (await run_execution._get_run_results({}, ctx, read_live_page=False))["data"]

    assert data["workflow_run_id"] == selected_run_id
    assert data["selected_by"] == selected_by
    assert data["trigger_type"] == selected_trigger_type
    assert _newer_run_ids(data) == listed_run_ids


@pytest.mark.asyncio
async def test_a_failed_newer_runs_lookup_keeps_the_run_and_marks_the_list_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = install_get_run_results_harness(monkeypatch, blocks=[], run_status="completed", carried_run_id="wr-1")
    monkeypatch.setattr(
        run_execution.app.DATABASE.workflow_runs,
        "get_workflow_runs_for_workflow_permanent_id",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )

    result = await run_execution._get_run_results({}, ctx, read_live_page=False)

    assert result["ok"] is True
    assert result["data"]["workflow_run_id"] == "wr-1"
    assert result["data"]["selected_by"] == "carried_from_chat"
    assert result["data"]["newer_finished_runs_unavailable"] is True
    assert "newer_finished_runs" not in result["data"]


@pytest.mark.asyncio
async def test_the_history_query_lists_only_newer_finished_runs_of_this_workflow_and_org(
    monkeypatch: pytest.MonkeyPatch, sqlite_engine: AsyncEngine
) -> None:
    db = AgentDB("sqlite+aiosqlite:///:memory:", db_engine=sqlite_engine)
    selected_at = datetime(2026, 4, 21, 12, 0, 0)
    later = selected_at + timedelta(hours=1)

    def run_row(workflow_run_id: str, **overrides: Any) -> WorkflowRunModel:
        fields: dict[str, Any] = {
            "workflow_run_id": workflow_run_id,
            "workflow_id": "wf_primary",
            "workflow_permanent_id": WPID,
            "organization_id": ORG,
            "status": "completed",
            "created_at": later,
        }
        fields.update(overrides)
        return WorkflowRunModel(**fields)

    async with db.Session() as session:
        for workflow_id, wpid in (("wf_primary", WPID), ("wf_other", "wpid_other")):
            session.add(
                WorkflowModel(
                    workflow_id=workflow_id,
                    workflow_permanent_id=wpid,
                    organization_id=ORG,
                    title=workflow_id,
                    workflow_definition={"blocks": [], "parameters": []},
                    version=1,
                )
            )
        session.add_all(
            [
                run_row("wr_selected", created_at=selected_at),
                run_row("wr_scheduled", trigger_type=WorkflowRunTriggerType.scheduled),
                run_row("wr_newest", created_at=later + timedelta(hours=1)),
                run_row("wr_running", status="running"),
                run_row("wr_queued", status="queued"),
                run_row("wr_other_org", organization_id="o_other"),
                run_row("wr_other_chat", copilot_session_id="wcs_other"),
                run_row("wr_same_instant", created_at=selected_at),
                run_row("wr_other_workflow", workflow_id="wf_other", workflow_permanent_id="wpid_other"),
            ]
        )
        await session.commit()
    monkeypatch.setattr(run_execution, "app", SimpleNamespace(DATABASE=db))
    selected = await db.workflow_runs.get_workflow_run(workflow_run_id="wr_selected", organization_id=ORG)
    assert selected is not None

    facts = await run_execution._run_selection_facts(
        selected, organization_id=ORG, workflow_permanent_id=WPID, selected_by="carried_from_chat"
    )

    assert [entry["workflow_run_id"] for entry in facts["newer_finished_runs"]] == ["wr_newest", "wr_scheduled"]


PAGED_SECRET = "tok-9f8e7d6c5b4a"
LOOP_ITERATIONS = 29


def _paged_run_rows() -> list[list[MagicMock]]:
    """A for-loop row and two child rows per iteration whose created_at ties, in the two orders the
    repository may return tied rows in; each list is newest first, as the repository returns it."""

    def row(block_id: str, label: str, block_type: str, created_at: datetime, output: object) -> MagicMock:
        block = run_result_block_row(label, "completed")
        block.workflow_run_block_id = block_id
        block.block_type = SimpleNamespace(name=block_type)
        block.created_at = created_at
        block.output = output
        block.parent_workflow_run_block_id = None
        block.current_index = None
        block.current_value = None
        return block

    loop = row("wrb_loop", "each_item", "FOR_LOOP", HARNESS_RUN_CREATED_AT, [{"item": "x" * 1000}] * 60)
    pairs: list[tuple[MagicMock, MagicMock]] = []
    for index in range(LOOP_ITERATIONS):
        at = HARNESS_RUN_CREATED_AT + timedelta(seconds=index + 1)
        opened = row(f"wrb_open_{index:02d}", "open_item", "NAVIGATION", at, "p" * 150 + PAGED_SECRET + " tail")
        opened.failure_reason = f"retried item {index}: " + "r" * 6000
        extracted = row(
            f"wrb_extract_{index:02d}",
            "extract_item",
            "EXTRACTION",
            at,
            {"extracted_information": {"name": f"item {index}", "detail": "d" * 5000}},
        )
        for child in (opened, extracted):
            child.parent_workflow_run_block_id = "wrb_loop"
            child.current_index = index
            child.current_value = json.dumps({"name": f"item {index}"})
        pairs.append((opened, extracted))
    oldest_first = [loop, *(child for pair in pairs for child in pair)]
    swapped = [loop, *(child for pair in pairs for child in reversed(pair))]
    return [list(reversed(oldest_first)), list(reversed(swapped))]


def _install_paged_run(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    orders = _paged_run_rows()
    ctx = install_get_run_results_harness(monkeypatch, blocks=orders[0], run_status="completed")
    reads = iter(range(1_000))
    run_execution.app.DATABASE.observer.get_workflow_run_blocks.side_effect = lambda **_: orders[next(reads) // 2 % 2]

    async def attach_registered(*, workflow_run_id: str, data: dict[str, Any], **_: object) -> dict[str, Any]:
        data["registered_output_parameter_values"] = [
            {"output_parameter_key": "report_output", "block_label": "report", "value": {"note": PAGED_SECRET}}
        ]
        run_execution._merge_registered_output_parameter_values_into_blocks(data)
        return {}

    monkeypatch.setattr(run_execution, "_attach_registered_output_parameter_values", attach_registered)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._authority_tool_error", lambda *_args: None)
    ctx.secret_scrub_values = [PAGED_SECRET]
    return ctx


async def _get_run_results_page(ctx: SimpleNamespace, **arguments: object) -> tuple[str, dict[str, Any]]:
    raw = await get_run_results_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="get_run_results"), json.dumps(arguments)
    )
    return raw, json.loads(raw)


@pytest.mark.asyncio
async def test_following_the_cursor_returns_every_row_once_in_a_stable_order(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _install_paged_run(monkeypatch)

    first_raw, first = await _get_run_results_page(ctx, workflow_run_id="wr-1")
    page = first
    raws = [first_raw]
    keys = [row["row_key"] for row in first["data"]["blocks"]]
    while cursor := page["data"].get("next_block_cursor"):
        raw, page = await _get_run_results_page(ctx, block_cursor=cursor)
        raws.append(raw)
        keys.extend(row["row_key"] for row in page["data"]["blocks"])

    expected = ["wrb_loop"]
    for index in range(LOOP_ITERATIONS):
        expected += [f"wrb_extract_{index:02d}", f"wrb_open_{index:02d}"]
    assert keys == [*expected, "registered:report"]
    assert first["data"]["total_block_rows"] == len(keys) == 60
    assert len(first_raw) < 50_000
    child = next(row for row in first["data"]["blocks"] if row["row_key"] == "wrb_open_00")
    assert child["parent_workflow_run_block_id"] == "wrb_loop"
    assert child["current_index"] == 0
    assert child["current_value_preview"] == json.dumps({"name": "item 0"})
    assert child["output_chars"] > len(child["output_preview"])
    assert all(PAGED_SECRET[:8] not in raw for raw in raws)


@pytest.mark.asyncio
async def test_row_keys_return_full_output_and_account_for_every_row_they_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _install_paged_run(monkeypatch)
    requested = [
        "wrb_loop",
        *(f"wrb_extract_{index:02d}" for index in range(8)),
        *(f"wrb_open_{index:02d}" for index in range(12)),
        "wrb_missing",
    ]

    raw, page = await _get_run_results_page(ctx, workflow_run_id="wr-1", row_keys=requested)

    assert len(raw) < 50_000
    data = page["data"]
    details = {row["row_key"]: row for row in data["block_details"]}
    deferred = [row["row_key"] for row in data["deferred_row_keys"]]
    assert data["unknown_row_keys"] == ["wrb_missing"]
    assert sorted([*details, *deferred]) == sorted(requested[:-1])
    assert deferred
    loop = details["wrb_loop"]
    assert loop["child_count"] == 2 * LOOP_ITERATIONS
    assert set(loop["output"]) == {"chars", "preview"}
    assert details["wrb_extract_00"]["output"] == {"extracted_information": {"name": "item 0", "detail": "d" * 5000}}
    assert all("action_observations" in row for row in details.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"workflow_run_id": "wr-1", "block_cursor": "wr-other:20"},
        {"block_cursor": "not-a-cursor"},
        {"block_cursor": "wr-1:500"},
        {"workflow_run_id": "wr-1", "row_keys": [f"wrb_{index}" for index in range(26)]},
    ],
)
async def test_a_foreign_or_unusable_page_request_is_refused(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, object]
) -> None:
    ctx = _install_paged_run(monkeypatch)

    _, page = await _get_run_results_page(ctx, **arguments)

    assert page["ok"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "big_output,registered_value",
    [({"text": "x" * 35_000}, {"text": "x" * 35_000}), ({"value": "x" * 35_000}, "x" * 35_000)],
)
async def test_a_large_single_output_read_by_key_comes_back_whole(
    monkeypatch: pytest.MonkeyPatch, big_output: dict[str, str], registered_value: object
) -> None:
    block = run_result_block_row("big", "completed")
    block.created_at = HARNESS_RUN_CREATED_AT
    block.output = big_output
    block.parent_workflow_run_block_id = None
    block.current_index = None
    block.current_value = None
    ctx = install_get_run_results_harness(monkeypatch, blocks=[block], run_status="completed")

    async def attach_registered(*, workflow_run_id: str, data: dict[str, Any], **_: object) -> dict[str, Any]:
        data["registered_output_parameter_values"] = [
            {"output_parameter_key": "big_output", "block_label": "big", "value": registered_value}
        ]
        run_execution._merge_registered_output_parameter_values_into_blocks(data)
        return {}

    monkeypatch.setattr(run_execution, "_attach_registered_output_parameter_values", attach_registered)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._authority_tool_error", lambda *_args: None)
    ctx.secret_scrub_values = []

    raw, page = await _get_run_results_page(ctx, workflow_run_id="wr-1", row_keys=["wrb_big"])

    assert len(raw) < 50_000
    assert page["data"]["block_details"][0]["output"] == big_output


def test_a_row_with_a_huge_failure_reason_still_returns_its_output_inside_the_cut() -> None:
    block = {
        "workflow_run_block_id": "wrb_big",
        "label": "step",
        "status": "failed",
        "failure_reason": "f" * 60_000,
        "output": {"answer": "the recorded output"},
    }
    facts: dict[str, Any] = {
        "wrb_big": {
            "created_at": HARNESS_RUN_CREATED_AT,
            "parent_workflow_run_block_id": None,
            "current_index": None,
            "current_value": None,
        }
    }
    result = {"ok": True, "data": {"workflow_run_id": "wr-1", "overall_status": "failed", "blocks": [block]}}

    page = run_execution.project_run_results_page(result, facts, row_keys=["wrb_big"])

    row = page["data"]["block_details"][0]
    assert row["failure_reason_chars"] == 60_000
    assert '"the recorded output"' in json.dumps(page)[:50_000]


def test_the_first_page_cursor_survives_a_head_cut_behind_a_large_run_field() -> None:
    blocks = [{"workflow_run_block_id": f"wrb_{i:02d}", "label": "step", "status": "completed"} for i in range(30)]
    facts: dict[str, Any] = {
        block["workflow_run_block_id"]: {
            "created_at": HARNESS_RUN_CREATED_AT + timedelta(seconds=i),
            "parent_workflow_run_block_id": None,
            "current_index": None,
            "current_value": None,
        }
        for i, block in enumerate(blocks)
    }
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr-1",
            "overall_status": "failed",
            "failure_reason": "f" * 55_000,
            "blocks": blocks,
        },
    }

    page = run_execution.project_run_results_page(result, facts)

    assert '"next_block_cursor": "wr-1:20"' in json.dumps(page)[:50_000]


@pytest.mark.asyncio
async def test_origin_rows_are_valued_the_way_verified_recording_values_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_by_permanent_id", AsyncMock(return_value=None))
    workflow = await inert_approval_workflow(INERT_APPROVAL_WORKFLOW_YAML, workflow_id="w_origin")

    row = {"row": ORIGIN_OUTPUT_SENTINEL}
    snapshot = origin_block_outputs_from_rows(
        workflow.workflow_definition,
        *merge_origin_rows(
            origin_block_rows(workflow, "approval", status="failed", minute=1, registered=False),
            origin_block_rows(workflow, "approval", minute=2, row_output=row, value=None),
            origin_block_rows(workflow, "approval", status="failed", minute=3, parent="wrb_loop", registered=False),
            origin_block_rows(workflow, "source_status", minute=4, row_output=row, registered=False),
        ),
    )

    approval = snapshot.outputs["approval"]
    assert (approval.status, approval.has_value, approval.value) == ("completed", True, None)
    source_status = snapshot.outputs["source_status"]
    assert (source_status.has_value, source_status.value) == (True, {"row": ORIGIN_OUTPUT_SENTINEL})
    assert ORIGIN_OUTPUT_SENTINEL not in repr(snapshot)
    assert ORIGIN_OUTPUT_SENTINEL not in repr(source_status)


@pytest.mark.asyncio
async def test_origin_outputs_seed_the_categories_the_run_had_before_an_output_only_relabel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_by_permanent_id", AsyncMock(return_value=None))
    workflow = await inert_approval_workflow(INERT_APPROVAL_WORKFLOW_YAML, workflow_id="w_origin")
    before, after = unsolved_captcha_relabel_categories()

    def seeded(categories: list[dict] | None) -> object:
        output = {"status": "failed", "failure_category": categories}
        rows = merge_origin_rows(
            origin_block_rows(workflow, "approval", status="failed", value=output),
            origin_block_rows(workflow, "source_status", status="failed", row_output=output, registered=False),
        )
        snapshot = origin_block_outputs_from_rows(workflow.workflow_definition, *rows)
        return {label: output.value for label, output in snapshot.outputs.items()}

    assert seeded(after) == seeded(before)


@pytest.mark.parametrize(
    ("run", "expected_refusal", "loads_rows"),
    [
        (_run(), None, True),
        (_run(browser_session_id=None), None, True),
        (_run(status=WorkflowRunStatus.running), OriginOutputRefusal.ORIGIN_UNSETTLED, False),
        (WorkflowRunNotFound(RUN), OriginOutputRefusal.FOREIGN_OR_MISMATCHED_ORIGIN, False),
        (_run(organization_id="o_other"), OriginOutputRefusal.FOREIGN_OR_MISMATCHED_ORIGIN, False),
        (_run(workflow_permanent_id="wpid_other"), OriginOutputRefusal.FOREIGN_OR_MISMATCHED_ORIGIN, False),
        (RuntimeError("the run store is unreachable"), OriginOutputRefusal.OUTPUT_UNAVAILABLE, False),
    ],
    ids=[
        "usable",
        "no_recorded_browser",
        "still_running",
        "run_not_found",
        "foreign_organization",
        "workflow_mismatch",
        "lookup_failed",
    ],
)
@pytest.mark.asyncio
async def test_origin_outputs_load_only_from_an_owned_finished_run(
    monkeypatch: pytest.MonkeyPatch,
    run: WorkflowRun | Exception,
    expected_refusal: OriginOutputRefusal | None,
    loads_rows: bool,
) -> None:
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_by_permanent_id", AsyncMock(return_value=None))
    workflow = await inert_approval_workflow(INERT_APPROVAL_WORKFLOW_YAML, workflow_id="w_origin")
    _install_run(monkeypatch, run)
    get_workflow = AsyncMock(return_value=workflow)
    get_rows = AsyncMock(return_value=origin_block_rows(workflow, "approval")[0])
    monkeypatch.setattr(app.DATABASE.workflows, "get_workflow", get_workflow)
    monkeypatch.setattr(app.DATABASE.observer, "get_workflow_run_blocks", get_rows)
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_run_output_parameters",
        AsyncMock(return_value=origin_block_rows(workflow, "approval", value={"secret": ORIGIN_OUTPUT_SENTINEL})[1]),
    )
    ctx = _Ctx()

    with capture_logs() as logs:
        await seed_repair_origin_run(ctx, workflow_run_id=RUN)

    owned = expected_refusal in (None, OriginOutputRefusal.ORIGIN_UNSETTLED)
    assert ctx.repair_origin_outputs_run_id == (RUN if owned else None)
    assert isinstance(ctx.repair_origin_outputs, OriginOutputSnapshot) is loads_rows
    if not loads_rows:
        assert ctx.repair_origin_outputs is expected_refusal
    # Rows of a foreign or unsettled run are never read, not merely discarded.
    assert get_rows.await_count == (1 if loads_rows else 0)
    if loads_rows:
        get_workflow.assert_awaited_once_with(workflow_id="w_origin", organization_id=ORG)
        assert isinstance(ctx.repair_origin_outputs, OriginOutputSnapshot)
        assert ctx.repair_origin_outputs.outputs["approval"].value == {"secret": ORIGIN_OUTPUT_SENTINEL}
    assert ORIGIN_OUTPUT_SENTINEL not in repr(logs)
    assert ORIGIN_OUTPUT_SENTINEL not in repr(ctx)


@pytest.mark.parametrize(
    ("run_overrides", "parameters", "expected"),
    [
        ({"copilot_session_id": "cs_test_run"}, ORIGIN_VALUES, None),
        ({"debug_session_id": "ds_debugger"}, ORIGIN_VALUES, None),
        ({}, RuntimeError("the parameter store is unreachable"), OriginOutputRefusal.OUTPUT_UNAVAILABLE),
        ({"failure_reason": SCRUBBED_VALUE}, ORIGIN_VALUES, OriginOutputRefusal.OUTPUT_UNAVAILABLE),
        ({}, [origin_run_input("resume", SCRUBBED_VALUE)], OriginOutputRefusal.OUTPUT_UNAVAILABLE),
        ({"created_at": datetime(2020, 1, 1, tzinfo=UTC)}, ORIGIN_VALUES, OriginOutputRefusal.OUTPUT_UNAVAILABLE),
        ({"script_run": {"script_id": "s_cached"}}, ORIGIN_VALUES, OriginOutputRefusal.CHANGED_EXECUTION_SETTINGS),
        ({"parent_workflow_run_id": "wr_parent"}, ORIGIN_VALUES, OriginOutputRefusal.CHANGED_EXECUTION_SETTINGS),
    ],
    ids=[
        "copilot_test_run_is_never_an_origin",
        "debugger_block_run_is_never_an_origin",
        "unreadable_inputs_never_reuse",
        "retention_scrubbed_failure_reason",
        "retention_scrubbed_input",
        "version_overwritten_after_the_run_started",
        "origin_ran_a_cached_script",
        "child_run_inherited_ancestor_prompts",
    ],
)
@pytest.mark.asyncio
async def test_origin_outputs_need_a_real_run_and_its_recorded_inputs(
    monkeypatch: pytest.MonkeyPatch,
    run_overrides: dict[str, str | datetime],
    parameters: list[tuple[WorkflowParameter, WorkflowRunParameter]] | Exception,
    expected: OriginOutputRefusal | None,
) -> None:
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_by_permanent_id", AsyncMock(return_value=None))
    workflow = await inert_approval_workflow(INERT_APPROVAL_WORKFLOW_YAML, workflow_id="w_origin")
    _install_run(monkeypatch, _run(**run_overrides), parameters)
    monkeypatch.setattr(app.DATABASE.workflows, "get_workflow", AsyncMock(return_value=workflow))
    rows = origin_block_rows(workflow, "approval", value={"authorized": True})
    monkeypatch.setattr(app.DATABASE.observer, "get_workflow_run_blocks", AsyncMock(return_value=rows[0]))
    monkeypatch.setattr(
        app.DATABASE.workflow_runs, "get_workflow_run_output_parameters", AsyncMock(return_value=rows[1])
    )
    ctx = _Ctx()

    await seed_repair_origin_run(ctx, workflow_run_id=RUN)

    assert ctx.repair_origin_outputs is expected
    if expected is None:
        assert ctx.repair_origin_outputs_run_id is None


@pytest.mark.asyncio
async def test_no_headers_and_empty_headers_are_the_same_execution_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_by_permanent_id", AsyncMock(return_value=None))
    workflow = await inert_approval_workflow(INERT_APPROVAL_WORKFLOW_YAML, workflow_id="w_origin")

    saved_by_copilot = OriginExecutionSettings.of(workflow.model_copy(update={"extra_http_headers": {}}))

    assert saved_by_copilot == OriginExecutionSettings.of(
        workflow.model_copy(update={"extra_http_headers": None}), _run()
    )


@pytest.mark.asyncio
async def test_a_turn_opened_about_no_run_loads_no_origin_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_run(monkeypatch, _run())
    ctx = _Ctx(repair_origin_outputs_run_id="wr_stale")

    await seed_repair_origin_run(ctx, workflow_run_id=None)

    assert (ctx.repair_origin_outputs_run_id, ctx.repair_origin_outputs) == (None, None)


@pytest.mark.asyncio
async def test_the_origin_runs_own_setting_overrides_win_over_its_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_by_permanent_id", AsyncMock(return_value=None))
    workflow = await inert_approval_workflow(INERT_APPROVAL_WORKFLOW_YAML, workflow_id="w_origin")
    version = workflow.model_copy(
        update={"proxy_location": ProxyLocation.US_CA, "browser_profile_id": "bp_version", "model": {"name": "m"}}
    )
    _install_run(monkeypatch, _run(proxy_location=ProxyLocation.US_NY, extra_http_headers={"X-Run": "1"}))
    monkeypatch.setattr(app.DATABASE.workflows, "get_workflow", AsyncMock(return_value=version))
    monkeypatch.setattr(app.DATABASE.observer, "get_workflow_run_blocks", AsyncMock(return_value=[]))
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run_output_parameters", AsyncMock(return_value=[]))
    ctx = _Ctx()

    await seed_repair_origin_run(ctx, workflow_run_id=RUN)

    assert isinstance(ctx.repair_origin_outputs, OriginOutputSnapshot)
    assert ctx.repair_origin_outputs.settings == OriginExecutionSettings(
        proxy_location=ProxyLocation.US_NY,
        browser_profile_id="bp_version",
        browser_profile_key=None,
        model={"name": "m"},
        extra_http_headers={"X-Run": "1"},
    )


# Every other Workflow field is classified here as not changing what a block run outputs.
_WORKFLOW_FIELDS_NOT_COMPARED = frozenset(
    {
        "workflow_id",
        "organization_id",
        "title",
        "workflow_permanent_id",
        "version",
        "is_saved_task",
        "is_template",
        "description",
        "workflow_definition",
        "webhook_callback_url",
        "totp_verification_url",
        "totp_identifier",
        "persist_browser_session",
        "reuse_browser_session",
        "mask_secrets",
        "pin_saved_session_ip",
        "status",
        "max_screenshot_scrolls",
        "max_elapsed_time_minutes",
        "cdp_connect_headers",
        "run_with",
        "browser_type",
        "ai_fallback",
        "cache_key",
        "adaptive_caching",
        "enable_self_healing",
        "code_version",
        "generate_script_on_terminal",
        "run_sequentially",
        "sequential_key",
        "folder_id",
        "import_error",
        "created_by",
        "edited_by",
        "copilot_authored",
        # Filled only by the workflow detail endpoint; never read at run time.
        "effective_default_engine",
        "original_created_by",
        "original_created_at",
        "created_at",
        "modified_at",
        "deleted_at",
    }
)


def test_every_workflow_field_is_compared_across_origin_and_test_or_classified_as_not_shaping_output() -> None:
    compared = {setting.name for setting in fields(OriginExecutionSettings)}

    assert compared <= set(Workflow.model_fields)
    assert set(Workflow.model_fields) - compared - _WORKFLOW_FIELDS_NOT_COMPARED == set()
