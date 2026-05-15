import base64
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.schemas.scripts import (
    DeployCachedScriptCacheContext,
    DeployCachedScriptRequest,
    FileEncoding,
    ScriptFileCreate,
)
from skyvern.services import cached_script_deploy_service


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("utf-8")


def _workflow(*, cache_key: str | None = "default", version: int = 3) -> Workflow:
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
        run_with="code",
        cache_key=cache_key,
        code_version=2,
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )


def _request(
    source: str,
    *,
    resolved_cache_key_value: str | None = None,
    dry_run: bool = True,
    cache_key: str | None = "default",
) -> DeployCachedScriptRequest:
    return DeployCachedScriptRequest(
        workflow_id="wf_latest",
        workflow_version=3,
        cache_key=cache_key,
        cache_context=DeployCachedScriptCacheContext(parameters={}, adaptive_caching=True),
        resolved_cache_key_value=resolved_cache_key_value,
        dry_run=dry_run,
        files=[
            ScriptFileCreate(
                path="main.py",
                content=_b64(source),
                encoding=FileEncoding.BASE64,
                mime_type="text/x-python",
            )
        ],
    )


@pytest.fixture(autouse=True)
def _stub_app(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = _workflow()

    class Workflows:
        async def get_workflow_by_permanent_id(self, **_: object) -> Workflow:
            return workflow

    monkeypatch.setattr(
        cached_script_deploy_service.app,
        "DATABASE",
        SimpleNamespace(workflows=Workflows()),
    )
    monkeypatch.setattr(
        cached_script_deploy_service.app,
        "AGENT_FUNCTION",
        SimpleNamespace(detect_ats_platform=lambda domain: None),
    )


@pytest.mark.asyncio
async def test_dry_run_returns_cache_key_and_block_plan() -> None:
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
    await skyvern.validate(complete_criterion="done", terminate_criterion="stop", label="check")
"""

    response = await cached_script_deploy_service.dry_run_cached_script_deploy(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        request=_request(source, resolved_cache_key_value="default:example.com:v2"),
    )

    assert response.dry_run is True
    assert response.would_create_script is True
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

    response = await cached_script_deploy_service.dry_run_cached_script_deploy(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        request=_request(source, resolved_cache_key_value="default:example.com:v2", cache_key=None),
    )

    assert response.cache_key == "default"
    assert response.cache_key_value == "default:example.com:v2"


@pytest.mark.asyncio
async def test_dry_run_rejects_commit_mode_until_writes_are_implemented() -> None:
    with pytest.raises(HTTPException) as exc:
        await cached_script_deploy_service.dry_run_cached_script_deploy(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request("import skyvern\nasync def run(parameters):\n    pass\n", dry_run=False),
        )

    assert exc.value.status_code == 400
    assert "Commit mode is not enabled" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_dry_run_rejects_missing_globals() -> None:
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(url=LOGIN_URL, prompt="...", label="step_a", cache_key="step_a")
"""

    with pytest.raises(HTTPException) as exc:
        await cached_script_deploy_service.dry_run_cached_script_deploy(
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
        await cached_script_deploy_service.dry_run_cached_script_deploy(
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
        await cached_script_deploy_service.dry_run_cached_script_deploy(
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            request=_request(source, resolved_cache_key_value="wrong:v2"),
        )

    assert exc.value.status_code == 400
    assert "Resolved cache key value mismatch" in str(exc.value.detail)
