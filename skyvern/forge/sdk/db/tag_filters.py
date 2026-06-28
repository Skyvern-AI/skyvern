from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.sql.elements import ColumnElement

from skyvern.forge.sdk.db.models import WorkflowTagEventModel
from skyvern.forge.sdk.workflow.models.tags import TagEventType


def workflow_tag_wpid_subqueries(
    workflow_tag_filter: list[tuple[str | None, str | None]] | None,
    organization_id: str | None = None,
) -> list[Any]:
    """One scalar ``workflow_permanent_id`` subquery per term from ``WorkflowTagEventModel``.

    Shared by the analytics summary filter and the workflows-list filter: exact ``(key, value)``
    (OR within a key), group-only ``(key, None)``, and label-only ``(None, value)``, AND-ed across
    distinct terms. Shape semantics are exercised in ``tests/unit/test_workflows_list_tag_filter.py``
    and ``tests/cloud/test_analytics_workflow_tag_filter.py``."""
    if not workflow_tag_filter:
        return []

    exact_values_by_key: dict[str, list[str]] = {}
    key_only_terms: list[str] = []
    value_only_terms: list[str] = []
    for key, value in workflow_tag_filter:
        if key is not None and value is not None:
            exact_values_by_key.setdefault(key, []).append(value)
        elif key is not None:
            key_only_terms.append(key)
        elif value is not None:
            value_only_terms.append(value)

    def _base_filters() -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [
            WorkflowTagEventModel.deleted_at.is_(None),
            WorkflowTagEventModel.superseded_at.is_(None),
            WorkflowTagEventModel.event_type == TagEventType.SET.value,
        ]
        if organization_id is not None:
            filters.append(WorkflowTagEventModel.organization_id == organization_id)
        return filters

    def _subquery(*term_filters: ColumnElement[bool]) -> Any:
        return (
            select(WorkflowTagEventModel.workflow_permanent_id)
            .where(and_(*_base_filters(), *term_filters))
            .scalar_subquery()
        )

    subqueries: list[Any] = []
    for key, values in exact_values_by_key.items():
        subqueries.append(_subquery(WorkflowTagEventModel.key == key, WorkflowTagEventModel.value.in_(values)))
    for key in key_only_terms:
        subqueries.append(_subquery(WorkflowTagEventModel.key == key))
    for value in value_only_terms:
        subqueries.append(_subquery(WorkflowTagEventModel.value == value))
    return subqueries
