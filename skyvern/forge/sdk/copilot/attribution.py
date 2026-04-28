from __future__ import annotations

from typing import Any

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.db._sentinels import _UNSET
from skyvern.forge.sdk.workflow.models.workflow import Workflow

LOG = structlog.get_logger()


def is_copilot_born_initial_write(workflow: Workflow | None) -> bool:
    if workflow is None:
        return False
    if workflow.created_by is not None:
        return False
    if workflow.version != 1:
        return False
    return len(workflow.workflow_definition.blocks) == 0


async def resolve_copilot_created_by_stamp(workflow_id: str, organization_id: str) -> Any:
    """Return ``"copilot"`` for a copilot-born initial write, ``_UNSET`` otherwise.

    ``_UNSET`` (not ``None``) so the repo's omit-vs-clear sentinel preserves prior values.
    """
    try:
        workflow = await app.WORKFLOW_SERVICE.get_workflow(
            workflow_id=workflow_id,
            organization_id=organization_id,
        )
    except Exception:
        LOG.warning(
            "Failed pre-update workflow read for copilot attribution; skipping created_by stamp",
            workflow_id=workflow_id,
            exc_info=True,
        )
        return _UNSET
    try:
        if is_copilot_born_initial_write(workflow):
            return "copilot"
    except Exception:
        LOG.warning(
            "is_copilot_born_initial_write raised; skipping created_by stamp",
            workflow_id=workflow_id,
            exc_info=True,
        )
    return _UNSET
