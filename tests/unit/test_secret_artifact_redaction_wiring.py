import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.workflow import service as workflow_service_module
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.utils.secret_redaction import REDACTED_SECRET_PLACEHOLDER

SECRET = "hunter2secret"
WORKFLOW_RUN_ID = "wr_secret_redaction"


def _har_bytes() -> bytes:
    return json.dumps(
        {
            "log": {
                "entries": [
                    {
                        "request": {
                            "url": f"https://example.test/path?token={SECRET}",
                            "headers": [
                                {"name": "Authorization", "value": f"Bearer {SECRET}"},
                                {"name": "X-Safe", "value": "safe"},
                            ],
                            "cookies": [{"name": "session", "value": SECRET}],
                        },
                        "response": {"headers": [], "cookies": []},
                    }
                ]
            }
        }
    ).encode()


def _fake_app(har_data: bytes = b"", browser_log: bytes = b"") -> SimpleNamespace:
    return SimpleNamespace(
        BROWSER_MANAGER=SimpleNamespace(
            get_har_data=AsyncMock(return_value=har_data),
            get_browser_console_log=AsyncMock(return_value=browser_log),
        ),
        ARTIFACT_MANAGER=SimpleNamespace(
            create_artifact=AsyncMock(return_value="artifact_1"),
            create_task_archive=AsyncMock(return_value="archive_1"),
        ),
        WORKFLOW_CONTEXT_MANAGER=SimpleNamespace(get_secret_values_for_run=Mock(return_value={SECRET})),
    )


def _workflow_objects() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    browser_state = SimpleNamespace(browser_context=None, browser_artifacts=SimpleNamespace(traces_dir=None))
    last_step = SimpleNamespace(step_id="step_1", task_id="task_1")
    workflow = SimpleNamespace(workflow_id="wf_1")
    workflow_run = SimpleNamespace(workflow_run_id=WORKFLOW_RUN_ID)
    return browser_state, last_step, workflow, workflow_run


@pytest.mark.asyncio
async def test_persist_har_data_redacts_secret_and_sensitive_header(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app = _fake_app(har_data=_har_bytes())
    monkeypatch.setattr(workflow_service_module, "app", fake_app)
    monkeypatch.setattr(workflow_service_module.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)

    browser_state, last_step, workflow, workflow_run = _workflow_objects()
    service = WorkflowService()

    await service.persist_har_data(browser_state, last_step, workflow, workflow_run)

    fake_app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run.assert_called_once_with(WORKFLOW_RUN_ID)
    fake_app.ARTIFACT_MANAGER.create_artifact.assert_awaited_once()
    artifact_kwargs = fake_app.ARTIFACT_MANAGER.create_artifact.await_args.kwargs
    stored_data = artifact_kwargs["data"]
    stored_har = json.loads(stored_data)
    request = stored_har["log"]["entries"][0]["request"]

    assert artifact_kwargs["artifact_type"] == ArtifactType.HAR
    assert request["headers"][0]["value"] == REDACTED_SECRET_PLACEHOLDER
    assert REDACTED_SECRET_PLACEHOLDER.encode() in stored_data
    assert SECRET.encode() not in stored_data


@pytest.mark.asyncio
async def test_persist_browser_console_log_redacts_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app = _fake_app(browser_log=f"console leaked {SECRET}".encode())
    monkeypatch.setattr(workflow_service_module, "app", fake_app)
    monkeypatch.setattr(workflow_service_module.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)

    browser_state, last_step, workflow, workflow_run = _workflow_objects()
    service = WorkflowService()

    await service.persist_browser_console_log(browser_state, last_step, workflow, workflow_run)

    fake_app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run.assert_called_once_with(WORKFLOW_RUN_ID)
    fake_app.ARTIFACT_MANAGER.create_artifact.assert_awaited_once()
    artifact_kwargs = fake_app.ARTIFACT_MANAGER.create_artifact.await_args.kwargs
    stored_data = artifact_kwargs["data"]

    assert artifact_kwargs["artifact_type"] == ArtifactType.BROWSER_CONSOLE_LOG
    assert REDACTED_SECRET_PLACEHOLDER.encode() in stored_data
    assert SECRET.encode() not in stored_data


@pytest.mark.asyncio
async def test_bundled_debug_artifacts_redact_har_and_console_log(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app = _fake_app(har_data=_har_bytes(), browser_log=f"console leaked {SECRET}".encode())
    monkeypatch.setattr(workflow_service_module, "app", fake_app)
    monkeypatch.setattr(workflow_service_module.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)

    browser_state, last_step, workflow, workflow_run = _workflow_objects()
    service = WorkflowService()

    await service._persist_debug_artifacts_bundled(browser_state, last_step, workflow, workflow_run)

    fake_app.ARTIFACT_MANAGER.create_task_archive.assert_awaited_once()
    entries = fake_app.ARTIFACT_MANAGER.create_task_archive.await_args.kwargs["entries"]
    _, har_data = entries["har.har"]
    _, browser_log = entries["browser_console.log"]

    assert REDACTED_SECRET_PLACEHOLDER.encode() in har_data
    assert REDACTED_SECRET_PLACEHOLDER.encode() in browser_log
    assert SECRET.encode() not in har_data
    assert SECRET.encode() not in browser_log
