"""
Tests for click prompt parameterization in cached script generation.

When generating cached scripts, click action prompts (intention/reasoning) should
replace literal parameter values with f-string references to context.parameters[...],
so that re-runs with different values produce correct behavior.
"""

from typing import Any

import libcst as cst

from skyvern.core.script_generations.generate_script import (
    MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB,
    _action_to_stmt,
    _build_parameterized_prompt_cst,
    _build_value_to_param_lookup,
    _collect_secret_param_keys,
)
from skyvern.forge.sdk.workflow.models.parameter import is_sensitive_workflow_parameter
from skyvern.webeye.actions.actions import ActionType


def _make_action(
    action_type: str,
    field_name: str | None = None,
    text: str = "",
    option: str = "",
    file_url: str = "",
) -> dict[str, Any]:
    action: dict[str, Any] = {"action_type": action_type}
    if field_name:
        action["field_name"] = field_name
    if text:
        action["text"] = text
    if option:
        action["option"] = option
    if file_url:
        action["file_url"] = file_url
    return action


# ---------------------------------------------------------------------------
# _build_value_to_param_lookup
# ---------------------------------------------------------------------------


class TestBuildValueToParamLookup:
    def test_collects_input_text_values(self) -> None:
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, field_name="patient_id", text="542-641-668"),
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert lookup == {"542-641-668": "patient_id"}

    def test_collects_select_option_values(self) -> None:
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.SELECT_OPTION, field_name="state", option="California"),
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert lookup == {"California": "state"}

    def test_collects_upload_file_values(self) -> None:
        actions_by_task = {
            "task-1": [
                _make_action(
                    ActionType.UPLOAD_FILE,
                    field_name="document",
                    file_url="https://example.com/report.pdf",
                ),
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert lookup == {"https://example.com/report.pdf": "document"}

    def test_skips_actions_without_field_name(self) -> None:
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, text="some value without field name"),
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert lookup == {}

    def test_skips_short_values(self) -> None:
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, field_name="flag", text="No"),
                _make_action(ActionType.INPUT_TEXT, field_name="code", text="CA"),
                _make_action(ActionType.INPUT_TEXT, field_name="num", text="1"),
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert lookup == {}

    def test_boundary_value_at_min_length(self) -> None:
        """Values at exactly MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB should be included."""
        value = "x" * MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, field_name="field", text=value),
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert value in lookup

    def test_sorted_by_descending_length(self) -> None:
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, field_name="short_field", text="abcd"),
                _make_action(ActionType.INPUT_TEXT, field_name="long_field", text="abcdefghij"),
                _make_action(ActionType.INPUT_TEXT, field_name="mid_field", text="abcdef"),
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        keys = list(lookup.keys())
        assert keys == ["abcdefghij", "abcdef", "abcd"]

    def test_first_writer_wins_on_duplicate_values(self) -> None:
        """When two actions have the same text but different field_names, the
        first one wins. This is rare in practice (the LLM tagging the same value
        as two distinct concepts is unusual); silent wrong-binding for the
        second concept is acceptable risk vs the complexity of detecting and
        dropping the value."""
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, field_name="first_field", text="same-value"),
                _make_action(ActionType.INPUT_TEXT, field_name="second_field", text="same-value"),
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert lookup["same-value"] == "first_field"

    def test_skips_click_actions(self) -> None:
        actions_by_task = {
            "task-1": [
                {"action_type": ActionType.CLICK, "field_name": "click_field", "text": "some text"},
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert lookup == {}

    def test_multiple_tasks(self) -> None:
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, field_name="patient_id", text="542-641-668"),
            ],
            "task-2": [
                _make_action(ActionType.INPUT_TEXT, field_name="doctor_name", text="Dr. Smith"),
            ],
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert lookup == {"542-641-668": "patient_id", "Dr. Smith": "doctor_name"}

    def test_empty_actions(self) -> None:
        lookup = _build_value_to_param_lookup({})
        assert lookup == {}

    # ------------------------------------------------------------------
    # SKY-9295: workflow input parameter values feed the lookup so click
    # prompts containing literal account numbers / dates / etc. get
    # parameterized at generation time instead of baked in run-specifically.
    # ------------------------------------------------------------------

    def test_collects_workflow_parameter_values(self) -> None:
        """Workflow input parameter values populate the lookup keyed by param name."""
        lookup = _build_value_to_param_lookup(
            {},
            workflow_parameters={
                "account_number": "51410020",
                "download_start_date": "12/8/2025",
            },
        )
        assert lookup == {"51410020": "account_number", "12/8/2025": "download_start_date"}

    def test_workflow_param_short_value_skipped(self) -> None:
        """Short param values (below MIN_PARAM_VALUE_LENGTH) don't pollute the lookup."""
        lookup = _build_value_to_param_lookup(
            {},
            workflow_parameters={"division": "0", "request_id": "1"},
        )
        assert lookup == {}

    def test_workflow_param_credential_reference_skipped(self) -> None:
        """Values starting with ``cred_`` are credential refs and must not be substituted."""
        lookup = _build_value_to_param_lookup(
            {},
            workflow_parameters={
                "credentials": "cred_520125108297961972",
                "account_number": "51410020",
            },
        )
        assert "cred_520125108297961972" not in lookup
        assert lookup == {"51410020": "account_number"}

    def test_workflow_param_falsy_value_skipped(self) -> None:
        lookup = _build_value_to_param_lookup(
            {},
            workflow_parameters={"empty_field": "", "null_field": None, "zero_field": 0},
        )
        assert lookup == {}

    def test_workflow_param_non_string_value_coerced(self) -> None:
        """Numeric / non-string param values get coerced to str before length check."""
        lookup = _build_value_to_param_lookup(
            {},
            workflow_parameters={"account_number": 51410020},
        )
        assert lookup == {"51410020": "account_number"}

    def test_action_field_name_wins_over_workflow_param(self) -> None:
        """When an action and a workflow param share a value, the action's field_name wins."""
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, field_name="patient_id", text="542-641-668"),
            ]
        }
        lookup = _build_value_to_param_lookup(
            actions_by_task,
            workflow_parameters={"customer_ref": "542-641-668"},
        )
        assert lookup["542-641-668"] == "patient_id"

    def test_workflow_param_supplements_action_lookup(self) -> None:
        """Workflow-level params are ADDED on top of action-derived ones, not replacing."""
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, field_name="username", text="alice@example.com"),
            ]
        }
        lookup = _build_value_to_param_lookup(
            actions_by_task,
            workflow_parameters={"account_number": "51410020"},
        )
        assert lookup == {
            "alice@example.com": "username",
            "51410020": "account_number",
        }

    def test_workflow_param_none_arg_preserves_old_behavior(self) -> None:
        """Passing workflow_parameters=None matches the pre-fix call signature."""
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, field_name="patient_id", text="542-641-668"),
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task, workflow_parameters=None)
        assert lookup == {"542-641-668": "patient_id"}

    def test_workflow_param_with_secret_param_keys_skipped(self) -> None:
        """Param keys flagged as secret/credential are skipped even if the value
        doesn't start with ``cred_`` (e.g. an azure_secret param whose runtime
        value is the resolved secret string)."""
        lookup = _build_value_to_param_lookup(
            {},
            workflow_parameters={
                "azure_password": "supersecret_value_98765",
                "account_number": "51410020",
            },
            secret_param_keys=frozenset({"azure_password"}),
        )
        assert "supersecret_value_98765" not in lookup
        assert lookup == {"51410020": "account_number"}

    def test_workflow_param_undeclared_key_skipped(self) -> None:
        """Param keys not declared in the workflow definition are dropped — emitting
        ``context.parameters[undeclared_key]`` would crash the cached script with
        KeyError at runtime."""
        lookup = _build_value_to_param_lookup(
            {},
            workflow_parameters={
                "account_number": "51410020",
                "rogue_key": "76543210",
            },
            declared_keys=frozenset({"account_number"}),
        )
        assert "76543210" not in lookup
        assert lookup == {"51410020": "account_number"}

    def test_workflow_param_declared_keys_none_skips_filter(self) -> None:
        """``declared_keys=None`` keeps the legacy permissive behavior — every key
        is allowed (used by paths that don't have access to the workflow definition)."""
        lookup = _build_value_to_param_lookup(
            {},
            workflow_parameters={"account_number": "51410020"},
            declared_keys=None,
        )
        assert lookup == {"51410020": "account_number"}

    def test_workflow_param_false_boolean_not_dropped(self) -> None:
        """A literal ``False`` is a legitimate prompt token (e.g. "auto-renew=False");
        the falsy guard must only drop ``None`` / empty-string, not all falsy values.
        Note: ``0`` is still skipped, but by the min-length floor (``str(0)`` is 1
        char), not by the falsy guard."""
        lookup = _build_value_to_param_lookup(
            {},
            workflow_parameters={"auto_renew": False},
        )
        assert lookup == {"False": "auto_renew"}

    def test_workflow_param_first_writer_wins_on_collision(self) -> None:
        """When two workflow params share the same value (rare in practice),
        the first key encountered wins. Silent wrong-binding for the other
        concept is acceptable vs the complexity of detecting + dropping."""
        lookup = _build_value_to_param_lookup(
            {},
            workflow_parameters={
                "recipient": "alice@example.com",
                "customer_email": "alice@example.com",
                "account_number": "51410020",
            },
        )
        assert lookup["alice@example.com"] == "recipient"
        assert lookup["51410020"] == "account_number"

    def test_action_workflow_share_value_action_wins(self) -> None:
        """Action-vs-workflow precedence preserved: when an action's field_name
        and a workflow param key both bind to the same value, the action wins
        (more specific — tied to a specific page interaction)."""
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, field_name="patient_id", text="542-641-668"),
            ]
        }
        lookup = _build_value_to_param_lookup(
            actions_by_task,
            workflow_parameters={"customer_ref": "542-641-668"},
        )
        assert lookup["542-641-668"] == "patient_id"

    def test_workflow_param_oversized_value_skipped(self) -> None:
        """Values longer than ``MAX_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB`` (multi-KB
        JSON payloads, long blobs) are skipped — they would never legitimately
        appear verbatim in a click prompt and would blow up the per-action find
        loop in ``_build_parameterized_prompt_cst``."""
        from skyvern.core.script_generations.generate_script import MAX_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB

        oversized = "x" * (MAX_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB + 1)
        lookup = _build_value_to_param_lookup(
            {},
            workflow_parameters={
                "huge_payload": oversized,
                "account_number": "51410020",
            },
        )
        assert oversized not in lookup
        assert lookup == {"51410020": "account_number"}


# ---------------------------------------------------------------------------
# _build_parameterized_prompt_cst
# ---------------------------------------------------------------------------


class TestBuildParameterizedPromptCst:
    def test_returns_none_when_no_matches(self) -> None:
        result = _build_parameterized_prompt_cst(
            "Click the submit button",
            {"542-641-668": "patient_id"},
        )
        assert result is None

    def test_single_substitution(self) -> None:
        result = _build_parameterized_prompt_cst(
            "Which card corresponds to the referral for ID 542-641-668?",
            {"542-641-668": "patient_id"},
        )
        assert result is not None
        assert isinstance(result, cst.FormattedString)
        code = cst.Module(body=[]).code_for_node(result)
        assert "context.parameters" in code  # nosemgrep: incomplete-url-substring-sanitization
        assert "patient_id" in code
        assert "542-641-668" not in code

    def test_multiple_substitutions(self) -> None:
        result = _build_parameterized_prompt_cst(
            "Find patient 542-641-668 with doctor Dr. Smith",
            {"542-641-668": "patient_id", "Dr. Smith": "doctor_name"},
        )
        assert result is not None
        code = cst.Module(body=[]).code_for_node(result)
        assert "patient_id" in code
        assert "doctor_name" in code
        assert "542-641-668" not in code
        assert "Dr. Smith" not in code

    def test_substitution_at_start(self) -> None:
        result = _build_parameterized_prompt_cst(
            "542-641-668 is the patient ID to search for",
            {"542-641-668": "patient_id"},
        )
        assert result is not None
        parts = result.parts
        # First part should be the expression (substitution at start)
        assert isinstance(parts[0], cst.FormattedStringExpression)

    def test_substitution_at_end(self) -> None:
        result = _build_parameterized_prompt_cst(
            "Search for patient 542-641-668",
            {"542-641-668": "patient_id"},
        )
        assert result is not None
        parts = result.parts
        # Last part should be the expression (substitution at end)
        assert isinstance(parts[-1], cst.FormattedStringExpression)

    def test_empty_intention(self) -> None:
        result = _build_parameterized_prompt_cst("", {"542-641-668": "patient_id"})
        assert result is None

    def test_empty_lookup(self) -> None:
        result = _build_parameterized_prompt_cst(
            "Which card corresponds to the referral for ID 542-641-668?",
            {},
        )
        assert result is None

    def test_longer_match_preferred_over_shorter(self) -> None:
        """When values overlap, the longer value (sorted first) takes precedence."""
        result = _build_parameterized_prompt_cst(
            "Enter 542-641-668-999 here",
            {
                "542-641-668-999": "full_id",
                "542-641-668": "partial_id",
            },
        )
        assert result is not None
        code = cst.Module(body=[]).code_for_node(result)
        assert "full_id" in code
        assert "partial_id" not in code

    def test_generates_valid_fstring_syntax(self) -> None:
        """The generated f-string should be parseable Python."""
        result = _build_parameterized_prompt_cst(
            "Which card area corresponds to ID 542-641-668?",
            {"542-641-668": "patient_id"},
        )
        assert result is not None
        code = cst.Module(body=[]).code_for_node(result)
        # Should be a valid f-string — verify it starts with f" or f'
        assert code.startswith("f'") or code.startswith('f"')
        # The full expression should be compilable
        compile(code, "<test>", "eval")

    def test_repeated_value_in_intention(self) -> None:
        """If the same value appears twice, both occurrences should be replaced."""
        result = _build_parameterized_prompt_cst(
            "Compare 542-641-668 with 542-641-668",
            {"542-641-668": "patient_id"},
        )
        assert result is not None
        code = cst.Module(body=[]).code_for_node(result)
        # The literal should not appear at all
        assert "542-641-668" not in code
        # context.parameters should appear twice (once per occurrence)
        assert code.count("context.parameters") == 2


# ---------------------------------------------------------------------------
# Integration: _action_to_stmt with value_to_param
# ---------------------------------------------------------------------------


class TestActionToStmtClickParameterization:
    """End-to-end tests exercising _action_to_stmt for click actions."""

    def _render(self, stmt: cst.BaseStatement) -> str:
        return cst.Module(body=[stmt]).code

    def test_click_prompt_parameterized(self) -> None:
        """Click action with matching value in intention gets an f-string prompt."""
        act: dict[str, Any] = {
            "action_type": "click",
            "xpath": "//div[@class='card']",
            "intention": "Which card corresponds to the referral for ID 542-641-668?",
        }
        task: dict[str, Any] = {}
        value_to_param = {"542-641-668": "patient_id"}

        stmt = _action_to_stmt(act, task, value_to_param=value_to_param)
        code = self._render(stmt)

        assert "context.parameters" in code  # nosemgrep: incomplete-url-substring-sanitization
        assert "patient_id" in code
        assert "542-641-668" not in code
        # Should be an f-string
        assert "f'" in code or 'f"' in code

    def test_click_prompt_literal_when_no_lookup(self) -> None:
        """Click action without value_to_param produces a plain string prompt."""
        act: dict[str, Any] = {
            "action_type": "click",
            "xpath": "//div[@class='card']",
            "intention": "Click the submit button",
        }
        task: dict[str, Any] = {}

        stmt = _action_to_stmt(act, task, value_to_param=None)
        code = self._render(stmt)

        assert "Click the submit button" in code
        assert "context.parameters" not in code

    def test_click_prompt_literal_when_no_match(self) -> None:
        """Click action with non-matching lookup produces a plain string prompt."""
        act: dict[str, Any] = {
            "action_type": "click",
            "xpath": "//button",
            "intention": "Click the submit button",
        }
        task: dict[str, Any] = {}
        value_to_param = {"542-641-668": "patient_id"}

        stmt = _action_to_stmt(act, task, value_to_param=value_to_param)
        code = self._render(stmt)

        assert "Click the submit button" in code
        assert "context.parameters" not in code

    def test_fill_action_unaffected_by_value_to_param(self) -> None:
        """Fill actions should still use the field_name mechanism, not value_to_param."""
        act: dict[str, Any] = {
            "action_type": "input_text",
            "xpath": "//input[@name='search']",
            "text": "542-641-668",
            "field_name": "patient_id",
        }
        task: dict[str, Any] = {}
        value_to_param = {"542-641-668": "patient_id"}

        stmt = _action_to_stmt(act, task, value_to_param=value_to_param)
        code = self._render(stmt)

        # Should use context.parameters via the field_name mechanism, not f-string
        assert "context.parameters" in code  # nosemgrep: incomplete-url-substring-sanitization
        assert "patient_id" in code


# ---------------------------------------------------------------------------
# is_sensitive_workflow_parameter / _collect_secret_param_keys
# ---------------------------------------------------------------------------


class TestIsSensitiveWorkflowParameter:
    def test_secret_credential_types_flagged(self) -> None:
        for ptype in (
            "aws_secret",
            "azure_secret",
            "credential",
            "onepassword",
            "azure_vault_credential",
            "bitwarden_login_credential",
            "bitwarden_sensitive_information",
            "bitwarden_credit_card_data",
        ):
            assert is_sensitive_workflow_parameter({"parameter_type": ptype, "key": "x"}), ptype

    def test_workflow_credential_id_flagged(self) -> None:
        """Workflow params declared with workflow_parameter_type=credential_id resolve
        to a credential at runtime — substituting their value into a prompt would
        leak the credential indirection."""
        assert is_sensitive_workflow_parameter(
            {"parameter_type": "workflow", "workflow_parameter_type": "credential_id", "key": "k"}
        )

    def test_regular_workflow_string_not_flagged(self) -> None:
        assert not is_sensitive_workflow_parameter(
            {"parameter_type": "workflow", "workflow_parameter_type": "string", "key": "x"}
        )

    def test_unknown_type_safely_ignored(self) -> None:
        assert not is_sensitive_workflow_parameter({"parameter_type": "not_a_real_type", "key": "x"})
        assert not is_sensitive_workflow_parameter({})

    def test_typed_credential_parameter_object_input(self) -> None:
        """Helper accepts a typed ``CredentialParameter`` object (not just a dict).
        Reviewer-side ingestion paths consume typed Parameter instances loaded
        from ``get_workflow_run_parameters`` tuples."""
        from datetime import datetime

        from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter

        param = CredentialParameter(
            credential_parameter_id="cp_1",
            workflow_id="w_1",
            credential_id="cred_abc",
            key="login",
            created_at=datetime(2026, 1, 1),
            modified_at=datetime(2026, 1, 1),
        )
        assert is_sensitive_workflow_parameter(param)

    def test_typed_aws_secret_parameter_object_input(self) -> None:
        from datetime import datetime

        from skyvern.forge.sdk.workflow.models.parameter import AWSSecretParameter

        param = AWSSecretParameter(
            aws_secret_parameter_id="asp_1",
            workflow_id="w_1",
            aws_key="some/aws/key",
            key="db_password",
            created_at=datetime(2026, 1, 1),
            modified_at=datetime(2026, 1, 1),
        )
        assert is_sensitive_workflow_parameter(param)

    def test_typed_workflow_parameter_credential_id(self) -> None:
        """Typed ``WorkflowParameter`` with ``workflow_parameter_type=CREDENTIAL_ID``
        resolves to a credential at runtime — must be flagged."""
        from datetime import datetime

        from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter, WorkflowParameterType

        param = WorkflowParameter(
            workflow_parameter_id="wp_1",
            workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
            workflow_id="w_1",
            key="selected_credential",
            created_at=datetime(2026, 1, 1),
            modified_at=datetime(2026, 1, 1),
        )
        assert is_sensitive_workflow_parameter(param)

    def test_typed_workflow_parameter_string_not_flagged(self) -> None:
        from datetime import datetime

        from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter, WorkflowParameterType

        param = WorkflowParameter(
            workflow_parameter_id="wp_1",
            workflow_parameter_type=WorkflowParameterType.STRING,
            workflow_id="w_1",
            key="account_number",
            created_at=datetime(2026, 1, 1),
            modified_at=datetime(2026, 1, 1),
        )
        assert not is_sensitive_workflow_parameter(param)


class TestCollectSecretParamKeys:
    def test_collects_only_credential_keys(self) -> None:
        workflow = {
            "workflow_definition": {
                "parameters": [
                    {"key": "account_number", "parameter_type": "workflow", "workflow_parameter_type": "string"},
                    {"key": "login_creds", "parameter_type": "credential"},
                    {"key": "vault", "parameter_type": "azure_vault_credential"},
                    {
                        "key": "selected_credential_id",
                        "parameter_type": "workflow",
                        "workflow_parameter_type": "credential_id",
                    },
                ]
            }
        }
        keys = _collect_secret_param_keys(workflow)
        assert keys == frozenset({"login_creds", "vault", "selected_credential_id"})

    def test_returns_empty_when_no_definition(self) -> None:
        assert _collect_secret_param_keys({}) == frozenset()
        assert _collect_secret_param_keys({"workflow_definition": None}) == frozenset()
