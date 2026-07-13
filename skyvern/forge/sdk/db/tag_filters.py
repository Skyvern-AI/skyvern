from collections.abc import Sequence
from typing import Any, Protocol

from sqlalchemy import and_, select
from sqlalchemy.sql.elements import ColumnElement

from skyvern.forge.sdk.db.models import WorkflowRunTagEventModel, WorkflowTagEventModel
from skyvern.forge.sdk.workflow.models.tags import TagEventType


class _TagEventModel(Protocol):
    organization_id: Any
    key: Any
    value: Any
    event_type: Any
    superseded_at: Any


def _tag_event_entity_subqueries(
    *,
    event_model: type[_TagEventModel],
    entity_column: Any,
    tag_filter: Sequence[tuple[str | None, str | None]] | None,
    organization_id: str | None = None,
    soft_delete_column: Any | None = None,
) -> list[Any]:
    if not tag_filter:
        return []

    exact_values_by_key: dict[str, list[str]] = {}
    key_only_terms: list[str] = []
    value_only_terms: list[str] = []
    for key, value in tag_filter:
        if key is not None and value is not None:
            exact_values_by_key.setdefault(key, []).append(value)
        elif key is not None:
            key_only_terms.append(key)
        elif value is not None:
            value_only_terms.append(value)

    def _base_filters() -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [
            event_model.superseded_at.is_(None),
            event_model.event_type == TagEventType.SET.value,
        ]
        if soft_delete_column is not None:
            filters.append(soft_delete_column.is_(None))
        if organization_id is not None:
            filters.append(event_model.organization_id == organization_id)
        return filters

    def _subquery(*term_filters: ColumnElement[bool]) -> Any:
        return select(entity_column).where(and_(*_base_filters(), *term_filters)).scalar_subquery()

    subqueries: list[Any] = []
    for key, values in exact_values_by_key.items():
        subqueries.append(_subquery(event_model.key == key, event_model.value.in_(values)))
    for key in key_only_terms:
        subqueries.append(_subquery(event_model.key == key))
    for value in value_only_terms:
        subqueries.append(_subquery(event_model.value == value))
    return subqueries


def workflow_tag_wpid_subqueries(
    workflow_tag_filter: Sequence[tuple[str | None, str | None]] | None,
    organization_id: str | None = None,
) -> list[Any]:
    """One scalar ``workflow_permanent_id`` subquery per term from ``WorkflowTagEventModel``.

    Shared by the analytics summary filter and the workflows-list filter: exact ``(key, value)``
    (OR within a key), group-only ``(key, None)``, and label-only ``(None, value)``, AND-ed across
    distinct terms. Shape semantics are exercised in ``tests/unit/test_workflows_list_tag_filter.py``
    and ``tests/cloud/test_analytics_workflow_tag_filter.py``."""
    return _tag_event_entity_subqueries(
        event_model=WorkflowTagEventModel,
        entity_column=WorkflowTagEventModel.workflow_permanent_id,
        tag_filter=workflow_tag_filter,
        organization_id=organization_id,
        soft_delete_column=WorkflowTagEventModel.deleted_at,
    )


def run_tag_run_id_subqueries(
    run_tag_filter: Sequence[tuple[str | None, str | None]] | None,
    organization_id: str | None = None,
) -> list[Any]:
    """One scalar ``workflow_run_id`` subquery per term from ``WorkflowRunTagEventModel``."""
    return _tag_event_entity_subqueries(
        event_model=WorkflowRunTagEventModel,
        entity_column=WorkflowRunTagEventModel.workflow_run_id,
        tag_filter=run_tag_filter,
        organization_id=organization_id,
    )
