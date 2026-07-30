"""enable_self_healing YAML semantics: omitted means inherit-on-update (None), never an implicit
disable — older clients that don't send the field must not clobber the setting. TaskV2 limit
defaults follow settings at runtime but document a static schema default."""

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.workflow.models.block import TaskV2Block
from skyvern.schemas.workflows import TaskV2BlockYAML, WorkflowCreateYAMLRequest, WorkflowDefinitionYAML


def _request(**kwargs: object) -> WorkflowCreateYAMLRequest:
    return WorkflowCreateYAMLRequest(
        title="t",
        workflow_definition=WorkflowDefinitionYAML(parameters=[], blocks=[]),
        **kwargs,
    )


def test_omitted_enable_self_healing_is_none() -> None:
    assert _request().enable_self_healing is None


def test_explicit_false_survives() -> None:
    assert _request(enable_self_healing=False).enable_self_healing is False


def test_explicit_true_survives() -> None:
    assert _request(enable_self_healing=True).enable_self_healing is True


@pytest.mark.parametrize("block_type", [TaskV2BlockYAML, TaskV2Block])
def test_task_v2_limit_defaults_keep_runtime_settings_out_of_schema(
    monkeypatch: pytest.MonkeyPatch,
    block_type: type[TaskV2BlockYAML] | type[TaskV2Block],
) -> None:
    monkeypatch.setattr(settings, "MAX_ITERATIONS_PER_TASK_V2", 51)
    monkeypatch.setattr(settings, "MAX_STEPS_PER_TASK_V2", 26)

    block = block_type.model_construct()
    properties = block_type.model_json_schema()["properties"]

    assert (block.max_iterations, block.max_steps) == (51, 26)
    assert properties["max_iterations"]["default"] == 50
    assert properties["max_steps"]["default"] == 25
