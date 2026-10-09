"""Runtime grading of a workflow's declared completion contract.

The contract lives on the workflow version and is graded from execution-layer evidence, so the
verdict does not depend on which engine ran the blocks or how generated code described its outcome.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.exceptions import WorkflowNotFound
from skyvern.forge.sdk.copilot.tools.workflow_update import CodeArtifactCompletionCriterion
from skyvern.forge.sdk.db.models import WorkflowRunAttemptModel
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow import service as service_module
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.runtime_completion import (
    CompletionCriterion,
    carried_contract,
    contract_from_code_artifact_metadata,
    grade_completion_contract,
    parse_completion_contract,
    with_contract,
)
from skyvern.forge.sdk.workflow.service import run_selection_is_partial
from skyvern.schemas.workflows import BlockType
from tests.unit.conftest import FakeWorkflowRunAttemptsRepository

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


def _downloaded_file(name: str, *, artifact_id: str | None = None):
    """What ``get_downloaded_files`` actually returns: a FileInfo, never a bare filename.

    A bare string makes the grader raise on ``.artifact_id``, and the raise is swallowed into an
    ungraded ``completed`` — a green that proves nothing about delivery."""
    return SimpleNamespace(artifact_id=artifact_id, filename=name, checksum=None, file_size=None)


def _wire_finalize(monkeypatch, *, contract, downloaded, run_blocks=(), attempts=None):
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
        SimpleNamespace(
            get_downloaded_files=AsyncMock(
                return_value=[
                    entry if hasattr(entry, "artifact_id") else _downloaded_file(entry, artifact_id=f"a_{index}")
                    for index, entry in enumerate(downloaded)
                ]
            )
        ),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.observer, "get_workflow_run_blocks", AsyncMock(return_value=list(run_blocks))
    )
    if attempts is not None:
        run.failure_reason = None
        run.started_at = attempts[-1].started_at
        run.finished_at = None
        monkeypatch.setattr(
            service_module.app.DATABASE, "workflow_run_attempts", FakeWorkflowRunAttemptsRepository(attempts)
        )
        monkeypatch.setattr(
            service_module.app.DATABASE.artifacts, "list_download_artifacts_for_attempt", AsyncMock(return_value=[])
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
async def test_a_run_that_delivered_fewer_files_than_declared_is_terminated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the grader actually ran rather than raising its way to `completed`.

    The neighbouring completes-on-delivery case cannot tell those apart: a grader that raises is
    swallowed and leaves the run completed, which is the same status a satisfied contract writes.
    A shortfall is only reachable when grading really executed."""
    contract = {
        "schema_version": 1,
        "criteria": [{"id": "declared_download", "kind": "registered_download", "min_count": 2}],
    }
    service, run, statuses = _wire_finalize(monkeypatch, contract=contract, downloaded=["invoice.pdf"])

    await service._finalize_workflow_run_status(
        workflow_run_id=run.workflow_run_id,
        workflow_run=run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
    )

    assert statuses == [WorkflowRunStatus.terminated]


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


@pytest.mark.asyncio
async def test_finalize_grades_the_version_execution_loaded_when_it_is_soft_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execution resolves the pinned version deleted-inclusive; grading must not re-read it through the
    active-only lookup and wave the contract through once that version has been soft-deleted."""
    service, run, statuses = _wire_finalize(monkeypatch, contract=None, downloaded=[])

    async def _soft_deleted(workflow_id: str, organization_id: str | None = None):
        raise WorkflowNotFound(workflow_id=workflow_id)

    monkeypatch.setattr(service, "get_workflow", _soft_deleted)
    executed = SimpleNamespace(workflow_definition={**_DEFINITION_BASE, "completion_contract": _DERIVED_CONTRACT})

    await service._finalize_workflow_run_status(
        workflow_run_id=run.workflow_run_id,
        workflow_run=run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
        workflow=executed,
    )

    assert statuses == [WorkflowRunStatus.terminated]


def test_download_contract_comes_from_model_declared_artifact_metadata() -> None:
    metadata = {
        "download_statement": {
            "completion_criteria": [
                {
                    "id": "deliver_statement",
                    "text": "The requested statement is delivered as a registered file.",
                    "deliverable_kind": "registered_download",
                }
            ]
        }
    }

    contract = contract_from_code_artifact_metadata(metadata)

    assert contract == {
        "schema_version": 1,
        "criteria": [{"id": "deliver_statement", "kind": "registered_download", "min_count": 1}],
    }


def test_ordinary_artifact_criterion_does_not_create_a_download_contract() -> None:
    metadata = {
        "extract_status": {
            "completion_criteria": [
                {
                    "id": "return_status",
                    "text": "The current status is returned.",
                    "output_path": "output.status",
                }
            ]
        }
    }

    assert contract_from_code_artifact_metadata(metadata) is None


@pytest.mark.parametrize(
    ("published", "unmet"),
    [(0, ("pdf", "xlsx", "zip")), (1, ("xlsx", "zip")), (2, ("zip",)), (3, ())],
)
def test_each_generated_promise_needs_its_own_published_file(published: int, unmet: tuple[str, ...]) -> None:
    contract = contract_from_code_artifact_metadata(
        {
            "build_report": {
                "completion_criteria": [
                    {"id": criterion_id, "deliverable_kind": "generated_file"}
                    for criterion_id in ("pdf", "xlsx", "zip")
                ]
            }
        }
    )

    verdict = grade_completion_contract(
        parse_completion_contract({"completion_contract": contract}),
        registered_download_count=0,
        generated_file_count=published,
    )

    assert (verdict.satisfied, verdict.unmet_criterion_ids) == (not unmet, unmet)


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


@pytest.mark.asyncio
async def test_finalize_grades_a_test_run_against_the_requested_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """A copilot test run executes a version the obligation has not been written onto yet, so a run
    that registered nothing must still not finalize as completed."""
    service, run, statuses = _wire_finalize(monkeypatch, contract=None, downloaded=[])

    await service._finalize_workflow_run_status(
        workflow_run_id=run.workflow_run_id,
        workflow_run=run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
        requested_completion_contract=_DERIVED_CONTRACT,
    )

    assert statuses == [WorkflowRunStatus.terminated]


@pytest.mark.asyncio
async def test_finalize_completes_a_test_run_that_produced_the_requested_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, run, statuses = _wire_finalize(monkeypatch, contract=None, downloaded=["invoice.pdf"])

    await service._finalize_workflow_run_status(
        workflow_run_id=run.workflow_run_id,
        workflow_run=run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
        requested_completion_contract=_DERIVED_CONTRACT,
    )

    assert statuses == [WorkflowRunStatus.completed]


@pytest.mark.asyncio
async def test_a_subset_run_stays_ungraded_even_with_a_requested_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    service, run, statuses = _wire_finalize(monkeypatch, contract=None, downloaded=[])

    await service._finalize_workflow_run_status(
        workflow_run_id=run.workflow_run_id,
        workflow_run=run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
        is_partial_run=True,
        requested_completion_contract=_DERIVED_CONTRACT,
    )

    assert statuses == [WorkflowRunStatus.completed]


def _workflow_with_blocks(*labels: str, finally_block_label: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_definition=SimpleNamespace(
            blocks=[SimpleNamespace(label=label) for label in labels],
            finally_block_label=finally_block_label,
        )
    )


def test_a_selection_naming_every_block_is_not_a_partial_run() -> None:
    workflow = _workflow_with_blocks("download_statement", "summarize")

    assert run_selection_is_partial(workflow, None) is False
    assert run_selection_is_partial(workflow, ["download_statement", "summarize"]) is False
    assert run_selection_is_partial(workflow, ["download_statement"]) is True


def test_the_finally_block_is_not_owed_by_a_full_selection() -> None:
    """execute_workflow runs the finally block on its own path, so a full selection never names it.

    Counting it as unrun would silently skip contract grading for every workflow that has one."""
    workflow = _workflow_with_blocks("download_statement", "summarize", "cleanup", finally_block_label="cleanup")

    assert run_selection_is_partial(workflow, ["download_statement", "summarize"]) is False
    assert run_selection_is_partial(workflow, ["download_statement"]) is True


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
    """The watcher tags a session download with its producing run when it observes the file, so a
    row can already exist for this run before any claim reaches it."""
    service, run, statuses = _wire_finalize(monkeypatch, contract=_DERIVED_CONTRACT, downloaded=[])
    monkeypatch.setattr(service, "_session_download_artifact_ids", AsyncMock(return_value={"a_1"}))

    await service._finalize_workflow_run_status(
        workflow_run_id=run.workflow_run_id,
        workflow_run=run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
    )

    assert statuses == [WorkflowRunStatus.completed]


@pytest.mark.asyncio
async def test_an_id_less_registered_file_and_a_session_row_are_counted_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without HMAC signing the run read lists the run's own storage prefix, which cannot contain a
    browser-session download, so the two reads report different files and both count."""
    contract = {
        "schema_version": 1,
        "criteria": [{"id": "declared_download", "kind": "registered_download", "min_count": 2}],
    }
    service, run, statuses = _wire_finalize(
        monkeypatch, contract=contract, downloaded=[FileInfo(url="s3://b/one.pdf", artifact_id=None)]
    )
    monkeypatch.setattr(service, "_session_download_artifact_ids", AsyncMock(return_value={"a_1"}))

    await service._finalize_workflow_run_status(
        workflow_run_id=run.workflow_run_id,
        workflow_run=run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
    )

    assert statuses == [WorkflowRunStatus.completed]


@pytest.mark.asyncio
async def test_one_file_reported_by_both_download_sources_does_not_satisfy_a_two_file_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session read and the run read overlap on the same run key, so a single stamped download
    appears in both. Counting it twice would pass a contract the run never met."""
    contract = {
        "schema_version": 1,
        "criteria": [{"id": "declared_download", "kind": "registered_download", "min_count": 2}],
    }
    service, run, statuses = _wire_finalize(
        monkeypatch, contract=contract, downloaded=[FileInfo(url="s3://b/one.pdf", artifact_id="a_1")]
    )
    monkeypatch.setattr(service, "_session_download_artifact_ids", AsyncMock(return_value={"a_1"}))

    await service._finalize_workflow_run_status(
        workflow_run_id=run.workflow_run_id,
        workflow_run=run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
    )

    assert statuses == [WorkflowRunStatus.terminated]


_T0 = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
_EARLIER_ATTEMPT_THEN_CURRENT = (
    (1, _T0 - timedelta(minutes=20), _T0 - timedelta(minutes=5)),
    (2, _T0 - timedelta(minutes=1), None),
)
_ONE_ATTEMPT = ((1, _T0 - timedelta(minutes=1), None),)


def _declared_contract(*kinds: str) -> dict[str, object] | None:
    """What the real producer carries when each kind is declared by its own block under one shared criterion id."""
    return contract_from_code_artifact_metadata(
        {
            f"block_{index}": {
                "completion_criteria": [
                    CodeArtifactCompletionCriterion.model_validate(
                        {"id": "capture", "deliverable_kind": kind}
                    ).model_dump(mode="json")
                ]
            }
            for index, kind in enumerate(kinds)
        }
    )


def _registered(artifact_id: str, *, minutes_from_t0: int = 0) -> FileInfo:
    return FileInfo(
        url=f"s3://b/{artifact_id}", artifact_id=artifact_id, modified_at=_T0 + timedelta(minutes=minutes_from_t0)
    )


def _block_row(output: dict[str, object], block_type: BlockType = BlockType.CODE) -> SimpleNamespace:
    return SimpleNamespace(block_type=block_type, output=output)


def _attempt_row(number: int, started_at: datetime, finished_at: datetime | None) -> WorkflowRunAttemptModel:
    return WorkflowRunAttemptModel(
        workflow_run_id="wr_1",
        attempt_number=number,
        organization_id="o_1",
        status="running",
        started_at=started_at,
        created_at=started_at,
        finished_at=finished_at,
    )


_STAMPED = {"generated_file_artifact_ids": ["a_gen"]}


@pytest.mark.parametrize(
    ("kinds", "downloaded", "run_blocks", "attempts", "expected_status", "unmet_promise"),
    [
        pytest.param(
            ("generated_file",),
            [_registered("a_gen")],
            [_block_row(_STAMPED)],
            _ONE_ATTEMPT,
            WorkflowRunStatus.completed,
            None,
            id="generated-promise-met-by-a-same-attempt-published-file",
        ),
        pytest.param(
            ("generated_file",),
            [_registered("a_gen")],
            [_block_row(_STAMPED)],
            _EARLIER_ATTEMPT_THEN_CURRENT,
            WorkflowRunStatus.completed,
            None,
            id="generated-promise-met-by-a-current-attempt-published-file-after-a-retry",
        ),
        pytest.param(
            ("generated_file",),
            [],
            [],
            None,
            WorkflowRunStatus.terminated,
            "generate",
            id="generated-promise-with-nothing-registered",
        ),
        pytest.param(
            ("generated_file",),
            [_registered("a_site")],
            [_block_row({"rows": 3, "downloaded_file_artifact_ids": ["a_site"]})],
            _ONE_ATTEMPT,
            WorkflowRunStatus.terminated,
            "generate",
            id="generated-promise-not-met-by-a-site-download-bound-to-an-in-process-code-row",
        ),
        pytest.param(
            ("generated_file",),
            [_registered("a_gen")],
            [],
            _ONE_ATTEMPT,
            WorkflowRunStatus.terminated,
            "generate",
            id="generated-promise-not-met-by-an-unstamped-site-download",
        ),
        pytest.param(
            ("generated_file",),
            [_registered("a_gen")],
            [_block_row(_STAMPED, BlockType.TEXT_PROMPT)],
            _ONE_ATTEMPT,
            WorkflowRunStatus.terminated,
            "generate",
            id="generated-promise-not-met-by-a-stamp-on-a-non-code-row",
        ),
        pytest.param(
            ("generated_file",),
            [_registered("a_gen", minutes_from_t0=-10)],
            [_block_row(_STAMPED)],
            _EARLIER_ATTEMPT_THEN_CURRENT,
            WorkflowRunStatus.terminated,
            "generate",
            id="generated-promise-not-met-by-an-earlier-attempt-published-file",
        ),
        pytest.param(
            ("registered_download",),
            [_registered("a_gen")],
            [_block_row(_STAMPED)],
            _ONE_ATTEMPT,
            WorkflowRunStatus.terminated,
            "download",
            id="download-promise-not-met-by-a-published-file",
        ),
        pytest.param(
            ("registered_download",),
            [_registered("a_site", minutes_from_t0=-10)],
            [],
            _EARLIER_ATTEMPT_THEN_CURRENT,
            WorkflowRunStatus.terminated,
            "download",
            id="download-promise-not-met-by-an-earlier-attempt-download",
        ),
        pytest.param(
            ("registered_download",),
            [_registered("a_site")],
            [],
            _EARLIER_ATTEMPT_THEN_CURRENT,
            WorkflowRunStatus.completed,
            None,
            id="download-promise-met-by-a-current-attempt-download",
        ),
        pytest.param(
            ("registered_download",),
            [],
            [_block_row({"downloaded_files": [{"file_name": "statement.pdf"}]})],
            None,
            WorkflowRunStatus.terminated,
            "download",
            id="download-promise-not-met-by-an-authored-filename",
        ),
        pytest.param(
            ("registered_download",),
            [],
            [_block_row({"downloaded_file_urls": ["https://files.example/statement.pdf"]})],
            None,
            WorkflowRunStatus.terminated,
            "download",
            id="download-promise-not-met-by-an-authored-url",
        ),
        pytest.param(
            ("registered_download", "generated_file"),
            [_registered("a_site")],
            [],
            _ONE_ATTEMPT,
            WorkflowRunStatus.terminated,
            "generate",
            id="both-promises-declared-and-only-a-site-download-registered",
        ),
    ],
)
@pytest.mark.asyncio
async def test_each_declared_deliverable_kind_is_met_only_by_its_own_registered_evidence(
    monkeypatch: pytest.MonkeyPatch,
    kinds: tuple[str, ...],
    downloaded: list[FileInfo],
    run_blocks: list[SimpleNamespace],
    attempts: tuple[tuple[int, datetime, datetime | None], ...] | None,
    expected_status: WorkflowRunStatus,
    unmet_promise: str | None,
) -> None:
    contract = _declared_contract(*kinds)
    assert contract is not None
    assert [criterion["kind"] for criterion in contract["criteria"]] == list(kinds)
    service, run, statuses = _wire_finalize(
        monkeypatch,
        contract=contract,
        downloaded=downloaded,
        run_blocks=run_blocks,
        attempts=None if attempts is None else [_attempt_row(*attempt) for attempt in attempts],
    )

    # `completed` is also what a swallowed grading error writes, so the verdict is read directly too.
    verdict = await service._grade_completion_contract(run)
    await service._finalize_workflow_run_status(
        workflow_run_id=run.workflow_run_id,
        workflow_run=run,
        pre_finally_status=WorkflowRunStatus.running,
        pre_finally_failure_reason=None,
    )

    assert verdict is not None
    assert verdict.satisfied is (unmet_promise is None)
    assert unmet_promise is None or (verdict.reason is not None and verdict.reason.endswith(f"to {unmet_promise}."))
    assert statuses == [expected_status]


def test_interactive_copilot_routes_do_not_own_completion_contract_lifecycle() -> None:
    from skyvern.forge.sdk.routes import workflow_copilot as route

    assert not hasattr(route, "_load_completion_criteria_snapshot")
    assert not hasattr(route, "_persist_completion_criteria_state")
    assert not hasattr(route, "_turn_completion_criteria")
    assert not hasattr(route, "_attach_requested_completion_contract")


def test_apply_proposed_workflow_route_is_bound_to_the_route_handler() -> None:
    """A helper inserted between the decorator and its function silently rebinds the endpoint to the
    helper, and the route then 422s on the helper's arguments."""
    from skyvern.forge.sdk.routes.workflow_copilot import base_router

    routes = [r for r in base_router.routes if getattr(r, "path", "") == "/workflow/copilot/apply-proposed-workflow"]
    assert routes, "route not registered"
    assert routes[0].endpoint.__name__ == "workflow_copilot_apply_proposed_workflow"
