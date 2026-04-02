"""Tests for LoginBlock default navigation_goal in workflow_definition_converter.

Regression test for SKY-8637: MCP-built workflows omit prompt for login block.
When a login block has no navigation_goal, the converter must apply
DEFAULT_LOGIN_PROMPT so the agent knows to fill credentials.
"""

from datetime import UTC, datetime

from skyvern.constants import DEFAULT_LOGIN_PROMPT
from skyvern.forge.sdk.workflow.models.block import LoginBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.workflows import LoginBlockYAML

_NOW = datetime.now(UTC)


def _make_output_parameter(label: str) -> OutputParameter:
    return OutputParameter(
        parameter_type="output",
        key=f"{label}_output",
        workflow_id="test_wf",
        output_parameter_id=f"op_{label}",
        created_at=_NOW,
        modified_at=_NOW,
    )


def _convert_login_block(block_yaml: LoginBlockYAML) -> LoginBlock:
    output_param = _make_output_parameter(block_yaml.label)
    parameters = {output_param.key: output_param}
    block = block_yaml_to_block(block_yaml, parameters)
    assert isinstance(block, LoginBlock)
    return block


class TestLoginBlockDefaultNavigationGoal:
    """LoginBlocks without navigation_goal must get DEFAULT_LOGIN_PROMPT."""

    def test_no_navigation_goal_gets_default(self) -> None:
        block_yaml = LoginBlockYAML(label="login")
        assert block_yaml.navigation_goal is None

        block = _convert_login_block(block_yaml)

        assert block.navigation_goal == DEFAULT_LOGIN_PROMPT

    def test_empty_string_navigation_goal_gets_default(self) -> None:
        block_yaml = LoginBlockYAML(label="login", navigation_goal="")

        block = _convert_login_block(block_yaml)

        assert block.navigation_goal == DEFAULT_LOGIN_PROMPT

    def test_whitespace_only_navigation_goal_gets_default(self) -> None:
        block_yaml = LoginBlockYAML(label="login", navigation_goal="   ")

        block = _convert_login_block(block_yaml)

        assert block.navigation_goal == DEFAULT_LOGIN_PROMPT

    def test_explicit_navigation_goal_preserved(self) -> None:
        custom_goal = "Navigate to the admin panel and log in"
        block_yaml = LoginBlockYAML(label="login", navigation_goal=custom_goal)

        block = _convert_login_block(block_yaml)

        assert block.navigation_goal == custom_goal

    def test_default_prompt_mentions_credentials(self) -> None:
        block_yaml = LoginBlockYAML(label="login")

        block = _convert_login_block(block_yaml)

        assert "credentials" in block.navigation_goal.lower()
        assert "password" in block.navigation_goal.lower()
        assert "login" in block.navigation_goal.lower()
