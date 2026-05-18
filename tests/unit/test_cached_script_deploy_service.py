import base64
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.repositories.scripts import WorkflowScriptUpsertStatus
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.schemas.scripts import (
    DeployCachedScriptCacheContext,
    DeployCachedScriptRequest,
    FileEncoding,
    ScriptFileCreate,
    ScriptStatus,
    WorkflowScript,
)
from skyvern.services import cached_script_deploy_service


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("utf-8")


def _workflow(
    *,
    cache_key: str | None = "default",
    version: int = 3,
    run_with: str = "code",
    code_version: int | None = 2,
) -> Workflow:
    return Workflow(
        workflow_id="wf_latest",
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        title="test",
        version=version,
        is_saved_task=False,
        workflow_definition={
            "parameters": [],
            "blocks": [
                {
                    "block_type": "navigation",
                    "label": "step_a",
                    "url": "https://example.com/login",
                    "navigation_goal": "Open",
                    "output_parameter": {
                        "parameter_type": "output",
                        "key": "step_a_output",
                        "output_parameter_id": "op_test",
                        "workflow_id": "wf_latest",
                        "created_at": datetime.now(timezone.utc),
                        "modified_at": datetime.now(timezone.utc),
                    },
                },
                {
                    "block_type": "validation",
                    "label": "check",
                    "complete_criterion": "done",
                    "terminate_criterion": "stop",
                    "output_parameter": {
                        "parameter_type": "output",
                        "key": "check_output",
                        "output_parameter_id": "op_check",
                        "workflow_id": "wf_latest",
                        "created_at": datetime.now(timezone.utc),
                        "modified_at": datetime.now(timezone.utc),
                    },
                },
            ],
        },
        run_with=run_with,
        cache_key=cache_key,
        code_version=code_version,
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )


def _workflow_script(
    *,
    workflow_script_id: str = "ws_created",
    script_id: str = "s_created",
    workflow_id: str | None = "wf_latest",
    workflow_run_id: str | None = "wr_source",
    cache_key: str = "default",
    cache_key_value: str = "default:example.com:v2",
    is_pinned: bool = True,
    pinned_at: datetime | None = None,
    pinned_by: str | None = None,
    created_at: datetime | None = None,
    modified_at: datetime | None = None,
    deleted_at: datetime | None = None,
) -> WorkflowScript:
    now = datetime.now(timezone.utc)
    return WorkflowScript(
        workflow_script_id=workflow_script_id,
        organization_id="org_test",
        script_id=script_id,
        workflow_permanent_id="wpid_test",
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        cache_key=cache_key,
        cache_key_value=cache_key_value,
        status=ScriptStatus.published,
        is_pinned=is_pinned,
        pinned_at=pinned_at,
        pinned_by=pinned_by,
        created_at=created_at or now,
        modified_at=modified_at or now,
        deleted_at=deleted_at,
    )


def _request(
    source: str,
    *,
    resolved_cache_key_value: str | None = None,
    dry_run: bool = True,
    requires_agent_overrides: dict[str, bool] | None = None,
    source_workflow_run_id: str | None = None,
    cache_key: str | None = "default",
    files: list[ScriptFileCreate] | None = None,
) -> DeployCachedScriptRequest:
    return DeployCachedScriptRequest(
        workflow_id="wf_latest",
        workflow_version=3,
        cache_key=cache_key,
        cache_context=DeployCachedScriptCacheContext(parameters={}, adaptive_caching=True),
        resolved_cache_key_value=resolved_cache_key_value,
        dry_run=dry_run,
        source_workflow_run_id=source_workflow_run_id,
        requires_agent_overrides=requires_agent_overrides or {},
        files=files
        or [
            ScriptFileCreate(
                path="main.py",
                content=_b64(source),
                encoding=FileEncoding.BASE64,
                mime_type="text/x-python",
            )
        ],
    )


@pytest.fixture(autouse=True)
def _stub_app(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    workflow = _workflow()
    state = SimpleNamespace(
        workflow=workflow,
        created_scripts=[],
        created_files=[],
        created_blocks=[],
        workflow_script_upserts=[],
        workflow_script_upsert_status=WorkflowScriptUpsertStatus.created,
        previous_workflow_script=None,
        workflow_updates=[],
        previous_dispatch_state=None,
        dispatch_restore_matches=True,
        workflow_script_soft_delete_matches=True,
        workflow_script_restore_matches=True,
        soft_deleted_workflow_scripts=[],
        restored_workflow_scripts=[],
        soft_deleted_script_revisions=[],
        artifacts=[],
        fail_dispatch_update=False,
        fail_create_artifact=False,
    )

    class Workflows:
        async def get_workflow_by_permanent_id(self, **_: object) -> Workflow:
            return state.workflow

        async def update_workflow(self, **kwargs: object) -> Workflow:
            state.workflow_updates.append(kwargs)
            return state.workflow

        async def restore_workflow_script_dispatch_if_matches(self, **kwargs: object) -> Workflow | None:
            state.workflow_updates.append(kwargs)
            return state.workflow if state.dispatch_restore_matches else None

        async def update_workflow_dispatch_state_if_latest(self, **kwargs: object) -> Workflow:
            state.workflow_updates.append(kwargs)
            if state.fail_dispatch_update:
                raise NotFoundError("Workflow not found or no longer latest")
            return state.workflow

        async def update_workflow_dispatch_state_if_latest_with_previous(self, **kwargs: object) -> SimpleNamespace:
            state.workflow_updates.append(kwargs)
            if state.fail_dispatch_update:
                raise NotFoundError("Workflow not found or no longer latest")
            previous_dispatch_state = state.previous_dispatch_state or SimpleNamespace(
                run_with=state.workflow.run_with,
                cache_key=state.workflow.cache_key,
                code_version=state.workflow.code_version,
            )
            return SimpleNamespace(workflow=state.workflow, previous_dispatch_state=previous_dispatch_state)

    class Scripts:
        async def create_script(self, **kwargs: object) -> SimpleNamespace:
            state.created_scripts.append(kwargs)
            return SimpleNamespace(
                script_id="s_created",
                script_revision_id="sr_created",
                version=1,
                run_id=kwargs.get("run_id"),
            )

        async def create_script_file(self, **kwargs: object) -> SimpleNamespace:
            state.created_files.append(kwargs)
            return SimpleNamespace(file_id=f"sf_{len(state.created_files)}")

        async def upsert_script_block(self, **kwargs: object) -> SimpleNamespace:
            state.created_blocks.append(kwargs)
            return SimpleNamespace(script_block_id=f"sb_{len(state.created_blocks)}")

        async def upsert_workflow_script(self, **kwargs: object) -> SimpleNamespace:
            state.workflow_script_upserts.append(kwargs)
            workflow_script = _workflow_script(
                script_id=str(kwargs["script_id"]),
                workflow_id=kwargs.get("workflow_id"),
                workflow_run_id=kwargs.get("workflow_run_id"),
                cache_key=str(kwargs["cache_key"]),
                cache_key_value=str(kwargs["cache_key_value"]),
                is_pinned=bool(kwargs["is_pinned"]),
            )
            return SimpleNamespace(
                status=state.workflow_script_upsert_status,
                workflow_script=workflow_script,
                previous_workflow_script=state.previous_workflow_script,
            )

        async def soft_delete_workflow_script_if_matches(self, **kwargs: object) -> bool:
            state.soft_deleted_workflow_scripts.append(kwargs["workflow_script"])
            return state.workflow_script_soft_delete_matches

        async def restore_workflow_script_if_matches(self, **kwargs: object) -> bool:
            state.restored_workflow_scripts.append(kwargs)
            return state.workflow_script_restore_matches

        async def soft_delete_script_by_revision(self, **kwargs: object) -> None:
            state.soft_deleted_script_revisions.append(kwargs["script_revision_id"])

    class ArtifactManager:
        async def create_script_file_artifact(self, **kwargs: object) -> str:
            if state.fail_create_artifact:
                raise RuntimeError("artifact write failed")
            state.artifacts.append(kwargs)
            return f"artifact_{len(state.artifacts)}"

    monkeypatch.setattr(
        cached_script_deploy_service.app,
        "DATABASE",
        SimpleNamespace(workflows=Workflows(), scripts=Scripts()),
    )
    monkeypatch.setattr(
        cached_script_deploy_service.app,
        "ARTIFACT_MANAGER",
        ArtifactManager(),
    )
    monkeypatch.setattr(
        cached_script_deploy_service.app,
        "AGENT_FUNCTION",
        SimpleNamespace(detect_ats_platform=lambda domain: None),
    )
    return state


@pytest.mark.asyncio
async def test_dry_run_returns_cache_key_and_block_plan() -> None:
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
    await skyvern.validate(complete_criterion="done", terminate_criterion="stop", label="check")
"""

    response = await cached_script_deploy_service.deploy_cached_script(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        request=_request(source, resolved_cache_key_value="default:example.com:v2"),
    )

    assert response.dry_run is True
    assert response.would_create_script is True
    assert response.script_was_created is False
    assert response.cache_key_value == "default:example.com:v2"
    assert response.cacheable_block_count == 1
    assert response.skipped_block_labels == ["check"]
    assert [block.label for block in response.blocks] == ["step_a", "check"]


@pytest.mark.asyncio
async def test_dry_run_uses_workflow_cache_key_when_override_is_omitted() -> None:
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    response = await cached_script_deploy_service.deploy_cached_script(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        request=_request(source, resolved_cache_key_value="default:example.com:v2", cache_key=None),
    )

    assert response.cache_key == "default"
    assert response.cache_key_value == "default:example.com:v2"


@pytest.mark.asyncio
async def test_commit_mode_creates_script_blocks_mapping_and_updates_workflow(
    _stub_app: SimpleNamespace,
) -> None:
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
    await skyvern.validate(complete_criterion="done", terminate_criterion="stop", label="check")
"""

    response = await cached_script_deploy_service.deploy_cached_script(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        request=_request(
            source,
            resolved_cache_key_value="default:example.com:v2",
            dry_run=False,
            source_workflow_run_id="wr_source",
        ),
    )

    state = _stub_app
    assert response.dry_run is False
    assert response.would_create_script is True
    assert response.script_was_created is True
    assert response.script_id == "s_created"
    assert response.script_revision_id == "sr_created"
    assert response.workflow_script_id == "ws_created"
    assert response.workflow_script_upsert_status == "created"
    assert state.created_scripts == [{"organization_id": "org_test", "run_id": "wr_source"}]
    assert state.created_files[0]["file_path"] == "main.py"
    assert state.created_files[0]["encoding"] == "base64"
    assert [block["script_block_label"] for block in state.created_blocks] == ["step_a", "check"]
    assert state.created_blocks[0]["run_signature"].startswith("await skyvern.run_task")
    assert state.created_blocks[0]["requires_agent"] is False
    assert state.created_blocks[1]["requires_agent"] is True
    assert state.workflow_script_upserts[0]["script_id"] == "s_created"
    assert state.workflow_script_upserts[0]["cache_key_value"] == "default:example.com:v2"
    assert state.workflow_script_upserts[0]["is_pinned"] is True
    assert state.workflow_updates == [
        {
            "workflow_id": "wf_latest",
            "workflow_permanent_id": "wpid_test",
            "organization_id": "org_test",
            "expected_version": 3,
            "run_with": "code",
            "cache_key": "default",
            "code_version": 2,
        }
    ]


@pytest.mark.asyncio
async def test_requires_agent_override_takes_precedence_for_non_cacheable_block(
    _stub_app: SimpleNamespace,
) -> None:
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
    await skyvern.validate(complete_criterion="done", terminate_criterion="stop", label="check")
"""

    await cached_script_deploy_service.deploy_cached_script(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        request=_request(
            source,
            resolved_cache_key_value="default:example.com:v2",
            dry_run=False,
            requires_agent_overrides={"check": False},
        ),
    )

    assert _stub_app.created_blocks[1]["script_block_label"] == "check"
    assert _stub_app.created_blocks[1]["requires_agent"] is False


@pytest.mark.asyncio
async def test_commit_mode_cleans_up_script_when_file_persist_fails(
    _stub_app: SimpleNamespace,
) -> None:
    _stub_app.fail_create_artifact = True
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    with pytest.raises(RuntimeError, match="artifact write failed"):
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(
                source,
                resolved_cache_key_value="default:example.com:v2",
                dry_run=False,
                source_workflow_run_id="wr_source",
            ),
        )

    assert _stub_app.soft_deleted_script_revisions == ["sr_created"]
    assert _stub_app.soft_deleted_workflow_scripts == []
    assert _stub_app.workflow_updates == []


@pytest.mark.asyncio
async def test_commit_mode_cleans_up_mapping_when_workflow_update_fails(
    _stub_app: SimpleNamespace,
) -> None:
    _stub_app.fail_dispatch_update = True
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    with pytest.raises(HTTPException) as exc:
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(
                source,
                resolved_cache_key_value="default:example.com:v2",
                dry_run=False,
                source_workflow_run_id="wr_source",
            ),
        )

    assert exc.value.status_code == 409
    assert "became stale" in str(exc.value.detail)
    assert [row.workflow_script_id for row in _stub_app.soft_deleted_workflow_scripts] == ["ws_created"]
    assert _stub_app.soft_deleted_script_revisions == ["sr_created"]
    assert _stub_app.workflow_updates == [
        {
            "workflow_id": "wf_latest",
            "workflow_permanent_id": "wpid_test",
            "organization_id": "org_test",
            "expected_version": 3,
            "run_with": "code",
            "cache_key": "default",
            "code_version": 2,
        }
    ]


@pytest.mark.asyncio
async def test_commit_mode_restores_updated_mapping_when_workflow_update_fails(
    _stub_app: SimpleNamespace,
) -> None:
    previous_workflow_script = _workflow_script(
        script_id="s_previous",
        workflow_id="wf_previous",
        workflow_run_id="wr_previous",
        is_pinned=False,
    )
    _stub_app.workflow_script_upsert_status = WorkflowScriptUpsertStatus.updated
    _stub_app.previous_workflow_script = previous_workflow_script
    _stub_app.fail_dispatch_update = True
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    with pytest.raises(HTTPException) as exc:
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(
                source,
                resolved_cache_key_value="default:example.com:v2",
                dry_run=False,
                source_workflow_run_id="wr_source",
            ),
        )

    assert exc.value.status_code == 409
    assert _stub_app.soft_deleted_workflow_scripts == []
    assert _stub_app.restored_workflow_scripts[0]["restore_workflow_script"] == previous_workflow_script
    assert _stub_app.restored_workflow_scripts[0]["current_workflow_script"].script_id == "s_created"
    assert _stub_app.soft_deleted_script_revisions == ["sr_created"]


@pytest.mark.asyncio
async def test_commit_mode_restores_workflow_dispatch_after_late_failure(
    _stub_app: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_app.workflow = _workflow(cache_key="old-cache", run_with="agent", code_version=None)
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    def fail_response(**_: object) -> None:
        raise RuntimeError("response construction failed")

    monkeypatch.setattr(cached_script_deploy_service, "_response_from_plan", fail_response)

    with pytest.raises(RuntimeError, match="response construction failed"):
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(
                source,
                cache_key="new-cache",
                resolved_cache_key_value="new-cache:v2",
                dry_run=False,
                source_workflow_run_id="wr_source",
            ),
        )

    assert _stub_app.workflow_updates == [
        {
            "workflow_id": "wf_latest",
            "workflow_permanent_id": "wpid_test",
            "organization_id": "org_test",
            "expected_version": 3,
            "run_with": "code",
            "cache_key": "new-cache",
            "code_version": 2,
        },
        {
            "workflow_id": "wf_latest",
            "organization_id": "org_test",
            "run_with": "agent",
            "cache_key": "old-cache",
            "code_version": None,
            "current_run_with": "code",
            "current_cache_key": "new-cache",
            "current_code_version": 2,
        },
    ]
    assert [row.workflow_script_id for row in _stub_app.soft_deleted_workflow_scripts] == ["ws_created"]
    assert _stub_app.soft_deleted_script_revisions == ["sr_created"]


@pytest.mark.asyncio
async def test_commit_mode_restores_dispatch_state_captured_during_write(
    _stub_app: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_app.workflow = _workflow(cache_key="old-plan-cache", run_with="agent", code_version=None)
    _stub_app.previous_dispatch_state = SimpleNamespace(
        run_with="code",
        cache_key="concurrent-cache",
        code_version=2,
    )
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    def fail_response(**_: object) -> None:
        raise RuntimeError("response construction failed")

    monkeypatch.setattr(cached_script_deploy_service, "_response_from_plan", fail_response)

    with pytest.raises(RuntimeError, match="response construction failed"):
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(
                source,
                cache_key="new-cache",
                resolved_cache_key_value="new-cache:v2",
                dry_run=False,
                source_workflow_run_id="wr_source",
            ),
        )

    assert _stub_app.workflow_updates[-1] == {
        "workflow_id": "wf_latest",
        "organization_id": "org_test",
        "run_with": "code",
        "cache_key": "concurrent-cache",
        "code_version": 2,
        "current_run_with": "code",
        "current_cache_key": "new-cache",
        "current_code_version": 2,
    }
    assert [row.workflow_script_id for row in _stub_app.soft_deleted_workflow_scripts] == ["ws_created"]
    assert _stub_app.soft_deleted_script_revisions == ["sr_created"]


@pytest.mark.asyncio
async def test_commit_mode_skips_dispatch_restore_when_current_state_changed(
    _stub_app: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_app.workflow = _workflow(cache_key="old-cache", run_with="agent", code_version=None)
    _stub_app.dispatch_restore_matches = False
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    def fail_response(**_: object) -> None:
        raise RuntimeError("response construction failed")

    monkeypatch.setattr(cached_script_deploy_service, "_response_from_plan", fail_response)

    with pytest.raises(RuntimeError, match="response construction failed"):
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(
                source,
                cache_key="new-cache",
                resolved_cache_key_value="new-cache:v2",
                dry_run=False,
                source_workflow_run_id="wr_source",
            ),
        )

    assert _stub_app.workflow_updates[-1] == {
        "workflow_id": "wf_latest",
        "organization_id": "org_test",
        "run_with": "agent",
        "cache_key": "old-cache",
        "code_version": None,
        "current_run_with": "code",
        "current_cache_key": "new-cache",
        "current_code_version": 2,
    }
    assert [row.workflow_script_id for row in _stub_app.soft_deleted_workflow_scripts] == ["ws_created"]
    assert _stub_app.soft_deleted_script_revisions == ["sr_created"]


@pytest.mark.asyncio
async def test_commit_mode_skips_created_mapping_delete_when_current_state_changed(
    _stub_app: SimpleNamespace,
) -> None:
    _stub_app.fail_dispatch_update = True
    _stub_app.workflow_script_soft_delete_matches = False
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    with pytest.raises(HTTPException) as exc:
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(
                source,
                resolved_cache_key_value="default:example.com:v2",
                dry_run=False,
                source_workflow_run_id="wr_source",
            ),
        )

    assert exc.value.status_code == 409
    assert [row.workflow_script_id for row in _stub_app.soft_deleted_workflow_scripts] == ["ws_created"]
    assert _stub_app.soft_deleted_script_revisions == ["sr_created"]


@pytest.mark.asyncio
async def test_commit_mode_skips_mapping_restore_when_current_state_changed(
    _stub_app: SimpleNamespace,
) -> None:
    previous_workflow_script = _workflow_script(
        script_id="s_previous",
        workflow_id="wf_previous",
        workflow_run_id="wr_previous",
        is_pinned=False,
    )
    _stub_app.workflow_script_upsert_status = WorkflowScriptUpsertStatus.updated
    _stub_app.previous_workflow_script = previous_workflow_script
    _stub_app.fail_dispatch_update = True
    _stub_app.workflow_script_restore_matches = False
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    with pytest.raises(HTTPException) as exc:
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(
                source,
                resolved_cache_key_value="default:example.com:v2",
                dry_run=False,
                source_workflow_run_id="wr_source",
            ),
        )

    assert exc.value.status_code == 409
    assert _stub_app.soft_deleted_workflow_scripts == []
    assert _stub_app.restored_workflow_scripts[0]["restore_workflow_script"] == previous_workflow_script
    assert _stub_app.restored_workflow_scripts[0]["current_workflow_script"].script_id == "s_created"
    assert _stub_app.soft_deleted_script_revisions == ["sr_created"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_path",
    ["../outside.py", "/main.py", "dir//main.py", "dir/../main.py", "dir\\main.py", "./main.py"],
)
async def test_dry_run_rejects_unsafe_file_paths(file_path: str) -> None:
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""
    files = [
        ScriptFileCreate(
            path="main.py",
            content=_b64(source),
            encoding=FileEncoding.BASE64,
            mime_type="text/x-python",
        ),
        ScriptFileCreate(
            path=file_path,
            content=_b64("x = 1\n"),
            encoding=FileEncoding.BASE64,
            mime_type="text/x-python",
        ),
    ]

    with pytest.raises(HTTPException) as exc:
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(source, files=files),
        )

    assert exc.value.status_code == 400
    assert "relative POSIX path" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_dry_run_rejects_duplicate_file_paths() -> None:
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""
    files = [
        ScriptFileCreate(
            path="main.py",
            content=_b64(source),
            encoding=FileEncoding.BASE64,
            mime_type="text/x-python",
        ),
        ScriptFileCreate(
            path="main.py",
            content=_b64("x = 1\n"),
            encoding=FileEncoding.BASE64,
            mime_type="text/x-python",
        ),
    ]

    with pytest.raises(HTTPException) as exc:
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(source, files=files),
        )

    assert exc.value.status_code == 400
    assert "Duplicate script file path" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_dry_run_rejects_invalid_base64_in_non_main_file() -> None:
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""
    files = [
        ScriptFileCreate(
            path="main.py",
            content=_b64(source),
            encoding=FileEncoding.BASE64,
            mime_type="text/x-python",
        ),
        ScriptFileCreate(
            path="helper.py",
            content=_b64("x = 1\n") + "!!!",
            encoding=FileEncoding.BASE64,
            mime_type="text/x-python",
        ),
    ]

    with pytest.raises(HTTPException) as exc:
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(source, files=files),
        )

    assert exc.value.status_code == 400
    assert "not valid base64" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_dry_run_rejects_oversized_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cached_script_deploy_service, "_MAX_SCRIPT_FILE_BYTES", 4)
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""
    files = [
        ScriptFileCreate(
            path="main.py",
            content=_b64(source),
            encoding=FileEncoding.BASE64,
            mime_type="text/x-python",
        ),
        ScriptFileCreate(
            path="helper.py",
            content=_b64("x = 1\n"),
            encoding=FileEncoding.BASE64,
            mime_type="text/x-python",
        ),
    ]

    with pytest.raises(HTTPException) as exc:
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(source, files=files),
        )

    assert exc.value.status_code == 400
    assert "exceeds maximum size" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_dry_run_rejects_missing_globals() -> None:
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(url=LOGIN_URL, prompt="...", label="step_a", cache_key="step_a")
"""

    with pytest.raises(HTTPException) as exc:
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(source),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == {"missing_globals": {"step_a": ["LOGIN_URL"]}}


@pytest.mark.asyncio
async def test_dry_run_returns_400_for_run_signature_validation_errors() -> None:
    source = """
from constants import *
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    with pytest.raises(HTTPException) as exc:
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(source),
        )

    assert exc.value.status_code == 400
    assert "Wildcard imports" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_dry_run_rejects_cache_key_assertion_mismatch() -> None:
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    with pytest.raises(HTTPException) as exc:
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(source, resolved_cache_key_value="wrong:v2"),
        )

    assert exc.value.status_code == 400
    assert "Resolved cache key value mismatch" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_commit_mode_rejects_null_cache_key(_stub_app: SimpleNamespace) -> None:
    _stub_app.workflow = _workflow(cache_key=None)
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    with pytest.raises(HTTPException) as exc:
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(
                source,
                resolved_cache_key_value="example.com:v2",
                dry_run=False,
                cache_key=None,
            ),
        )

    assert exc.value.status_code == 400
    assert "non-null workflow cache_key" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_dry_run_rejects_null_cache_key(_stub_app: SimpleNamespace) -> None:
    _stub_app.workflow = _workflow(cache_key=None)
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    with pytest.raises(HTTPException) as exc:
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(
                source,
                resolved_cache_key_value="example.com:v2",
                cache_key=None,
            ),
        )

    assert exc.value.status_code == 400
    assert "non-null workflow cache_key" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_commit_mode_maps_stale_dispatch_update_to_conflict(_stub_app: SimpleNamespace) -> None:
    _stub_app.fail_dispatch_update = True
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    with pytest.raises(HTTPException) as exc:
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(
                source,
                resolved_cache_key_value="default:example.com:v2",
                dry_run=False,
            ),
        )

    assert exc.value.status_code == 409
    assert "became stale" in str(exc.value.detail)
