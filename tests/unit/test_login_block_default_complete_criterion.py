"""Tests for LoginBlock default complete_criterion in workflow_definition_converter."""

from datetime import UTC, datetime

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
    """Helper to convert a LoginBlockYAML through block_yaml_to_block."""
    output_param = _make_output_parameter(block_yaml.label)
    parameters = {output_param.key: output_param}
    block = block_yaml_to_block(block_yaml, parameters)
    assert isinstance(block, LoginBlock)
    return block


class TestLoginBlockDefaultCompleteCriterion:
    """Tests that LoginBlocks get a sensible default complete_criterion when none is provided."""

    def test_no_complete_criterion_gets_default(self) -> None:
        """LoginBlock without complete_criterion should get the default login verification string."""
        block_yaml = LoginBlockYAML(
            label="login",
            navigation_goal="Log in to the website",
        )
        assert block_yaml.complete_criterion is None

        block = _convert_login_block(block_yaml)

        assert block.complete_criterion is not None
        assert "logged-in indicators" in block.complete_criterion
        assert "homepage" in block.complete_criterion.lower()

    def test_empty_string_complete_criterion_gets_default(self) -> None:
        """LoginBlock with empty string complete_criterion should get the default."""
        block_yaml = LoginBlockYAML(
            label="login",
            navigation_goal="Log in to the website",
            complete_criterion="",
        )

        block = _convert_login_block(block_yaml)

        assert block.complete_criterion is not None
        assert "logged-in indicators" in block.complete_criterion

    def test_whitespace_only_complete_criterion_gets_default(self) -> None:
        """LoginBlock with whitespace-only complete_criterion should get the default."""
        block_yaml = LoginBlockYAML(
            label="login",
            navigation_goal="Log in to the website",
            complete_criterion="   ",
        )

        block = _convert_login_block(block_yaml)

        assert block.complete_criterion is not None
        assert "logged-in indicators" in block.complete_criterion

    def test_explicit_complete_criterion_preserved(self) -> None:
        """LoginBlock with an explicit complete_criterion should keep it unchanged."""
        custom_criterion = "URL contains '/dashboard'"
        block_yaml = LoginBlockYAML(
            label="login",
            navigation_goal="Log in to the website",
            complete_criterion=custom_criterion,
        )

        block = _convert_login_block(block_yaml)

        assert block.complete_criterion == custom_criterion

    def test_default_criterion_mentions_key_indicators(self) -> None:
        """The default criterion should mention the key logged-in indicators."""
        block_yaml = LoginBlockYAML(
            label="login",
            navigation_goal="Log in to the website",
        )

        block = _convert_login_block(block_yaml)

        criterion = block.complete_criterion
        assert criterion is not None
        # Should mention checking for logout button / sign out
        assert "Sign out" in criterion or "Log out" in criterion or "Logout" in criterion
        # Should mention checking for username/account in header
        assert "user name" in criterion or "email" in criterion or "account name" in criterion
        # Should warn about homepage redirect not meaning failure
        assert "homepage" in criterion.lower()
