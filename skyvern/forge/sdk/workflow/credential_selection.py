from __future__ import annotations

import random
from collections import Counter
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from skyvern.forge import app
from skyvern.forge.sdk.db.enums import BrowserSeedSource
from skyvern.forge.sdk.db.models import (
    CredentialModel,
    WorkflowRunCredentialSelectionModel,
    WorkflowRunModel,
)
from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter

if TYPE_CHECKING:
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition

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


async def clear_credential_selections_for_retry(
    workflow_run_id: str,
    organization_id: str,
    attempt_number: int,
    *,
    workflow_definition: WorkflowDefinition | None,
    session: AsyncSession | None = None,
    browser_address_replaced: bool = False,
) -> None:
    if attempt_number < 2 or workflow_definition is None:
        return
    if session is None:
        async with app.DATABASE.Session() as owned_session:
            await clear_credential_selections_for_retry(
                workflow_run_id,
                organization_id,
                attempt_number,
                workflow_definition=workflow_definition,
                session=owned_session,
                browser_address_replaced=browser_address_replaced,
            )
            await owned_session.commit()
        return
    selections = (
        await session.scalars(
            select(WorkflowRunCredentialSelectionModel).where(
                WorkflowRunCredentialSelectionModel.workflow_run_id == workflow_run_id,
                WorkflowRunCredentialSelectionModel.organization_id == organization_id,
            )
        )
    ).all()
    if not selections:
        return
    workflow_run = await session.scalar(
        select(WorkflowRunModel).where(
            WorkflowRunModel.workflow_run_id == workflow_run_id,
            WorkflowRunModel.organization_id == organization_id,
        )
    )
    if workflow_run is None:
        return
    # Atomic retry preparation has already restored the next attempt's session. Its cookies must
    # stay paired with the current credentials.
    if workflow_run.browser_session_id:
        LOG.info(
            "Skipped retry credential rotation because the browser session is retained",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            attempt_number=attempt_number,
            browser_session_id=workflow_run.browser_session_id,
        )
        return
    if workflow_run.browser_address and not browser_address_replaced:
        LOG.info(
            "Skipped retry credential rotation because browser_address is retained",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            attempt_number=attempt_number,
        )
        return
    parameter_key_counts = Counter(parameter.key for parameter in workflow_definition.parameters)
    pools = {
        parameter.key: parameter
        for parameter in workflow_definition.parameters
        if isinstance(parameter, CredentialParameter)
        and parameter.credential_ids
        and parameter_key_counts[parameter.key] == 1
    }
    candidate_ids = {selection.credential_id for selection in selections}
    for selection in selections:
        if parameter := pools.get(selection.parameter_key):
            candidate_ids.update({parameter.credential_id, *(parameter.credential_ids or [])})
    parallel_credential_ids = set(
        (
            await session.scalars(
                select(CredentialModel.credential_id).where(
                    CredentialModel.organization_id == organization_id,
                    CredentialModel.credential_id.in_(candidate_ids),
                    CredentialModel.run_sequentially.is_(False),
                    CredentialModel.deleted_at.is_(None),
                )
            )
        ).all()
    )
    # Only Temporal can admit a sequential credential. Rotate only verified parallel pools from
    # the execution definition; credential side tables can be stale after an in-place edit.
    rotating_selections = [
        selection
        for selection in selections
        if selection.credential_id != workflow_run.sequential_credential_id
        and selection.parameter_key in pools
        and {
            selection.credential_id,
            pools[selection.parameter_key].credential_id,
            *(pools[selection.parameter_key].credential_ids or []),
        }
        <= parallel_credential_ids
    ]
    if not rotating_selections:
        return
    # This column has no run FK: an attempt-qualified key frees the current binding's unique slot,
    # while the unchanged parameter key and timestamp keep the row visible to LRU history queries.
    credential_changed = False
    for selection in sorted(rotating_selections, key=lambda selection: selection.parameter_key):
        await session.execute(
            update(WorkflowRunCredentialSelectionModel)
            .where(WorkflowRunCredentialSelectionModel.selection_id == selection.selection_id)
            .values(workflow_run_id=f"{workflow_run_id}:attempt:{attempt_number - 1}")
        )
        parameter = pools[selection.parameter_key]
        credential_ids = parameter.credential_ids or []
        if normalize_selection_strategy(parameter.selection_strategy) == RANDOM:
            candidates = [credential_id for credential_id in credential_ids if credential_id != selection.credential_id]
            selected_credential_id = random.choice(candidates or credential_ids)
            session.add(
                WorkflowRunCredentialSelectionModel(
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    workflow_permanent_id=selection.workflow_permanent_id,
                    parameter_key=selection.parameter_key,
                    credential_id=selected_credential_id,
                )
            )
        else:
            selected_credential_id = (
                await app.DATABASE.workflow_run_credential_selections._create_round_robin_selection(
                    session,
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id,
                    workflow_permanent_id=selection.workflow_permanent_id,
                    parameter_key=selection.parameter_key,
                    credential_ids=credential_ids,
                )
            )
        credential_changed = credential_changed or selected_credential_id != selection.credential_id
    if credential_changed:
        # Own memory is also segmented by the selected credentials, including its write-back sink.
        await session.execute(
            update(WorkflowRunModel)
            .where(
                WorkflowRunModel.workflow_run_id == workflow_run_id,
                WorkflowRunModel.organization_id == organization_id,
                WorkflowRunModel.browser_seed_source.in_([BrowserSeedSource.credential, BrowserSeedSource.own_memory]),
            )
            .values(browser_profile_id=None, browser_seed_source=None, browser_sink_profile_id=None)
        )
