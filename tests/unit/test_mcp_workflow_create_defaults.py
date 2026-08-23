"""Tests for MCP workflow create code v2 defaults."""

import json

import pytest
import yaml

from skyvern.cli.mcp_tools import workflow as workflow_tools
from skyvern.cli.mcp_tools.workflow import (
    _CODE_V2_DEFAULTS,
    _inject_code_block_derived_steps,
    _inject_code_block_prompt_defaults,
    _inject_missing_top_level_defaults,
    _inject_workflow_update_code_block_prompt_defaults,
    _parse_definition,
)
from skyvern.forge.sdk.db.models import WorkflowModel
from skyvern.forge.sdk.db.repositories import workflows as workflow_repository
from skyvern.schemas.runs import ProxyLocation
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest

_ACTION_CODE = 'await page.goto("https://example.com")\nawait page.click("button.submit")'


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


def test_code_defaults_injected_when_not_specified() -> None:
    """The shared top-level injector adds omitted Code 2.0 defaults."""
    definition = _minimal_workflow_json()
    result = _inject_missing_top_level_defaults(definition, "json", _CODE_V2_DEFAULTS)
    parsed = json.loads(result)
    assert parsed["code_version"] == 2
    assert parsed["run_with"] == "agent"


def test_code_defaults_injected_in_auto_mode() -> None:
    """Auto format also injects defaults for JSON input."""
    definition = _minimal_workflow_json()
    result = _inject_missing_top_level_defaults(definition, "auto", _CODE_V2_DEFAULTS)
    parsed = json.loads(result)
    assert parsed["code_version"] == 2
    assert parsed["run_with"] == "agent"


@pytest.mark.parametrize(
    ("overrides", "expected_code_version", "expected_run_with"),
    [
        ({"code_version": 1, "run_with": "code"}, 1, "code"),
        ({"code_version": None, "run_with": None}, None, None),
    ],
)
def test_explicit_code_default_values_and_nulls_preserved_for_yaml(
    overrides: dict[str, object],
    expected_code_version: int | None,
    expected_run_with: str | None,
) -> None:
    """Top-level default injection is membership-based, including for YAML nulls."""
    definition = yaml.safe_dump(json.loads(_minimal_workflow_json(**overrides)), sort_keys=False)
    result = _inject_missing_top_level_defaults(definition, "yaml", _CODE_V2_DEFAULTS)
    parsed = yaml.safe_load(result)
    assert parsed["code_version"] == expected_code_version
    assert parsed["run_with"] == expected_run_with


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
    result = _inject_missing_top_level_defaults(bad_json, "json", _CODE_V2_DEFAULTS)
    assert result == bad_json


@pytest.mark.asyncio
async def test_create_yaml_persists_code_v2_defaults_without_disturbing_code_block_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """YAML defaults reach the repository model after the full MCP create pipeline."""
    backend_requests: list[WorkflowCreateYAMLRequest] = []
    repository_rows: list[WorkflowModel] = []

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: object,
        ) -> None:
            return None

        def add(self, workflow: WorkflowModel) -> None:
            repository_rows.append(workflow)

        async def commit(self) -> None:
            return None

        async def refresh(self, workflow: WorkflowModel) -> None:
            return None

    repository = workflow_repository.WorkflowsRepository(FakeSession)  # type: ignore[arg-type]
    monkeypatch.setattr(workflow_repository, "convert_to_workflow", lambda model, debug_enabled: model)

    async def fake_create_workflow_raw(
        *,
        json_definition: dict[str, object] | None,
        yaml_definition: str | None,
        folder_id: str | None,
    ) -> dict[str, object]:
        assert json_definition is None
        assert yaml_definition is not None
        assert folder_id is None
        backend_request = WorkflowCreateYAMLRequest.model_validate(yaml.safe_load(yaml_definition))
        backend_requests.append(backend_request)
        await repository.create_workflow(
            title=backend_request.title,
            workflow_definition=backend_request.workflow_definition.model_dump(mode="json"),
            proxy_location=backend_request.proxy_location,
            run_with=backend_request.run_with,
            code_version=backend_request.code_version,
        )
        repository_row = repository_rows[-1]
        return {
            "workflow_permanent_id": "wpid_test",
            "workflow_id": "wf_test",
            "title": repository_row.title,
            "version": 1,
            "status": "published",
            "run_with": repository_row.run_with,
            "code_version": repository_row.code_version,
        }

    monkeypatch.setattr(workflow_tools, "create_workflow_raw", fake_create_workflow_raw)
    definition = f"""
title: YAML Code Workflow
workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: step1
      code: |
        {_ACTION_CODE.replace(chr(10), chr(10) + "        ")}
"""

    result = await workflow_tools.skyvern_workflow_create(definition=definition, format="yaml")

    assert result["ok"] is True, result
    assert len(backend_requests) == 1
    assert len(repository_rows) == 1
    backend_request = backend_requests[0]
    repository_row = repository_rows[0]
    assert repository_row.code_version == 2
    assert repository_row.run_with == "agent"
    assert backend_request.code_version == 2
    block = backend_request.workflow_definition.blocks[0]
    assert block.prompt == ""
    assert block.steps is not None
    assert [step.action_type for step in block.steps] == ["goto_url", "click"]


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


def test_code_block_steps_derived_for_code_first_block() -> None:
    """A code-first block (non-null prompt) without steps gets them derived from its code."""
    definition = _code_workflow_json(
        [{"block_type": "code", "label": "step1", "code": _ACTION_CODE, "prompt": "Open the page and submit"}]
    )
    result = _inject_code_block_derived_steps(definition, "json")
    steps = json.loads(result)["workflow_definition"]["blocks"][0]["steps"]
    assert [step["action_type"] for step in steps] == ["goto_url", "click"]
    assert [(step["line_start"], step["line_end"]) for step in steps] == [(1, 1), (2, 2)]
    assert all(step["description"] for step in steps)


def test_code_block_steps_derived_for_yaml() -> None:
    yaml_str = """
title: Test
workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: step1
      prompt: ""
      code: |
        await page.goto("https://example.com")
"""
    result = _inject_code_block_derived_steps(yaml_str, "yaml")
    steps = yaml.safe_load(result)["workflow_definition"]["blocks"][0]["steps"]
    assert [step["action_type"] for step in steps] == ["goto_url"]


def test_code_block_steps_not_derived_without_prompt() -> None:
    """Blocks without a prompt (missing key or explicit null) are legacy and get no steps."""
    for prompt_fields in ({}, {"prompt": None}):
        definition = _code_workflow_json(
            [{"block_type": "code", "label": "step1", "code": _ACTION_CODE, **prompt_fields}]
        )
        result = _inject_code_block_derived_steps(definition, "json")
        assert result == definition


def test_code_block_explicit_steps_preserved() -> None:
    explicit = [{"description": "Author step", "action_type": "click", "line_start": 1, "line_end": 1}]
    definition = _code_workflow_json(
        [{"block_type": "code", "label": "step1", "code": _ACTION_CODE, "prompt": "", "steps": explicit}]
    )
    result = _inject_code_block_derived_steps(definition, "json")
    assert json.loads(result)["workflow_definition"]["blocks"][0]["steps"] == explicit


def test_code_block_explicit_empty_steps_preserved() -> None:
    """An explicitly-supplied `steps: []` is an authored empty outline, not absence."""
    definition = _code_workflow_json(
        [{"block_type": "code", "label": "step1", "code": _ACTION_CODE, "prompt": "", "steps": []}]
    )
    result = _inject_code_block_derived_steps(definition, "json")
    assert json.loads(result)["workflow_definition"]["blocks"][0]["steps"] == []


def test_code_block_null_steps_derived() -> None:
    """`steps: null` is the workflow-get shape for blocks without steps, so a get -> edit -> update
    round trip must derive it like an omitted key."""
    definition = _code_workflow_json(
        [{"block_type": "code", "label": "step1", "code": _ACTION_CODE, "prompt": "", "steps": None}]
    )
    result = _inject_code_block_derived_steps(definition, "json")
    steps = json.loads(result)["workflow_definition"]["blocks"][0]["steps"]
    assert [step["action_type"] for step in steps] == ["goto_url", "click"]


def test_code_block_actionless_code_unchanged() -> None:
    """Code with no browser actions derives no steps; the definition passes through untouched."""
    definition = _code_workflow_json([{"block_type": "code", "label": "step1", "code": "x = 1", "prompt": ""}])
    result = _inject_code_block_derived_steps(definition, "json")
    assert result == definition


def test_code_block_steps_derived_inside_for_loop() -> None:
    definition = _code_workflow_json(
        [
            {
                "block_type": "for_loop",
                "label": "loop",
                "loop_over_parameter_key": "items",
                "loop_blocks": [{"block_type": "code", "label": "inner", "code": _ACTION_CODE, "prompt": ""}],
            }
        ]
    )
    result = _inject_code_block_derived_steps(definition, "json")
    inner = json.loads(result)["workflow_definition"]["blocks"][0]["loop_blocks"][0]
    assert [step["action_type"] for step in inner["steps"]] == ["goto_url", "click"]


def test_code_block_steps_invalid_json_passthrough() -> None:
    bad_json = "not valid json {"
    assert _inject_code_block_derived_steps(bad_json, "json") == bad_json


def test_create_pipeline_defaults_prompt_then_derives_steps() -> None:
    """The create-path composition: an omitted prompt defaults to "" and steps are then derived."""
    definition = _code_workflow_json([{"block_type": "code", "label": "step1", "code": _ACTION_CODE}])
    result = _inject_code_block_prompt_defaults(definition, "json", existing_code_labels=frozenset())
    result = _inject_code_block_derived_steps(result, "json")
    block = json.loads(result)["workflow_definition"]["blocks"][0]
    assert block["prompt"] == ""
    assert [step["action_type"] for step in block["steps"]] == ["goto_url", "click"]


def test_create_explicit_null_prompt_block_round_trips_unchanged() -> None:
    """AC3 on create: a legacy block cloned via workflow get (prompt: null) gets neither prompt nor steps."""
    definition = _code_workflow_json([{"block_type": "code", "label": "legacy", "code": _ACTION_CODE, "prompt": None}])
    result = _inject_code_block_prompt_defaults(definition, "json", existing_code_labels=frozenset())
    result = _inject_code_block_derived_steps(result, "json")
    assert json.loads(result) == json.loads(definition)


@pytest.mark.asyncio
async def test_update_existing_old_code_block_round_trips_unchanged() -> None:
    """AC3 on update: an existing old code block resubmitted as-is gets neither prompt nor steps,
    while a brand-new block in the same update gets both."""
    existing = {
        "workflow_definition": {
            "blocks": [{"block_type": "code", "label": "old_block", "code": _ACTION_CODE}],
        }
    }

    async def fetch_existing() -> dict[str, object]:
        return existing

    definition = _code_workflow_json(
        [
            {"block_type": "code", "label": "old_block", "code": _ACTION_CODE},
            {"block_type": "code", "label": "brand_new", "code": _ACTION_CODE},
        ]
    )
    result = await _inject_workflow_update_code_block_prompt_defaults(definition, "json", fetch_existing)
    result = _inject_code_block_derived_steps(result, "json")
    blocks = json.loads(result)["workflow_definition"]["blocks"]
    assert blocks[0] == {"block_type": "code", "label": "old_block", "code": _ACTION_CODE}
    assert blocks[1]["prompt"] == ""
    assert [step["action_type"] for step in blocks[1]["steps"]] == ["goto_url", "click"]


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
