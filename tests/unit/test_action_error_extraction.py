from skyvern.errors.errors import UserDefinedError
from skyvern.webeye.actions.actions import Action, ClickAction, CompleteAction
from skyvern.webeye.actions.models import DetailedAgentStepOutput
from skyvern.webeye.actions.responses import ActionSuccess


def _error(code: str) -> UserDefinedError:
    return UserDefinedError(error_code=code, reasoning="mapped error", confidence_float=0.9)


def _output_for_action(action: Action) -> DetailedAgentStepOutput:
    return DetailedAgentStepOutput(
        scraped_page=None,
        extract_action_prompt=None,
        llm_response=None,
        actions=None,
        action_results=None,
        actions_and_results=[(action, [ActionSuccess()])],
    )


def test_extract_errors_preserves_decisive_action_errors() -> None:
    error = _error("task_failed")
    action = CompleteAction(errors=[error])

    assert _output_for_action(action).extract_errors() == [error]


def test_extract_errors_includes_download_action_errors() -> None:
    error = _error("data_not_downloadable")
    action = ClickAction(element_id="download-link", download=True, errors=[error], terminal_user_errors=True)

    assert _output_for_action(action).extract_errors() == [error]


def test_agent_step_output_marks_extracted_errors_terminal() -> None:
    error = _error("data_not_downloadable")
    action = ClickAction(element_id="download-link", download=True, errors=[error], terminal_user_errors=True)

    output = _output_for_action(action).to_agent_step_output()

    assert output.errors == [error]
    assert output.terminal_user_errors is True


def test_extract_errors_ignores_download_action_errors_without_terminal_flag() -> None:
    error = _error("inline_download_error")
    action = ClickAction(element_id="download-link", download=True, errors=[error])

    output = _output_for_action(action).to_agent_step_output()

    assert output.errors == []
    assert output.terminal_user_errors is False


def test_extract_errors_ignores_non_download_web_action_errors() -> None:
    error = _error("inline_form_error")
    action = ClickAction(element_id="submit", download=False, errors=[error])

    output = _output_for_action(action).to_agent_step_output()

    assert output.errors == []
    assert output.terminal_user_errors is False
