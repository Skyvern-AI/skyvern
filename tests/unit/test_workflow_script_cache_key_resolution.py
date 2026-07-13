from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from jinja2 import UndefinedError

from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.workflow.models.workflow import Workflow, is_adaptive_caching_from_effective_state
from skyvern.services import workflow_script_service
from skyvern.services.workflow_script_service import (
    CacheKeyResolutionError,
    detect_workflow_platform,
    detect_workflow_platform_for_tagging,
    resolve_cache_key_value,
)


def _workflow(cache_key: str | None = "default", url: str | None = "https://example.com/login") -> Workflow:
    blocks = []
    if url is not None:
        blocks.append(
            {
                "block_type": "navigation",
                "label": "open_site",
                "url": url,
                "navigation_goal": "Open the site",
                "output_parameter": {
                    "parameter_type": "output",
                    "key": "open_site_output",
                    "output_parameter_id": "op_test",
                    "workflow_id": "wf_test",
                    "created_at": datetime.now(timezone.utc),
                    "modified_at": datetime.now(timezone.utc),
                },
            }
        )

    return Workflow(
        workflow_id="wf_test",
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        title="test",
        version=1,
        is_saved_task=False,
        workflow_definition={"parameters": [], "blocks": blocks},
        run_with="code",
        cache_key=cache_key,
        code_version=2,
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )


@pytest.fixture(autouse=True)
def _stub_platform_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        workflow_script_service.app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            detect_ats_platform=lambda domain: "known_platform" if domain == "ats.example.com" else None,
            detect_platform_for_tagging=lambda domain: "tagging_platform" if domain == "tagging.example.com" else None,
        ),
    )


def test_resolve_default_cache_key_enriches_with_block_domain() -> None:
    workflow = _workflow(cache_key="default", url="https://essentials.example.com/login")

    assert resolve_cache_key_value(workflow, {}, adaptive_caching=False) == "default:essentials.example.com"


def test_resolve_empty_cache_key_uses_platform_when_detected() -> None:
    workflow = _workflow(cache_key="", url="https://ats.example.com/login")

    assert resolve_cache_key_value(workflow, {}, adaptive_caching=False) == "known_platform"


def test_detect_workflow_platform_uses_first_resolved_block_domain() -> None:
    workflow = _workflow(cache_key="custom", url="https://ats.example.com/login")

    assert detect_workflow_platform(workflow, {}) == "known_platform"


def test_detect_workflow_platform_returns_none_when_detector_has_no_verdict() -> None:
    workflow = _workflow(cache_key="custom", url="https://essentials.example.com/login")

    assert detect_workflow_platform(workflow, {}) is None


def test_cache_key_ignores_tagging_only_platforms() -> None:
    workflow = _workflow(cache_key="default", url="https://tagging.example.com/jobs/1")

    assert resolve_cache_key_value(workflow, {}, adaptive_caching=False) == "default:tagging.example.com"


def test_tagging_detector_uses_tagging_only_platforms() -> None:
    workflow = _workflow(cache_key="default", url="https://tagging.example.com/jobs/1")

    assert detect_workflow_platform_for_tagging(workflow, {}) == "tagging_platform"


def test_oss_platform_tagging_is_noop() -> None:
    assert AgentFunction().detect_platform_for_tagging("https://tagging.example.com/jobs/1") is None


def test_resolve_appends_v2_when_adaptive_caching_enabled() -> None:
    workflow = _workflow(cache_key="custom", url=None)

    assert resolve_cache_key_value(workflow, {}, adaptive_caching=True) == "custom:v2"


def test_strict_mode_errors_on_missing_cache_key_variable() -> None:
    workflow = _workflow(cache_key="{{ payer }}:custom", url=None)

    with pytest.raises(CacheKeyResolutionError):
        resolve_cache_key_value(workflow, {}, adaptive_caching=False, strict=True)


def test_strict_mode_errors_on_missing_block_url_variable_for_default_key() -> None:
    workflow = _workflow(cache_key="default", url="{{ host }}/login")

    with pytest.raises(CacheKeyResolutionError):
        resolve_cache_key_value(workflow, {}, adaptive_caching=False, strict=True)


def test_tolerant_domain_resolution_preserves_existing_swallow_behavior() -> None:
    workflow = _workflow(cache_key="default", url="{{ host }}/login")

    assert resolve_cache_key_value(workflow, {}, adaptive_caching=False, strict=False) == "default:https:///login"


def test_tolerant_resolution_swallows_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = _workflow(cache_key="custom", url=None)

    def _raise(*_: object, **__: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(workflow_script_service, "_render_cache_template", _raise)

    assert resolve_cache_key_value(workflow, {}, adaptive_caching=False, strict=False) == ""


def test_strict_block_url_helper_surfaces_template_errors() -> None:
    with pytest.raises(UndefinedError):
        workflow_script_service._resolve_block_url_for_cache_key("{{ host }}/login", {}, strict=True)


def test_domain_override_allows_strict_default_key_without_block_url_context() -> None:
    workflow = _workflow(cache_key="default", url="{{ host }}/login")

    assert (
        resolve_cache_key_value(
            workflow,
            {},
            adaptive_caching=True,
            strict=True,
            domain_override="essentials.example.com",
        )
        == "default:essentials.example.com:v2"
    )


@pytest.mark.parametrize(
    ("workflow_run_with", "run_run_with", "code_version", "adaptive_caching", "expected"),
    [
        ("code", None, 2, False, True),
        ("code", None, 1, True, False),
        ("code", None, None, True, True),
        ("agent", "code", 2, False, True),
        ("code", "agent", 2, False, False),
    ],
)
def test_adaptive_caching_from_effective_state(
    workflow_run_with: str,
    run_run_with: str | None,
    code_version: int | None,
    adaptive_caching: bool,
    expected: bool,
) -> None:
    assert (
        is_adaptive_caching_from_effective_state(
            workflow_run_with=workflow_run_with,
            run_run_with=run_run_with,
            code_version=code_version,
            adaptive_caching=adaptive_caching,
        )
        is expected
    )
