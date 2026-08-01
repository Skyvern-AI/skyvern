from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.artifact import manager as artifact_manager_module
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.utils.secret_redaction import REDACTED_SECRET_PLACEHOLDER
from tests.unit.forge.sdk.artifact.storage.test_helpers import (
    TEST_ORGANIZATION_ID,
    create_fake_step,
    create_fake_workflow_run_block,
)

TEST_STEP_ID = "step_secret_redaction"


class _FakeStorage:
    def __init__(self) -> None:
        self.stored: list[tuple[Artifact, bytes]] = []

    def build_uri(self, **_: object) -> str:
        return "s3://bucket/artifact"

    def build_workflow_run_block_uri(self, **_: object) -> str:
        return "s3://bucket/workflow-run-block-artifact"

    async def store_artifact(self, artifact: Artifact, data: bytes) -> None:
        self.stored.append((artifact, data))


class _FakeArtifactDatabase:
    async def create_artifact(
        self,
        artifact_id: str,
        artifact_type: ArtifactType,
        uri: str,
        **kwargs: object,
    ) -> Artifact:
        now = datetime.now(UTC)
        return Artifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            uri=uri,
            organization_id=str(kwargs["organization_id"]),
            step_id=kwargs.get("step_id"),
            task_id=kwargs.get("task_id"),
            workflow_run_id=kwargs.get("workflow_run_id"),
            workflow_run_block_id=kwargs.get("workflow_run_block_id"),
            run_id=kwargs.get("run_id"),
            created_at=now,
            modified_at=now,
        )


@pytest.fixture
def artifact_redaction_setup(
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> _FakeStorage:
    storage = _FakeStorage()
    monkeypatch.setattr(artifact_manager_module.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    monkeypatch.setattr(artifact_manager_module.app, "STORAGE", storage)
    monkeypatch.setattr(
        artifact_manager_module.app,
        "DATABASE",
        SimpleNamespace(artifacts=_FakeArtifactDatabase()),
    )
    monkeypatch.setattr(
        artifact_manager_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        workflow_context_manager_factory(
            workflow_run_id="wr_redact",
            mask_secrets=False,
            secrets={"password": "secret-value"},
        ),
    )
    skyvern_context.reset()
    skyvern_context.set(SkyvernContext(organization_id=TEST_ORGANIZATION_ID, workflow_run_id="wr_redact"))
    yield storage
    skyvern_context.reset()


@pytest.mark.asyncio
async def test_create_artifact_redacts_textual_artifact_data(artifact_redaction_setup: _FakeStorage) -> None:
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)

    await manager.create_artifact(
        step=step,
        artifact_type=ArtifactType.HTML_SCRAPE,
        data=b"<html>secret-value</html>",
    )
    await manager.wait_for_upload_aiotasks([step.task_id])

    assert artifact_redaction_setup.stored[0][1] == f"<html>{REDACTED_SECRET_PLACEHOLDER}</html>".encode()


@pytest.mark.asyncio
async def test_create_artifact_redacts_hashed_href_map(artifact_redaction_setup: _FakeStorage) -> None:
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)

    await manager.create_artifact(
        step=step,
        artifact_type=ArtifactType.HASHED_HREF_MAP,
        data=b'{"h1": "https://example.test/callback?token=secret-value"}',
    )
    await manager.wait_for_upload_aiotasks([step.task_id])

    stored = artifact_redaction_setup.stored[0][1]
    assert b"secret-value" not in stored
    assert REDACTED_SECRET_PLACEHOLDER.encode() in stored


@pytest.mark.asyncio
async def test_create_artifact_redacts_using_artifact_workflow_run_id_without_context(
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    storage = _FakeStorage()
    monkeypatch.setattr(artifact_manager_module.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    monkeypatch.setattr(artifact_manager_module.app, "STORAGE", storage)
    monkeypatch.setattr(
        artifact_manager_module.app,
        "DATABASE",
        SimpleNamespace(artifacts=_FakeArtifactDatabase()),
    )
    monkeypatch.setattr(
        artifact_manager_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        workflow_context_manager_factory(
            workflow_run_id="wr_redact",
            mask_secrets=False,
            secrets={"password": "secret-value"},
        ),
    )
    skyvern_context.reset()
    assert skyvern_context.current() is None

    manager = ArtifactManager()
    workflow_run_block = create_fake_workflow_run_block("wr_redact", "wrb_redact")

    await manager.create_workflow_run_block_artifact(
        workflow_run_block=workflow_run_block,
        artifact_type=ArtifactType.HTML_SCRAPE,
        data=b"<html>secret-value</html>",
    )
    await manager.wait_for_upload_aiotasks([workflow_run_block.workflow_run_block_id])

    assert storage.stored[0][0].workflow_run_id == "wr_redact"
    assert storage.stored[0][1] == f"<html>{REDACTED_SECRET_PLACEHOLDER}</html>".encode()


@pytest.mark.asyncio
async def test_create_artifact_leaves_binary_artifact_data_unchanged(
    artifact_redaction_setup: _FakeStorage,
) -> None:
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)

    await manager.create_artifact(
        step=step,
        artifact_type=ArtifactType.SCREENSHOT_LLM,
        data=b"png secret-value bytes",
    )
    await manager.wait_for_upload_aiotasks([step.task_id])

    assert artifact_redaction_setup.stored[0][1] == b"png secret-value bytes"


@pytest.mark.asyncio
async def test_create_artifact_leaves_textual_artifact_data_unchanged_when_flag_disabled(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifact_manager_module.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", False)
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)

    await manager.create_artifact(
        step=step,
        artifact_type=ArtifactType.HTML_SCRAPE,
        data=b"<html>secret-value</html>",
    )
    await manager.wait_for_upload_aiotasks([step.task_id])

    assert artifact_redaction_setup.stored[0][1] == b"<html>secret-value</html>"


@pytest.mark.asyncio
async def test_create_artifact_redacts_har_structured_fields_with_empty_secret_set(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    monkeypatch.setattr(
        artifact_manager_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        workflow_context_manager_factory(workflow_run_id="wr_redact"),
    )
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)

    await manager.create_artifact(
        step=step,
        artifact_type=ArtifactType.HAR,
        data=b'{"log":{"entries":[{"request":{"headers":[{"name":"Authorization","value":"Bearer token"}]}}]}}',
    )
    await manager.wait_for_upload_aiotasks([step.task_id])

    assert REDACTED_SECRET_PLACEHOLDER.encode() in artifact_redaction_setup.stored[0][1]
    assert b"Bearer token" not in artifact_redaction_setup.stored[0][1]


@pytest.mark.asyncio
async def test_create_artifact_redacts_har_when_workflow_opted_out(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    monkeypatch.setattr(
        artifact_manager_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        workflow_context_manager_factory(workflow_run_id="wr_redact", mask_secrets=False),
    )
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)
    har_data = b'{"log":{"entries":[{"request":{"headers":[{"name":"Authorization","value":"Bearer token"}]}}]}}'

    await manager.create_artifact(step=step, artifact_type=ArtifactType.HAR, data=har_data)
    await manager.wait_for_upload_aiotasks([step.task_id])

    stored = artifact_redaction_setup.stored[0][1]
    assert REDACTED_SECRET_PLACEHOLDER.encode() in stored
    assert b"Bearer token" not in stored
