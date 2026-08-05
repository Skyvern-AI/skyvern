from collections.abc import Callable

from skyvern.forge.sdk.api.llm import api_handler_factory
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.utils.secret_redaction import REDACTED_SECRET_PLACEHOLDER


def test_redact_prompt_text_removes_real_secret_and_keeps_placeholder(monkeypatch) -> None:
    redacted = api_handler_factory._redact_prompt_text(
        "password real-password id placeholder_ab12_password",
        {"real-password"},
    )

    assert redacted == f"password {REDACTED_SECRET_PLACEHOLDER} id placeholder_ab12_password"


def test_current_secret_values_for_redaction_ignores_workflow_opt_out(
    monkeypatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    monkeypatch.setattr(api_handler_factory.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    manager = workflow_context_manager_factory(
        workflow_run_id="wr_redact",
        mask_secrets=False,
        secrets={"password": "real-password"},
    )
    monkeypatch.setattr(api_handler_factory.app, "WORKFLOW_CONTEXT_MANAGER", manager)

    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_redact")):
        assert api_handler_factory._current_secret_values_for_redaction() == {"real-password"}


def test_current_secret_values_for_redaction_respects_global_flag(
    monkeypatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    monkeypatch.setattr(api_handler_factory.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", False)
    manager = workflow_context_manager_factory(
        workflow_run_id="wr_redact",
        mask_secrets=False,
        secrets={"password": "real-password"},
    )
    monkeypatch.setattr(api_handler_factory.app, "WORKFLOW_CONTEXT_MANAGER", manager)

    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_redact")):
        assert api_handler_factory._current_secret_values_for_redaction() == set()


def test_redact_message_text_content_redacts_tool_call_arguments() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "safe",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "submit",
                        "arguments": '{"password": "real-password"}',
                    },
                }
            ],
        }
    ]

    redacted = api_handler_factory._redact_message_text_content(messages, {"real-password"})

    assert redacted is not None
    arguments = redacted[0]["tool_calls"][0]["function"]["arguments"]
    assert arguments == f'{{"password": "{REDACTED_SECRET_PLACEHOLDER}"}}'
    assert messages[0]["tool_calls"][0]["function"]["arguments"] == '{"password": "real-password"}'


def test_redact_message_text_content_redacts_nested_tool_result_blocks() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "content": [
                        {"type": "text", "text": "returned real-password"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                    ],
                }
            ],
        }
    ]

    redacted = api_handler_factory._redact_message_text_content(messages, {"real-password"})

    assert redacted is not None
    tool_result_content = redacted[0]["content"][0]["content"]
    assert tool_result_content[0]["text"] == f"returned {REDACTED_SECRET_PLACEHOLDER}"
    assert tool_result_content[1] is messages[0]["content"][0]["content"][1]
    assert messages[0]["content"][0]["content"][0]["text"] == "returned real-password"
