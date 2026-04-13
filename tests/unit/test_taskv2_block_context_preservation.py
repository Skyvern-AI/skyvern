from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.experimentation import providers as providers_module
from skyvern.services import task_v2_service


class CaptureLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.records.append(("info", event, kwargs))

    def debug(self, event: str, **kwargs: Any) -> None:
        self.records.append(("debug", event, kwargs))


@pytest.fixture(autouse=True)
def reset_context() -> None:
    skyvern_context.reset()
    yield
    skyvern_context.reset()


def test_scoped_child_context_preserves_parent_loop_internal_state():
    """Nested child scopes should not clobber the parent's loop state."""
    original_state = {"downloaded_file_signatures_before_iteration": [("a.pdf", "abc", "https://files/a.pdf")]}
    original_context = SkyvernContext(
        organization_id="org_1",
        workflow_run_id="wr_1",
        run_id="wr_1",
        loop_internal_state=original_state,
    )
    skyvern_context.set(original_context)

    with skyvern_context.scoped(
        SkyvernContext(
            organization_id="org_child",
            workflow_run_id="wr_child",
            workflow_permanent_id="wfp_child",
            task_v2_id="tsk_v2_child",
            run_id="wr_1",
        )
    ):
        current_context = skyvern_context.ensure_context()
        assert current_context.workflow_run_id == "wr_child"
        assert current_context.loop_internal_state is None

    result_context = skyvern_context.current()
    assert result_context.loop_internal_state is not None
    assert result_context.loop_internal_state == original_state


def test_scoped_child_context_restores_parent_and_flushes_child_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = CaptureLogger()
    monkeypatch.setattr(skyvern_context, "LOG", logger)

    parent_context = SkyvernContext(
        organization_id="org_parent",
        workflow_run_id="wr_parent",
        workflow_permanent_id="wfp_parent",
        step_id="step_parent",
        script_id="script_parent",
        run_id="wr_parent",
    )
    skyvern_context.set(parent_context)

    child_context = SkyvernContext(
        organization_id="org_child",
        workflow_run_id="wr_child",
        workflow_permanent_id="wfp_child",
        task_v2_id="tsk_v2_child",
        run_id="wr_parent",
    )

    with skyvern_context.scoped(child_context):
        current_context = skyvern_context.ensure_context()
        assert current_context.workflow_run_id == "wr_child"
        assert current_context.step_id is None
        assert current_context.script_id is None

        providers_module.record_feature_flag_resolution(
            feature_name="TEST_FLAG",
            resolution_kind="enabled",
            resolved_value=True,
        )

    restored_context = skyvern_context.ensure_context()
    assert restored_context is parent_context
    assert restored_context.workflow_run_id == "wr_parent"
    assert restored_context.workflow_permanent_id == "wfp_parent"
    assert restored_context.step_id == "step_parent"
    assert restored_context.script_id == "script_parent"

    assert logger.records == [
        (
            "info",
            "workflow_feature_flags",
            {
                "organization_id": "org_child",
                "workflow_run_id": "wr_child",
                "workflow_permanent_id": "wfp_child",
                "task_v2_id": "tsk_v2_child",
                "feature_resolutions": {"TEST_FLAG": True},
                "service_name": settings.OTEL_SERVICE_NAME,
            },
        )
    ]


def test_scoped_child_summary_does_not_fragment_parent_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = CaptureLogger()
    monkeypatch.setattr(skyvern_context, "LOG", logger)

    parent_context = SkyvernContext(
        organization_id="org_parent",
        workflow_run_id="wr_parent",
        workflow_permanent_id="wfp_parent",
        run_id="wr_parent",
    )
    skyvern_context.set(parent_context)
    providers_module.record_feature_flag_resolution(
        feature_name="PARENT_BEFORE",
        resolution_kind="enabled",
        resolved_value=True,
    )

    with skyvern_context.scoped(
        SkyvernContext(
            organization_id="org_child",
            workflow_run_id="wr_child",
            workflow_permanent_id="wfp_child",
            task_v2_id="tsk_v2_child",
            run_id="wr_parent",
        )
    ):
        providers_module.record_feature_flag_resolution(
            feature_name="CHILD_FLAG",
            resolution_kind="enabled",
            resolved_value=False,
        )

    providers_module.record_feature_flag_resolution(
        feature_name="PARENT_AFTER",
        resolution_kind="enabled",
        resolved_value=False,
    )
    skyvern_context.reset()

    assert logger.records == [
        (
            "info",
            "workflow_feature_flags",
            {
                "organization_id": "org_child",
                "workflow_run_id": "wr_child",
                "workflow_permanent_id": "wfp_child",
                "task_v2_id": "tsk_v2_child",
                "feature_resolutions": {"CHILD_FLAG": False},
                "service_name": settings.OTEL_SERVICE_NAME,
            },
        ),
        (
            "info",
            "workflow_feature_flags",
            {
                "organization_id": "org_parent",
                "workflow_run_id": "wr_parent",
                "workflow_permanent_id": "wfp_parent",
                "feature_resolutions": {
                    "PARENT_AFTER": False,
                    "PARENT_BEFORE": True,
                },
                "service_name": settings.OTEL_SERVICE_NAME,
            },
        ),
    ]


def test_replace_flushes_existing_context_before_overwrite(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = CaptureLogger()
    monkeypatch.setattr(skyvern_context, "LOG", logger)

    original_context = SkyvernContext(
        organization_id="org_original",
        workflow_run_id="wr_original",
        workflow_permanent_id="wfp_original",
        run_id="wr_original",
    )
    skyvern_context.set(original_context)
    providers_module.record_feature_flag_resolution(
        feature_name="ORIGINAL_FLAG",
        resolution_kind="value",
        resolved_value="variant-a",
    )

    replacement_context = SkyvernContext(
        organization_id="org_replacement",
        workflow_run_id="wr_replacement",
        workflow_permanent_id="wfp_replacement",
        run_id="wr_replacement",
    )
    skyvern_context.replace(replacement_context)

    assert logger.records == [
        (
            "info",
            "workflow_feature_flags",
            {
                "organization_id": "org_original",
                "workflow_run_id": "wr_original",
                "workflow_permanent_id": "wfp_original",
                "feature_resolutions": {"ORIGINAL_FLAG": "variant-a"},
                "service_name": settings.OTEL_SERVICE_NAME,
            },
        )
    ]
    assert skyvern_context.current() is replacement_context


@pytest.mark.asyncio
async def test_run_task_v2_copies_parent_loop_state_into_child_context(monkeypatch: pytest.MonkeyPatch) -> None:
    loop_state = {"downloaded_file_signatures_before_iteration": [("a.pdf", "abc", "https://files/a.pdf")]}
    parent_context = SkyvernContext(
        organization_id="org_parent",
        organization_name="Parent Org",
        workflow_run_id="wr_parent",
        root_workflow_run_id="wr_root",
        run_id="wr_parent",
        loop_internal_state=loop_state,
    )
    skyvern_context.set(parent_context)

    task_v2 = SimpleNamespace(
        observer_cruise_id="tsk_v2_child",
        workflow_id="wf_child",
        workflow_run_id="wr_child",
    )
    captured_contexts: list[SkyvernContext] = []

    async def fake_run_task_v2_helper(**_: Any) -> tuple[object, object, object]:
        current_context = skyvern_context.ensure_context()
        captured_contexts.append(current_context)
        return (
            SimpleNamespace(workflow_id="wf_child"),
            SimpleNamespace(parent_workflow_run_id=None, browser_address=None),
            task_v2,
        )

    monkeypatch.setattr(task_v2_service, "run_task_v2_helper", fake_run_task_v2_helper)

    with patch("skyvern.services.task_v2_service.app") as mock_app:
        mock_app.DATABASE.observer.get_task_v2 = AsyncMock(return_value=task_v2)
        mock_app.WORKFLOW_SERVICE.clean_up_workflow = AsyncMock()

        result = await task_v2_service.run_task_v2(
            organization=SimpleNamespace(organization_id="org_parent", organization_name="Parent Org"),
            task_v2_id="tsk_v2_child",
        )

    assert result is task_v2
    assert len(captured_contexts) == 1
    child_context = captured_contexts[0]
    assert child_context.run_id == "wr_parent"
    assert child_context.root_workflow_run_id == "wr_root"
    assert child_context.loop_internal_state == loop_state
    assert child_context.loop_internal_state is not loop_state
    assert skyvern_context.current() is parent_context
