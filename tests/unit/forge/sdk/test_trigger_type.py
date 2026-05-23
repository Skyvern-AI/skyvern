from skyvern.constants import SKYVERN_MCP_USER_AGENT, SKYVERN_UI_USER_AGENT
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.routes.trigger_type import workflow_run_trigger_type_from_user_agent


def test_workflow_run_trigger_type_from_user_agent_ui_returns_manual() -> None:
    assert workflow_run_trigger_type_from_user_agent(SKYVERN_UI_USER_AGENT) == WorkflowRunTriggerType.manual


def test_workflow_run_trigger_type_from_user_agent_mcp_returns_manual() -> None:
    assert workflow_run_trigger_type_from_user_agent(SKYVERN_MCP_USER_AGENT) == WorkflowRunTriggerType.manual


def test_workflow_run_trigger_type_from_user_agent_none_returns_api() -> None:
    assert workflow_run_trigger_type_from_user_agent(None) == WorkflowRunTriggerType.api


def test_workflow_run_trigger_type_from_user_agent_unknown_returns_api() -> None:
    assert workflow_run_trigger_type_from_user_agent("some-other-client") == WorkflowRunTriggerType.api
