"""The repair turn's binding to the run it was opened about.

The cases that matter are the refusals. A binding that silently fell back to the chat's browser
would answer a question about the failed run with a different browser's contents, and the answer
would look exactly like a real one.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skyvern.exceptions import WorkflowRunNotFound
from skyvern.forge.sdk.copilot.repair_origin_run import (
    RepairOriginRefusal,
    resolve_repair_origin_binding,
    seed_repair_origin_run,
)
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus

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


def _install_run(monkeypatch: pytest.MonkeyPatch, run: object | Exception) -> None:
    async def get_workflow_run(*, workflow_run_id: str, organization_id: str | None = None) -> object:
        if isinstance(run, Exception):
            raise run
        return run

    from skyvern.forge import app

    monkeypatch.setattr(app, "WORKFLOW_SERVICE", SimpleNamespace(get_workflow_run=get_workflow_run), raising=False)


def _run(**overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = {
        "workflow_run_id": RUN,
        "organization_id": ORG,
        "workflow_permanent_id": WPID,
        "browser_session_id": RUN_BROWSER,
        "status": WorkflowRunStatus.failed,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


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
    from skyvern.forge.sdk.copilot.tools import _record_run_blocks_result

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
    import json
    from unittest.mock import AsyncMock

    from skyvern.forge.sdk.copilot.agent import run_copilot_agent
    from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest
    from tests.unit.copilot_test_helpers import stub_copilot_agent_loop

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
    from skyvern.forge.sdk.copilot.tools import run_execution

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
    from skyvern.forge.sdk.copilot.tools import run_execution

    async def run_results(  # type: ignore[no-untyped-def]
        params, ctx, *, read_live_page=True, admit_sensitive_origin_artifact=True
    ):
        return {"ok": True, "data": {}}

    def explode(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("projection failed")

    monkeypatch.setattr(run_execution, "_get_run_results", run_results)
    monkeypatch.setattr(run_execution, "finalize_build_test_result", explode)

    assert await run_execution.hydrate_prior_run_packet(SimpleNamespace(), workflow_run_id=RUN) is None  # type: ignore[arg-type]
