from datetime import datetime, timezone
from typing import Any, TypeAlias, cast

import pytest

from skyvern.client.types.for_loop_block_yaml_loop_blocks_item import (
    ForLoopBlockYamlLoopBlocksItem_Validation,
)
from skyvern.client.types.validation_block_yaml import ValidationBlockYaml
from skyvern.client.types.while_loop_block_yaml_loop_blocks_item import (
    WhileLoopBlockYamlLoopBlocksItem_Validation,
)
from skyvern.client.types.workflow_definition_yaml_blocks_item import (
    WorkflowDefinitionYamlBlocksItem_Validation,
)
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.schemas.tasks import TaskType
from skyvern.forge.sdk.workflow.exceptions import InvalidWorkflowDefinition
from skyvern.forge.sdk.workflow.models.block import ValidationBlock
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
    OutputParameter,
    ParameterType,
)
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.workflows import ValidationBlockYAML
from skyvern.webeye.actions.actions import CompleteAction, TerminateAction
from skyvern.webeye.actions.models import DetailedAgentStepOutput
from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess


def _output_parameter(label: str) -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=f"{label}_output",
        description="test output",
        output_parameter_id=f"op_{label}",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )


class TestValidationBlockExtractionFields:
    def test_yaml_accepts_extraction_fields(self) -> None:
        schema = {"type": "object", "properties": {"price": {"type": "number"}}}
        block_yaml = ValidationBlockYAML(
            label="validate_cart",
            navigation_goal="validate the cart total",
            complete_criterion="cart total matches expected total",
            data_extraction_goal="extract the cart subtotal as price",
            data_schema=schema,
        )

        assert block_yaml.navigation_goal == "validate the cart total"
        assert block_yaml.data_extraction_goal == "extract the cart subtotal as price"
        assert block_yaml.data_schema == schema

    def test_yaml_defaults_extraction_fields_to_none(self) -> None:
        block_yaml = ValidationBlockYAML(
            label="validate_cart",
            complete_criterion="cart total matches expected total",
        )

        assert block_yaml.navigation_goal is None
        assert block_yaml.data_extraction_goal is None
        assert block_yaml.data_schema is None

    def test_converter_passes_extraction_fields_to_validation_block(self) -> None:
        schema = {"type": "object", "properties": {"price": {"type": "number"}}}
        block_yaml = ValidationBlockYAML(
            label="validate_cart",
            navigation_goal="validate the cart total",
            terminate_criterion="cart is empty",
            data_extraction_goal="extract the cart subtotal as price",
            data_schema=schema,
        )
        parameters: dict[str, PARAMETER_TYPE] = {
            "validate_cart_output": cast(PARAMETER_TYPE, _output_parameter("validate_cart")),
        }

        block = block_yaml_to_block(block_yaml, parameters)

        assert isinstance(block, ValidationBlock)
        assert block.navigation_goal == "validate the cart total"
        assert block.data_extraction_goal == "extract the cart subtotal as price"
        assert block.data_schema == schema
        assert block.terminate_criterion == "cart is empty"
        assert block.disable_cache is False

    def test_converter_threads_disable_cache_to_validation_block(self) -> None:
        block_yaml = ValidationBlockYAML(
            label="validate_cart",
            complete_criterion="cart total matches expected total",
            disable_cache=True,
        )
        parameters: dict[str, PARAMETER_TYPE] = {
            "validate_cart_output": cast(PARAMETER_TYPE, _output_parameter("validate_cart")),
        }

        block = block_yaml_to_block(block_yaml, parameters)

        assert isinstance(block, ValidationBlock)
        assert block.disable_cache is True

    def test_converter_rejects_extraction_goal_without_navigation_goal(self) -> None:
        block_yaml = ValidationBlockYAML(
            label="validate_cart",
            complete_criterion="cart total matches expected total",
            data_extraction_goal="extract the cart subtotal as price",
        )
        parameters: dict[str, PARAMETER_TYPE] = {
            "validate_cart_output": cast(PARAMETER_TYPE, _output_parameter("validate_cart")),
        }

        with pytest.raises(InvalidWorkflowDefinition, match="navigation_goal"):
            block_yaml_to_block(block_yaml, parameters)

    def test_yaml_roundtrip_preserves_extraction_fields(self) -> None:
        schema = {"type": "object", "properties": {"price": {"type": "number"}}}
        original = ValidationBlockYAML(
            label="validate_cart",
            navigation_goal="validate the cart total",
            complete_criterion="cart total matches expected total",
            terminate_criterion="cart is empty",
            data_extraction_goal="extract the cart subtotal as price",
            data_schema=schema,
            error_code_mapping={"NO_CART": "cart is missing"},
            parameter_keys=["expected_total"],
            disable_cache=True,
        )

        dumped = original.model_dump()
        restored = ValidationBlockYAML.model_validate(dumped)

        assert restored.navigation_goal == original.navigation_goal
        assert restored.data_extraction_goal == original.data_extraction_goal
        assert restored.data_schema == original.data_schema
        assert restored.complete_criterion == original.complete_criterion
        assert restored.terminate_criterion == original.terminate_criterion
        assert restored.error_code_mapping == original.error_code_mapping
        assert restored.parameter_keys == original.parameter_keys
        assert restored.disable_cache == original.disable_cache


FernValidationYamlModel: TypeAlias = (
    type[ValidationBlockYaml]
    | type[WorkflowDefinitionYamlBlocksItem_Validation]
    | type[ForLoopBlockYamlLoopBlocksItem_Validation]
    | type[WhileLoopBlockYamlLoopBlocksItem_Validation]
)


class TestValidationBlockFernSdkFields:
    def test_fern_validation_yaml_models_accept_runtime_extraction_fields(self) -> None:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"price": {"type": "number"}},
        }
        model_classes: tuple[FernValidationYamlModel, ...] = (
            ValidationBlockYaml,
            WorkflowDefinitionYamlBlocksItem_Validation,
            ForLoopBlockYamlLoopBlocksItem_Validation,
            WhileLoopBlockYamlLoopBlocksItem_Validation,
        )

        for model_class in model_classes:
            block_yaml = model_class(
                label="validate_cart",
                navigation_goal="validate the cart total",
                complete_criterion="cart total matches expected total",
                data_extraction_goal="extract the cart subtotal as price",
                data_schema=schema,
            )

            assert block_yaml.navigation_goal == "validate the cart total"
            assert block_yaml.data_extraction_goal == "extract the cart subtotal as price"
            assert block_yaml.data_schema == schema


class TestValidationBlockRuntimeExtractionGate:
    def test_converter_output_satisfies_runtime_extraction_gate(self) -> None:
        schema = {"type": "object", "properties": {"price": {"type": "number"}}}
        block_yaml = ValidationBlockYAML(
            label="validate_cart",
            navigation_goal="validate the cart total",
            complete_criterion="cart total matches expected total",
            data_extraction_goal="extract the cart subtotal as price",
            data_schema=schema,
        )
        parameters: dict[str, PARAMETER_TYPE] = {
            "validate_cart_output": cast(PARAMETER_TYPE, _output_parameter("validate_cart")),
        }

        block = block_yaml_to_block(block_yaml, parameters)

        assert isinstance(block, ValidationBlock)
        assert block.task_type == TaskType.validation
        assert block.navigation_goal is not None
        assert block.data_extraction_goal is not None
        assert block.data_schema is not None

    def test_step_has_completed_goal_true_on_successful_complete_action(self) -> None:
        complete_action = CompleteAction(reasoning="criterion met", verified=True)
        detailed_output = DetailedAgentStepOutput(
            scraped_page=None,
            extract_action_prompt=None,
            llm_response=None,
            actions=[complete_action],
            action_results=[ActionSuccess()],
            actions_and_results=[(complete_action, [ActionSuccess()])],
        )

        assert ForgeAgent.step_has_completed_goal(detailed_output) is True

    def test_step_has_completed_goal_false_on_terminate_action(self) -> None:
        terminate_action = TerminateAction(reasoning="cannot continue")
        detailed_output = DetailedAgentStepOutput(
            scraped_page=None,
            extract_action_prompt=None,
            llm_response=None,
            actions=[terminate_action],
            action_results=[ActionFailure(exception=Exception("terminated"))],
            actions_and_results=[(terminate_action, [ActionFailure(exception=Exception("terminated"))])],
        )

        assert ForgeAgent.step_has_completed_goal(detailed_output) is False
