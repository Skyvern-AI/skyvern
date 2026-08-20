import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow import context_manager as context_manager_module
from skyvern.forge.sdk.workflow import service as workflow_service_module
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.utils.secret_redaction import REDACTED_SECRET_PLACEHOLDER

SECRET = "hunter2secret"
RUNTIME_SECRET = "runtime-otp-999111"
WORKFLOW_RUN_ID = "wr_secret_redaction"


def _har_bytes(secret: str = SECRET) -> bytes:
    return json.dumps(
        {
            "log": {
                "entries": [
                    {
                        "request": {
                            "url": f"https://example.test/path?token={secret}",
                            "headers": [
                                {"name": "Authorization", "value": f"Bearer {secret}"},
                                {"name": "X-Safe", "value": "safe"},
                            ],
                            "cookies": [{"name": "session", "value": secret}],
                        },
                        "response": {"headers": [], "cookies": []},
                    }
                ]
            }
        }
    ).encode()


def _fake_app(
    har_data: bytes = b"",
    browser_log: bytes = b"",
    redaction_enabled: bool = True,
    runtime_secret_values: set[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        BROWSER_MANAGER=SimpleNamespace(
            get_har_data=AsyncMock(return_value=har_data),
            get_browser_console_log=AsyncMock(return_value=browser_log),
        ),
        ARTIFACT_MANAGER=SimpleNamespace(
            create_artifact=AsyncMock(return_value="artifact_1"),
            create_task_archive=AsyncMock(return_value="archive_1"),
        ),
        WORKFLOW_CONTEXT_MANAGER=SimpleNamespace(
            secret_redaction_enabled_for_run=Mock(return_value=redaction_enabled),
            get_secret_values_for_run=Mock(return_value={SECRET}),
            runtime_secret_values_for_artifacts=Mock(return_value=set(runtime_secret_values or set())),
        ),
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

    browser_state, last_step, workflow, workflow_run = _workflow_objects()
    service = WorkflowService()

    await service.persist_har_data(browser_state, last_step, workflow, workflow_run)

    fake_app.WORKFLOW_CONTEXT_MANAGER.secret_redaction_enabled_for_run.assert_called_once_with(WORKFLOW_RUN_ID)
    fake_app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run.assert_called_once_with(WORKFLOW_RUN_ID)
    fake_app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts.assert_not_called()
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
async def test_persist_har_data_skips_all_redaction_when_run_not_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_har = _har_bytes()
    fake_app = _fake_app(har_data=original_har, redaction_enabled=False)
    monkeypatch.setattr(workflow_service_module, "app", fake_app)

    browser_state, last_step, workflow, workflow_run = _workflow_objects()
    service = WorkflowService()

    await service.persist_har_data(browser_state, last_step, workflow, workflow_run)

    fake_app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run.assert_not_called()
    fake_app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts.assert_called_once()
    artifact_kwargs = fake_app.ARTIFACT_MANAGER.create_artifact.await_args.kwargs
    stored_data = artifact_kwargs["data"]
    request = json.loads(stored_data)["log"]["entries"][0]["request"]

    assert stored_data == original_har
    assert request["headers"][0]["value"] == f"Bearer {SECRET}"


@pytest.mark.asyncio
async def test_persist_har_data_redacts_runtime_secret_when_not_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app = _fake_app(
        har_data=_har_bytes(RUNTIME_SECRET),
        redaction_enabled=False,
        runtime_secret_values={RUNTIME_SECRET},
    )
    monkeypatch.setattr(workflow_service_module, "app", fake_app)

    browser_state, last_step, workflow, workflow_run = _workflow_objects()
    service = WorkflowService()

    await service.persist_har_data(browser_state, last_step, workflow, workflow_run)

    fake_app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run.assert_not_called()
    fake_app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts.assert_called_once()
    artifact_kwargs = fake_app.ARTIFACT_MANAGER.create_artifact.await_args.kwargs
    stored_data = artifact_kwargs["data"]

    assert REDACTED_SECRET_PLACEHOLDER.encode() in stored_data
    assert RUNTIME_SECRET.encode() not in stored_data


@pytest.mark.asyncio
async def test_persist_browser_console_log_redacts_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app = _fake_app(browser_log=f"console leaked {SECRET}".encode())
    monkeypatch.setattr(workflow_service_module, "app", fake_app)

    browser_state, last_step, workflow, workflow_run = _workflow_objects()
    service = WorkflowService()

    await service.persist_browser_console_log(browser_state, last_step, workflow, workflow_run)

    fake_app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run.assert_called_once_with(WORKFLOW_RUN_ID)
    fake_app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts.assert_not_called()
    fake_app.ARTIFACT_MANAGER.create_artifact.assert_awaited_once()
    artifact_kwargs = fake_app.ARTIFACT_MANAGER.create_artifact.await_args.kwargs
    stored_data = artifact_kwargs["data"]

    assert artifact_kwargs["artifact_type"] == ArtifactType.BROWSER_CONSOLE_LOG
    assert REDACTED_SECRET_PLACEHOLDER.encode() in stored_data
    assert SECRET.encode() not in stored_data


@pytest.mark.asyncio
async def test_persist_browser_console_log_redacts_runtime_secret_when_not_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_app = _fake_app(
        browser_log=f"console leaked {RUNTIME_SECRET}".encode(),
        redaction_enabled=False,
        runtime_secret_values={RUNTIME_SECRET},
    )
    monkeypatch.setattr(workflow_service_module, "app", fake_app)

    browser_state, last_step, workflow, workflow_run = _workflow_objects()
    service = WorkflowService()

    await service.persist_browser_console_log(browser_state, last_step, workflow, workflow_run)

    fake_app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run.assert_not_called()
    fake_app.ARTIFACT_MANAGER.create_artifact.assert_awaited_once()
    artifact_kwargs = fake_app.ARTIFACT_MANAGER.create_artifact.await_args.kwargs
    stored_data = artifact_kwargs["data"]

    assert REDACTED_SECRET_PLACEHOLDER.encode() in stored_data
    assert RUNTIME_SECRET.encode() not in stored_data


@pytest.mark.asyncio
async def test_bundled_debug_artifacts_redact_har_and_console_log(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app = _fake_app(har_data=_har_bytes(), browser_log=f"console leaked {SECRET}".encode())
    monkeypatch.setattr(workflow_service_module, "app", fake_app)

    browser_state, last_step, workflow, workflow_run = _workflow_objects()
    service = WorkflowService()

    await service._persist_debug_artifacts_bundled(browser_state, last_step, workflow, workflow_run)

    fake_app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts.assert_not_called()
    fake_app.ARTIFACT_MANAGER.create_task_archive.assert_awaited_once()
    entries = fake_app.ARTIFACT_MANAGER.create_task_archive.await_args.kwargs["entries"]
    _, har_data = entries["har.har"]
    _, browser_log = entries["browser_console.log"]

    assert REDACTED_SECRET_PLACEHOLDER.encode() in har_data
    assert REDACTED_SECRET_PLACEHOLDER.encode() in browser_log
    assert SECRET.encode() not in har_data
    assert SECRET.encode() not in browser_log


@pytest.mark.asyncio
async def test_bundled_debug_artifacts_skip_redaction_when_not_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    original_har = _har_bytes()
    original_log = f"console leaked {SECRET}".encode()
    fake_app = _fake_app(har_data=original_har, browser_log=original_log, redaction_enabled=False)
    monkeypatch.setattr(workflow_service_module, "app", fake_app)

    browser_state, last_step, workflow, workflow_run = _workflow_objects()
    service = WorkflowService()

    await service._persist_debug_artifacts_bundled(browser_state, last_step, workflow, workflow_run)

    fake_app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run.assert_not_called()
    fake_app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts.assert_called_once()
    entries = fake_app.ARTIFACT_MANAGER.create_task_archive.await_args.kwargs["entries"]
    _, har_data = entries["har.har"]
    _, browser_log = entries["browser_console.log"]

    assert har_data == original_har
    assert browser_log == original_log


@pytest.mark.asyncio
async def test_bundled_debug_artifacts_redact_runtime_secret_when_not_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_app = _fake_app(
        har_data=_har_bytes(RUNTIME_SECRET),
        browser_log=f"console leaked {RUNTIME_SECRET}".encode(),
        redaction_enabled=False,
        runtime_secret_values={RUNTIME_SECRET},
    )
    monkeypatch.setattr(workflow_service_module, "app", fake_app)

    browser_state, last_step, workflow, workflow_run = _workflow_objects()
    service = WorkflowService()

    await service._persist_debug_artifacts_bundled(browser_state, last_step, workflow, workflow_run)

    fake_app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run.assert_not_called()
    entries = fake_app.ARTIFACT_MANAGER.create_task_archive.await_args.kwargs["entries"]
    _, har_data = entries["har.har"]
    _, browser_log = entries["browser_console.log"]

    assert REDACTED_SECRET_PLACEHOLDER.encode() in har_data
    assert REDACTED_SECRET_PLACEHOLDER.encode() in browser_log
    assert RUNTIME_SECRET.encode() not in har_data
    assert RUNTIME_SECRET.encode() not in browser_log


@pytest.mark.asyncio
async def test_runtime_secret_values_for_artifacts_respects_global_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    # The runtime-only floor is gated on the global kill switch alone (no per-run opt-in), mirroring
    # the bare-task principle in WorkflowContextManager.get_secret_values_for_run.
    monkeypatch.setattr(context_manager_module.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    context = SkyvernContext(organization_id="org_1")
    context.register_secret_value(RUNTIME_SECRET)
    skyvern_context.set(context)
    try:
        manager = WorkflowContextManager()
        assert manager.runtime_secret_values_for_artifacts() == {RUNTIME_SECRET}

        monkeypatch.setattr(context_manager_module.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", False)
        assert manager.runtime_secret_values_for_artifacts() == set()
    finally:
        skyvern_context.reset()

    monkeypatch.setattr(context_manager_module.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    assert skyvern_context.current() is None
    assert WorkflowContextManager().runtime_secret_values_for_artifacts() == set()


def test_task_artifact_gate_floors_runtime_secret_for_har_and_console(monkeypatch: pytest.MonkeyPatch) -> None:
    # Task-level HAR/console persistence goes through _maybe_redact_artifact_data; a mask-off
    # workflow must still have runtime-resolved secrets floored there (llm surfaces stay gated).
    from skyvern.forge.sdk.artifact import manager as artifact_manager_module

    fake_app = SimpleNamespace(
        WORKFLOW_CONTEXT_MANAGER=SimpleNamespace(
            artifact_redaction_enabled=Mock(return_value=False),
            get_secret_values_for_run=Mock(side_effect=AssertionError("gated set must not be consulted")),
            runtime_secret_values_for_artifacts=Mock(return_value={RUNTIME_SECRET}),
        )
    )
    monkeypatch.setattr(artifact_manager_module, "app", fake_app)
    skyvern_context.set(SkyvernContext(workflow_run_id=WORKFLOW_RUN_ID))
    try:
        har_out = artifact_manager_module._maybe_redact_artifact_data(
            ArtifactType.HAR, _har_bytes(RUNTIME_SECRET), WORKFLOW_RUN_ID
        )
        console_out = artifact_manager_module._maybe_redact_artifact_data(
            ArtifactType.BROWSER_CONSOLE_LOG, f"code={RUNTIME_SECRET}".encode(), WORKFLOW_RUN_ID
        )
        llm_out = artifact_manager_module._maybe_redact_artifact_data(
            ArtifactType.LLM_REQUEST, f"code={RUNTIME_SECRET}".encode(), WORKFLOW_RUN_ID
        )
    finally:
        skyvern_context.reset()
    assert RUNTIME_SECRET.encode() not in har_out
    assert RUNTIME_SECRET.encode() not in console_out
    assert llm_out == f"code={RUNTIME_SECRET}".encode()


def test_task_artifact_gate_untouched_when_no_runtime_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.artifact import manager as artifact_manager_module

    fake_app = SimpleNamespace(
        WORKFLOW_CONTEXT_MANAGER=SimpleNamespace(
            artifact_redaction_enabled=Mock(return_value=False),
            runtime_secret_values_for_artifacts=Mock(return_value=set()),
        )
    )
    monkeypatch.setattr(artifact_manager_module, "app", fake_app)
    payload = _har_bytes(SECRET)
    out = artifact_manager_module._maybe_redact_artifact_data(ArtifactType.HAR, payload, WORKFLOW_RUN_ID)
    assert out == payload
