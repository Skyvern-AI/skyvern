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
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import structlog

from skyvern.exceptions import WorkflowRunNotFound
from skyvern.forge import app
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus

LOG = structlog.get_logger()


class RepairOriginRefusal(StrEnum):
    NOT_REQUESTED = "not_requested"
    RUN_NOT_FOUND = "run_not_found"
    FOREIGN_ORGANIZATION = "foreign_organization"
    WORKFLOW_MISMATCH = "workflow_mismatch"
    NO_RECORDED_BROWSER = "no_recorded_browser"


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


@dataclass(frozen=True, slots=True)
class RepairOriginBinding:
    workflow_run_id: str | None
    browser_session_id: str | None
    refusal: RepairOriginRefusal | None
    status: WorkflowRunStatus | None = None

    @property
    def usable(self) -> bool:
        return self.workflow_run_id is not None and self.browser_session_id is not None

    @property
    def finished(self) -> bool:
        """The run is over, so its record is settled and safe to read. Whether it succeeded is a
        fact the packet carries and the model reads, not one this decides on the model's behalf."""
        return self.status is not None and self.status.is_final()


def _refused(reason: RepairOriginRefusal, status: WorkflowRunStatus | None = None) -> RepairOriginBinding:
    return RepairOriginBinding(workflow_run_id=None, browser_session_id=None, refusal=reason, status=status)


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
    if not run.browser_session_id:
        return _refused(RepairOriginRefusal.NO_RECORDED_BROWSER, status=run.status)

    return RepairOriginBinding(
        workflow_run_id=run.workflow_run_id,
        browser_session_id=run.browser_session_id,
        refusal=None,
        status=run.status,
    )


async def seed_repair_origin_run(ctx: RepairTurnContext, *, workflow_run_id: str | None) -> RepairOriginBinding:
    """Seed the turn's last-run identity from the run it was opened about, before it acts.

    A test run inside the turn overwrites this the ordinary way, so a turn that re-runs is looking
    at what it just did rather than at what it inherited.
    """
    binding = await resolve_repair_origin_binding(
        workflow_run_id=workflow_run_id,
        organization_id=ctx.organization_id,
        workflow_permanent_id=ctx.workflow_permanent_id,
    )
    if binding.usable:
        ctx.last_run_blocks_workflow_run_id = binding.workflow_run_id
        ctx.last_run_blocks_browser_session_id = binding.browser_session_id
    LOG.info(
        "copilot_repair_origin_binding",
        requested_workflow_run_id=workflow_run_id,
        seeded=binding.usable,
        refusal=binding.refusal.value if binding.refusal else None,
        workflow_run_id=binding.workflow_run_id,
    )
    return binding
