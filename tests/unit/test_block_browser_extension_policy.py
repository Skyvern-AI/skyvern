from __future__ import annotations

from datetime import UTC, datetime

from skyvern.forge.sdk.workflow.models.block import LoginBlock, NavigationBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition

_NOW = datetime.now(UTC)


def _output_parameter(label: str) -> OutputParameter:
    return OutputParameter(
        parameter_type="output",
        key=f"{label}_output",
        workflow_id="test_wf",
        output_parameter_id=f"op_{label}",
        created_at=_NOW,
        modified_at=_NOW,
    )


def test_login_block_uses_pristine_browser_launch() -> None:
    block = LoginBlock(label="login", output_parameter=_output_parameter("login"))

    assert not block.allow_content_blocking_extensions_for_browser_launch()


def test_navigation_block_allows_content_blocking_extensions() -> None:
    block = NavigationBlock(
        label="navigate",
        output_parameter=_output_parameter("navigate"),
        navigation_goal="Open the page",
    )

    assert block.allow_content_blocking_extensions_for_browser_launch()


def test_workflow_definition_with_later_login_block_uses_pristine_browser_launch() -> None:
    navigation_block = NavigationBlock(
        label="navigate",
        output_parameter=_output_parameter("navigate"),
        navigation_goal="Open the login page",
    )
    login_block = LoginBlock(label="login", output_parameter=_output_parameter("login"))
    workflow_definition = WorkflowDefinition(parameters=[], blocks=[navigation_block, login_block])

    assert not workflow_definition.allow_content_blocking_extensions_for_browser_launch()
    assert not navigation_block.allow_content_blocking_extensions_for_browser_launch(workflow=workflow_definition)
