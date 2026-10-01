"""Bind a repair turn to the run it was opened about.

A turn that is asked to fix a failed run arrives knowing that run's id and nothing else. The
browser the run used is recorded on the run, not carried in the request, and a fresh turn holds
neither — so a tool asked to look at ``last_run`` had nothing to look at until the turn ran
something itself. That is the wrong moment: by the time a turn has run, it has usually already
written, and the point of looking is to inform the write.

The binding is taken from the run record the server owns, never from the request's own browser,
which is the chat's. A run that cannot be shown to belong to this workflow and organization, or
that recorded no browser, leaves the binding unset: an unavailable target is a fact the turn can
report, and quietly substituting the chat's browser would answer a question about one browser with
another one's contents.

Any run that passes the ownership checks, including an inherited Copilot test run, also supplies
its stored input values to the turn's test runs; they are never placed directly in model input or
logs, though a test run's own output can echo one like any run input.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

import structlog

from skyvern.constants import SCRUBBED_VALUE
from skyvern.exceptions import WorkflowRunNotFound
from skyvern.forge import app
from skyvern.forge.sdk.api.files import is_uploaded_file_id
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunOutputParameter,
    WorkflowRunParameter,
    WorkflowRunStatus,
)
from skyvern.schemas.proxy_location import ResolvedProxyLocationInput, runtime_proxy_location
from skyvern.services.uploaded_file_service import resolve_file_reference

LOG = structlog.get_logger()


class RepairOriginRefusal(StrEnum):
    NOT_REQUESTED = "not_requested"
    RUN_NOT_FOUND = "run_not_found"
    FOREIGN_ORGANIZATION = "foreign_organization"
    WORKFLOW_MISMATCH = "workflow_mismatch"
    NO_RECORDED_BROWSER = "no_recorded_browser"
    LOOKUP_FAILED = "lookup_failed"


# What a tool asked for ``last_run`` is told when the binding was refused. NOT_REQUESTED is absent
# on purpose: no run to bind at all is the resolver's own sentence, not a failure to explain.
_REFUSAL_SENTENCES: dict[RepairOriginRefusal, str] = {
    RepairOriginRefusal.RUN_NOT_FOUND: "The last run this chat recorded is no longer readable.",
    RepairOriginRefusal.FOREIGN_ORGANIZATION: "The last run this chat recorded belongs to another organization.",
    RepairOriginRefusal.WORKFLOW_MISMATCH: "The last run this chat recorded belongs to another workflow.",
    RepairOriginRefusal.NO_RECORDED_BROWSER: "The last run this chat recorded did not record a browser session.",
    RepairOriginRefusal.LOOKUP_FAILED: "The last run this chat recorded could not be looked up.",
}


class OriginOutputRefusal(StrEnum):
    FOREIGN_OR_MISMATCHED_ORIGIN = "foreign_or_mismatched_origin"
    ORIGIN_UNSETTLED = "origin_unsettled"
    ORDER_UNPROVABLE = "order_unprovable"
    UPSTREAM_ABSENT = "upstream_absent"
    UPSTREAM_FAILED = "upstream_failed"
    CHANGED_PRODUCER = "changed_producer"
    CHANGED_INPUT = "changed_input"
    CHANGED_EXECUTION_SETTINGS = "changed_execution_settings"
    OUTPUT_UNAVAILABLE = "output_unavailable"


OriginInputValue = bool | int | float | str | dict | list


_BINDING_REFUSAL_TO_OUTPUT_REFUSAL: dict[RepairOriginRefusal, OriginOutputRefusal] = {
    RepairOriginRefusal.RUN_NOT_FOUND: OriginOutputRefusal.FOREIGN_OR_MISMATCHED_ORIGIN,
    RepairOriginRefusal.FOREIGN_ORGANIZATION: OriginOutputRefusal.FOREIGN_OR_MISMATCHED_ORIGIN,
    RepairOriginRefusal.WORKFLOW_MISMATCH: OriginOutputRefusal.FOREIGN_OR_MISMATCHED_ORIGIN,
}


@dataclass(frozen=True, slots=True)
class OriginBlockOutput:
    status: str | None
    has_value: bool
    created_at: datetime
    value: dict | list | str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class OriginExecutionSettings:
    """The workflow-level settings a run executes under: the run's own override where it set one, else its version's."""

    proxy_location: ResolvedProxyLocationInput
    browser_profile_id: str | None
    browser_profile_key: str | None
    model: dict[str, Any] | None
    extra_http_headers: dict[str, str] | None = field(repr=False)

    @classmethod
    def of(cls, workflow: Workflow, run: WorkflowRun | None = None) -> OriginExecutionSettings:
        run_proxy = run.proxy_location if run is not None else None
        run_profile_id = run.browser_profile_id if run is not None else None
        run_headers = run.extra_http_headers if run is not None else None
        # Saving through Copilot turns absent headers into {}, which sends the same request.
        headers = (run_headers if run_headers is not None else workflow.extra_http_headers) or None
        return cls(
            proxy_location=runtime_proxy_location(run_proxy if run_proxy is not None else workflow.proxy_location),
            browser_profile_id=run_profile_id if run_profile_id is not None else workflow.browser_profile_id,
            browser_profile_key=workflow.browser_profile_key,
            model=workflow.model,
            extra_http_headers=headers,
        )


@dataclass(frozen=True, slots=True)
class OriginOutputSnapshot:
    """The origin run's latest top-level row per label, beside the definition version that run executed."""

    definition: WorkflowDefinition = field(repr=False)
    outputs: dict[str, OriginBlockOutput] = field(repr=False)
    # None when the run's settings were not read, which no test run's settings can be proven equal to.
    settings: OriginExecutionSettings | None = None
    # Every input value the run recorded, unfiltered: an output is only reusable with the inputs it was computed from.
    input_values: dict[str, OriginInputValue] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class OriginOutputRefusalDetail:
    reason: OriginOutputRefusal
    block_label: str
    origin_workflow_run_id: str | None
    parameter_key: str | None = None
    changed_label: str | None = None
    changed_settings: tuple[str, ...] = ()

    @property
    def output_key(self) -> str:
        return f"{self.block_label}_output"

    def as_payload(self) -> dict[str, str | list[str]]:
        payload: dict[str, str | list[str]] = {
            "reason": self.reason.value,
            "block_label": self.block_label,
            "output_key": self.output_key,
        }
        optional = {
            "origin_workflow_run_id": self.origin_workflow_run_id,
            "parameter_key": self.parameter_key,
            "changed_label": self.changed_label,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        if self.changed_settings:
            payload["changed_settings"] = list(self.changed_settings)
        return payload


def origin_block_outputs_from_rows(
    definition: WorkflowDefinition,
    run_blocks: Iterable[WorkflowRunBlock],
    output_parameter_rows: Iterable[WorkflowRunOutputParameter],
    input_values: dict[str, OriginInputValue] | None = None,
    settings: OriginExecutionSettings | None = None,
) -> OriginOutputSnapshot:
    """Values each block the way verified recording does: its registered output parameter first, even
    when that value is None, else the row's own output."""
    output_parameter_ids = {block.label: block.output_parameter.output_parameter_id for block in definition.blocks}
    registered_by_parameter_id = {
        row.output_parameter_id: row for row in sorted(output_parameter_rows, key=lambda row: row.created_at)
    }
    latest_rows: dict[str, WorkflowRunBlock] = {}
    for run_block in sorted(run_blocks, key=lambda run_block: run_block.created_at):
        label = run_block.label
        if run_block.parent_workflow_run_block_id is None and label is not None and label in output_parameter_ids:
            latest_rows[label] = run_block
    outputs: dict[str, OriginBlockOutput] = {}
    for label, run_block in latest_rows.items():
        registered = registered_by_parameter_id.get(output_parameter_ids[label])
        row = OriginBlockOutput(status=run_block.status, has_value=False, created_at=run_block.created_at)
        if registered is not None:
            row = replace(row, has_value=True, value=registered.value)
        elif run_block.output is not None:
            row = replace(row, has_value=True, value=run_block.output)
        outputs[label] = row
    return OriginOutputSnapshot(
        definition=definition,
        outputs=outputs,
        settings=settings,
        input_values=dict(input_values or {}),
    )


class RepairTurnContext(Protocol):
    """The part of a turn's context this binding reads and writes.

    The two it only reads are properties: a mutable protocol member is invariant, so a context
    holding a plain ``str`` would not satisfy a declared ``str | None``.
    """

    @property
    def organization_id(self) -> str: ...

    @property
    def workflow_permanent_id(self) -> str | None: ...

    last_run_blocks_workflow_run_id: str | None
    last_run_blocks_browser_session_id: str | None
    last_run_binding_unavailable_reason: str | None
    repair_origin_input_values: tuple[tuple[WorkflowParameter, WorkflowRunParameter], ...]
    repair_origin_is_copilot_run: bool
    repair_origin_outputs: OriginOutputSnapshot | OriginOutputRefusal | None
    repair_origin_outputs_run_id: str | None


@dataclass(frozen=True, slots=True)
class RepairOriginBinding:
    workflow_run_id: str | None
    browser_session_id: str | None
    refusal: RepairOriginRefusal | None
    status: WorkflowRunStatus | None = None
    copilot_run: bool = False
    debug_run: bool = False
    run: WorkflowRun | None = field(default=None, repr=False)

    @property
    def usable(self) -> bool:
        return self.workflow_run_id is not None and self.browser_session_id is not None

    @property
    def finished(self) -> bool:
        """The run is over, so its record is settled and safe to read. Whether it succeeded is a
        fact the packet carries and the model reads, not one this decides on the model's behalf."""
        return self.status is not None and self.status.is_final()


def _refused(
    reason: RepairOriginRefusal,
    status: WorkflowRunStatus | None = None,
    *,
    copilot_run: bool = False,
    debug_run: bool = False,
    run: WorkflowRun | None = None,
) -> RepairOriginBinding:
    return RepairOriginBinding(
        workflow_run_id=None,
        browser_session_id=None,
        refusal=reason,
        status=status,
        copilot_run=copilot_run,
        debug_run=debug_run,
        run=run,
    )


async def resolve_repair_origin_binding(
    *,
    workflow_run_id: str | None,
    organization_id: str,
    workflow_permanent_id: str | None,
) -> RepairOriginBinding:
    """The run a repair turn was opened about, and the browser that run actually used."""
    if not workflow_run_id:
        return _refused(RepairOriginRefusal.NOT_REQUESTED)

    try:
        run = await app.WORKFLOW_SERVICE.get_workflow_run(
            workflow_run_id=workflow_run_id, organization_id=organization_id
        )
    except WorkflowRunNotFound:
        return _refused(RepairOriginRefusal.RUN_NOT_FOUND)
    if run is None:
        return _refused(RepairOriginRefusal.RUN_NOT_FOUND)
    if run.organization_id != organization_id:
        return _refused(RepairOriginRefusal.FOREIGN_ORGANIZATION)
    # The field is required on the request, so an empty one is a mismatch rather than a reason to
    # skip the check: skipping would let any run in the organization bind its browser to this turn.
    if run.workflow_permanent_id != workflow_permanent_id:
        return _refused(RepairOriginRefusal.WORKFLOW_MISMATCH)
    copilot_run = run.copilot_session_id is not None
    debug_run = run.is_debug_session
    if not run.browser_session_id:
        return _refused(
            RepairOriginRefusal.NO_RECORDED_BROWSER,
            status=run.status,
            copilot_run=copilot_run,
            debug_run=debug_run,
            run=run,
        )

    return RepairOriginBinding(
        workflow_run_id=run.workflow_run_id,
        browser_session_id=run.browser_session_id,
        refusal=None,
        status=run.status,
        copilot_run=copilot_run,
        debug_run=debug_run,
        run=run,
    )


async def _file_id_is_reusable(run_parameter: WorkflowRunParameter, organization_id: str) -> bool:
    # A file attached to a run is deleted when that run ends, or later by the expiry sweep if that
    # delete failed, so only an unattached file that still resolves can outlive the test run.
    value = run_parameter.value
    if not isinstance(value, str) or not is_uploaded_file_id(value):
        return True
    file_id = value.strip()
    uploaded_file = await app.DATABASE.uploaded_files.get_uploaded_file(
        file_id=file_id, organization_id=organization_id
    )
    if uploaded_file is None or uploaded_file.run_id is not None:
        return False
    return await resolve_file_reference(file_id=file_id, organization_id=organization_id) is not None


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _load_origin_outputs(
    binding: RepairOriginBinding,
    *,
    workflow_run_id: str,
    organization_id: str,
    run_parameters: list[tuple[WorkflowParameter, WorkflowRunParameter]] | None,
) -> OriginOutputSnapshot | OriginOutputRefusal:
    if binding.refusal is not None and binding.refusal is not RepairOriginRefusal.NO_RECORDED_BROWSER:
        return _BINDING_REFUSAL_TO_OUTPUT_REFUSAL.get(binding.refusal, OriginOutputRefusal.OUTPUT_UNAVAILABLE)
    if not binding.finished:
        return OriginOutputRefusal.ORIGIN_UNSETTLED
    if binding.run is None or run_parameters is None:
        return OriginOutputRefusal.OUTPUT_UNAVAILABLE
    # The retention scrubber nulls output values in place and marks the run's failure reason and inputs,
    # so a scrubbed run's null outputs are not the values its blocks produced.
    if binding.run.failure_reason == SCRUBBED_VALUE or any(
        run_parameter.value == SCRUBBED_VALUE for _, run_parameter in run_parameters
    ):
        return OriginOutputRefusal.OUTPUT_UNAVAILABLE
    try:
        origin_workflow = await app.DATABASE.workflows.get_workflow(
            workflow_id=binding.run.workflow_id, organization_id=organization_id
        )
        # Auto-accept overwrites a version in place, so a row changed after the run started may no
        # longer be the definition the run executed.
        if origin_workflow is None or _as_utc(origin_workflow.modified_at) > _as_utc(binding.run.created_at):
            return OriginOutputRefusal.OUTPUT_UNAVAILABLE
        # A test of selected blocks never loads a cached script, so an output a script produced is not
        # what the test's agent run of the same block would produce. `script_run` records that a script
        # actually loaded, including runs a rollout upgraded to code without declaring run_with.
        if binding.run.script_run is not None:
            return OriginOutputRefusal.CHANGED_EXECUTION_SETTINGS
        # A child run's LLM blocks also render every ancestor's workflow_system_prompt, which a
        # standalone repair test never inherits.
        if binding.run.parent_workflow_run_id is not None:
            return OriginOutputRefusal.CHANGED_EXECUTION_SETTINGS
        run_blocks = await app.DATABASE.observer.get_workflow_run_blocks(
            workflow_run_id=workflow_run_id, organization_id=organization_id
        )
        output_parameter_rows = await app.DATABASE.workflow_runs.get_workflow_run_output_parameters(
            workflow_run_id=workflow_run_id
        )
        return origin_block_outputs_from_rows(
            origin_workflow.workflow_definition,
            run_blocks,
            output_parameter_rows,
            input_values={parameter.key: run_parameter.value for parameter, run_parameter in run_parameters},
            settings=OriginExecutionSettings.of(origin_workflow, binding.run),
        )
    except Exception as exc:
        # No exc_info: a row that fails to parse is quoted in its own exception message.
        LOG.warning(
            "copilot_repair_origin_outputs_unavailable",
            workflow_run_id=workflow_run_id,
            error_type=type(exc).__name__,
        )
        return OriginOutputRefusal.OUTPUT_UNAVAILABLE


async def seed_repair_origin_run(ctx: RepairTurnContext, *, workflow_run_id: str | None) -> RepairOriginBinding:
    """Seed the turn's last-run identity from the run it was opened about, before it acts.

    A test run inside the turn overwrites this the ordinary way, so a turn that re-runs is looking
    at what it just did rather than at what it inherited.
    """
    try:
        binding = await resolve_repair_origin_binding(
            workflow_run_id=workflow_run_id,
            organization_id=ctx.organization_id,
            workflow_permanent_id=ctx.workflow_permanent_id,
        )
    except Exception:
        # An inherited fact is worth less than the turn: an unreadable run leaves the binding unset
        # so the turn reports an unavailable target instead of failing before it starts.
        LOG.warning("copilot_repair_origin_lookup_failed", requested_workflow_run_id=workflow_run_id, exc_info=True)
        binding = _refused(RepairOriginRefusal.LOOKUP_FAILED)
    if binding.usable:
        ctx.last_run_blocks_workflow_run_id = binding.workflow_run_id
        ctx.last_run_blocks_browser_session_id = binding.browser_session_id
    ctx.last_run_binding_unavailable_reason = (
        _REFUSAL_SENTENCES.get(binding.refusal) if binding.refusal is not None else None
    )
    origin_input_values: tuple[tuple[WorkflowParameter, WorkflowRunParameter], ...] = ()
    owned_run = bool(workflow_run_id) and binding.refusal in (None, RepairOriginRefusal.NO_RECORDED_BROWSER)
    loaded: list[tuple[WorkflowParameter, WorkflowRunParameter]] | None = None
    if workflow_run_id and owned_run:
        try:
            loaded = await app.DATABASE.workflow_runs.get_workflow_run_parameters(workflow_run_id=workflow_run_id)
            origin_input_values = tuple(
                [pair for pair in loaded if await _file_id_is_reusable(pair[1], ctx.organization_id)]
            )
        except Exception as exc:
            # No exc_info: a value that fails to parse is quoted in its own exception message.
            LOG.warning(
                "copilot_repair_origin_input_values_unavailable",
                workflow_run_id=workflow_run_id,
                error_type=type(exc).__name__,
            )
    ctx.repair_origin_input_values = origin_input_values
    ctx.repair_origin_is_copilot_run = binding.copilot_run
    # A Copilot test run or a debugger block run may itself have been seeded, so neither is ever an
    # output origin and the turn plans exactly as with no origin.
    seeded_run = binding.copilot_run or binding.debug_run
    ctx.repair_origin_outputs_run_id = workflow_run_id if owned_run and not seeded_run else None
    ctx.repair_origin_outputs = (
        await _load_origin_outputs(
            binding,
            workflow_run_id=workflow_run_id,
            organization_id=ctx.organization_id,
            run_parameters=loaded,
        )
        if workflow_run_id and not seeded_run
        else None
    )
    origin_outputs = ctx.repair_origin_outputs
    LOG.info(
        "copilot_repair_origin_binding",
        requested_workflow_run_id=workflow_run_id,
        seeded=binding.usable,
        refusal=binding.refusal.value if binding.refusal else None,
        workflow_run_id=binding.workflow_run_id,
        origin_input_keys=[parameter.key for parameter, _ in origin_input_values],
        origin_is_copilot_run=binding.copilot_run,
        origin_output_labels=sorted(origin_outputs.outputs) if isinstance(origin_outputs, OriginOutputSnapshot) else [],
        origin_output_refusal=origin_outputs if isinstance(origin_outputs, OriginOutputRefusal) else None,
    )
    return binding
