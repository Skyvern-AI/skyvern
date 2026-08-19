import json
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import MagicMock

from skyvern.forge import app as forge_app
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.api.llm import api_handler_factory
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager, WorkflowRunContext
from skyvern.utils.secret_redaction import REDACTED_SECRET_PLACEHOLDER, redact_secrets_from_text
from skyvern.webeye.actions.handler import get_actual_value_of_parameter_if_secret


def _context_with_secret(workflow_run_id: str, token: str, value: str) -> WorkflowRunContext:
    context = WorkflowRunContext(
        workflow_title="t",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_run_id=workflow_run_id,
        aws_client=MagicMock(),
    )
    context.secrets[token] = value
    return context


def test_redact_prompt_text_removes_real_secret_and_keeps_placeholder(monkeypatch) -> None:
    redacted = api_handler_factory._redact_prompt_text(
        "password real-password id placeholder_ab12_password",
        {"real-password"},
    )

    assert redacted == f"password {REDACTED_SECRET_PLACEHOLDER} id placeholder_ab12_password"


def test_current_secret_values_for_redaction_respects_workflow_opt_out(
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
        assert api_handler_factory._current_secret_values_for_redaction() == set()


def test_current_secret_values_for_redaction_returns_values_when_workflow_opted_in(
    monkeypatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    monkeypatch.setattr(api_handler_factory.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    manager = workflow_context_manager_factory(
        workflow_run_id="wr_redact",
        mask_secrets=True,
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
        mask_secrets=True,
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


def test_build_navigation_payload_represents_credential_equal_value_as_placeholder(monkeypatch) -> None:
    """A plaintext param value that duplicates a stored credential value must reach the planner as
    the credential's resolvable placeholder token, not as a raw value the LLM-boundary redactor
    would one-way-replace with [REDACTED_SECRET] and then type verbatim.
    """
    workflow_run_id = "wr_collision"
    token = "placeholder_ab12_username"
    shared_value = "shared-cred-value-0001"

    context = _context_with_secret(workflow_run_id, token, shared_value)
    manager = WorkflowContextManager()
    manager.workflow_run_contexts[workflow_run_id] = context
    monkeypatch.setattr(forge_app, "WORKFLOW_CONTEXT_MANAGER", manager)

    task = SimpleNamespace(
        task_id="tsk_collision",
        workflow_run_id=workflow_run_id,
        navigation_payload={
            "email": shared_value,
            "credentials_1": {"username": token, "password": "placeholder_ab12_password"},
            "city": "Rivertown",
        },
    )
    agent = object.__new__(ForgeAgent)
    with skyvern_context.scoped(SkyvernContext(workflow_run_id=workflow_run_id)):
        result = agent._build_navigation_payload(task)  # type: ignore[arg-type]

    assert isinstance(result, dict)
    # The duplicated value is now the resolvable token, not the raw string.
    assert result["email"] == token
    # Non-colliding values and already-tokenized credentials are untouched.
    assert result["city"] == "Rivertown"
    assert result["credentials_1"]["username"] == token
    # The task's own payload is not mutated (raw value preserved for non-prompt consumers).
    assert task.navigation_payload["email"] == shared_value


def test_represented_value_survives_redaction_as_resolvable_token(monkeypatch) -> None:
    """Negative safety: no raw credential material reaches the live planner prompt, and the
    representation stays resolvable through the existing input path.
    """
    workflow_run_id = "wr_egress"
    token = "placeholder_cd34_username"
    shared_value = "shared-cred-value-0002"

    context = _context_with_secret(workflow_run_id, token, shared_value)
    manager = WorkflowContextManager()
    manager.workflow_run_contexts[workflow_run_id] = context
    monkeypatch.setattr(forge_app, "WORKFLOW_CONTEXT_MANAGER", manager)

    represented = context.represent_plaintext_secrets_as_placeholders({"email": shared_value})
    # Simulate the LLM-boundary redaction that runs on the live prompt text.
    redacted_prompt = redact_secrets_from_text(json.dumps(represented), {shared_value})

    assert shared_value not in redacted_prompt
    assert token in redacted_prompt
    assert REDACTED_SECRET_PLACEHOLDER not in redacted_prompt
    assert get_actual_value_of_parameter_if_secret(workflow_run_id, token) == shared_value


def test_redactor_still_one_way_redacts_secret_without_placeholder() -> None:
    """Unrelated caller unchanged: the persistence/log/artifact boundary keeps one-way-redacting a
    raw secret that has no placeholder token to [REDACTED_SECRET].
    """
    assert redact_secrets_from_text("leak shared-cred-value-0003 end", {"shared-cred-value-0003"}) == (
        f"leak {REDACTED_SECRET_PLACEHOLDER} end"
    )


def test_represent_only_replaces_whole_value_matches() -> None:
    workflow_run_id = "wr_unit"
    token = "placeholder_ef56_username"
    value = "shared-cred-value-0004"
    context = _context_with_secret(workflow_run_id, token, value)

    payload = {
        "match": value,
        "contains": f"prefix {value} suffix",
        "already_token": token,
        "other": "unrelated",
        "nested": [value, {"deep": value}, 7, None],
    }
    result = context.represent_plaintext_secrets_as_placeholders(payload)

    assert result["match"] == token
    assert result["contains"] == f"prefix {value} suffix"
    assert result["already_token"] == token
    assert result["other"] == "unrelated"
    assert result["nested"] == [token, {"deep": token}, 7, None]
    assert payload["match"] == value


def test_find_secret_placeholder_for_value_requires_whole_value_match() -> None:
    context = _context_with_secret("wr_find", "placeholder_gh78_username", "shared-cred-value-0005")

    assert context.find_secret_placeholder_for_value("shared-cred-value-0005") == "placeholder_gh78_username"
    assert context.find_secret_placeholder_for_value("shared-cred-value-0005 extra") is None
    assert context.find_secret_placeholder_for_value("placeholder_gh78_username") is None
    assert context.find_secret_placeholder_for_value("") is None
    assert context.find_secret_placeholder_for_value(1234) is None


def test_non_placeholder_secret_value_stays_raw_but_boundary_redactor_still_redacts() -> None:
    """Defense-in-depth layering: a value equal to a secret registered WITHOUT a placeholder_ key is
    not tokenized here (there is no resolvable token to use), yet the untouched LLM-boundary redactor
    still one-way-redacts it to [REDACTED_SECRET]. Tokenization is additive, not a replacement.
    """
    raw_secret = "master-secret-value-0006"
    context = WorkflowRunContext(
        workflow_title="t",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_run_id="wr_layering",
        aws_client=MagicMock(),
    )
    context.secrets["MASTER_PASSWORD"] = raw_secret

    represented = context.represent_plaintext_secrets_as_placeholders({"field": raw_secret})
    assert represented == {"field": raw_secret}
    assert redact_secrets_from_text(json.dumps(represented), {raw_secret}) == json.dumps(
        {"field": REDACTED_SECRET_PLACEHOLDER}
    )


def test_runtime_otp_value_is_not_represented_as_placeholder() -> None:
    """OTP codes are registered as secrets too, but keep their own resolution path — the
    credential-collision representation must leave them untouched.
    """
    context = _context_with_secret("wr_otp", "placeholder_ij90_otp", "654321")
    context.runtime_otp_values.add("654321")

    assert context.find_secret_placeholder_for_value("654321") is None
    assert context.represent_plaintext_secrets_as_placeholders({"verification_code": "654321"}) == {
        "verification_code": "654321"
    }


def test_find_secret_placeholder_respects_redactor_numeric_length_floor() -> None:
    """The matcher must not tokenize values the redactor itself would skip. A 4-digit numeric secret
    is below MIN_NUMERIC_SECRET_LENGTH (6), so the redactor ignores it; representing it would let a
    short throwaway value (a CVV/expiry) hijack every unrelated payload value that matches.
    """
    context = _context_with_secret("wr_floor_num", "placeholder_kl12_pin", "1234")
    assert context.find_secret_placeholder_for_value("1234") is None
    assert context.represent_plaintext_secrets_as_placeholders({"cvv": "1234"}) == {"cvv": "1234"}


def test_find_secret_placeholder_respects_min_secret_length() -> None:
    """A secret value below MIN_SECRET_LENGTH (4) is ignored by the redactor and must be ignored here."""
    context = _context_with_secret("wr_floor_min", "placeholder_mn34_x", "ab")
    assert context.find_secret_placeholder_for_value("ab") is None


def test_find_secret_placeholder_excludes_totp_sentinel() -> None:
    """TOTP sentinel values (e.g. BW_TOTP) are explicitly non-redactable and must never be tokenized."""
    context = _context_with_secret("wr_sentinel", "placeholder_op56_totp", "BW_TOTP")
    assert context.find_secret_placeholder_for_value("BW_TOTP") is None


def test_synthetic_totp_hint_is_never_tokenized(monkeypatch) -> None:
    """The synthetic '123456' TOTP format hint injected for the LLM must never become a resolvable
    credential token, even when a run credential's value is exactly '123456'. The representation pass
    runs before the hint is injected, so the throwaway hint stays an inert literal.
    """
    workflow_run_id = "wr_totp_order"
    context = _context_with_secret(workflow_run_id, "placeholder_pw01_password", "123456")
    monkeypatch.setattr(context, "totp_secret_value_key", lambda placeholder: "totp_key")
    monkeypatch.setattr(context, "get_original_secret_value_or_none", lambda key: "totp-seed")
    manager = WorkflowContextManager()
    manager.workflow_run_contexts[workflow_run_id] = context
    monkeypatch.setattr(forge_app, "WORKFLOW_CONTEXT_MANAGER", manager)
    monkeypatch.setattr(ForgeAgent, "_should_process_totp", lambda self, scraped_page: True, raising=False)

    task = SimpleNamespace(
        task_id="tsk_totp_order",
        workflow_run_id=workflow_run_id,
        navigation_payload={"login": {"totp": "placeholder_tt99_totp"}},
    )
    agent = object.__new__(ForgeAgent)
    with skyvern_context.scoped(SkyvernContext(workflow_run_id=workflow_run_id)):
        result = agent._build_navigation_payload(task, step=SimpleNamespace())  # type: ignore[arg-type]

    assert isinstance(result, dict)
    assert result["login"]["totp"] == "123456"
