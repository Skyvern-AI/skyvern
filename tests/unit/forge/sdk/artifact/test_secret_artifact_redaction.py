import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.artifact import manager as artifact_manager_module
from skyvern.forge.sdk.artifact.manager import ArtifactBatchData, ArtifactManager, BulkArtifactCreationRequest
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.models import ArtifactModel
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.utils.secret_redaction import REDACTED_SECRET_PLACEHOLDER, REDACTION_FAILED_STUB
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

    async def store_artifact_from_path(self, artifact: Artifact, path: str) -> None:
        self.stored.append((artifact, Path(path).read_bytes()))


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

    async def bulk_create_artifacts(self, artifact_models: list[ArtifactModel]) -> list[Artifact]:
        return [
            await self.create_artifact(
                model.artifact_id,
                ArtifactType(model.artifact_type),
                model.uri,
                organization_id=model.organization_id,
                task_id=model.task_id,
                step_id=model.step_id,
                workflow_run_id=model.workflow_run_id,
            )
            for model in artifact_models
        ]


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
            mask_secrets=True,
            secrets={"password": "secret-value"},
        ),
    )
    skyvern_context.reset()
    skyvern_context.set(SkyvernContext(organization_id=TEST_ORGANIZATION_ID, workflow_run_id="wr_redact"))
    yield storage
    skyvern_context.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artifact_type", "data"),
    [
        (ArtifactType.HTML_SCRAPE, b"<html>secret-value</html>"),
        (ArtifactType.HASHED_HREF_MAP, b'{"h1": "https://example.test/callback?token=secret-value"}'),
    ],
)
async def test_create_artifact_redacts_textual_artifact_data(
    artifact_redaction_setup: _FakeStorage, artifact_type: ArtifactType, data: bytes
) -> None:
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)

    await manager.create_artifact(step=step, artifact_type=artifact_type, data=data)
    await manager.wait_for_upload_aiotasks([step.task_id])

    assert artifact_redaction_setup.stored[0][1] == data.replace(b"secret-value", REDACTED_SECRET_PLACEHOLDER.encode())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artifact_type", "data", "failure"),
    [
        (ArtifactType.HTML_SCRAPE, b"<html>secret-value</html>", "lookup"),
        (ArtifactType.LLM_REQUEST, b'{"messages": [{"role": "user", "content": "secret-value"}]}', "lookup"),
        (
            ArtifactType.HAR,
            b'{"log":{"entries":[{"request":{"headers":[{"name":"Authorization","value":"Bearer token"}]}}]}}',
            "lookup",
        ),
        (
            ArtifactType.HAR,
            b'{"log":{"entries":[{"request":{"headers":[{"name":"Authorization","value":"Bearer token"}]}}]}}',
            "lookup_and_har_scrub",
        ),
        (ArtifactType.HTML_SCRAPE, b"<html>secret-value</html>", "lookup_with_redaction_switched_off"),
    ],
)
async def test_create_artifact_never_stores_raw_data_when_secret_lookup_fails(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    artifact_type: ArtifactType,
    data: bytes,
    failure: str,
) -> None:
    def failing_lookup(*_: object, **__: object) -> set[str]:
        raise RuntimeError("secret store unavailable")

    def failing_har_scrub(*_: object, **__: object) -> bytes:
        raise RuntimeError("HAR scrub failed")

    monkeypatch.setattr(
        artifact_manager_module.app.WORKFLOW_CONTEXT_MANAGER, "get_secret_values_for_run", failing_lookup
    )
    if failure == "lookup_and_har_scrub":
        monkeypatch.setattr(artifact_manager_module, "redact_har_bytes", failing_har_scrub)
    if failure == "lookup_with_redaction_switched_off":
        # With the switch off the run's opt-in is never read, so the failure has to come from that read.
        monkeypatch.setattr(artifact_manager_module.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", False)
        monkeypatch.setattr(
            artifact_manager_module.app.WORKFLOW_CONTEXT_MANAGER, "artifact_redaction_enabled", failing_lookup
        )
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)

    await manager.create_artifact(step=step, artifact_type=artifact_type, data=data)
    await manager.wait_for_upload_aiotasks([step.task_id])

    stored = artifact_redaction_setup.stored[0][1]
    if failure == "lookup_with_redaction_switched_off":
        # The kill switch restores the pre-redaction behavior: the artifact is stored as produced.
        assert stored == data
    elif artifact_type == ArtifactType.HAR and failure == "lookup":
        # The HAR keeps its structure; the name-based scrub still removes the credential header value.
        assert b"Bearer token" not in stored
        assert json.loads(stored)["log"]["entries"][0]["request"]["headers"][0]["value"] == REDACTED_SECRET_PLACEHOLDER
    else:
        assert stored == REDACTION_FAILED_STUB


@pytest.mark.asyncio
async def test_create_artifact_redacts_registered_bare_task_secret(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifact_manager_module.app, "WORKFLOW_CONTEXT_MANAGER", WorkflowContextManager())
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)
    context = SkyvernContext(
        organization_id=TEST_ORGANIZATION_ID,
        task_id=step.task_id,
        workflow_run_id=None,
    )
    context.register_secret_value("482913")
    skyvern_context.set(context)

    await manager.create_artifact(
        step=step,
        artifact_type=ArtifactType.LLM_REQUEST,
        data=b'{"role": "tool", "content": "verification_code: 482913"}',
    )
    await manager.wait_for_upload_aiotasks([step.task_id])

    artifact, stored = artifact_redaction_setup.stored[0]
    assert artifact.workflow_run_id is None
    assert b"482913" not in stored
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
            mask_secrets=True,
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


def _har_with_session_material() -> bytes:
    return json.dumps(
        {
            "log": {
                "entries": [
                    {
                        "request": {
                            "url": "https://example.test/login",
                            "headers": [
                                {"name": "Cookie", "value": "sid=cookie-value-1"},
                                {"name": "Authorization", "value": "Bearer bearer-value-1"},
                            ],
                            "cookies": [{"name": "sid", "value": "cookie-value-1"}],
                            "postData": {
                                "mimeType": "application/x-www-form-urlencoded",
                                "text": "",
                                "params": [
                                    {"name": "username", "value": "someone"},
                                    {"name": "password", "value": "form-password-1"},
                                ],
                            },
                        },
                        "response": {
                            "headers": [
                                {"name": "Set-Cookie", "value": "sid=cookie-value-2; Path=/"},
                                {"name": "Content-Type", "value": "application/json"},
                            ],
                            "cookies": [{"name": "sid", "value": "cookie-value-2"}],
                            "content": {"mimeType": "application/json", "text": '{"echo": "secret-value"}'},
                        },
                    }
                ]
            }
        }
    ).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["data", "path", "bulk_path"])
@pytest.mark.parametrize(("mask_secrets", "lookup_fails"), [(True, False), (False, False), (True, True)])
async def test_create_artifact_redacts_har_structured_fields_whether_or_not_workflow_opted_in(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
    mask_secrets: bool,
    lookup_fails: bool,
    source: str,
) -> None:
    context_manager = workflow_context_manager_factory(
        workflow_run_id="wr_redact", mask_secrets=mask_secrets, secrets={"password": "secret-value"}
    )
    if lookup_fails:

        def failing_lookup(*_: object, **__: object) -> set[str]:
            raise RuntimeError("secret store unavailable")

        monkeypatch.setattr(context_manager, "get_secret_values_for_run", failing_lookup)
    monkeypatch.setattr(artifact_manager_module.app, "WORKFLOW_CONTEXT_MANAGER", context_manager)
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)
    har_path = tmp_path / "run.har"
    har_path.write_bytes(_har_with_session_material())

    if source == "data":
        await manager.create_artifact(step=step, artifact_type=ArtifactType.HAR, data=har_path.read_bytes())
    elif source == "path":
        await manager.create_artifact(step=step, artifact_type=ArtifactType.HAR, path=str(har_path))
    else:
        artifact_model = ArtifactModel(
            artifact_id="a_har_bulk",
            artifact_type=ArtifactType.HAR,
            uri="s3://bucket/artifact",
            organization_id=TEST_ORGANIZATION_ID,
            task_id=step.task_id,
            step_id=step.step_id,
            workflow_run_id="wr_redact",
        )
        await manager.bulk_create_artifacts(
            [
                BulkArtifactCreationRequest(
                    artifacts=[ArtifactBatchData(artifact_model=artifact_model, path=str(har_path))],
                    primary_key=step.task_id,
                )
            ]
        )
    await manager.wait_for_upload_aiotasks([step.task_id])

    stored = artifact_redaction_setup.stored[0][1]
    for leaked in (b"cookie-value-1", b"cookie-value-2", b"bearer-value-1", b"form-password-1"):
        assert leaked not in stored
    entry = json.loads(stored)["log"]["entries"][0]
    # Antibot detection reads cookie names and the submission verifier reads content-type.
    assert [cookie["name"] for cookie in entry["request"]["cookies"]] == ["sid"]
    assert [cookie["name"] for cookie in entry["response"]["cookies"]] == ["sid"]
    assert {"name": "Content-Type", "value": "application/json"} in entry["response"]["headers"]
    assert {"name": "username", "value": "someone"} in entry["request"]["postData"]["params"]
    # Only an opted-in run whose secret lookup succeeded also scrubs its credential values from bodies.
    assert (b"secret-value" in stored) is (not mask_secrets or lookup_fails)
