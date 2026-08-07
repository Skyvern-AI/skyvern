import ast
import base64
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from skyvern.core.script_generations.generate_script import CodeGenResult
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.repositories.scripts import WorkflowScriptUpsertStatus
from skyvern.forge.sdk.routes import scripts as scripts_routes
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.schemas.scripts import (
    CreateScriptRequest,
    DeployCachedScriptCacheContext,
    DeployCachedScriptRequest,
    DeployCachedScriptResponse,
    DeployScriptRequest,
    FileEncoding,
    ScriptFileCreate,
    ScriptStatus,
    WorkflowScript,
)
from skyvern.services import cached_script_deploy_service, script_service, workflow_script_service


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("utf-8")


def _script_file(path: str, source: str, *, mime_type: str = "text/x-python") -> ScriptFileCreate:
    return ScriptFileCreate(
        path=path,
        content=_b64(source),
        encoding=FileEncoding.BASE64,
        mime_type=mime_type,
    )


_REALISTIC_GENERATED_MAIN = """
import skyvern

@skyvern.workflow(title="t")
async def run(parameters):
    page, context = await skyvern.setup(parameters, dict)
    try:
        await page.wait_for_load_state()
    except TimeoutError:
        pass
    else:
        await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""


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


async def _deploy_files(files: list[ScriptFileCreate]) -> DeployCachedScriptResponse:
    return await cached_script_deploy_service.deploy_cached_script(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        request=_request(_REALISTIC_GENERATED_MAIN, dry_run=False, files=files),
    )


def _validate_uploaded_source(source: str) -> dict[str, bytes]:
    return script_service.validate_uploaded_script_files([_script_file("main.py", source)])


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
                script_id=kwargs.get("script_id", "s_created"),
                script_revision_id="sr_created",
                version=kwargs.get("version", 1),
                run_id=kwargs.get("run_id"),
                created_at=datetime.now(timezone.utc),
            )

        async def create_script_file(self, **kwargs: object) -> SimpleNamespace:
            state.created_files.append(kwargs)
            return SimpleNamespace(file_id=f"sf_{len(state.created_files)}")

        async def get_latest_script_version(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(script_id="s_existing", script_revision_id="sr_existing", version=1, run_id=None)

        async def get_script_files(self, **_: object) -> list[object]:
            return []

        async def get_script_blocks_by_script_revision_id(self, **_: object) -> list[object]:
            return []

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

        async def get_workflow_script(self, **kwargs: object) -> None:
            return None

        async def create_workflow_script(self, **kwargs: object) -> SimpleNamespace:
            state.workflow_script_upserts.append(kwargs)
            return SimpleNamespace(**kwargs)

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


def _assert_no_persistence_writes(state: SimpleNamespace) -> None:
    assert state.created_scripts == []
    assert state.artifacts == []
    assert state.created_files == []
    assert state.created_blocks == []
    assert state.workflow_script_upserts == []
    assert state.workflow_updates == []


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
@pytest.mark.parametrize(
    "escape",
    [
        "page.page.context.pages[0]",
        "page.context.pages[0]",
        "page.locator('#target').click()",
        "await page.fill_form({})",
        "await page.fill_from_mapping([], {}, data={})",
        "await page.get_totp_digit(context, 'totp_code', 0)",
        "context.page.context.pages[0]",
    ],
)
async def test_unsafe_cached_page_capabilities_force_agent_fallback(escape: str) -> None:
    source = f"""\
import skyvern

@skyvern.cached(cache_key="step_a")
async def cached_step(page, context):
    return {escape}

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    response = await cached_script_deploy_service.deploy_cached_script(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        request=_request(source, requires_agent_overrides={"step_a": False}),
    )

    step = next(block for block in response.blocks if block.label == "step_a")
    assert step.requires_agent is True
    assert response.warnings == ["Cached block 'step_a' uses an unmediated browser capability; agent fallback required"]


@pytest.mark.asyncio
async def test_private_attribute_capability_escape_is_rejected_outright() -> None:
    # `page._ai` used to be classified by the visitor above and softened to an agent-fallback
    # warning. Since the mint-time `is_safe_script_code` gate (ADR-0012) now runs first and rejects
    # ANY private-attribute access file-wide, this specific escape can no longer reach that
    # classifier at all — it fails closed with a 400 instead of degrading. Flagged for review: this
    # narrows PR #13656's original fallback behavior for this one pattern; confirm this is the
    # intended precedence between the two gates rather than an accidental strictness regression.
    source = """\
import skyvern

@skyvern.cached(cache_key="step_a")
async def cached_step(page, context):
    return page._ai.page.request

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    with pytest.raises(HTTPException) as exc_info:
        await cached_script_deploy_service.deploy_cached_script(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(source, requires_agent_overrides={"step_a": False}),
        )

    assert exc_info.value.status_code == 400
    assert "not allowed" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_positional_cached_key_cannot_bypass_agent_fallback() -> None:
    source = """\
import skyvern

@skyvern.cached("step_a")
async def cached_step(page, context):
    return page.page.context.pages[0]

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    response = await cached_script_deploy_service.deploy_cached_script(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        request=_request(source, requires_agent_overrides={"step_a": False}),
    )

    step = next(block for block in response.blocks if block.label == "step_a")
    assert step.requires_agent is True


def test_duplicate_function_names_preserve_each_unsafe_cached_registration() -> None:
    source = """\
import skyvern

@skyvern.cached(cache_key="unsafe_first")
async def repeated(page, context):
    return page.page.context.pages[0]

@skyvern.cached(cache_key="safe_last")
async def repeated(page, context):
    await page.click(selector="#target")
"""

    assert cached_script_deploy_service._unsafe_cached_block_labels(source) == {"unsafe_first"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_helper_body",
    [
        "return page.page.context.pages[0]",
        "return await page.fill_form({})",
        "return await page.fill_from_mapping([], {}, data={})",
        "return await page.get_totp_digit(context, 'totp_code', 0)",
    ],
)
async def test_transitive_unsafe_cached_helpers_force_agent_fallback(unsafe_helper_body: str) -> None:
    source = f"""\
import skyvern

async def unsafe_helper(page, context):
    {unsafe_helper_body}

async def generated_bridge(page, context):
    return await unsafe_helper(page, context)

@skyvern.cached(cache_key="step_a")
async def cached_step(page, context):
    return await generated_bridge(page, context)

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    response = await cached_script_deploy_service.deploy_cached_script(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        request=_request(source, requires_agent_overrides={"step_a": False}),
    )

    step = next(block for block in response.blocks if block.label == "step_a")
    assert step.requires_agent is True
    assert response.warnings == ["Cached block 'step_a' uses an unmediated browser capability; agent fallback required"]


@pytest.mark.asyncio
async def test_safe_cached_helper_preserves_explicit_agent_override() -> None:
    source = """\
import skyvern

async def safe_helper(page):
    await page.click(selector="#target")

@skyvern.cached(cache_key="step_a")
async def cached_step(page, context):
    await safe_helper(page)

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""

    response = await cached_script_deploy_service.deploy_cached_script(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        request=_request(source, requires_agent_overrides={"step_a": False}),
    )

    step = next(block for block in response.blocks if block.label == "step_a")
    assert step.requires_agent is False
    assert response.warnings == []


def test_transitive_analysis_scans_a_long_helper_chain_linearly() -> None:
    class CountingVisitors(dict[str, cached_script_deploy_service._CachedCapabilityVisitor]):
        scans = 0

        def items(self):
            self.scans += 1
            return super().items()

    root = ast.parse("async def helper(): pass").body[0]
    assert isinstance(root, ast.AsyncFunctionDef)
    visitors = CountingVisitors()
    helper_count = 2_000
    for index in range(helper_count):
        visitor = cached_script_deploy_service._CachedCapabilityVisitor(root)
        visitor.directly_unsafe = index == helper_count - 1
        if index < helper_count - 1:
            visitor.local_calls.add(f"helper_{index + 1}")
        visitors[f"helper_{index}"] = visitor

    unsafe = cached_script_deploy_service._transitively_unsafe_function_names(visitors)

    assert unsafe == set(visitors)
    assert visitors.scans <= 2


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


@pytest.mark.parametrize(
    ("module_name", "source"),
    [
        ("runpy", "import runpy\nrunpy.run_module('helper')\n"),
        ("pickle", "import pickle\nvalue = pickle.loads(payload)\n"),
        ("marshal", "import marshal\nvalue = marshal.loads(payload)\n"),
        ("subprocess", "import subprocess\nsubprocess.Popen(['true'])\n"),
        ("ctypes", "import ctypes\n"),
        ("socket", "import socket\n"),
        ("operator", "import operator\noperator.attrgetter('__class__')\n"),
        ("multiprocessing", "import multiprocessing\n"),
        ("importlib", "import importlib\n"),
        ("zipimport", "import zipimport\nz = zipimport.zipimporter('x.zip')\nz.load_module('payload')\n"),
        ("imp", "import imp\nimp.load_source('payload', 'payload.dat')\n"),
        ("pkgutil", "import pkgutil\nloader = pkgutil.get_importer('x.zip')\n"),
        ("zipfile", "import zipfile\nzipfile.ZipFile('x.zip').extractall('payload')\n"),
        ("_pickle", "import _pickle\n"),
        ("_socket", "import _socket\n"),
        ("_posixsubprocess", "import _posixsubprocess\n"),
        ("_ctypes", "import _ctypes\n"),
        ("_multiprocessing", "import _multiprocessing\n"),
        ("_thread", "import _thread\n"),
        ("_socket", "from _socket import socket\n"),
        ("subprocess", "from subprocess import Popen\n"),
        ("nt", "import nt\n"),
        ("posix", "import posix\n"),
        (
            "logging",
            "import logging.config\nlogging.config.dictConfig({'version': 1, 'handler': {'()': 'os.system'}})\n",
        ),
    ],
)
def test_uploaded_python_rejects_module_execution_imports(module_name: str, source: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_uploaded_source(source)

    assert exc.value.status_code == 400
    assert module_name in str(exc.value.detail)


@pytest.mark.parametrize("module_name", ["os", "shutil", "nt", "posix"])
def test_uploaded_python_rejects_wildcard_imports(module_name: str) -> None:
    source = f"from {module_name} import *\n"

    with pytest.raises(HTTPException) as exc:
        _validate_uploaded_source(source)

    assert exc.value.status_code == 400
    assert "Wildcard imports" in str(exc.value.detail)


@pytest.mark.parametrize(
    ("blocked_name", "source"),
    [
        ("eval", "value = eval('1 + 1')\n"),
        ("exec", "exec('value = 1')\n"),
        ("__import__", "module = __import__('json')\n"),
        ("compile", "code = compile('value = 1', '<string>', 'exec')\n"),
        ("builtins", "import builtins as runtime\nvalue = runtime.eval('1 + 1')\n"),
        ("getattr", "g = getattr\nvalue = g(object, '__subclasses__')\n"),
        ("getattr", "dispatch = {'f': getattr}\nvalue = dispatch['f'](object, '__class__')\n"),
        ("setattr", "setter = setattr\nsetter(object, 'x', 1)\n"),
        ("delattr", "deleter = delattr\n"),
        ("vars", "inspect_vars = vars\n"),
        ("globals", "namespace = globals\n"),
    ],
)
def test_uploaded_python_rejects_dynamic_execution_builtins(blocked_name: str, source: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_uploaded_source(source)

    assert exc.value.status_code == 400
    assert blocked_name in str(exc.value.detail)


@pytest.mark.parametrize(
    ("blocked_name", "source"),
    [
        (
            "eval",
            "locals()['__builtins__']['eval'](\"__import__('os').system('id')\")\n",
        ),
        ("locals", "d = locals()\n"),
        ("eval", "d = {}\nx = d['eval']\n"),
        ("builtins", "import builtins\nb = builtins\nb.compile('1', '<s>', 'exec')\n"),
        ("builtins", "import builtins\nb = builtins\nx = b.locals()\n"),
        ("builtins", "import builtins\n"),
        ("builtins", "from builtins import compile as c\n"),
    ],
    ids=[
        "locals-chain",
        "locals-bare",
        "blocked-subscript-key",
        "rebound-builtins-compile",
        "rebound-builtins-locals",
        "import-builtins",
        "from-builtins",
    ],
)
def test_uploaded_python_rejects_locals_and_builtins_module_escapes(blocked_name: str, source: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_uploaded_source(source)

    assert exc.value.status_code == 400
    assert blocked_name in str(exc.value.detail)


@pytest.mark.parametrize(
    "source",
    [
        "import os\nvalue = os.getenv('KEY')\nos.makedirs('output', exist_ok=True)\n",
        "from urllib.parse import urlsplit\nupdated = urlsplit('https://example.com')._replace(scheme='http')\n",
        "value = '{}'.format(1)\n",
        "value = '{0} {name}'.format('x', name='y')\n",
        "value = '{0.name}'.format(obj)\n",
        "value = '{d[key]}'.format(d=mapping)\n",
        "__all__ = ['run']\n",
        "import re\nvalue = re.compile('ok')\n",
        "value = __file__\n",
        "if __name__ == '__main__':\n    pass\n",
        "value = module.__name__\n",
        "value = module.__all__\n",
        "value = module.__doc__\n",
        "value = page.context\n",
        "value = page.request\n",
        "from __future__ import annotations\n",
        "import os.path\n",
        "import sys\n",
        "value = '{}-{}'.format(a, b)\n",
        "template = '{}/{}'\nvalue = template.format(a, b)\n",
        "class C:\n    def __init__(self):\n        self._value = 1\n\n    def get(self):\n        return self._value\n",
        "class C(Base):\n    def __init__(self):\n        super().__init__()\n",
    ],
)
def test_uploaded_python_accepts_deployed_python_apis(source: str) -> None:
    assert _validate_uploaded_source(source) == {"main.py": source.encode("utf-8")}


@pytest.mark.parametrize(
    "snippet",
    [
        "import os\nos.system('true')\n",
        "import os\nos.popen('true')\n",
        "import os\nos.posix_spawn('/bin/true', ['/bin/true'], {})\n",
        "import os\nos.posix_spawnp('true', ['true'], {})\n",
        "import os\nos.spawnv(os.P_WAIT, '/bin/true', ['/bin/true'])\n",
        "import os\nos.spawnve(os.P_WAIT, '/bin/true', ['/bin/true'], {})\n",
        "import os\nos.spawnvp(os.P_WAIT, 'true', ['true'])\n",
        "import os\nos.spawnvpe(os.P_WAIT, 'true', ['true'], {})\n",
        "import os\nos.forkpty()\n",
        "import os\nos.execle('/bin/true', 'true', {})\n",
        "leak = ().__class__\n",
        "leak = f.__globals__\n",
        "leak = ().__class__.__subclasses__()\n",
        "from builtins import __dict__ as namespace\nleak = namespace['eval']\n",
        "import asyncio\nasyncio.get_event_loop().subprocess_shell(proto, 'id')\n",
        "import asyncio\nasyncio.get_event_loop().subprocess_exec(proto, 'id')\n",
        ("import typing\nclass C:\n    value: \"__import__('os').system('id')\"\ntyping.get_type_hints(C)\n"),
        (
            "import typing_extensions\nclass C:\n"
            "    value: \"__import__('os').system('id')\"\n"
            "typing_extensions.get_type_hints(C)\n"
        ),
        (
            "import os\nfrom string import Formatter\nclass C: pass\n"
            'run = Formatter().get_field("0.__init__.__globals__[os].system", (C(),), {})[0]\n'
            "run('id')\n"
        ),
        "class C:\n    def leak(self):\n        return self.__class__\n",
        "class C(Base):\n    def leak(self):\n        return super().__class__\n",
    ],
)
def test_uploaded_python_rejects_execution_and_escape_attributes(snippet: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_uploaded_source(snippet)

    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "source",
    [
        "class C: pass\nvalue = '{0.__init__.__globals__[__builtins__]}'.format(C())\n",
        "class C: pass\nvalue = str.format('{0.__init__.__globals__[__builtins__]}', C())\n",
        "class C: pass\ntemplate = '{0.__init__.__globals__[__builtins__]}'\nvalue = template.format(C())\n",
        ("class C: pass\ntemplate = '{0.__init__' + '.__globals__[__builtins__]}'\nvalue = template.format(C())\n"),
        "value = '{item[__class__]}'.format(item={})\n",
        "value = '{0:{1.__class__}}'.format('x', object())\n",
    ],
)
def test_uploaded_python_rejects_constant_format_field_traversal(source: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_uploaded_source(source)

    assert exc.value.status_code == 400
    assert "format field" in str(exc.value.detail)


def test_uploaded_python_rejects_runtime_format_string() -> None:
    source = "def render(template, value):\n    return template.format(value)\n"

    with pytest.raises(HTTPException) as exc:
        _validate_uploaded_source(source)

    assert exc.value.status_code == 400
    assert "statically resolvable" in str(exc.value.detail)


@pytest.mark.parametrize(
    "attribute",
    [
        "__getattribute__",
        "__getattr__",
        "__setattr__",
        "__delattr__",
        "__reduce__",
        "__reduce_ex__",
        "__dict__",
        "__class__",
        "__init_subclass__",
    ],
)
def test_uploaded_python_rejects_reflection_dunders_on_a_rebound_self(attribute: str) -> None:
    # ``self`` is an ordinary identifier: a module-level script can bind it to any object, so the
    # self/super carve-out is a name match and must not extend to reflection dunders.
    with pytest.raises(HTTPException) as exc:
        _validate_uploaded_source(f'self = ""\nvalue = self.{attribute}\n')

    assert exc.value.status_code == 400


def test_uploaded_python_rejects_the_rebound_self_subclasses_chain() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_uploaded_source(
            'self = ""\n'
            'self = self.__getattribute__("__class__")\n'
            'self = self.__getattribute__("__base__")\n'
            'self = self.__getattribute__("__subclasses__")\n'
            "self = self()\n"
            "value = len(self)\n"
        )

    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "source",
    [
        "class A:\n    def f(self):\n        return self._value\n",
        "class A:\n    def f(self):\n        return self._helper()\n",
        "class A(B):\n    def __init__(self):\n        super().__init__()\n",
        "class A(B):\n    def __enter__(self):\n        return super().__enter__()\n",
    ],
)
def test_uploaded_python_still_allows_ordinary_self_and_super_use(source: str) -> None:
    _validate_uploaded_source(source)


def test_uploaded_python_rejects_formatter_vformat() -> None:
    source = "import string\nvalue = string.Formatter().vformat('{0.__globals__[__builtins__]}', [fn], {})\n"

    with pytest.raises(HTTPException) as exc:
        _validate_uploaded_source(source)

    assert exc.value.status_code == 400
    assert "vformat" in str(exc.value.detail)


@pytest.mark.parametrize(
    "source",
    [
        ("class C: pass\nmethod = '{0.__init__.__globals__[__builtins__]}'.format\nvalue = method(C())\n"),
        ("class C: pass\nmethod = str.format\nvalue = method('{0.__init__.__globals__[__builtins__]}', C())\n"),
    ],
)
def test_uploaded_python_rejects_format_callable_alias(source: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_uploaded_source(source)

    assert exc.value.status_code == 400
    assert "called directly" in str(exc.value.detail)


@pytest.mark.parametrize("file_path", ["main.PY", "main.Py", "helper.PY"])
def test_uploaded_python_rejects_unsafe_uppercase_extension(file_path: str) -> None:
    with pytest.raises(HTTPException) as exc:
        script_service.validate_uploaded_script_files([_script_file(file_path, "import os\nos.system('id')\n")])

    assert exc.value.status_code == 400
    assert "system" in str(exc.value.detail)


def test_uploaded_python_rejects_unparseable_file() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_uploaded_source("def broken(:\n    pass\n")

    assert exc.value.status_code == 400
    assert "main.py" in str(exc.value.detail)


def test_uploaded_python_rejects_parser_stack_overflow() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_uploaded_source("+" * 200_000 + "1")

    assert exc.value.status_code == 400
    assert exc.value.detail == "Python file 'main.py' does not parse"


@pytest.mark.parametrize("error_type", [MemoryError, RecursionError])
def test_uploaded_python_handles_parser_resource_errors(
    monkeypatch: pytest.MonkeyPatch, error_type: type[BaseException]
) -> None:
    def raise_parser_error(*_: object, **__: object) -> None:
        raise error_type

    monkeypatch.setattr(script_service, "is_safe_script_code", raise_parser_error)

    with pytest.raises(HTTPException) as exc:
        _validate_uploaded_source("value = 1\n")

    assert exc.value.status_code == 400
    assert exc.value.detail == "Python file 'main.py' does not parse"


@pytest.mark.asyncio
async def test_build_file_tree_validates_when_decoded_bytes_are_omitted(_stub_app: SimpleNamespace) -> None:
    files = [_script_file("main.py", "import os\nos.system('id')\n")]

    with pytest.raises(HTTPException) as exc:
        await script_service.build_file_tree(
            files=files,
            organization_id="org_test",
            script_id="s_test",
            script_version=1,
            script_revision_id="sr_test",
        )

    assert exc.value.status_code == 400
    _assert_no_persistence_writes(_stub_app)


@pytest.mark.asyncio
async def test_generated_script_persists_unparseable_main_for_repair(
    monkeypatch: pytest.MonkeyPatch, _stub_app: SimpleNamespace
) -> None:
    source = "async def broken(:\n    pass\n"

    async def transform_workflow_run_to_code_gen_input(**_: object) -> SimpleNamespace:
        return SimpleNamespace(
            file_name="main.py",
            workflow_run={},
            workflow={},
            workflow_blocks=[{"label": "step_a"}],
            actions_by_task={},
            task_v2_child_blocks={},
        )

    async def generate_workflow_script_python_code(**_: object) -> CodeGenResult:
        return CodeGenResult(source_code=source, blocks_created=1, blocks_failed=0)

    async def skip_mint_audit(**_: object) -> None:
        return None

    monkeypatch.setattr(
        workflow_script_service,
        "transform_workflow_run_to_code_gen_input",
        transform_workflow_run_to_code_gen_input,
    )
    monkeypatch.setattr(
        workflow_script_service,
        "generate_workflow_script_python_code",
        generate_workflow_script_python_code,
    )
    monkeypatch.setattr(workflow_script_service, "is_adaptive_caching", lambda *_: False)
    monkeypatch.setattr(workflow_script_service, "_log_mint_audit_findings", skip_mint_audit)

    await workflow_script_service.generate_workflow_script(
        workflow_run=SimpleNamespace(workflow_run_id="wr_test"),
        workflow=SimpleNamespace(
            workflow_id="wf_test",
            workflow_permanent_id="wpid_test",
            organization_id="org_test",
            title="test",
            cache_key="default",
        ),
        script=SimpleNamespace(script_id="s_test", script_revision_id="sr_test", version=1),
        rendered_cache_key_value="default:test",
    )

    assert _stub_app.artifacts[0]["data"] == source.encode("utf-8")
    assert _stub_app.created_files[0]["file_path"] == "main.py"


@pytest.mark.asyncio
async def test_generated_script_tolerance_still_rejects_parseable_policy_violation(
    _stub_app: SimpleNamespace,
) -> None:
    files = [_script_file("main.py", "import os\nos.system('command')\n")]

    with pytest.raises(HTTPException) as exc:
        await script_service.build_file_tree(
            files=files,
            organization_id="org_test",
            script_id="s_test",
            script_version=1,
            script_revision_id="sr_test",
            allow_invalid_python_syntax=True,
        )

    assert exc.value.status_code == 400
    _assert_no_persistence_writes(_stub_app)


@pytest.mark.parametrize("file_path", ["data.pkl", "data.pickle"])
def test_uploaded_python_rejects_pickle_file_with_pandas_loader(file_path: str) -> None:
    files = [
        _script_file("main.py", f"import pandas\nvalue = pandas.read_pickle('{file_path}')\n"),
        _script_file(file_path, "serialized payload", mime_type="application/octet-stream"),
    ]

    with pytest.raises(HTTPException) as exc:
        script_service.validate_uploaded_script_files(files)

    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "source",
    [
        "import pandas\nvalue = pandas.read_pickle('data.bin')\n",
        "from pandas import read_pickle\nvalue = read_pickle('data.bin')\n",
        "import numpy as np\nvalue = np.load('data.npy', allow_pickle=True)\n",
        "from numpy import load\nvalue = load('data.npy', None, True)\n",
        "import numpy as np\noptions = {}\nvalue = np.load('data.npy', **options)\n",
        "import numpy as np\n(loader,) = (np.load,)\nvalue = loader('data.npy', allow_pickle=True)\n",
        "import numpy as np\nloader = [np.load][0]\nvalue = loader('data.npy', allow_pickle=True)\n",
        ("import numpy as np\ndef run(loader=np.load):\n    return loader('data.npy', allow_pickle=True)\n"),
    ],
)
def test_uploaded_python_rejects_pickle_backed_dependency_loaders(source: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_uploaded_source(source)

    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "source",
    [
        "import numpy as np\nvalue = np.load('data.npy')\n",
        "import numpy as np\nvalue = np.load('data.npy', allow_pickle=False)\n",
    ],
)
def test_uploaded_python_allows_numpy_load_without_pickle(source: str) -> None:
    _validate_uploaded_source(source)


@pytest.mark.parametrize(
    "source",
    [
        "import yaml\nvalue = yaml.unsafe_load(payload)\n",
        "from yaml import unsafe_load as load\nvalue = load(payload)\n",
        "import yaml\nvalue = yaml.load(payload, Loader=yaml.UnsafeLoader)\n",
        "import yaml\nvalue = yaml.load(payload, yaml.UnsafeLoader)\n",
        "import yaml\nvalue = yaml.load(payload, Loader=loader_type)\n",
        "import yaml\noptions = {'Loader': yaml.SafeLoader}\nvalue = yaml.load(payload, **options)\n",
        "import yaml\nload = yaml.load\nvalue = load(payload, Loader=yaml.UnsafeLoader)\n",
        "import yaml\nvalue = yaml.unsafe_load_all(payload)\n",
        "import yaml\nvalue = yaml.load_all(payload, Loader=yaml.Loader)\n",
        "import yaml\nvalue = yaml.load_all(payload)\n",
        "import yaml\nvalue = yaml.load_all(payload, yaml.CLoader)\n",
        "from yaml import unsafe_load_all as load_stream\nvalue = load_stream(payload)\n",
    ],
)
def test_uploaded_python_rejects_unsafe_yaml_loaders(source: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_uploaded_source(source)

    assert exc.value.status_code == 400
    assert "YAML" in str(exc.value.detail)


@pytest.mark.parametrize(
    "source",
    [
        "import yaml\nvalue = yaml.safe_load(payload)\n",
        "import yaml\nvalue = yaml.load(payload, Loader=yaml.SafeLoader)\n",
        "import yaml\nvalue = yaml.load(payload, yaml.SafeLoader)\n",
        "from yaml import SafeLoader, load\nvalue = load(payload, Loader=SafeLoader)\n",
        "import yaml\nvalue = yaml.safe_load_all(payload)\n",
        "import yaml\nvalue = yaml.full_load_all(payload)\n",
        "import yaml\nvalue = yaml.load_all(payload, Loader=yaml.SafeLoader)\n",
    ],
)
def test_uploaded_python_allows_safe_yaml_loaders(source: str) -> None:
    _validate_uploaded_source(source)


@pytest.mark.asyncio
async def test_create_script_route_rejects_eval_upload(_stub_app: SimpleNamespace) -> None:
    request = CreateScriptRequest(files=[_script_file("main.py", "value = eval('1 + 1')\n")])

    with pytest.raises(HTTPException) as exc:
        await scripts_routes.create_script(request, SimpleNamespace(organization_id="org_test"))

    assert exc.value.status_code == 400
    _assert_no_persistence_writes(_stub_app)


@pytest.mark.asyncio
async def test_deploy_script_route_rejects_eval_upload(_stub_app: SimpleNamespace) -> None:
    request = DeployScriptRequest(files=[_script_file("main.py", "value = eval('1 + 1')\n")])

    with pytest.raises(HTTPException) as exc:
        await scripts_routes.deploy_script(request, "s_existing", SimpleNamespace(organization_id="org_test"))

    assert exc.value.status_code == 400
    _assert_no_persistence_writes(_stub_app)


@pytest.mark.asyncio
async def test_commit_mode_accepts_blocked_names_as_parameter_field_declarations(
    _stub_app: SimpleNamespace,
) -> None:
    source = (
        """from pydantic import BaseModel


class WorkflowParameters(BaseModel):
    format: str
    system: str
    os: str
    stdout: str
"""
        + _REALISTIC_GENERATED_MAIN
    )

    response = await _deploy_files([_script_file("main.py", source)])

    assert response.script_was_created is True
    assert _stub_app.created_files[0]["file_path"] == "main.py"


@pytest.mark.asyncio
async def test_commit_mode_leaves_non_python_files_unaffected(_stub_app: SimpleNamespace) -> None:
    non_python_content = "import os\nexec('value = 1')\ndef broken(\n"
    files = [
        _script_file("main.py", _REALISTIC_GENERATED_MAIN),
        _script_file("notes.txt", non_python_content, mime_type="text/plain"),
    ]

    response = await _deploy_files(files)

    assert response.script_was_created is True
    assert [file["file_path"] for file in _stub_app.created_files] == ["main.py", "notes.txt"]
    assert _stub_app.artifacts[1]["data"] == non_python_content.encode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_path",
    [
        "../outside.py",
        "/main.py",
        "dir//main.py",
        "dir/../main.py",
        "dir\\main.py",
        "./main.py",
        "%2e%2e/outside.py",
        "main.py%00.txt",
        "C:/main.py",
    ],
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
        ScriptFileCreate.model_construct(
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
    monkeypatch.setattr(script_service, "_MAX_SCRIPT_FILE_BYTES", 4)
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


@pytest.mark.parametrize(
    "source",
    [
        "import logging.config\n",
        "from logging.config import dictConfig\ndictConfig({'version': 1})\n",
        "from logging import config\nconfig.dictConfig({'version': 1})\n",
        "import logging.config as c\nc.dictConfig({'version': 1})\n",
        "import logging\nlogging.config.dictConfig({'version': 1})\n",
        "import logging\nlogging.config.fileConfig('logging.ini')\n",
        "import logging\nlogging.config.BaseConfigurator({}).resolve('os.system')\n",
        "import logging\nlogging.config.BaseConfigurator({}).convert('ext://os.system')\n",
        "import logging\nlogging.config.dictConfigClass({}).configure()\n",
        "import logging\nlogging.config.listen(9999)\n",
    ],
    ids=[
        "import-submodule",
        "from-submodule",
        "from-parent",
        "aliased",
        "dict-config",
        "file-config",
        "base-configurator-resolve",
        "base-configurator-convert",
        "dict-config-class",
        "listen",
    ],
)
def test_uploaded_python_rejects_logging_config_callable_resolution(source: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_uploaded_source(source)

    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "source",
    [
        "import logging\nlog = logging.getLogger(__name__)\nlog.info('ready')\n",
        "from logging import getLogger\nlog = getLogger('run')\n",
    ],
    ids=["module", "from-import"],
)
def test_uploaded_python_allows_ordinary_logging(source: str) -> None:
    _validate_uploaded_source(source)
