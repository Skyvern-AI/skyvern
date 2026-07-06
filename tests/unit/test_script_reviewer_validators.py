"""Tests for ScriptReviewer validation methods."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.core.script_generations.generate_script import (
    MAX_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB,
    MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB,
)
from skyvern.forge import app
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameterType
from skyvern.schemas.scripts import ScriptFallbackEpisode
from skyvern.services.script_reviewer import ScriptReviewer
from tests.unit.force_stub_app import start_forge_stub_app


class TestValidateNoHardcodedValues:
    """Tests for _validate_no_hardcoded_values."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    @pytest.mark.parametrize(
        ("cases",),
        [
            pytest.param(
                [
                    (
                        "no_params_returns_none_none",
                        'await page.click(selector="button")',
                        None,
                        None,
                    ),
                    (
                        "no_params_returns_none_empty",
                        'await page.click(selector="button")',
                        {},
                        None,
                    ),
                    (
                        "short_values_ignored",
                        """
async def block_fn(page, context):
    if 'yes' == 'yes':
        return {"next_block_label": "send_email", "branch_index": 0}
""",
                        {"flag": "yes", "ok": "true"},
                        None,
                    ),
                    (
                        "allows_context_parameters_reference",
                        """
async def block_fn(page, context):
    recipient = context.parameters.get('recipient', '')
    if recipient != "":
        return {"next_block_label": "send_email", "branch_index": 0}
""",
                        {"recipient": "billing@acme-test.example.com"},
                        None,
                    ),
                    (
                        "allows_non_parameter_literals",
                        """
async def block_fn(page, context):
    await page.click(selector='button:has-text("Sign in")', ai='fallback', prompt='Click sign in')
""",
                        {"email": "user@example.com", "password": "secret123"},
                        None,
                    ),
                    (
                        "ignores_comments",
                        """
async def block_fn(page, context):
    # billing@acme-test.example.com is the test email
    recipient = context.parameters.get('recipient', '')
    if recipient != "":
        return {"next_block_label": "send_email", "branch_index": 0}
""",
                        {"recipient": "billing@acme-test.example.com"},
                        None,
                    ),
                    (
                        "allows_block_labels_and_keywords",
                        """
async def block_fn(page, context):
    return {"next_block_label": "send_email", "branch_index": 0}
""",
                        {"account_number": "12345678"},
                        None,
                    ),
                ],
                id="allowed-and-early-exit",
            ),
            pytest.param(
                [
                    (
                        "detects_hardcoded_email",
                        """
async def block_fn(page, context):
    recipient = 'billing@acme-test.example.com'
    if recipient != "":
        return {"next_block_label": "send_email", "branch_index": 0}
""",
                        {"recipient": "billing@acme-test.example.com"},
                        ("hardcoded", "recipient", "context.parameters"),
                    ),
                    (
                        "detects_hardcoded_url",
                        """
async def login(page, context):
    await page.goto('https://portal.vendor.com/login')
    await page.fill(selector='input[name="email"]', value=context.parameters['email'])
""",
                        {"website_url": "https://portal.vendor.com/login", "email": "user@example.com"},
                        ("website_url",),
                    ),
                    (
                        "detects_multiple_hardcoded_values",
                        """
async def block_fn(page, context):
    await page.goto('https://portal.vendor.com/login')
    await page.fill(selector='input', value='billing@acme-test.example.com')
""",
                        {
                            "website_url": "https://portal.vendor.com/login",
                            "recipient": "billing@acme-test.example.com",
                        },
                        ("2 hardcoded",),
                    ),
                ],
                id="exact-literal-detections",
            ),
            pytest.param(
                [
                    (
                        "detects_param_embedded_in_click_prompt",
                        """
async def download_invoice(page, context):
    await page.click(
        ai='fallback',
        prompt='Should I download the invoice dated 12/8/2025 for account 51410020?',
    )
""",
                        {"account_number": "51410020", "download_start_date": "12/8/2025"},
                        ("account_number", "download_start_date"),
                    ),
                    (
                        "detects_single_param_embedded_in_prompt",
                        """
async def block_fn(page, context):
    await page.click(prompt='Find the row for vendor CITYO49057A and click it')
""",
                        {"vendor_code": "CITYO49057A"},
                        ("vendor_code",),
                    ),
                    (
                        "substring_match_respects_short_value_guard",
                        """
async def block_fn(page, context):
    await page.click(prompt='Click the yesterday button')
""",
                        {"flag": "yes"},
                        None,
                    ),
                    (
                        "substring_match_skips_comments",
                        """
async def block_fn(page, context):
    # Note: 51410020 is the test account number for staging
    await page.click(prompt='Click the next button')
""",
                        {"account_number": "51410020"},
                        None,
                    ),
                    (
                        "substring_match_skips_structural_literals",
                        """
async def block_fn(page, context):
    return {"next_block_label": "send_email", "branch_index": 0}
""",
                        {"target": "block"},
                        None,
                    ),
                ],
                id="substring-matching",
            ),
            pytest.param(
                [
                    (
                        "validator_threshold_matches_generator_threshold",
                        """
async def block_fn(page, context):
    await page.click(prompt='Click the box labeled 1234 to continue')
""",
                        {"order_code": "1234"},
                        ("order_code",),
                    ),
                    (
                        "validator_skips_oversized_param_values",
                        f'''
async def block_fn(page, context):
    note = "{"x" * (MAX_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB + 1)}"
''',
                        {"huge_payload": "x" * (MAX_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB + 1)},
                        None,
                    ),
                ],
                id="generator-thresholds",
            ),
            pytest.param(
                [
                    (
                        "validator_catches_hardcoded_in_triple_quoted_prompt",
                        '''
async def block_fn(page, context):
    await page.click(
        ai='fallback',
        prompt=f"""Find the row for account 51410020
and click the download button""",
    )
''',
                        {"account_number": "51410020"},
                        ("account_number",),
                    ),
                    (
                        "validator_catches_hardcoded_in_fstring_text_segment",
                        """
async def block_fn(page, context):
    name = context.parameters['name']
    await page.click(prompt=f'Send invoice 51410020 to {name}')
""",
                        {"account_number": "51410020"},
                        ("account_number",),
                    ),
                    (
                        "validator_skips_fstring_interpolation_expression",
                        """
async def block_fn(page, context):
    await page.click(prompt=f"Send to {context.parameters['recipient']}")
""",
                        {"recipient": "alice@example.com"},
                        None,
                    ),
                ],
                id="fstring-literals",
            ),
            pytest.param(
                [
                    (
                        "prose_literal_band_catches_embedded_value",
                        """
async def block_fn(page, context):
    await page.click(prompt='Click 51410020 now')
""",
                        {"account_number": "51410020"},
                        ("account_number",),
                    ),
                    (
                        "prose_literal_band_below_floor_skips",
                        """
async def block_fn(page, context):
    await page.click(prompt='ok 1234')
""",
                        {"order_code": "1234"},
                        None,
                    ),
                    (
                        "prose_literal_no_whitespace_skips",
                        """
async def block_fn(page, context):
    return {"order_12345_id": "value", "branch_index": 0}
""",
                        {"order_id": "12345"},
                        None,
                    ),
                    (
                        "prose_literal_band_exact_match_still_fires",
                        """
async def block_fn(page, context):
    await page.fill(selector="#code", value="12345")
""",
                        {"order_code": "12345"},
                        ("order_code",),
                    ),
                ],
                id="prose-literal-band",
            ),
        ],
    )
    def test_no_hardcoded_value_cases(
        self,
        cases: list[tuple[str, str, dict[str, str] | None, tuple[str, ...] | None]],
    ) -> None:
        assert MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB == 4
        for case_id, code, params, expected_substrings in cases:
            error = self.reviewer._validate_no_hardcoded_values(code, params)
            if expected_substrings is None:
                assert error is None, case_id
                continue
            assert error is not None, case_id
            for expected in expected_substrings:
                assert expected in error, case_id


class TestValidateParameterPreservation:
    """Tests for _validate_parameter_preservation."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    @pytest.mark.parametrize(
        ("cases",),
        [
            pytest.param(
                [
                    (
                        "no_existing_code_returns_none",
                        "await page.fill(selector='#email', ai='proactive', prompt='email')",
                        None,
                        ["email"],
                        None,
                    ),
                    (
                        "no_parameter_keys_returns_none",
                        "await page.fill(selector='#email', ai='proactive', prompt='email')",
                        "await page.fill(selector='#email', value=context.parameters['email'])",
                        [],
                        None,
                    ),
                    (
                        "preserved_refs_returns_none",
                        """
choice = await page.classify(...)
if choice == 0:
    await page.fill(selector='#email', value=context.parameters['email'])
    await page.fill(selector='#pass', value=context.parameters['password'])
""",
                        """
await page.fill(selector='#email', value=context.parameters['email'])
await page.fill(selector='#pass', value=context.parameters['password'])
""",
                        ["email", "password"],
                        None,
                    ),
                    (
                        "ignores_refs_not_in_parameter_keys",
                        "await page.fill(selector='#x', ai='proactive', prompt='fill x')",
                        "await page.fill(selector='#x', value=context.parameters['invented_key'])",
                        ["email", "password"],
                        None,
                    ),
                ],
                id="preserved-or-unconstrained",
            ),
            pytest.param(
                [
                    (
                        "detects_dropped_refs",
                        """
choice = await page.classify(...)
if choice == 0:
    await page.fill(selector='#email', ai='proactive', prompt='fill email')
    await page.fill(selector='#pass', ai='proactive', prompt='fill password')
""",
                        """
await page.fill(selector='#email', value=context.parameters['email'])
await page.fill(selector='#pass', value=context.parameters['password'])
""",
                        ["email", "password"],
                        ("email", "password", "dropped"),
                    ),
                    (
                        "commented_ref_in_new_code_counts_as_dropped",
                        """
# was: context.parameters['email']
await page.fill(selector='#email', ai='proactive', prompt='fill email')
""",
                        "await page.fill(selector='#email', value=context.parameters['email'])",
                        ["email"],
                        ("email",),
                    ),
                ],
                id="dropped-references",
            ),
            pytest.param(
                [
                    (
                        "partial_drop_detected",
                        """
await page.fill(selector='#email', value=context.parameters['email'])
await page.fill(selector='#pass', ai='proactive', prompt='fill password')
""",
                        """
await page.fill(selector='#email', value=context.parameters['email'])
await page.fill(selector='#pass', value=context.parameters['password'])
""",
                        ["email", "password"],
                        ("context.parameters['password']", "!context.parameters['email']"),
                    )
                ],
                id="partial-drop",
            ),
        ],
    )
    def test_parameter_preservation_cases(
        self,
        cases: list[tuple[str, str, str | None, list[str], tuple[str, ...] | None]],
    ) -> None:
        for case_id, new_code, old_code, parameter_keys, expected_substrings in cases:
            error = self.reviewer._validate_parameter_preservation(new_code, old_code, parameter_keys)
            if expected_substrings is None:
                assert error is None, case_id
                continue
            assert error is not None, case_id
            for expected in expected_substrings:
                if expected.startswith("!"):
                    assert expected[1:] not in error, case_id
                else:
                    assert expected.lower() in error.lower(), case_id


class TestValidateBranchReturns:
    """Tests for _validate_branch_returns."""

    BRANCHES_TWO = [
        {"original_expression": "x > 0", "next_block_label": "block_3", "is_default": False},
        {"original_expression": None, "next_block_label": "block_4", "is_default": True},
    ]

    @pytest.mark.parametrize(
        ("cases",),
        [
            pytest.param(
                [
                    (
                        "valid_returns_passes",
                        """
async def block_fn(page, context):
    if context.parameters.get('x', 0) > 0:
        return {"next_block_label": "block_3", "branch_index": 0}
    else:
        return {"next_block_label": "block_4", "branch_index": 1}
""",
                        BRANCHES_TWO,
                        None,
                    ),
                    (
                        "none_label_valid_when_in_branches",
                        """
async def block_fn(page, context):
    if context.parameters.get('x', 0) > 0:
        return {"next_block_label": "block_3", "branch_index": 0}
    else:
        return {"next_block_label": None, "branch_index": 1}
""",
                        [
                            {"original_expression": "x > 0", "next_block_label": "block_3", "is_default": False},
                            {"original_expression": None, "next_block_label": None, "is_default": True},
                        ],
                        None,
                    ),
                    (
                        "single_quotes_handled",
                        """
async def block_fn(page, context):
    return {'next_block_label': 'block_3', 'branch_index': 0}
""",
                        BRANCHES_TWO,
                        None,
                    ),
                ],
                id="valid-literals",
            ),
            pytest.param(
                [
                    (
                        "invalid_label_detected",
                        """
async def block_fn(page, context):
    if context.parameters.get('x', 0) > 0:
        return {"next_block_label": "block_99", "branch_index": 0}
    else:
        return {"next_block_label": "block_4", "branch_index": 1}
""",
                        BRANCHES_TWO,
                        ("block_99",),
                    ),
                    (
                        "invalid_index_detected",
                        """
async def block_fn(page, context):
    if context.parameters.get('x', 0) > 0:
        return {"next_block_label": "block_3", "branch_index": 0}
    else:
        return {"next_block_label": "block_4", "branch_index": -1}
""",
                        BRANCHES_TWO,
                        ("-1",),
                    ),
                    (
                        "none_label_invalid_when_not_in_branches",
                        """
async def block_fn(page, context):
    if context.parameters.get('x', 0) > 0:
        return {"next_block_label": "block_3", "branch_index": 0}
    else:
        return {"next_block_label": None, "branch_index": 1}
""",
                        BRANCHES_TWO,
                        ("next_block_label",),
                    ),
                    (
                        "both_label_and_index_invalid",
                        """
async def block_fn(page, context):
    return {"next_block_label": "wrong_label", "branch_index": -1}
""",
                        BRANCHES_TWO,
                        ("wrong_label", "-1"),
                    ),
                ],
                id="invalid-literals",
            ),
            pytest.param(
                [
                    (
                        "no_literals_passes",
                        """
async def block_fn(page, context):
    label = compute_label()
    idx = compute_index()
    return {"next_block_label": label, "branch_index": idx}
""",
                        BRANCHES_TWO,
                        None,
                    ),
                    (
                        "comments_ignored",
                        """
async def block_fn(page, context):
    # return {"next_block_label": "block_99", "branch_index": -1}
    if context.parameters.get('x', 0) > 0:
        return {"next_block_label": "block_3", "branch_index": 0}
    else:
        return {"next_block_label": "block_4", "branch_index": 1}
""",
                        BRANCHES_TWO,
                        None,
                    ),
                ],
                id="ignored-static-unvalidated",
            ),
            pytest.param(
                [
                    (
                        "empty_branches_passes",
                        "return {}",
                        [],
                        None,
                    )
                ],
                id="empty-branches",
            ),
        ],
    )
    def test_branch_return_cases(
        self,
        cases: list[tuple[str, str, list[dict], tuple[str, ...] | None]],
    ) -> None:
        for case_id, code, branches, expected_substrings in cases:
            error = ScriptReviewer._validate_branch_returns(code, branches)
            if expected_substrings is None:
                assert error is None, case_id
                continue
            assert error is not None, case_id
            for expected in expected_substrings:
                assert expected in error, case_id


class TestLoadFilteredRunParamValues:
    """Tests for load_filtered_run_param_values."""

    def _make_workflow_param(
        self, key: str, parameter_type: WorkflowParameterType = WorkflowParameterType.STRING
    ) -> object:
        from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter

        return WorkflowParameter(
            workflow_parameter_id=f"wp_{key}",
            workflow_parameter_type=parameter_type,
            workflow_id="w_1",
            key=key,
            created_at=datetime(2026, 1, 1),
            modified_at=datetime(2026, 1, 1),
        )

    def _make_credential_param(self, key: str) -> object:
        from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter

        return CredentialParameter(
            credential_parameter_id=f"cp_{key}",
            workflow_id="w_1",
            credential_id="cred_abc",
            key=key,
            created_at=datetime(2026, 1, 1),
            modified_at=datetime(2026, 1, 1),
        )

    async def _run_with_params(self, monkeypatch: pytest.MonkeyPatch, param_tuples: list) -> dict[str, str]:
        from skyvern.services.script_reviewer import load_filtered_run_param_values

        async def fake_get(workflow_run_id: str) -> list:
            return param_tuples

        monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run_parameters", fake_get)
        return await load_filtered_run_param_values("wr_test")

    @pytest.mark.parametrize(
        ("case_id", "param_tuples", "expected"),
        [
            pytest.param(
                "filters_sensitive_credential_param",
                "credential-param",
                {"account_number": "51410020"},
                id="credential-param-filtered",
            ),
            pytest.param(
                "filters_credential_id_subtype",
                "credential-id-subtype",
                {"account_number": "51410020"},
                id="credential-id-filtered",
            ),
            pytest.param("skips_blank_values", "blank-values", {"real": "51410020"}, id="blank-values-skipped"),
        ],
    )
    @pytest.mark.asyncio
    async def test_load_filtered_run_param_value_cases(
        self,
        monkeypatch: pytest.MonkeyPatch,
        case_id: str,
        param_tuples: str,
        expected: dict[str, str],
    ) -> None:
        if param_tuples == "credential-param":
            tuples = [
                (self._make_credential_param("login"), SimpleNamespace(value="cred_xyz")),
                (self._make_workflow_param("account_number"), SimpleNamespace(value="51410020")),
            ]
        elif param_tuples == "credential-id-subtype":
            tuples = [
                (
                    self._make_workflow_param("selected_credential", WorkflowParameterType.CREDENTIAL_ID),
                    SimpleNamespace(value="cred_abc"),
                ),
                (self._make_workflow_param("account_number"), SimpleNamespace(value="51410020")),
            ]
        else:
            tuples = [
                (self._make_workflow_param("blank"), SimpleNamespace(value="   ")),
                (self._make_workflow_param("none_val"), SimpleNamespace(value=None)),
                (self._make_workflow_param("real"), SimpleNamespace(value="51410020")),
            ]
        result = await self._run_with_params(monkeypatch, tuples)
        assert result == expected, case_id

    @pytest.mark.asyncio
    async def test_db_failure_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.services.script_reviewer import load_filtered_run_param_values

        async def fake_get_raises(workflow_run_id: str) -> list:
            raise RuntimeError("DB connection lost")

        monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run_parameters", fake_get_raises)
        result = await load_filtered_run_param_values("wr_test")
        assert result == {}


class TestFilterSelfOutputParamKeys:
    """Tests for _filter_self_output_param_keys."""

    @pytest.mark.parametrize(
        ("cases",),
        [
            pytest.param(
                [
                    (
                        "drops_self_output_only",
                        ["account_number", "Login_block_output", "Search_block_output"],
                        "Login_block",
                        ["account_number", "Search_block_output"],
                    ),
                    (
                        "keeps_upstream_outputs",
                        ["Search_results_output", "Login_block_output"],
                        "Login_block",
                        ["Search_results_output"],
                    ),
                    (
                        "keeps_downstream_outputs",
                        ["Login_block_output", "Final_extract_output"],
                        "Login_block",
                        ["Final_extract_output"],
                    ),
                    (
                        "partial_match_not_filtered",
                        ["Login_block_output_other", "Login_block_output"],
                        "Login_block",
                        ["Login_block_output_other"],
                    ),
                ],
                id="self-output-filtered-exactly",
            ),
            pytest.param(
                [
                    ("empty_block_label_is_noop", ["a", "b_output"], "", ["a", "b_output"]),
                    (
                        "no_self_output_present",
                        ["account_number", "username"],
                        "Login_block",
                        ["account_number", "username"],
                    ),
                ],
                id="no-filter-needed",
            ),
        ],
    )
    def test_filter_self_output_param_key_cases(self, cases: list[tuple[str, list[str], str, list[str]]]) -> None:
        for case_id, keys, block_label, expected in cases:
            assert ScriptReviewer._filter_self_output_param_keys(keys, block_label) == expected, case_id


class TestValidateParameterReferencesSelfOutput:
    """Tests for _validate_parameter_references when a self-output key is in play."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    @pytest.mark.parametrize(
        ("cases",),
        [
            pytest.param(
                [
                    (
                        "self_output_reference_rejected",
                        """
async def block_fn(page, context):
    await page.fill(selector='#user', value=context.parameters['Login_block_output']['username'])
""",
                        ["account_number"],
                        "Login_block_output",
                        ("Login_block_output",),
                    ),
                    (
                        "self_output_error_message_explains_why",
                        "value=context.parameters['Login_block_output']",
                        ["account_number"],
                        "Login_block_output",
                        ("None",),
                    ),
                    (
                        "self_output_rejected_with_empty_parameter_keys",
                        "value=context.parameters['Login_block_output']['username']",
                        [],
                        "Login_block_output",
                        ("Login_block_output",),
                    ),
                ],
                id="self-output-rejected",
            ),
            pytest.param(
                [
                    (
                        "validator_unchanged_without_self_output_key",
                        "value=context.parameters['fake_key']",
                        ["real_key"],
                        None,
                        ("fake_key", "!self-output"),
                    ),
                    (
                        "get_access_invented_key_rejected",
                        "value=context.parameters.get('made_up_field')",
                        ["account_number"],
                        None,
                        ("made_up_field",),
                    ),
                ],
                id="invented-keys",
            ),
            pytest.param(
                [
                    (
                        "no_constraints_returns_none",
                        "value=context.parameters['anything']",
                        [],
                        None,
                        None,
                    ),
                    (
                        "empty_valid_set_allows_unknown_non_self_refs",
                        "value=context.parameters['some_other_key']",
                        [],
                        "Login_block_output",
                        None,
                    ),
                    (
                        "multiline_ref_inside_comment_block_not_flagged",
                        (
                            "# context.parameters['fake_key']\n"
                            "# context.parameters.get('another_fake_key')\n"
                            "value=context.parameters['account_number']\n"
                        ),
                        ["account_number"],
                        None,
                        None,
                    ),
                ],
                id="allowed-reference-shapes",
            ),
            pytest.param(
                [
                    (
                        "get_access_self_output_rejected",
                        "value=context.parameters.get('Login_block_output')",
                        ["account_number"],
                        "Login_block_output",
                        ("Login_block_output",),
                    ),
                    (
                        "get_access_with_default_self_output_rejected",
                        "value=context.parameters.get('Login_block_output', '')",
                        ["account_number"],
                        "Login_block_output",
                        ("Login_block_output",),
                    ),
                    (
                        "spaced_subscript_self_output_rejected",
                        "value=context.parameters[ 'Login_block_output' ]['username']",
                        ["account_number"],
                        "Login_block_output",
                        ("Login_block_output",),
                    ),
                    (
                        "spaced_get_access_self_output_rejected",
                        "value=context.parameters.get( 'Login_block_output' )",
                        ["account_number"],
                        "Login_block_output",
                        ("Login_block_output",),
                    ),
                    (
                        "multiline_subscript_self_output_rejected",
                        "value=context.parameters[\n    'Login_block_output'\n]['username']",
                        ["account_number"],
                        "Login_block_output",
                        ("Login_block_output",),
                    ),
                    (
                        "multiline_get_access_self_output_rejected",
                        "value=context.parameters.get(\n    'Login_block_output',\n    {},\n)",
                        ["account_number"],
                        "Login_block_output",
                        ("Login_block_output",),
                    ),
                    (
                        "space_before_subscript_self_output_rejected",
                        "value=context.parameters ['Login_block_output']",
                        ["account_number"],
                        "Login_block_output",
                        ("Login_block_output",),
                    ),
                    (
                        "space_before_get_self_output_rejected",
                        "value=context.parameters .get('Login_block_output')",
                        ["account_number"],
                        "Login_block_output",
                        ("Login_block_output",),
                    ),
                ],
                id="self-output-access-variants",
            ),
        ],
    )
    def test_parameter_reference_cases(
        self,
        cases: list[tuple[str, str, list[str], str | None, tuple[str, ...] | None]],
    ) -> None:
        for case_id, code, parameter_keys, self_output_key, expected_substrings in cases:
            error = self.reviewer._validate_parameter_references(
                code,
                parameter_keys=parameter_keys,
                self_output_key=self_output_key,
            )
            if expected_substrings is None:
                assert error is None, case_id
                continue
            assert error is not None, case_id
            for expected in expected_substrings:
                if expected.startswith("!"):
                    assert expected[1:] not in error.lower(), case_id
                else:
                    assert expected in error, case_id


class TestConditionalCodeSelfOutputRejection:
    """The conditional-review path must reject self-output parameter references."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    @pytest.mark.asyncio
    async def test_generate_conditional_code_calls_validator_with_self_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        start_forge_stub_app()

        block_label = "Branch_block"
        bad_code = (
            "async def block_fn(page, context):\n"
            "    if context.parameters['Branch_block_output']['flag']:\n"
            '        return {"next_block_label": "next", "branch_index": 0}\n'
            "    else:\n"
            '        return {"next_block_label": None, "branch_index": 1}\n'
        )

        async def fake_llm_handler(*args: object, **kwargs: object) -> str:
            return f"```python\n{bad_code}\n```"

        app.SCRIPT_REVIEWER_LLM_API_HANDLER = AsyncMock(side_effect=fake_llm_handler)

        validator_calls: list[dict[str, object]] = []
        original_validator = self.reviewer._validate_parameter_references

        def spy_validator(code: str, parameter_keys: list[str], self_output_key: str | None = None) -> str | None:
            validator_calls.append(
                {"code_len": len(code), "param_keys": list(parameter_keys), "self_output_key": self_output_key}
            )
            return original_validator(code, parameter_keys, self_output_key=self_output_key)

        monkeypatch.setattr(self.reviewer, "_validate_parameter_references", spy_validator)

        episode = ScriptFallbackEpisode(
            episode_id="ep_1",
            organization_id="o_test",
            workflow_permanent_id="wpid_test",
            workflow_run_id="wr_1",
            block_label=block_label,
            fallback_type="conditional_agent",
            agent_actions={
                "expressions": [
                    {
                        "original_expression": "x > 0",
                        "rendered_expression": "1 > 0",
                        "result": True,
                        "is_default": False,
                        "next_block_label": "next",
                    },
                    {
                        "original_expression": None,
                        "rendered_expression": None,
                        "result": False,
                        "is_default": True,
                        "next_block_label": None,
                    },
                ]
            },
            created_at=datetime(2026, 1, 1),
            modified_at=datetime(2026, 1, 1),
        )

        result = await self.reviewer._generate_conditional_code(
            block_label=block_label,
            episode=episode,
            organization_id="o_test",
            run_parameter_values=None,
            all_parameter_keys=["account_number", f"{block_label}_output"],
        )

        assert validator_calls, "validator was never invoked — wiring broken"
        assert all(call["self_output_key"] == f"{block_label}_output" for call in validator_calls), (
            f"validator received wrong self_output_key: {validator_calls}"
        )
        assert result is None, "self-output ref should have been rejected before persistence"

    @pytest.mark.asyncio
    async def test_review_conditional_blocks_threads_parameter_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        start_forge_stub_app()

        block_label = "Branch_block"

        async def fake_llm_handler(*args: object, **kwargs: object) -> str:
            return '```python\nasync def block_fn(page, context):\n    return {"next_block_label": None, "branch_index": 0}\n```'

        app.SCRIPT_REVIEWER_LLM_API_HANDLER = AsyncMock(side_effect=fake_llm_handler)

        load_calls: list[dict[str, str]] = []

        async def fake_load(organization_id: str, workflow_permanent_id: str) -> tuple:
            load_calls.append({"organization_id": organization_id, "workflow_permanent_id": workflow_permanent_id})
            return (
                {block_label: ""},
                ["account_number", f"{block_label}_output"],
                {},
            )

        monkeypatch.setattr(self.reviewer, "_load_workflow_context", fake_load)

        validator_calls: list[dict[str, object]] = []
        original_validator = self.reviewer._validate_parameter_references

        def spy_validator(code: str, parameter_keys: list[str], self_output_key: str | None = None) -> str | None:
            validator_calls.append({"param_keys": list(parameter_keys), "self_output_key": self_output_key})
            return original_validator(code, parameter_keys, self_output_key=self_output_key)

        monkeypatch.setattr(self.reviewer, "_validate_parameter_references", spy_validator)

        episode = ScriptFallbackEpisode(
            episode_id="ep_1",
            organization_id="o_test",
            workflow_permanent_id="wpid_test",
            workflow_run_id="wr_1",
            block_label=block_label,
            fallback_type="conditional_agent",
            agent_actions={
                "expressions": [
                    {
                        "original_expression": "x > 0",
                        "rendered_expression": "1 > 0",
                        "result": True,
                        "is_default": False,
                        "next_block_label": None,
                    },
                ]
            },
            created_at=datetime(2026, 1, 1),
            modified_at=datetime(2026, 1, 1),
        )

        await self.reviewer.review_conditional_blocks(
            organization_id="o_test",
            workflow_permanent_id="wpid_test",
            conditional_episodes=[episode],
            run_parameter_values=None,
        )

        assert load_calls == [{"organization_id": "o_test", "workflow_permanent_id": "wpid_test"}], (
            "review_conditional_blocks did not load workflow context — the wiring is broken"
        )
        assert validator_calls, "validator was never invoked from the conditional path"
        for call in validator_calls:
            assert "account_number" in call["param_keys"], "upstream key should have been threaded through"
            assert f"{block_label}_output" not in call["param_keys"], "self-output should have been filtered"
            assert call["self_output_key"] == f"{block_label}_output", "self_output_key not threaded correctly"

    def test_validator_directly_rejects_self_output(self) -> None:
        block_label = "Branch_block"
        code_with_self_output = (
            "async def block_fn(page, context):\n"
            "    if context.parameters['Branch_block_output']['flag']:\n"
            '        return {"next_block_label": "next", "branch_index": 0}\n'
        )
        param_keys = self.reviewer._filter_self_output_param_keys(sorted({"account_number"}), block_label)
        error = self.reviewer._validate_parameter_references(
            code_with_self_output, param_keys, self_output_key=f"{block_label}_output"
        )
        assert error is not None
        assert "Branch_block_output" in error
        assert "None" in error
