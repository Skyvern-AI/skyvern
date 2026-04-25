"""Tests for cache invalidation when a workflow's parameter set changes (SKY-9254)."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.models.parameter import (
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from skyvern.schemas.scripts import Script
from skyvern.services.workflow_script_service import _invalidate_if_parameters_changed


def _workflow_param(key: str) -> WorkflowParameter:
    now = datetime.now(timezone.utc)
    return WorkflowParameter(
        key=key,
        description="",
        workflow_parameter_id=f"wp_{key}",
        workflow_parameter_type=WorkflowParameterType.STRING,
        workflow_id="w_current",
        created_at=now,
        modified_at=now,
    )


def _workflow(workflow_id: str, param_keys: list[str]) -> Workflow:
    now = datetime.now(timezone.utc)
    return Workflow(
        workflow_id=workflow_id,
        organization_id="o_test",
        title="Test",
        workflow_permanent_id="wpid_test",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(
            blocks=[],
            parameters=[_workflow_param(k) for k in param_keys],
        ),
        created_at=now,
        modified_at=now,
    )


def _script() -> Script:
    now = datetime.now(timezone.utc)
    return Script(
        script_id="s_test",
        script_revision_id="sr_test",
        organization_id="o_test",
        run_id="wr_old",
        version=1,
        created_at=now,
        modified_at=now,
    )


@pytest.mark.asyncio
async def test_does_not_invalidate_when_workflow_version_matches() -> None:
    """No DB work past the source lookup if the cached row was produced by the
    current workflow version — this is the hot path on every cache hit."""
    mock_db = MagicMock()
    mock_db.scripts.get_workflow_script_source_workflow_id = AsyncMock(return_value="w_current")
    mock_db.workflows.get_workflow = AsyncMock()

    with patch("skyvern.services.workflow_script_service.app") as mock_app:
        mock_app.DATABASE = mock_db
        result = await _invalidate_if_parameters_changed(
            workflow=_workflow("w_current", ["name"]),
            existing_script=_script(),
            cache_key_value="default:v2",
            workflow_run_id="wr_new",
        )

    assert result is False
    mock_db.workflows.get_workflow.assert_not_called()


@pytest.mark.asyncio
async def test_does_not_invalidate_when_source_workflow_id_missing() -> None:
    """Legacy rows without a workflow_id stored can't be diffed — keep serving."""
    mock_db = MagicMock()
    mock_db.scripts.get_workflow_script_source_workflow_id = AsyncMock(return_value=None)
    mock_db.workflows.get_workflow = AsyncMock()

    with patch("skyvern.services.workflow_script_service.app") as mock_app:
        mock_app.DATABASE = mock_db
        result = await _invalidate_if_parameters_changed(
            workflow=_workflow("w_current", ["name"]),
            existing_script=_script(),
            cache_key_value="default:v2",
            workflow_run_id="wr_new",
        )

    assert result is False
    mock_db.workflows.get_workflow.assert_not_called()


@pytest.mark.asyncio
async def test_invalidates_when_prior_workflow_hard_deleted() -> None:
    """If the old workflow row is gone, we can't verify param set — play it safe."""
    mock_db = MagicMock()
    mock_db.scripts.get_workflow_script_source_workflow_id = AsyncMock(return_value="w_old")
    mock_db.workflows.get_workflow = AsyncMock(return_value=None)

    with patch("skyvern.services.workflow_script_service.app") as mock_app:
        mock_app.DATABASE = mock_db
        result = await _invalidate_if_parameters_changed(
            workflow=_workflow("w_current", ["name"]),
            existing_script=_script(),
            cache_key_value="default:v2",
            workflow_run_id="wr_new",
        )

    assert result is True


@pytest.mark.asyncio
async def test_does_not_invalidate_when_param_keys_identical() -> None:
    """Cosmetic edits (title, description, webhook, proxy) bump workflow_id
    but don't change the parameter set — cache stays warm."""
    mock_db = MagicMock()
    mock_db.scripts.get_workflow_script_source_workflow_id = AsyncMock(return_value="w_old")
    mock_db.workflows.get_workflow = AsyncMock(return_value=_workflow("w_old", ["name", "email"]))

    with patch("skyvern.services.workflow_script_service.app") as mock_app:
        mock_app.DATABASE = mock_db
        result = await _invalidate_if_parameters_changed(
            workflow=_workflow("w_current", ["name", "email"]),
            existing_script=_script(),
            cache_key_value="default:v2",
            workflow_run_id="wr_new",
        )

    assert result is False


@pytest.mark.asyncio
async def test_invalidates_when_parameter_added() -> None:
    """This is the SKY-9254 case: a phone parameter was added post-cache."""
    mock_db = MagicMock()
    mock_db.scripts.get_workflow_script_source_workflow_id = AsyncMock(return_value="w_old")
    mock_db.workflows.get_workflow = AsyncMock(return_value=_workflow("w_old", ["name"]))

    with patch("skyvern.services.workflow_script_service.app") as mock_app:
        mock_app.DATABASE = mock_db
        result = await _invalidate_if_parameters_changed(
            workflow=_workflow("w_current", ["name", "phone"]),
            existing_script=_script(),
            cache_key_value="default:v2",
            workflow_run_id="wr_new",
        )

    assert result is True


@pytest.mark.asyncio
async def test_invalidates_when_parameter_removed() -> None:
    mock_db = MagicMock()
    mock_db.scripts.get_workflow_script_source_workflow_id = AsyncMock(return_value="w_old")
    mock_db.workflows.get_workflow = AsyncMock(return_value=_workflow("w_old", ["name", "phone"]))

    with patch("skyvern.services.workflow_script_service.app") as mock_app:
        mock_app.DATABASE = mock_db
        result = await _invalidate_if_parameters_changed(
            workflow=_workflow("w_current", ["name"]),
            existing_script=_script(),
            cache_key_value="default:v2",
            workflow_run_id="wr_new",
        )

    assert result is True


@pytest.mark.asyncio
async def test_invalidates_when_parameter_renamed() -> None:
    """Rename = remove old key + add new key. Both directions in the symmetric diff."""
    mock_db = MagicMock()
    mock_db.scripts.get_workflow_script_source_workflow_id = AsyncMock(return_value="w_old")
    mock_db.workflows.get_workflow = AsyncMock(return_value=_workflow("w_old", ["phone_number"]))

    with patch("skyvern.services.workflow_script_service.app") as mock_app:
        mock_app.DATABASE = mock_db
        result = await _invalidate_if_parameters_changed(
            workflow=_workflow("w_current", ["phone"]),
            existing_script=_script(),
            cache_key_value="default:v2",
            workflow_run_id="wr_new",
        )

    assert result is True
