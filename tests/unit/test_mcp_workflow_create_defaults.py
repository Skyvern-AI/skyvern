"""Tests for MCP workflow create code v2 defaults."""

import json

import pytest
import yaml

from skyvern.cli.mcp_tools.workflow import (
    _inject_code_block_prompt_defaults,
    _inject_code_v2_defaults,
    _inject_missing_top_level_defaults,
    _inject_workflow_update_code_block_prompt_defaults,
    _parse_definition,
)
from skyvern.schemas.runs import ProxyLocation


def _minimal_workflow_json(**overrides: object) -> str:
    """Return a minimal valid workflow JSON string with optional field overrides."""
    base: dict[str, object] = {
        "title": "Test Workflow",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "navigation",
                    "label": "step1",
                    "url": "https://example.com",
                    "title": "Step 1",
                    "navigation_goal": "Click the button",
                }
            ],
        },
    }
    base.update(overrides)
    return json.dumps(base)


def test_defaults_injected_when_not_specified() -> None:
    """When code_version and run_with are omitted, _inject_code_v2_defaults adds them."""
    definition = _minimal_workflow_json()
    result = _inject_code_v2_defaults(definition, "json")
    parsed = json.loads(result)
    assert parsed["code_version"] == 2
    assert parsed["run_with"] == "agent"


def test_defaults_injected_in_auto_mode() -> None:
    """Auto format also injects defaults for JSON input."""
    definition = _minimal_workflow_json()
    result = _inject_code_v2_defaults(definition, "auto")
    parsed = json.loads(result)
    assert parsed["code_version"] == 2
    assert parsed["run_with"] == "agent"


def test_explicit_values_preserved() -> None:
    """When the user explicitly sets these fields, their values are preserved."""
    definition = _minimal_workflow_json(code_version=1, run_with="code")
    result = _inject_code_v2_defaults(definition, "json")
    parsed = json.loads(result)
    assert parsed["code_version"] == 1
    assert parsed["run_with"] == "code"


def test_explicit_null_run_with_preserved() -> None:
    """When the user explicitly sets run_with to null, it stays null."""
    definition = _minimal_workflow_json(run_with=None)
    result = _inject_code_v2_defaults(definition, "json")
    parsed = json.loads(result)
    assert parsed["run_with"] is None
    # code_version was not set, so it gets the default
    assert parsed["code_version"] == 2


def test_proxy_default_injected_when_not_specified_json() -> None:
    """MCP create should default omitted proxy_location to residential US."""
    definition = _minimal_workflow_json()
    result = _inject_missing_top_level_defaults(
        definition,
        "json",
        {"proxy_location": ProxyLocation.RESIDENTIAL},
    )
    parsed = json.loads(result)
    assert parsed["proxy_location"] == ProxyLocation.RESIDENTIAL


def test_explicit_null_proxy_location_preserved_json() -> None:
    """An explicit null proxy_location should not be overwritten by the default injector."""
    definition = _minimal_workflow_json(proxy_location=None)
    result = _inject_missing_top_level_defaults(
        definition,
        "json",
        {"proxy_location": ProxyLocation.RESIDENTIAL},
    )
    parsed = json.loads(result)
    assert "proxy_location" in parsed
    assert parsed["proxy_location"] is None


def test_proxy_default_injected_for_yaml() -> None:
    """YAML definitions should receive the same omitted proxy default."""
    yaml_str = """
title: Test
workflow_definition:
  parameters: []
  blocks:
    - block_type: navigation
      label: step1
      url: https://example.com
      title: Step 1
      navigation_goal: Click the button
"""
    result = _inject_missing_top_level_defaults(
        yaml_str,
        "yaml",
        {"proxy_location": ProxyLocation.RESIDENTIAL},
    )
    parsed = yaml.safe_load(result)
    assert parsed["proxy_location"] == ProxyLocation.RESIDENTIAL


def test_invalid_json_passthrough() -> None:
    """Invalid JSON is passed through (let _parse_definition handle the error)."""
    bad_json = "not valid json {"
    result = _inject_code_v2_defaults(bad_json, "json")
    assert result == bad_json


def _code_workflow_json(blocks: list[dict[str, object]]) -> str:
    return json.dumps(
        {
            "title": "Test Workflow",
            "workflow_definition": {"parameters": [], "blocks": blocks},
        }
    )


def test_code_block_prompt_defaulted_on_create() -> None:
    """A code block without a prompt key gets prompt "" (the editor's new-block default)."""
    definition = _code_workflow_json([{"block_type": "code", "label": "step1", "code": "x = 1"}])
    result = _inject_code_block_prompt_defaults(definition, "json", existing_code_labels=frozenset())
    blocks = json.loads(result)["workflow_definition"]["blocks"]
    assert blocks[0]["prompt"] == ""


def test_code_block_explicit_prompt_preserved() -> None:
    definition = _code_workflow_json([{"block_type": "code", "label": "step1", "code": "x = 1", "prompt": "Do X"}])
    result = _inject_code_block_prompt_defaults(definition, "json", existing_code_labels=frozenset())
    blocks = json.loads(result)["workflow_definition"]["blocks"]
    assert blocks[0]["prompt"] == "Do X"


def test_code_block_explicit_null_prompt_preserved() -> None:
    """An explicit null prompt (e.g. a legacy block round-tripped through workflow get) stays null."""
    definition = _code_workflow_json([{"block_type": "code", "label": "step1", "code": "x = 1", "prompt": None}])
    result = _inject_code_block_prompt_defaults(definition, "json", existing_code_labels=frozenset())
    blocks = json.loads(result)["workflow_definition"]["blocks"]
    assert blocks[0]["prompt"] is None


def test_code_block_prompt_not_defaulted_for_existing_label() -> None:
    """On update, an existing code block resubmitted without a prompt key is not migrated."""
    definition = _code_workflow_json(
        [
            {"block_type": "code", "label": "old_block", "code": "x = 1"},
            {"block_type": "code", "label": "new_block", "code": "y = 2"},
        ]
    )
    result = _inject_code_block_prompt_defaults(definition, "json", existing_code_labels=frozenset({"old_block"}))
    blocks = json.loads(result)["workflow_definition"]["blocks"]
    assert "prompt" not in blocks[0]
    assert blocks[1]["prompt"] == ""


def test_code_block_prompt_defaulted_inside_for_loop() -> None:
    definition = _code_workflow_json(
        [
            {
                "block_type": "for_loop",
                "label": "loop",
                "loop_over_parameter_key": "items",
                "loop_blocks": [{"block_type": "code", "label": "inner", "code": "x = 1"}],
            }
        ]
    )
    result = _inject_code_block_prompt_defaults(definition, "json", existing_code_labels=frozenset())
    loop = json.loads(result)["workflow_definition"]["blocks"][0]
    assert loop["loop_blocks"][0]["prompt"] == ""


def test_non_code_blocks_untouched_by_prompt_default() -> None:
    definition = _minimal_workflow_json()
    result = _inject_code_block_prompt_defaults(definition, "json", existing_code_labels=frozenset())
    blocks = json.loads(result)["workflow_definition"]["blocks"]
    assert "prompt" not in blocks[0]


def test_code_block_prompt_defaulted_for_yaml() -> None:
    yaml_str = """
title: Test
workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: step1
      code: x = 1
"""
    result = _inject_code_block_prompt_defaults(yaml_str, "yaml", existing_code_labels=frozenset())
    parsed = yaml.safe_load(result)
    assert parsed["workflow_definition"]["blocks"][0]["prompt"] == ""


def test_code_block_prompt_invalid_json_passthrough() -> None:
    bad_json = "not valid json {"
    result = _inject_code_block_prompt_defaults(bad_json, "json", existing_code_labels=frozenset())
    assert result == bad_json


@pytest.mark.asyncio
async def test_update_wrapper_excludes_existing_code_labels_including_nested() -> None:
    existing = {
        "workflow_definition": {
            "blocks": [
                {"block_type": "code", "label": "old_top", "code": "x = 1"},
                {
                    "block_type": "for_loop",
                    "label": "loop",
                    "loop_blocks": [{"block_type": "code", "label": "old_nested", "code": "y = 2"}],
                },
            ]
        }
    }

    async def fetch_existing() -> dict[str, object]:
        return existing

    definition = _code_workflow_json(
        [
            {"block_type": "code", "label": "old_top", "code": "x = 1"},
            {"block_type": "code", "label": "old_nested", "code": "y = 2"},
            {"block_type": "code", "label": "brand_new", "code": "z = 3"},
        ]
    )
    result = await _inject_workflow_update_code_block_prompt_defaults(definition, "json", fetch_existing)
    blocks = json.loads(result)["workflow_definition"]["blocks"]
    assert "prompt" not in blocks[0]
    assert "prompt" not in blocks[1]
    assert blocks[2]["prompt"] == ""


def test_parse_definition_unaffected() -> None:
    """_parse_definition itself does NOT inject defaults (used by both create and update)."""
    definition = _minimal_workflow_json()
    json_def, _, err = _parse_definition(definition, "json")
    assert err is None
    assert json_def is not None
    assert isinstance(json_def, dict)
    # run_with should be "agent" (schema default), not "code"
    assert json_def.get("run_with") == "agent"
    assert json_def.get("code_version") != 2
