"""Tests for ScriptReviewer validation methods."""

import pytest

from skyvern.services.script_reviewer import ScriptReviewer


class TestValidateNoHardcodedValues:
    """Tests for _validate_no_hardcoded_values."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_no_params_returns_none(self) -> None:
        code = 'await page.click(selector="button")'
        assert self.reviewer._validate_no_hardcoded_values(code, None) is None
        assert self.reviewer._validate_no_hardcoded_values(code, {}) is None

    def test_short_values_ignored(self) -> None:
        """Values shorter than 5 chars should not trigger (too many false positives)."""
        code = """
async def block_fn(page, context):
    if 'yes' == 'yes':
        return {"next_block_label": "send_email", "branch_index": 0}
"""
        params = {"flag": "yes", "ok": "true"}
        assert self.reviewer._validate_no_hardcoded_values(code, params) is None

    def test_detects_hardcoded_email(self) -> None:
        """Should catch a hardcoded email that should be context.parameters['recipient']."""
        code = """
async def block_fn(page, context):
    recipient = 'billing@acme-test.example.com'
    if recipient != "":
        return {"next_block_label": "send_email", "branch_index": 0}
"""
        params = {"recipient": "billing@acme-test.example.com"}
        error = self.reviewer._validate_no_hardcoded_values(code, params)
        assert error is not None
        assert "hardcoded" in error.lower()
        assert "recipient" in error
        assert "context.parameters" in error  # nosemgrep: incomplete-url-substring-sanitization

    def test_detects_hardcoded_url(self) -> None:
        """Should catch a hardcoded URL that should be context.parameters['website_url']."""
        code = """
async def login(page, context):
    await page.goto('https://portal.vendor.com/login')
    await page.fill(selector='input[name="email"]', value=context.parameters['email'])
"""
        params = {"website_url": "https://portal.vendor.com/login", "email": "user@example.com"}
        error = self.reviewer._validate_no_hardcoded_values(code, params)
        assert error is not None
        assert "website_url" in error

    def test_allows_context_parameters_reference(self) -> None:
        """Using context.parameters['key'] should NOT trigger the validator."""
        code = """
async def block_fn(page, context):
    recipient = context.parameters.get('recipient', '')
    if recipient != "":
        return {"next_block_label": "send_email", "branch_index": 0}
"""
        params = {"recipient": "billing@acme-test.example.com"}
        assert self.reviewer._validate_no_hardcoded_values(code, params) is None

    def test_allows_non_parameter_literals(self) -> None:
        """String literals that aren't parameter values should be fine."""
        code = """
async def block_fn(page, context):
    await page.click(selector='button:has-text("Sign in")', ai='fallback', prompt='Click sign in')
"""
        params = {"email": "user@example.com", "password": "secret123"}
        assert self.reviewer._validate_no_hardcoded_values(code, params) is None

    def test_ignores_comments(self) -> None:
        """Values in comments should not trigger."""
        code = """
async def block_fn(page, context):
    # billing@acme-test.example.com is the test email
    recipient = context.parameters.get('recipient', '')
    if recipient != "":
        return {"next_block_label": "send_email", "branch_index": 0}
"""
        params = {"recipient": "billing@acme-test.example.com"}
        assert self.reviewer._validate_no_hardcoded_values(code, params) is None

    def test_detects_multiple_hardcoded_values(self) -> None:
        """Should report multiple hardcoded values."""
        code = """
async def block_fn(page, context):
    await page.goto('https://portal.vendor.com/login')
    await page.fill(selector='input', value='billing@acme-test.example.com')
"""
        params = {
            "website_url": "https://portal.vendor.com/login",
            "recipient": "billing@acme-test.example.com",
        }
        error = self.reviewer._validate_no_hardcoded_values(code, params)
        assert error is not None
        assert "2 hardcoded" in error

    def test_allows_block_labels_and_keywords(self) -> None:
        """Common structural strings like block labels should not trigger."""
        code = """
async def block_fn(page, context):
    return {"next_block_label": "send_email", "branch_index": 0}
"""
        # Even if "send_email" is technically a value somewhere, it's a block label
        params = {"account_number": "12345678"}
        assert self.reviewer._validate_no_hardcoded_values(code, params) is None

    # ------------------------------------------------------------------
    # SKY-9295: substring matching — param values often appear embedded
    # inside longer click-prompt literals like
    #   prompt='Should I download invoice 12/8/2025 for account 51410020?'
    # rather than as standalone literals. Pre-fix the matcher used exact
    # set membership (`param_value in code_literals`) and missed these.
    # ------------------------------------------------------------------

    def test_detects_param_embedded_in_click_prompt(self) -> None:
        """Account number embedded inside a click prompt string should trigger."""
        code = """
async def download_invoice(page, context):
    await page.click(
        ai='fallback',
        prompt='Should I download the invoice dated 12/8/2025 for account 51410020?',
    )
"""
        params = {"account_number": "51410020", "download_start_date": "12/8/2025"}
        error = self.reviewer._validate_no_hardcoded_values(code, params)
        assert error is not None
        assert "account_number" in error
        assert "download_start_date" in error

    def test_detects_single_param_embedded_in_prompt(self) -> None:
        """Validator catches a substring even when only one param value is embedded."""
        code = """
async def block_fn(page, context):
    await page.click(prompt='Find the row for vendor CITYO49057A and click it')
"""
        params = {"vendor_code": "CITYO49057A"}
        error = self.reviewer._validate_no_hardcoded_values(code, params)
        assert error is not None
        assert "vendor_code" in error

    def test_substring_match_respects_short_value_guard(self) -> None:
        """Short param values still don't false-positive even with substring matching."""
        # "yes" appears inside "yesterday" but is below the 5-char length guard.
        code = """
async def block_fn(page, context):
    await page.click(prompt='Click the yesterday button')
"""
        params = {"flag": "yes"}
        assert self.reviewer._validate_no_hardcoded_values(code, params) is None

    def test_substring_match_skips_comments(self) -> None:
        """Embedded matches inside comments should still be ignored."""
        code = """
async def block_fn(page, context):
    # Note: 51410020 is the test account number for staging
    await page.click(prompt='Click the next button')
"""
        params = {"account_number": "51410020"}
        assert self.reviewer._validate_no_hardcoded_values(code, params) is None

    def test_substring_match_skips_structural_literals(self) -> None:
        """Substring matching only applies to prose literals (whitespace + ≥20 chars).
        A short structural token like ``'next_block_label'`` should not trigger a
        false positive when a param value happens to be embedded in the token."""
        code = """
async def block_fn(page, context):
    return {"next_block_label": "send_email", "branch_index": 0}
"""
        # "block" (5 chars) is a substring of "next_block_label" but
        # "next_block_label" is a structural keyword, not a prose literal — it
        # has no whitespace and is only 16 chars. The validator must ignore it.
        params = {"target": "block"}
        assert self.reviewer._validate_no_hardcoded_values(code, params) is None

    def test_validator_threshold_matches_generator_threshold(self) -> None:
        """Validator's minimum length must match the generator's substitution
        threshold (``MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB``). A 4-char value
        embedded in a prose literal is hardcoded and should be flagged."""
        from skyvern.core.script_generations.generate_script import MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB

        # Sanity: keep this test honest if the generator threshold ever changes.
        assert MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB == 4

        # Embed "1234" in a prose literal (whitespace + > prose-min-len).
        code = """
async def block_fn(page, context):
    await page.click(prompt='Click the box labeled 1234 to continue')
"""
        params = {"order_code": "1234"}
        error = self.reviewer._validate_no_hardcoded_values(code, params)
        assert error is not None
        assert "order_code" in error

    # ------------------------------------------------------------------
    # SKY-9295 v2: libcst-based literal extraction so triple-quoted f-strings
    # and other multiline prompts are no longer a blind spot.
    # ------------------------------------------------------------------

    def test_validator_catches_hardcoded_in_triple_quoted_prompt(self) -> None:
        """Generator emits triple-quoted f-strings when prompt content has
        newlines or quotes (generate_script.py:547-548). The pre-libcst regex
        scanner missed those. With libcst, literals from triple-quoted f-strings
        are visible to substring matching."""
        code = '''
async def block_fn(page, context):
    await page.click(
        ai='fallback',
        prompt=f"""Find the row for account 51410020
and click the download button""",
    )
'''
        params = {"account_number": "51410020"}
        error = self.reviewer._validate_no_hardcoded_values(code, params)
        assert error is not None
        assert "account_number" in error

    def test_validator_catches_hardcoded_in_fstring_text_segment(self) -> None:
        """Static text segments of an f-string (between {expr} interpolations)
        are scannable; if a parameter value is baked into the text portion, the
        validator must flag it."""
        code = """
async def block_fn(page, context):
    name = context.parameters['name']
    await page.click(prompt=f'Send invoice 51410020 to {name}')
"""
        params = {"account_number": "51410020"}
        error = self.reviewer._validate_no_hardcoded_values(code, params)
        assert error is not None
        assert "account_number" in error

    def test_validator_skips_fstring_interpolation_expression(self) -> None:
        """Interpolation expressions reference runtime values
        (``context.parameters[...]``), not literals — they shouldn't be scanned
        for hardcoded values. A reference to a key by string subscript inside
        an interpolation must NOT be flagged."""
        code = """
async def block_fn(page, context):
    await page.click(prompt=f"Send to {context.parameters['recipient']}")
"""
        params = {"recipient": "alice@example.com"}
        # No literal contains "alice@example.com"; only the interpolation
        # references the key. Validator must not false-positive.
        error = self.reviewer._validate_no_hardcoded_values(code, params)
        assert error is None

    # ------------------------------------------------------------------
    # SKY-9295 v2: prose-literal threshold (whitespace + ≥ PROSE_LITERAL_MIN_LEN
    # chars). The floor is 8 (down from a stricter 20) so short embedded
    # prompts like ``'Click 51410020 now'`` are still caught.
    # ------------------------------------------------------------------

    def test_prose_literal_band_catches_embedded_value(self) -> None:
        """A short prose literal with whitespace catches an embedded param
        value via substring matching."""
        code = """
async def block_fn(page, context):
    await page.click(prompt='Click 51410020 now')
"""
        # "Click 51410020 now" = 18 chars, has whitespace, ≥ 8. Prose-eligible.
        params = {"account_number": "51410020"}
        error = self.reviewer._validate_no_hardcoded_values(code, params)
        assert error is not None
        assert "account_number" in error

    def test_prose_literal_band_below_floor_skips(self) -> None:
        """Whitespace-bearing literals shorter than the prose floor are not
        eligible for substring matching (and exact-match still fails because
        the literal isn't equal to the param value)."""
        # "ok 1234" = 7 chars, < 8 → not prose; not equal to "1234" → not exact-match.
        code = """
async def block_fn(page, context):
    await page.click(prompt='ok 1234')
"""
        params = {"order_code": "1234"}
        error = self.reviewer._validate_no_hardcoded_values(code, params)
        assert error is None

    def test_prose_literal_no_whitespace_skips(self) -> None:
        """Mid-length literals without whitespace are not prose — they're
        structural identifiers (``order_12345_id``, dict keys, etc.)."""
        code = """
async def block_fn(page, context):
    return {"order_12345_id": "value", "branch_index": 0}
"""
        params = {"order_id": "12345"}
        error = self.reviewer._validate_no_hardcoded_values(code, params)
        assert error is None

    def test_prose_literal_band_exact_match_still_fires(self) -> None:
        """A param value that equals an entire short literal is still flagged
        via the exact-match path (which runs against every literal regardless
        of prose status)."""
        code = """
async def block_fn(page, context):
    await page.fill(selector="#code", value="12345")
"""
        params = {"order_code": "12345"}
        error = self.reviewer._validate_no_hardcoded_values(code, params)
        assert error is not None
        assert "order_code" in error

    # ------------------------------------------------------------------
    # SKY-9295 v2: MAX cap aligned with generator.
    # ------------------------------------------------------------------

    def test_validator_skips_oversized_param_values(self) -> None:
        """Param values longer than ``MAX_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB``
        are skipped — the generator wouldn't have substituted them, so the
        validator must not flag them either (else generator/validator drift
        creates an unfixable diagnostic loop)."""
        from skyvern.core.script_generations.generate_script import MAX_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB

        oversized = "x" * (MAX_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB + 1)
        # Even though the literal contains the oversized value, it must be skipped.
        code = f'''
async def block_fn(page, context):
    note = "{oversized}"
'''
        params = {"huge_payload": oversized}
        error = self.reviewer._validate_no_hardcoded_values(code, params)
        assert error is None


class TestValidateParameterPreservation:
    """Tests for _validate_parameter_preservation."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_no_existing_code_returns_none(self) -> None:
        new_code = "await page.fill(selector='#email', ai='proactive', prompt='email')"
        assert self.reviewer._validate_parameter_preservation(new_code, None, ["email"]) is None

    def test_no_parameter_keys_returns_none(self) -> None:
        old_code = "await page.fill(selector='#email', value=context.parameters['email'])"
        new_code = "await page.fill(selector='#email', ai='proactive', prompt='email')"
        assert self.reviewer._validate_parameter_preservation(new_code, old_code, []) is None

    def test_preserved_refs_returns_none(self) -> None:
        """When all parameter refs are preserved, validation passes."""
        old_code = """
await page.fill(selector='#email', value=context.parameters['email'])
await page.fill(selector='#pass', value=context.parameters['password'])
"""
        new_code = """
choice = await page.classify(...)
if choice == 0:
    await page.fill(selector='#email', value=context.parameters['email'])
    await page.fill(selector='#pass', value=context.parameters['password'])
"""
        assert self.reviewer._validate_parameter_preservation(new_code, old_code, ["email", "password"]) is None

    def test_detects_dropped_refs(self) -> None:
        """When the LLM drops value= refs, validation catches it."""
        old_code = """
await page.fill(selector='#email', value=context.parameters['email'])
await page.fill(selector='#pass', value=context.parameters['password'])
"""
        new_code = """
choice = await page.classify(...)
if choice == 0:
    await page.fill(selector='#email', ai='proactive', prompt='fill email')
    await page.fill(selector='#pass', ai='proactive', prompt='fill password')
"""
        error = self.reviewer._validate_parameter_preservation(new_code, old_code, ["email", "password"])
        assert error is not None
        assert "email" in error
        assert "password" in error
        assert "dropped" in error.lower()

    def test_ignores_refs_not_in_parameter_keys(self) -> None:
        """Spurious refs in old code that aren't valid keys should be ignored."""
        old_code = "await page.fill(selector='#x', value=context.parameters['invented_key'])"
        new_code = "await page.fill(selector='#x', ai='proactive', prompt='fill x')"
        assert self.reviewer._validate_parameter_preservation(new_code, old_code, ["email", "password"]) is None

    def test_partial_drop_detected(self) -> None:
        """Dropping one ref while keeping another should flag only the dropped one."""
        old_code = """
await page.fill(selector='#email', value=context.parameters['email'])
await page.fill(selector='#pass', value=context.parameters['password'])
"""
        new_code = """
await page.fill(selector='#email', value=context.parameters['email'])
await page.fill(selector='#pass', ai='proactive', prompt='fill password')
"""
        error = self.reviewer._validate_parameter_preservation(new_code, old_code, ["email", "password"])
        assert error is not None
        assert "context.parameters['password']" in error
        assert "context.parameters['email']" not in error

    def test_commented_ref_in_new_code_counts_as_dropped(self) -> None:
        """A parameter ref only in a comment in new code should be flagged as dropped."""
        old_code = "await page.fill(selector='#email', value=context.parameters['email'])"
        new_code = """
# was: context.parameters['email']
await page.fill(selector='#email', ai='proactive', prompt='fill email')
"""
        error = self.reviewer._validate_parameter_preservation(new_code, old_code, ["email"])
        assert error is not None
        assert "email" in error


class TestValidateBranchReturns:
    """Tests for _validate_branch_returns."""

    BRANCHES_TWO = [
        {"original_expression": "x > 0", "next_block_label": "block_3", "is_default": False},
        {"original_expression": None, "next_block_label": "block_4", "is_default": True},
    ]

    def test_valid_returns_passes(self) -> None:
        code = """
async def block_fn(page, context):
    if context.parameters.get('x', 0) > 0:
        return {"next_block_label": "block_3", "branch_index": 0}
    else:
        return {"next_block_label": "block_4", "branch_index": 1}
"""
        assert ScriptReviewer._validate_branch_returns(code, self.BRANCHES_TWO) is None

    def test_invalid_label_detected(self) -> None:
        """Labels not in the branch definitions should be flagged."""
        code = """
async def block_fn(page, context):
    if context.parameters.get('x', 0) > 0:
        return {"next_block_label": "block_99", "branch_index": 0}
    else:
        return {"next_block_label": "block_4", "branch_index": 1}
"""
        error = ScriptReviewer._validate_branch_returns(code, self.BRANCHES_TWO)
        assert error is not None
        assert "block_99" in error

    def test_invalid_index_detected(self) -> None:
        """branch_index values outside 0..N-1 should be flagged."""
        code = """
async def block_fn(page, context):
    if context.parameters.get('x', 0) > 0:
        return {"next_block_label": "block_3", "branch_index": 0}
    else:
        return {"next_block_label": "block_4", "branch_index": -1}
"""
        error = ScriptReviewer._validate_branch_returns(code, self.BRANCHES_TWO)
        assert error is not None
        assert "-1" in error

    def test_none_label_invalid_when_not_in_branches(self) -> None:
        """None next_block_label should be flagged when no branch has a null target."""
        code = """
async def block_fn(page, context):
    if context.parameters.get('x', 0) > 0:
        return {"next_block_label": "block_3", "branch_index": 0}
    else:
        return {"next_block_label": None, "branch_index": 1}
"""
        error = ScriptReviewer._validate_branch_returns(code, self.BRANCHES_TWO)
        assert error is not None
        assert "next_block_label" in error

    def test_none_label_valid_when_in_branches(self) -> None:
        """None next_block_label should pass when a branch has a null target."""
        branches = [
            {"original_expression": "x > 0", "next_block_label": "block_3", "is_default": False},
            {"original_expression": None, "next_block_label": None, "is_default": True},
        ]
        code = """
async def block_fn(page, context):
    if context.parameters.get('x', 0) > 0:
        return {"next_block_label": "block_3", "branch_index": 0}
    else:
        return {"next_block_label": None, "branch_index": 1}
"""
        assert ScriptReviewer._validate_branch_returns(code, branches) is None

    def test_no_literals_passes(self) -> None:
        """Code using variables (not literals) should pass — can't validate statically."""
        code = """
async def block_fn(page, context):
    label = compute_label()
    idx = compute_index()
    return {"next_block_label": label, "branch_index": idx}
"""
        assert ScriptReviewer._validate_branch_returns(code, self.BRANCHES_TWO) is None

    def test_comments_ignored(self) -> None:
        """Values in comments should not trigger validation."""
        code = """
async def block_fn(page, context):
    # return {"next_block_label": "block_99", "branch_index": -1}
    if context.parameters.get('x', 0) > 0:
        return {"next_block_label": "block_3", "branch_index": 0}
    else:
        return {"next_block_label": "block_4", "branch_index": 1}
"""
        assert ScriptReviewer._validate_branch_returns(code, self.BRANCHES_TWO) is None

    def test_empty_branches_passes(self) -> None:
        assert ScriptReviewer._validate_branch_returns("return {}", []) is None

    def test_single_quotes_handled(self) -> None:
        """Single-quoted keys should be parsed correctly."""
        code = """
async def block_fn(page, context):
    return {'next_block_label': 'block_3', 'branch_index': 0}
"""
        assert ScriptReviewer._validate_branch_returns(code, self.BRANCHES_TWO) is None

    def test_both_label_and_index_invalid(self) -> None:
        """Both invalid label and index should be reported."""
        code = """
async def block_fn(page, context):
    return {"next_block_label": "wrong_label", "branch_index": -1}
"""
        error = ScriptReviewer._validate_branch_returns(code, self.BRANCHES_TWO)
        assert error is not None
        assert "wrong_label" in error
        assert "-1" in error


class TestLoadFilteredRunParamValues:
    """Tests for load_filtered_run_param_values (shared loader for 3 reviewer ingestion sites)."""

    def _make_workflow_param(self, key: str) -> object:
        from datetime import datetime

        from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter, WorkflowParameterType

        return WorkflowParameter(
            workflow_parameter_id=f"wp_{key}",
            workflow_parameter_type=WorkflowParameterType.STRING,
            workflow_id="w_1",
            key=key,
            created_at=datetime(2026, 1, 1),
            modified_at=datetime(2026, 1, 1),
        )

    def _make_credential_param(self, key: str) -> object:
        from datetime import datetime

        from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter

        return CredentialParameter(
            credential_parameter_id=f"cp_{key}",
            workflow_id="w_1",
            credential_id="cred_abc",
            key=key,
            created_at=datetime(2026, 1, 1),
            modified_at=datetime(2026, 1, 1),
        )

    async def _run_with_params(self, monkeypatch: object, param_tuples: list) -> dict[str, str]:
        from skyvern.services.script_reviewer import load_filtered_run_param_values

        async def fake_get(workflow_run_id: str) -> list:
            return param_tuples

        # Patch DATABASE.workflow_runs.get_workflow_run_parameters
        from skyvern.forge import app

        monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run_parameters", fake_get)  # type: ignore[attr-defined]
        return await load_filtered_run_param_values("wr_test")

    @pytest.mark.asyncio
    async def test_filters_sensitive_credential_param(self, monkeypatch: object) -> None:
        from types import SimpleNamespace

        wf_param_secret = self._make_credential_param("login")
        wf_param_normal = self._make_workflow_param("account_number")
        param_tuples = [
            (wf_param_secret, SimpleNamespace(value="cred_xyz")),
            (wf_param_normal, SimpleNamespace(value="51410020")),
        ]
        result = await self._run_with_params(monkeypatch, param_tuples)
        assert result == {"account_number": "51410020"}

    @pytest.mark.asyncio
    async def test_filters_credential_id_subtype(self, monkeypatch: object) -> None:
        from datetime import datetime
        from types import SimpleNamespace

        from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter, WorkflowParameterType

        wf_param_credential_id = WorkflowParameter(
            workflow_parameter_id="wp_cred",
            workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
            workflow_id="w_1",
            key="selected_credential",
            created_at=datetime(2026, 1, 1),
            modified_at=datetime(2026, 1, 1),
        )
        wf_param_normal = self._make_workflow_param("account_number")
        param_tuples = [
            (wf_param_credential_id, SimpleNamespace(value="cred_abc")),
            (wf_param_normal, SimpleNamespace(value="51410020")),
        ]
        result = await self._run_with_params(monkeypatch, param_tuples)
        assert "selected_credential" not in result
        assert result == {"account_number": "51410020"}

    @pytest.mark.asyncio
    async def test_db_failure_returns_empty(self, monkeypatch: object) -> None:
        from skyvern.forge import app
        from skyvern.services.script_reviewer import load_filtered_run_param_values

        async def fake_get_raises(workflow_run_id: str) -> list:
            raise RuntimeError("DB connection lost")

        monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run_parameters", fake_get_raises)  # type: ignore[attr-defined]
        result = await load_filtered_run_param_values("wr_test")
        assert result == {}

    @pytest.mark.asyncio
    async def test_skips_blank_values(self, monkeypatch: object) -> None:
        from types import SimpleNamespace

        wf_param_a = self._make_workflow_param("blank")
        wf_param_b = self._make_workflow_param("none_val")
        wf_param_c = self._make_workflow_param("real")
        param_tuples = [
            (wf_param_a, SimpleNamespace(value="   ")),  # whitespace-only
            (wf_param_b, SimpleNamespace(value=None)),
            (wf_param_c, SimpleNamespace(value="51410020")),
        ]
        result = await self._run_with_params(monkeypatch, param_tuples)
        assert result == {"real": "51410020"}
