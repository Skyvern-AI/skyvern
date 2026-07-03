from __future__ import annotations

import random

import structlog
from sqlalchemy.exc import IntegrityError

from skyvern.forge import app

LOG = structlog.get_logger()

ROUND_ROBIN = "round_robin"
RANDOM = "random"
VALID_SELECTION_STRATEGIES = frozenset({ROUND_ROBIN, RANDOM})


def normalize_selection_strategy(selection_strategy: str | None) -> str:
    return selection_strategy or ROUND_ROBIN


async def select_credential_for_run(
    workflow_run_id: str,
    organization_id: str,
    workflow_permanent_id: str,
    parameter_key: str,
    credential_ids: list[str],
    selection_strategy: str | None,
) -> str:
    existing = await app.DATABASE.workflow_run_credential_selections.get_selection(
        workflow_run_id=workflow_run_id,
        parameter_key=parameter_key,
    )
    if existing:
        return existing

    strategy = normalize_selection_strategy(selection_strategy)
    try:
        if strategy == RANDOM:
            selected = await app.DATABASE.workflow_run_credential_selections.create_selection(
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                parameter_key=parameter_key,
                credential_id=random.choice(credential_ids),
            )
        else:
            selected = await app.DATABASE.workflow_run_credential_selections.create_round_robin_selection(
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                parameter_key=parameter_key,
                credential_ids=credential_ids,
            )
    except IntegrityError:
        existing_selection = await app.DATABASE.workflow_run_credential_selections.get_selection(
            workflow_run_id=workflow_run_id,
            parameter_key=parameter_key,
        )
        if not existing_selection:
            raise
        selected = existing_selection

    LOG.info(
        "Selected workflow run credential",
        workflow_run_id=workflow_run_id,
        parameter_key=parameter_key,
        credential_id=selected,
        strategy=strategy,
    )
    return selected
