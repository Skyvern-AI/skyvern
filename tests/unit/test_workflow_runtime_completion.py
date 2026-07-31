"""Runtime grading of a workflow's declared completion contract.

The contract lives on the workflow version and is graded from execution-layer evidence, so the
verdict does not depend on which engine ran the blocks or how generated code described its outcome.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.workflow import service as service_module
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.runtime_completion import (
    CompletionCriterion,
    carried_contract,
    contract_from_request_criteria,
    grade_completion_contract,
    parse_completion_contract,
    with_contract,
)

_DOWNLOAD_CONTRACT = {
    "completion_contract": {
        "schema_version": 1,
        "criteria": [{"id": "must_download", "kind": "registered_download", "min_count": 1}],
    }
}


def test_workflow_without_a_contract_declares_nothing() -> None:
    assert parse_completion_contract({}) == ()
    assert parse_completion_contract(None) == ()
    assert parse_completion_contract({"completion_contract": {"criteria": "nope"}}) == ()


def test_download_contract_parses() -> None:
    (criterion,) = parse_completion_contract(_DOWNLOAD_CONTRACT)
    assert criterion == CompletionCriterion(id="must_download", kind="registered_download", min_count=1)


def test_unknown_kinds_are_dropped_not_failed() -> None:
    """An older worker must keep running a newer workflow, and must never fail what it cannot grade."""
    contract = {
        "completion_contract": {
            "criteria": [
                {"id": "future", "kind": "some_future_kind"},
                {"id": "must_download", "kind": "registered_download"},
            ]
        }
    }
    parsed = parse_completion_contract(contract)
    assert [c.id for c in parsed] == ["must_download"]


def test_a_run_that_registered_a_file_satisfies_the_contract() -> None:
    criteria = parse_completion_contract(_DOWNLOAD_CONTRACT)
    verdict = grade_completion_contract(criteria, registered_download_count=1)
    assert verdict.satisfied is True
    assert verdict.unmet_criterion_ids == ()


def test_a_run_that_registered_nothing_is_unmet() -> None:
    """The production shape: the block returned cleanly, the run produced no file."""
    criteria = parse_completion_contract(_DOWNLOAD_CONTRACT)
    verdict = grade_completion_contract(criteria, registered_download_count=0)
    assert verdict.satisfied is False
    assert verdict.unmet_criterion_ids == ("must_download",)
    assert verdict.reason


def test_no_criteria_grades_as_satisfied() -> None:
    """Contract-less workflows keep their existing outcome."""
    assert grade_completion_contract((), registered_download_count=0).satisfied is True


def test_min_count_is_honored_and_floored_at_one() -> None:
    contract = {"completion_contract": {"criteria": [{"kind": "registered_download", "min_count": 2}]}}
    criteria = parse_completion_contract(contract)
    assert grade_completion_contract(criteria, registered_download_count=1).satisfied is False
    assert grade_completion_contract(criteria, registered_download_count=2).satisfied is True

    zero = parse_completion_contract(
        {"completion_contract": {"criteria": [{"kind": "registered_download", "min_count": 0}]}}
    )
    assert zero[0].min_count == 1


_DERIVED_CONTRACT = {
    "schema_version": 1,
    "criteria": [{"id": "declared_download", "kind": "registered_download", "min_count": 1}],
}


def _wire_finalize(monkeypatch, *, contract, downloaded):
    """A WorkflowService with just enough wired to exercise the finalize status decision."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    service = WorkflowService()
    run = SimpleNamespace(
        workflow_run_id="wr_1",
        workflow_id="w_pinned",
        workflow_permanent_id="wpid_1",
        organization_id="o_1",
        status=WorkflowRunStatus.running,
    )
    statuses: list[WorkflowRunStatus] = []

    async def _update(workflow_run_id, status, **kwargs):
        statuses.append(status)
        return run

    async def _get_workflow(workflow_id, organization_id=None):
        definition = dict(_DEFINITION_BASE)
        if contract is not None:
            definition["completion_contract"] = contract
        return SimpleNamespace(workflow_definition=definition)

    monkeypatch.setattr(service, "_update_workflow_run_status_if_not_final", _update)
    monkeypatch.setattr(service, "get_workflow", _get_workflow)
    monkeypatch.setattr(
        service_module.app,
        "STORAGE",
        SimpleNamespace(get_downloaded_files=AsyncMock(return_value=list(downloaded))),
    )
    return service, run, statuses


_DEFINITION_BASE: dict = {"version": 2, "parameters": [], "blocks": []}


_DOWNLOAD_CODE = (
    'async with page.expect_download() as dl:\n    await page.locator("a").click()\nreturn {"downloaded_files": []}'
)


_DOWNLOAD_YAML = """title: Harbor bill
workflow_definition:
  version: 2
  parameters: []
  blocks:
    - block_type: code
      label: download_statement
      code: |
        async with page.expect_download(timeout=15000) as dl:
            await page.locator("#currentBill").click()
        download = await dl.value
        return {"downloaded_files": [{"file_name": download.suggested_filename}]}
"""

_PLAIN_YAML = """title: Plain
workflow_definition:
  version: 2
  parameters: []
  blocks:
    - block_type: code
      label: extract
      code: |
        return {"rows": []}
"""


@pytest.mark.asyncio
async def test_finalize_terminates_a_run_that_did_not_produce_its_declared_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline behavior: a run whose workflow declares a download and registered none must not
    finalize as completed."""
    service, run, statuses = _wire_finalize(monkeypatch, contract=_DERIVED_CONTRACT, downloaded=[])

    await service._finalize_workflow_run_status(
        workflow_run_id=run.workflow_run_id,
        workflow_run=run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
    )

    assert statuses == [WorkflowRunStatus.terminated]


@pytest.mark.asyncio
async def test_finalize_completes_a_run_that_produced_its_declared_file(monkeypatch: pytest.MonkeyPatch) -> None:
    service, run, statuses = _wire_finalize(monkeypatch, contract=_DERIVED_CONTRACT, downloaded=["invoice.pdf"])

    await service._finalize_workflow_run_status(
        workflow_run_id=run.workflow_run_id,
        workflow_run=run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
    )

    assert statuses == [WorkflowRunStatus.completed]


@pytest.mark.asyncio
async def test_finalize_leaves_a_contract_less_workflow_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    service, run, statuses = _wire_finalize(monkeypatch, contract=None, downloaded=[])

    await service._finalize_workflow_run_status(
        workflow_run_id=run.workflow_run_id,
        workflow_run=run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
    )

    assert statuses == [WorkflowRunStatus.completed]


@pytest.mark.asyncio
async def test_finalize_grades_the_version_the_run_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An edit mid-run must not judge this run by a contract it never executed."""
    service, run, statuses = _wire_finalize(monkeypatch, contract=None, downloaded=[])
    seen: list[str] = []

    async def _get_workflow(workflow_id: str, organization_id: str | None = None):
        seen.append(workflow_id)
        return SimpleNamespace(workflow_definition={})

    monkeypatch.setattr(service, "get_workflow", _get_workflow)
    await service._finalize_workflow_run_status(
        workflow_run_id=run.workflow_run_id,
        workflow_run=run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
    )

    assert seen == [run.workflow_id]


def test_contract_comes_from_the_request_not_the_code() -> None:
    """The obligation is what the user asked for, never the shape of the generated code."""
    from skyvern.forge.sdk.copilot.completion_verification import registered_download_completion_criterion
    from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion as RequestCriterion

    assert contract_from_request_criteria([registered_download_completion_criterion()]) is not None
    assert contract_from_request_criteria([RequestCriterion(id="c0", outcome="something else")]) is None
    assert contract_from_request_criteria([]) is None
    assert contract_from_request_criteria(None) is None


def test_requested_contract_round_trips_through_the_parser() -> None:
    from skyvern.forge.sdk.copilot.completion_verification import registered_download_completion_criterion

    contract = contract_from_request_criteria([registered_download_completion_criterion()])
    criteria = parse_completion_contract({"completion_contract": contract})
    assert [c.kind for c in criteria] == ["registered_download"]
    assert grade_completion_contract(criteria, registered_download_count=0).satisfied is False
    assert grade_completion_contract(criteria, registered_download_count=1).satisfied is True


@pytest.mark.asyncio
async def test_finalize_skips_grading_a_partial_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frontier run of a block subset was never asked to produce the whole deliverable."""
    service, run, statuses = _wire_finalize(monkeypatch, contract=_DERIVED_CONTRACT, downloaded=[])

    await service._finalize_workflow_run_status(
        workflow_run_id=run.workflow_run_id,
        workflow_run=run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
        is_partial_run=True,
    )

    assert statuses == [WorkflowRunStatus.completed]


def test_a_stored_contract_survives_a_write_that_does_not_carry_one() -> None:
    """Non-copilot save paths rebuild the definition through models that omit the field."""
    stored = {"completion_contract": _DERIVED_CONTRACT, "blocks": []}
    rebuilt = with_contract({"blocks": []}, carried_contract(stored))
    assert rebuilt["completion_contract"] == _DERIVED_CONTRACT


def test_an_incoming_contract_is_not_overwritten_by_the_carried_one() -> None:
    incoming = {"completion_contract": {"schema_version": 1, "criteria": []}, "blocks": []}
    rebuilt = with_contract(dict(incoming), carried_contract({"completion_contract": _DERIVED_CONTRACT}))
    assert rebuilt["completion_contract"] == incoming["completion_contract"]


def test_no_stored_contract_leaves_the_definition_untouched() -> None:
    assert "completion_contract" not in with_contract({"blocks": []}, carried_contract({"blocks": []}))


@pytest.mark.asyncio
async def test_finalize_counts_session_scoped_downloads_not_yet_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session downloads are tagged with the run id during cleanup, after this grade runs."""
    service, run, statuses = _wire_finalize(monkeypatch, contract=_DERIVED_CONTRACT, downloaded=[])
    monkeypatch.setattr(service, "_session_download_count", AsyncMock(return_value=1))

    await service._finalize_workflow_run_status(
        workflow_run_id=run.workflow_run_id,
        workflow_run=run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
    )

    assert statuses == [WorkflowRunStatus.completed]


@pytest.mark.asyncio
async def test_both_acceptance_paths_attach_the_same_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """The obligation must not depend on whether the user clicked Accept or had auto-accept on."""
    from skyvern.forge.sdk.copilot.completion_verification import registered_download_completion_criterion
    from skyvern.forge.sdk.routes import workflow_copilot as route

    snapshot = SimpleNamespace(active=SimpleNamespace(criteria=[registered_download_completion_criterion()]))
    monkeypatch.setattr(route, "_load_completion_criteria_snapshot", AsyncMock(return_value=snapshot))
    chat = SimpleNamespace(workflow_copilot_chat_id="wcc_1", workflow_permanent_id="wpid_1")

    for target in (
        SimpleNamespace(workflow_definition=SimpleNamespace(completion_contract=None)),  # manual accept
        SimpleNamespace(workflow_definition=SimpleNamespace(completion_contract=None)),  # auto accept
    ):
        await route._attach_requested_completion_contract(chat, target)
        assert target.workflow_definition.completion_contract["criteria"][0]["kind"] == "registered_download"


@pytest.mark.asyncio
async def test_no_active_criteria_attaches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.routes import workflow_copilot as route

    monkeypatch.setattr(
        route, "_load_completion_criteria_snapshot", AsyncMock(return_value=SimpleNamespace(active=None))
    )
    target = SimpleNamespace(workflow_definition=SimpleNamespace(completion_contract=None))
    await route._attach_requested_completion_contract(
        SimpleNamespace(workflow_copilot_chat_id="c", workflow_permanent_id="w"), target
    )
    assert target.workflow_definition.completion_contract is None


def test_apply_proposed_workflow_route_is_bound_to_the_route_handler() -> None:
    """A helper inserted between the decorator and its function silently rebinds the endpoint to the
    helper, and the route then 422s on the helper's arguments."""
    from skyvern.forge.sdk.routes.workflow_copilot import base_router

    routes = [r for r in base_router.routes if getattr(r, "path", "") == "/workflow/copilot/apply-proposed-workflow"]
    assert routes, "route not registered"
    assert routes[0].endpoint.__name__ == "workflow_copilot_apply_proposed_workflow"


def test_a_request_criterion_is_recognized_by_its_typed_deliverable_fields() -> None:
    """The persisted criterion carries deliverable_kind/output_path; the synthetic id is the
    copilot's separate internal marker, and keying on it alone misses every real request."""
    requested = SimpleNamespace(
        id="c0",
        outcome="the current electricity statement is downloaded as a PDF",
        deliverable_kind="registered_download",
        declared_deliverable_kind="registered_download",
        output_path="output.downloaded_files",
    )
    unrelated = SimpleNamespace(id="c1", outcome="a summary", deliverable_kind=None, output_path=None)

    assert contract_from_request_criteria([unrelated, requested]) is not None
    assert contract_from_request_criteria([unrelated]) is None


def test_output_path_alone_identifies_a_requested_download() -> None:
    by_path = SimpleNamespace(id="c0", deliverable_kind=None, output_path="output.downloaded_files")
    assert contract_from_request_criteria([by_path]) is not None


@pytest.mark.asyncio
async def test_auto_accept_uses_this_turns_criteria_not_the_stored_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-accept commits before the turn's criteria are persisted, so a turn that first mints a
    download criterion must still carry the contract."""
    from skyvern.forge.sdk.copilot.completion_verification import registered_download_completion_criterion
    from skyvern.forge.sdk.routes import workflow_copilot as route

    # The stored snapshot is pre-turn and has nothing; this turn's state holds the new criterion.
    monkeypatch.setattr(
        route, "_load_completion_criteria_snapshot", AsyncMock(return_value=SimpleNamespace(active=None))
    )
    agent_result = SimpleNamespace(
        completion_criteria_turn_state=SimpleNamespace(
            decision=SimpleNamespace(criteria=(registered_download_completion_criterion(),))
        )
    )
    target = SimpleNamespace(workflow_definition=SimpleNamespace(completion_contract=None))

    await route._attach_requested_completion_contract(
        SimpleNamespace(workflow_copilot_chat_id="c", workflow_permanent_id="w"), target, agent_result
    )

    assert target.workflow_definition.completion_contract is not None


@pytest.mark.asyncio
async def test_manual_accept_still_falls_back_to_the_stored_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Manual accept runs in a later request with no turn state, so the persisted set is the source."""
    from skyvern.forge.sdk.copilot.completion_verification import registered_download_completion_criterion
    from skyvern.forge.sdk.routes import workflow_copilot as route

    snapshot = SimpleNamespace(active=SimpleNamespace(criteria=[registered_download_completion_criterion()]))
    monkeypatch.setattr(route, "_load_completion_criteria_snapshot", AsyncMock(return_value=snapshot))
    target = SimpleNamespace(workflow_definition=SimpleNamespace(completion_contract=None))

    await route._attach_requested_completion_contract(
        SimpleNamespace(workflow_copilot_chat_id="c", workflow_permanent_id="w"), target
    )

    assert target.workflow_definition.completion_contract is not None
