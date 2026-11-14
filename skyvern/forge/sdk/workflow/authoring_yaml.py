from __future__ import annotations

from copy import deepcopy
from typing import Any

import structlog
import yaml
from pydantic import ValidationError

from skyvern.forge.sdk.workflow.models.parameter import ParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.forge.sdk.workflow.private_settings import strip_private_workflow_settings
from skyvern.forge.sdk.workflow.secret_encryption import mask_workflow_definition_secrets
from skyvern.schemas.workflows import BlockType, WorkflowCreateYAMLRequest

LOG = structlog.get_logger()


def strip_runtime_block_fields(block: dict[str, Any]) -> dict[str, Any]:
    cleaned = deepcopy(block)
    cleaned.pop("output_parameter", None)
    cleaned.pop("workflow_system_prompt", None)

    parameters = cleaned.pop("parameters", None)
    if isinstance(parameters, list) and "parameter_keys" not in cleaned:
        parameter_keys = [
            parameter.get("key")
            for parameter in parameters
            if isinstance(parameter, dict)
            and parameter.get("key")
            and parameter.get("parameter_type") != ParameterType.OUTPUT.value
        ]
        if parameter_keys:
            cleaned["parameter_keys"] = parameter_keys

    loop_over = cleaned.pop("loop_over", None)
    if isinstance(loop_over, dict) and "loop_over_parameter_key" not in cleaned:
        loop_over_parameter_key = loop_over.get("key")
        if loop_over_parameter_key:
            cleaned["loop_over_parameter_key"] = loop_over_parameter_key

    loop_blocks = cleaned.get("loop_blocks")
    if isinstance(loop_blocks, list):
        cleaned["loop_blocks"] = [
            strip_runtime_block_fields(loop_block) if isinstance(loop_block, dict) else loop_block
            for loop_block in loop_blocks
        ]
    return cleaned


def runs_in_list_order(definition: dict[str, Any]) -> bool:
    """Whether the top level runs in list order (version 1), deriving a missing version as the schema validator does."""
    version = definition.get("version")
    if version is not None:
        return isinstance(version, int) and version < 2
    blocks = definition.get("blocks")
    return not (
        isinstance(blocks, list)
        and any(
            isinstance(block, dict)
            and (block.get("block_type") == BlockType.CONDITIONAL or block.get("next_block_label") is not None)
            for block in blocks
        )
    )


def _drop_list_order_chain(definition: dict[str, Any]) -> None:
    # Legacy version 1 rows carry stitched next_block_label pointers that validation rejects at version 1.
    blocks = definition.get("blocks")
    if not (
        isinstance(definition.get("version"), int)
        and runs_in_list_order(definition)
        and isinstance(blocks, list)
        and all(isinstance(block, dict) and block.get("block_type") != BlockType.CONDITIONAL for block in blocks)
    ):
        return
    following = [block.get("label") for block in blocks[1:]] + [None]
    if all(block.get("next_block_label") == label for block, label in zip(blocks, following)):
        for block in blocks:
            block.pop("next_block_label", None)


def authoring_yaml_workflow_definition(persisted_definition: dict[str, Any]) -> dict[str, Any]:
    workflow_definition = deepcopy(persisted_definition)
    parameters = workflow_definition.get("parameters")
    if isinstance(parameters, list):
        workflow_definition["parameters"] = [
            parameter
            for parameter in parameters
            if not (isinstance(parameter, dict) and parameter.get("parameter_type") == ParameterType.OUTPUT.value)
        ]
        for parameter in workflow_definition["parameters"]:
            if isinstance(parameter, dict) and parameter.get("parameter_type") == ParameterType.CONTEXT.value:
                source = parameter.get("source")
                if isinstance(source, dict) and isinstance(source.get("key"), str):
                    parameter["source_parameter_key"] = source["key"]
                    parameter.pop("source")
                    parameter.pop("value", None)

    blocks = workflow_definition.get("blocks")
    if isinstance(blocks, list):
        workflow_definition["blocks"] = [
            strip_runtime_block_fields(block) if isinstance(block, dict) else block for block in blocks
        ]
    _drop_list_order_chain(workflow_definition)
    return workflow_definition


def workflow_to_authoring_yaml(workflow: Workflow) -> str:
    workflow_data = workflow.model_dump(mode="json", exclude_none=True)
    strip_private_workflow_settings(workflow_data)
    mask_workflow_definition_secrets(workflow_data.get("workflow_definition"))
    workflow_definition = authoring_yaml_workflow_definition(workflow_data.get("workflow_definition") or {})

    request_data = {
        key: workflow_data[key]
        for key in WorkflowCreateYAMLRequest.model_fields
        if key != "workflow_definition" and key in workflow_data
    }
    request_data["workflow_definition"] = workflow_definition

    try:
        workflow_request = WorkflowCreateYAMLRequest.model_validate(request_data)
        yaml_data = workflow_request.model_dump(mode="json", exclude_none=True)
    except ValidationError:
        LOG.warning(
            "Persisted workflow did not round-trip through the authoring YAML schema; using best-effort workflow dump",
            workflow_id=workflow.workflow_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            exc_info=True,
        )
        yaml_data = request_data
    return yaml.safe_dump(yaml_data, sort_keys=False)
