from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.todo_list import render_todo_list, todo_list_prompt
from skyvern.forge.sdk.schemas.credentials import Credential, CredentialType, CredentialVaultType
from tests.unit.copilot_test_helpers import make_copilot_ctx


def _login_policy() -> RequestPolicy:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    credential = Credential(
        credential_id="cred-1",
        organization_id="org-1",
        name="Example Login",
        vault_type=CredentialVaultType.SKYVERN,
        item_id="item_1",
        credential_type=CredentialType.PASSWORD,
        username="user@example.com",
        card_last4=None,
        card_brand=None,
        created_at=now,
        modified_at=now,
    )
    return RequestPolicy(login_intent=True, resolved_credentials=[credential])


def _output_criterion(path: str) -> CompletionCriterion:
    return CompletionCriterion(id=f"crit-{path}", outcome=f"observe {path}", output_path=path)


def test_login_not_attempted() -> None:
    ctx = make_copilot_ctx(request_policy=_login_policy())
    todo = render_todo_list(ctx)
    assert todo is not None
    assert "- Login: credential resolved but login not yet attempted" in todo
    assert "- The site has not been acted on yet (0 interactions recorded)" in todo


def test_login_filled_but_no_page_reached_by_interaction() -> None:
    ctx = make_copilot_ctx(request_policy=_login_policy())
    ctx.scout_trajectory = [
        {"tool_name": "fill_credential_field", "source_url": "https://example.com/login", "credential_id": "cred-1"}
    ]
    assert (
        render_todo_list(ctx)
        == "- Login: credential resolved but login not completed (no page reached by interaction yet)"
    )


def test_read_only_page_observations_do_not_complete_login() -> None:
    ctx = make_copilot_ctx(request_policy=_login_policy())
    ctx.scout_trajectory = [
        {"tool_name": "fill_credential_field", "source_url": "https://example.com/login", "credential_id": "cred-1"}
    ]
    ctx.prior_observed_acted_pages = [{"url": "https://example.com/dashboard", "reached_via": "inspection"}]
    ctx.observed_browser_urls = ["https://example.com/dashboard"]
    todo = render_todo_list(ctx)
    assert todo is not None
    assert "login not completed" in todo


def test_login_completed_via_interaction_reached_page() -> None:
    ctx = make_copilot_ctx(request_policy=_login_policy())
    ctx.prior_fill_carry = [
        {"tool_name": "fill_credential_field", "source_url": "https://example.com/login", "credential_id": "cred-1"}
    ]
    ctx.prior_observed_acted_pages = [{"url": "https://example.com/dashboard", "reached_via": "interaction"}]
    assert render_todo_list(ctx) is None


def test_outputs_not_yet_observed() -> None:
    policy = RequestPolicy(completion_criteria=[_output_criterion("output.number_of_visitors_last_week")])
    ctx = make_copilot_ctx(request_policy=policy)
    ctx.scout_trajectory = [{"tool_name": "click", "source_url": "https://example.com/stats"}]
    assert render_todo_list(ctx) == "- Outputs not yet observed: output.number_of_visitors_last_week"


def test_credential_id_on_non_fill_tool_is_not_a_login_attempt() -> None:
    ctx = make_copilot_ctx(request_policy=_login_policy())
    ctx.scout_trajectory = [
        {"tool_name": "click", "source_url": "https://example.com/login", "credential_id": "cred-1"}
    ]
    todo = render_todo_list(ctx)
    assert todo is not None
    assert "login not yet attempted" in todo


def test_interaction_reached_page_suppresses_zero_interactions_line() -> None:
    policy = RequestPolicy(completion_criteria=[_output_criterion("output.total")])
    ctx = make_copilot_ctx(request_policy=policy)
    ctx.prior_observed_acted_pages = [{"url": "https://example.com/stats", "reached_via": "interaction"}]
    todo = render_todo_list(ctx)
    assert todo is not None
    assert "has not been acted on" not in todo


def test_zero_interactions_line_appended_when_work_remains() -> None:
    policy = RequestPolicy(completion_criteria=[_output_criterion("output.total")])
    ctx = make_copilot_ctx(request_policy=policy)
    todo = render_todo_list(ctx)
    assert todo is not None
    assert "- Outputs not yet observed: output.total" in todo
    assert "- The site has not been acted on yet (0 interactions recorded)" in todo


def test_outputs_satisfied_by_verification() -> None:
    policy = RequestPolicy(completion_criteria=[_output_criterion("output.total")])
    ctx = make_copilot_ctx(request_policy=policy)
    ctx.completion_verification_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["crit-output.total"],
        verdicts=[
            CriterionVerdict(
                criterion_id="crit-output.total",
                state="satisfied",
                reason_code="observed",
                output_path="output.total",
            )
        ],
    )
    assert render_todo_list(ctx) is None


def test_definition_plane_satisfied_verdict_does_not_clear_output() -> None:
    policy = RequestPolicy(completion_criteria=[_output_criterion("output.total")])
    ctx = make_copilot_ctx(request_policy=policy)
    ctx.completion_verification_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["crit-output.total"],
        verdicts=[
            CriterionVerdict(
                criterion_id="crit-output.total",
                state="satisfied",
                reason_code="definition_parameters_referenced",
                output_path="output.total",
            )
        ],
    )
    todo = render_todo_list(ctx)
    assert todo is not None
    assert "- Outputs not yet observed: output.total" in todo


def test_definition_level_criterion_output_is_never_pending() -> None:
    criterion = CompletionCriterion(
        id="crit-def", outcome="workflow references the total parameter", level="definition", output_path="output.total"
    )
    policy = RequestPolicy(completion_criteria=[criterion])
    ctx = make_copilot_ctx(request_policy=policy)
    ctx.scout_trajectory = [{"tool_name": "click", "source_url": "https://example.com/stats"}]
    assert render_todo_list(ctx) is None


def test_all_done_returns_none() -> None:
    assert render_todo_list(make_copilot_ctx()) is None


def test_login_and_outputs_combined() -> None:
    policy = _login_policy()
    policy.completion_criteria = [_output_criterion("output.total")]
    todo = render_todo_list(make_copilot_ctx(request_policy=policy))
    assert todo is not None
    assert "- Login: credential resolved but login not yet attempted" in todo
    assert "- Outputs not yet observed: output.total" in todo


def test_dynamic_system_prompt_injects_todo_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_module, "_build_system_prompt", lambda **_: "BASE PROMPT")
    instructions = agent_module._build_dynamic_system_prompt(tool_usage_guide="", config=agent_module.CopilotConfig())
    ctx = make_copilot_ctx(request_policy=_login_policy())
    prompt = instructions(SimpleNamespace(context=ctx), None)
    assert "TODO — outstanding before you reply:" in prompt
    assert "Login: credential resolved but login not yet attempted" in prompt


def test_dynamic_system_prompt_adds_nothing_when_nothing_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_module, "_build_system_prompt", lambda **_: "BASE PROMPT")
    instructions = agent_module._build_dynamic_system_prompt(tool_usage_guide="", config=agent_module.CopilotConfig())
    prompt = instructions(SimpleNamespace(context=make_copilot_ctx(request_policy=RequestPolicy())), None)
    assert "TODO — outstanding before you reply:" not in prompt


def test_todo_list_prompt_empty_when_nothing_pending() -> None:
    assert todo_list_prompt(make_copilot_ctx()) == ""
