from time import perf_counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.services import browser_session_artifacts as artifacts
from skyvern.forge.sdk.services.request_principal import AuthKind, RequestPrincipal
from skyvern.forge.sdk.workflow.service import WorkflowService


def test_denied_redaction_handles_signed_urls_and_file_info_by_shape() -> None:
    signed = [
        "https://files.example/a?X-Amz-Signature=sample",
        "https://files.example/a?X-Goog-Signature=sample",
        "https://files.example/a?sv=1&sig=sample",
        "https://files.example/a?expiry=1&kid=k&sig=sample",
        "https://files.example/a?AWSAccessKeyId=sample&Signature=sample",
    ]
    file_info = FileInfo(url="https://files.example/a", filename="a.pdf", artifact_id="art_1")
    original = {
        "values": signed + [str(file_info.model_dump()), "https://example.test/page", "ordinary"],
        "file": file_info,
        "nested": {"artifact_id": "art_1", "recording_url": "https://files.example/recording"},
    }
    redacted = artifacts.BrowserSessionArtifactAccess(False).redact_output(original)
    assert redacted["values"] == ["[redacted]"] * 6 + ["https://example.test/page", "ordinary"]
    assert redacted["file"].url == "[redacted]" and redacted["file"].artifact_id is None
    assert redacted["nested"] == {"artifact_id": None, "recording_url": None}
    assert artifacts.BrowserSessionArtifactAccess(True).redact_output(original) is original
    assert file_info.url == "https://files.example/a" and file_info.artifact_id == "art_1"

    plain_metadata = {"url": "https://example.test/start", "modified_at": "2026-01-01", "status": "completed"}
    assert artifacts.BrowserSessionArtifactAccess(False).redact_output(plain_metadata) == plain_metadata
    assert artifacts.BrowserSessionArtifactAccess(False).redact_output(str(plain_metadata)) == str(plain_metadata)
    legacy_file = FileInfo(url="https://files.example/legacy", filename="a.pdf").model_dump()
    assert artifacts.BrowserSessionArtifactAccess(False).redact_output(legacy_file)["url"] is None
    assert artifacts.BrowserSessionArtifactAccess(False).redact_output(str(legacy_file)) == "[redacted]"


@pytest.mark.parametrize("separator", ["", ","])
def test_denied_redaction_bounds_repeated_url_prefixes(separator: str) -> None:
    access = artifacts.BrowserSessionArtifactAccess(False)
    value = separator.join(["https://"] * 7_000) + "?signal=ordinary"
    started = perf_counter()
    assert access.redact_output(value) == value
    assert perf_counter() - started < 1.5

    signed = value + "&X-Amz-Signature=sample"
    assert access.redact_output(signed) == "[redacted]"
    oversized = "x" * (artifacts._MAX_REDACTION_SCAN_CHARS + 1) + "https://files.test/?sig=sample"
    assert access.redact_output(oversized) == "[redacted]"
    assert artifacts.BrowserSessionArtifactAccess(True).redact_output(oversized) == oversized
    assert access.redact_output("\u0130" * 40_000) == "[redacted]"


@pytest.mark.asyncio
async def test_default_scope_checks_request_principal_and_all_run_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    principal = RequestPrincipal("org", AuthKind.scoped_api_key, api_key_access_mode="scoped")
    authorize = AsyncMock(side_effect=lambda _principal, _org, session_id: session_id != "task_session")
    sessions = SimpleNamespace(
        get_persistent_browser_session_ids_by_runnable_ids=AsyncMock(return_value={"run_session", "task_session"})
    )
    database = SimpleNamespace(
        browser_sessions=sessions,
        workflow_runs=SimpleNamespace(
            get_workflow_run=AsyncMock(return_value=SimpleNamespace(browser_session_id="stored_run_session"))
        ),
    )
    monkeypatch.setattr(artifacts, "get_request_principal", lambda: principal)
    monkeypatch.setattr(
        artifacts,
        "app",
        SimpleNamespace(
            DATABASE=database,
            AGENT_FUNCTION=SimpleNamespace(
                should_check_browser_session_artifacts=AsyncMock(return_value=True),
                can_access_browser_session_artifacts=authorize,
            ),
        ),
    )

    access = await artifacts.browser_session_artifact_access(
        "org", session_ids=["stored_task_session"], workflow_run_id="run", runnable_ids=["task"]
    )
    original = {"nested": [{"downloaded_files": ["secret"], "task_screenshots": ["secret"], "value": 1}]}
    assert not access.allowed
    assert access.redact_output(original) == {
        "nested": [{"downloaded_files": None, "task_screenshots": None, "value": 1}]
    }
    assert original["nested"][0]["downloaded_files"] == ["secret"]
    assert all(call.args[:2] == (principal, "org") for call in authorize.await_args_list)
    sessions.get_persistent_browser_session_ids_by_runnable_ids.assert_awaited_once_with({"run", "task"}, "org")
    authorize.reset_mock()
    authorize.side_effect = None
    authorize.return_value = True
    access = await artifacts.browser_session_artifact_access(
        "org", session_ids=["stored_task_session"], workflow_run_id="run", runnable_ids=["task"]
    )
    assert access.allowed
    assert {call.args[2] for call in authorize.await_args_list} == {
        "stored_task_session",
        "stored_run_session",
        "run_session",
        "task_session",
    }


@pytest.mark.asyncio
async def test_system_scope_and_non_enforcing_fast_path_skip_session_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    principal = RequestPrincipal("org", AuthKind.bearer, user_id="member")
    lookup = AsyncMock()
    check = AsyncMock(return_value=False)
    monkeypatch.setattr(artifacts, "get_request_principal", lambda: principal)
    monkeypatch.setattr(
        artifacts,
        "app",
        SimpleNamespace(
            DATABASE=SimpleNamespace(
                workflow_runs=SimpleNamespace(get_workflow_run=lookup),
                browser_sessions=SimpleNamespace(get_persistent_browser_session_ids_by_runnable_ids=lookup),
            ),
            AGENT_FUNCTION=SimpleNamespace(should_check_browser_session_artifacts=check),
        ),
    )
    assert (await artifacts.browser_session_artifact_access("org", workflow_run_id="run")).allowed
    check.assert_awaited_once_with(principal, "org")
    with artifacts.browser_session_artifact_system_read():
        assert (await artifacts.browser_session_artifact_access("org", workflow_run_id="run")).allowed
    assert check.await_count == 1
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_absent_principal_does_not_implicitly_use_system_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    authorize = AsyncMock(return_value=False)
    monkeypatch.setattr(artifacts, "get_request_principal", lambda: None)
    monkeypatch.setattr(
        artifacts,
        "app",
        SimpleNamespace(
            AGENT_FUNCTION=SimpleNamespace(
                should_check_browser_session_artifacts=AsyncMock(return_value=True),
                can_access_browser_session_artifacts=authorize,
            )
        ),
    )
    access = await artifacts.browser_session_artifact_access("org", session_ids=["session"])
    assert not access.allowed
    authorize.assert_awaited_once_with(None, "org", "session")


@pytest.mark.asyncio
async def test_worker_webhook_uses_system_scope_but_api_cancel_keeps_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    principal = RequestPrincipal("org", AuthKind.bearer, user_id="member")
    scopes: list[tuple[RequestPrincipal | None] | None] = []

    async def prepare(*_args: object, **_kwargs: object) -> None:
        scopes.append(artifacts.browser_session_artifact_read_scope())

    service = WorkflowService()
    monkeypatch.setattr(service, "prepare_workflow_webhook", prepare)
    run = SimpleNamespace(status=SimpleNamespace(is_final=lambda: True), finished_at=None, workflow_run_id="run")
    with artifacts.browser_session_artifact_read(principal):
        await service.execute_workflow_webhook(run, claim_kind=None, finalizer="api_cancel")
        await service.execute_workflow_webhook(run, claim_kind=None, finalizer="worker_cleanup")
    assert scopes == [(principal,), None]
