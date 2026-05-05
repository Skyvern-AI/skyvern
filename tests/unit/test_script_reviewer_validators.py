"""Tests for ScriptReviewer validation methods."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.schemas.scripts import ScriptFallbackEpisode
from skyvern.services.script_reviewer import ScriptReviewer
from tests.unit.force_stub_app import start_forge_stub_app


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


class TestFilterSelfOutputParamKeys:
    """Tests for _filter_self_output_param_keys.

    Each block carries an auto-generated ``<block_label>_output`` parameter.
    Inside the block's own cached function that slot is ``None`` until the
    block has finished running, so any ``context.parameters['<self>_output']``
    reference crashes with ``'NoneType' object is not subscriptable``.
    Upstream and downstream block outputs are valid references — only the
    *current* block's output must be filtered.
    """

    def test_drops_self_output_only(self) -> None:
        keys = ["account_number", "Login_block_output", "Search_block_output"]
        filtered = ScriptReviewer._filter_self_output_param_keys(keys, "Login_block")
        assert filtered == ["account_number", "Search_block_output"]

    def test_keeps_upstream_outputs(self) -> None:
        """Upstream `_output` keys are legitimate cross-block references and must survive."""
        keys = ["Search_results_output", "Login_block_output"]
        filtered = ScriptReviewer._filter_self_output_param_keys(keys, "Login_block")
        assert "Search_results_output" in filtered
        assert "Login_block_output" not in filtered

    def test_keeps_downstream_outputs(self) -> None:
        """Downstream `_output` keys aren't read by the current block in practice,
        but filtering only the self-output keeps the rule narrow and predictable."""
        keys = ["Login_block_output", "Final_extract_output"]
        filtered = ScriptReviewer._filter_self_output_param_keys(keys, "Login_block")
        assert "Final_extract_output" in filtered
        assert "Login_block_output" not in filtered

    def test_empty_block_label_is_noop(self) -> None:
        """An empty block_label means we don't know what to filter — pass through."""
        keys = ["a", "b_output"]
        assert ScriptReviewer._filter_self_output_param_keys(keys, "") == keys

    def test_no_self_output_present(self) -> None:
        keys = ["account_number", "username"]
        assert ScriptReviewer._filter_self_output_param_keys(keys, "Login_block") == keys

    def test_partial_match_not_filtered(self) -> None:
        """`block_output_other` matches `<block>_output` as a prefix but isn't the
        self-output key. The filter must be exact-match, not prefix-match."""
        keys = ["Login_block_output_other", "Login_block_output"]
        filtered = ScriptReviewer._filter_self_output_param_keys(keys, "Login_block")
        assert filtered == ["Login_block_output_other"]


class TestValidateParameterReferencesSelfOutput:
    """Tests for _validate_parameter_references when a self-output key is in play."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_self_output_reference_rejected(self) -> None:
        """When `parameter_keys` excludes the self-output, a `context.parameters['<self>_output']`
        reference in the LLM output must be rejected by the validator."""
        code = """
async def block_fn(page, context):
    await page.fill(selector='#user', value=context.parameters['Login_block_output']['username'])
"""
        # parameter_keys does NOT contain Login_block_output (already filtered)
        error = self.reviewer._validate_parameter_references(
            code,
            parameter_keys=["account_number"],
            self_output_key="Login_block_output",
        )
        assert error is not None
        assert "Login_block_output" in error

    def test_self_output_error_message_explains_why(self) -> None:
        """The retry hint should specifically explain the self-output crash so the LLM
        doesn't reintroduce the same pattern on the next attempt."""
        code = "value=context.parameters['Login_block_output']"
        error = self.reviewer._validate_parameter_references(
            code,
            parameter_keys=["account_number"],
            self_output_key="Login_block_output",
        )
        assert error is not None
        assert "None" in error  # message should mention the None / NoneType crash mode

    def test_validator_unchanged_without_self_output_key(self) -> None:
        """Existing callers that don't pass `self_output_key` keep the prior behavior."""
        code = "value=context.parameters['fake_key']"
        error = self.reviewer._validate_parameter_references(code, parameter_keys=["real_key"])
        assert error is not None
        assert "fake_key" in error
        # No self-output augmentation when caller didn't opt in.
        assert "self-output" not in error.lower()

    # ------------------------------------------------------------------
    # Empty-valid-set edge case: when the workflow's only declared parameter
    # was the block's own output (filtered → empty parameter_keys), the
    # validator must still scan to catch a self-output ref.
    # ------------------------------------------------------------------

    def test_self_output_rejected_with_empty_parameter_keys(self) -> None:
        """When parameter_keys is empty BUT self_output_key is set, the
        validator must still reject self-output references — otherwise the bug
        slips through any workflow whose only parameter was the block's own
        output."""
        code = "value=context.parameters['Login_block_output']['username']"
        error = self.reviewer._validate_parameter_references(
            code,
            parameter_keys=[],
            self_output_key="Login_block_output",
        )
        assert error is not None
        assert "Login_block_output" in error

    def test_no_constraints_returns_none(self) -> None:
        """No parameter_keys AND no self_output_key → no validation possible."""
        code = "value=context.parameters['anything']"
        assert self.reviewer._validate_parameter_references(code, parameter_keys=[]) is None

    def test_empty_valid_set_allows_unknown_non_self_refs(self) -> None:
        """Empty ``parameter_keys`` + ``self_output_key`` in scope: the reviewer
        rejects only the self-output ref. Other unknown refs may be runtime-
        valid synthesized keys (``GeneratedWorkflowParameters`` field names)
        that the reviewer doesn't load into its valid set, so we let them
        through rather than risking false-positive rejection of legitimate
        deterministic-named parameters.
        """
        code = "value=context.parameters['some_other_key']"
        error = self.reviewer._validate_parameter_references(
            code,
            parameter_keys=[],
            self_output_key="Login_block_output",
        )
        assert error is None

    # ------------------------------------------------------------------
    # `.get('key')` access: regex must catch this form too — the reviewer
    # prompt's example output uses ``.get(...)``, so the LLM can emit it.
    # ------------------------------------------------------------------

    def test_get_access_self_output_rejected(self) -> None:
        """`context.parameters.get('<self>_output')` must be caught the same as
        the subscript form."""
        code = "value=context.parameters.get('Login_block_output')"
        error = self.reviewer._validate_parameter_references(
            code,
            parameter_keys=["account_number"],
            self_output_key="Login_block_output",
        )
        assert error is not None
        assert "Login_block_output" in error

    def test_get_access_with_default_self_output_rejected(self) -> None:
        """The `.get(key, default)` two-argument form is also caught."""
        code = "value=context.parameters.get('Login_block_output', '')"
        error = self.reviewer._validate_parameter_references(
            code,
            parameter_keys=["account_number"],
            self_output_key="Login_block_output",
        )
        assert error is not None
        assert "Login_block_output" in error

    def test_get_access_invented_key_rejected(self) -> None:
        """Generic invented-key detection also applies via the `.get()` regex."""
        code = "value=context.parameters.get('made_up_field')"
        error = self.reviewer._validate_parameter_references(
            code,
            parameter_keys=["account_number"],
        )
        assert error is not None
        assert "made_up_field" in error

    # ------------------------------------------------------------------
    # Whitespace inside subscript brackets / `.get()` parens. Reformatted
    # variants like ``[ 'key' ]`` must not bypass the validator.
    # ------------------------------------------------------------------

    def test_spaced_subscript_self_output_rejected(self) -> None:
        """`context.parameters[ 'X_output' ]['username']` must be caught despite
        whitespace inside the outer subscript brackets."""
        code = "value=context.parameters[ 'Login_block_output' ]['username']"
        error = self.reviewer._validate_parameter_references(
            code,
            parameter_keys=["account_number"],
            self_output_key="Login_block_output",
        )
        assert error is not None
        assert "Login_block_output" in error

    def test_spaced_get_access_self_output_rejected(self) -> None:
        """`.get( 'X_output' )` form with whitespace must also be caught."""
        code = "value=context.parameters.get( 'Login_block_output' )"
        error = self.reviewer._validate_parameter_references(
            code,
            parameter_keys=["account_number"],
            self_output_key="Login_block_output",
        )
        assert error is not None
        assert "Login_block_output" in error

    # ------------------------------------------------------------------
    # Multiline access: ``ruff format`` can wrap long subscripts across
    # multiple lines. The validator must still match.
    # ------------------------------------------------------------------

    def test_multiline_subscript_self_output_rejected(self) -> None:
        """A subscript split across multiple lines must still be caught."""
        code = "value=context.parameters[\n    'Login_block_output'\n]['username']"
        error = self.reviewer._validate_parameter_references(
            code,
            parameter_keys=["account_number"],
            self_output_key="Login_block_output",
        )
        assert error is not None
        assert "Login_block_output" in error

    def test_multiline_get_access_self_output_rejected(self) -> None:
        """A `.get(...)` call split across multiple lines must still be caught."""
        code = "value=context.parameters.get(\n    'Login_block_output',\n    {},\n)"
        error = self.reviewer._validate_parameter_references(
            code,
            parameter_keys=["account_number"],
            self_output_key="Login_block_output",
        )
        assert error is not None
        assert "Login_block_output" in error

    # ------------------------------------------------------------------
    # Whitespace between ``parameters`` and the accessor: valid Python
    # (``context.parameters [...]``, ``context.parameters .get(...)``)
    # must be matched too.
    # ------------------------------------------------------------------

    def test_space_before_subscript_self_output_rejected(self) -> None:
        """`context.parameters ['X_output']` (space between `parameters` and `[`)
        must be caught."""
        code = "value=context.parameters ['Login_block_output']"
        error = self.reviewer._validate_parameter_references(
            code,
            parameter_keys=["account_number"],
            self_output_key="Login_block_output",
        )
        assert error is not None
        assert "Login_block_output" in error

    def test_space_before_get_self_output_rejected(self) -> None:
        """`context.parameters .get('X_output')` (space between `parameters` and
        `.`) must be caught."""
        code = "value=context.parameters .get('Login_block_output')"
        error = self.reviewer._validate_parameter_references(
            code,
            parameter_keys=["account_number"],
            self_output_key="Login_block_output",
        )
        assert error is not None
        assert "Login_block_output" in error

    def test_multiline_ref_inside_comment_block_not_flagged(self) -> None:
        """Multiline scan must still skip refs that live inside comment lines.
        Each comment line is replaced with a blank line before scanning, so a
        block of comment-prefixed lines containing a fake ref must not be
        flagged as a violation."""
        code = (
            "# context.parameters['fake_key']\n"
            "# context.parameters.get('another_fake_key')\n"
            "value=context.parameters['account_number']\n"
        )
        error = self.reviewer._validate_parameter_references(
            code,
            parameter_keys=["account_number"],
        )
        assert error is None


class TestConditionalCodeSelfOutputRejection:
    """The conditional-review path also persists code via
    ``create_script_version_from_review``, so it must run the same self-output
    parameter validation as ``_review_block_internal``."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    @pytest.mark.asyncio
    async def test_generate_conditional_code_calls_validator_with_self_output(self, monkeypatch: object) -> None:
        """End-to-end wiring: drive ``_generate_conditional_code`` with a mocked
        LLM that returns code containing the self-output pattern, and assert
        ``_validate_parameter_references`` was invoked with the block's
        ``self_output_key``. A future refactor that drops the validator call
        or threads the wrong ``block_label`` will make this test fail."""
        start_forge_stub_app()

        block_label = "Branch_block"

        # Conditional code containing the self-output crash pattern. The mocked
        # LLM returns this twice (max_attempts=2) so the validator's rejection
        # path runs both attempts.
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

        # Spy on the validator to confirm the wiring threads `self_output_key`.
        validator_calls: list[dict[str, object]] = []
        original_validator = self.reviewer._validate_parameter_references

        def spy_validator(code: str, parameter_keys: list[str], self_output_key: str | None = None) -> str | None:
            validator_calls.append(
                {"code_len": len(code), "param_keys": list(parameter_keys), "self_output_key": self_output_key}
            )
            return original_validator(code, parameter_keys, self_output_key=self_output_key)

        monkeypatch.setattr(self.reviewer, "_validate_parameter_references", spy_validator)

        # Build a minimal episode with a branch expression so
        # `_generate_conditional_code` proceeds past its early returns.
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

        # Validator must have been called at least once with the correct
        # self_output_key for this block. This is the wiring assertion.
        assert validator_calls, "validator was never invoked — wiring broken"
        assert all(call["self_output_key"] == f"{block_label}_output" for call in validator_calls), (
            f"validator received wrong self_output_key: {validator_calls}"
        )

        # The bad_code references the block's self-output, so the validator
        # rejects it on every attempt and the function returns None (no code
        # accepted for persistence). This confirms the rejection chain reaches
        # the persistence gate.
        assert result is None, "self-output ref should have been rejected before persistence"

    @pytest.mark.asyncio
    async def test_review_conditional_blocks_threads_parameter_keys(self, monkeypatch: object) -> None:
        """Outer-wiring test: ``review_conditional_blocks`` loads workflow
        context internally and threads ``all_parameter_keys`` into
        ``_generate_conditional_code``. A refactor that drops that load (or
        threads the wrong keys) must fail this test."""
        start_forge_stub_app()

        block_label = "Branch_block"

        async def fake_llm_handler(*args: object, **kwargs: object) -> str:
            return '```python\nasync def block_fn(page, context):\n    return {"next_block_label": None, "branch_index": 0}\n```'

        app.SCRIPT_REVIEWER_LLM_API_HANDLER = AsyncMock(side_effect=fake_llm_handler)

        # Spy on `_load_workflow_context` to confirm it's called from the
        # conditional path AND to control what it returns.
        load_calls: list[dict[str, str]] = []

        async def fake_load(organization_id: str, workflow_permanent_id: str) -> tuple:
            load_calls.append({"organization_id": organization_id, "workflow_permanent_id": workflow_permanent_id})
            return (
                {block_label: ""},  # goals
                ["account_number", f"{block_label}_output"],  # all_parameter_keys
                {},  # block_criteria
            )

        monkeypatch.setattr(self.reviewer, "_load_workflow_context", fake_load)

        # Spy on the validator to confirm `_generate_conditional_code` was
        # called WITH the keys threaded through `_load_workflow_context`.
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

        # Assertion 1: workflow context was loaded by review_conditional_blocks itself.
        assert load_calls == [{"organization_id": "o_test", "workflow_permanent_id": "wpid_test"}], (
            "review_conditional_blocks did not load workflow context — the wiring is broken"
        )
        # Assertion 2: the validator received the loaded keys, with the self-output filtered out.
        assert validator_calls, "validator was never invoked from the conditional path"
        for call in validator_calls:
            assert "account_number" in call["param_keys"], "upstream key should have been threaded through"
            assert f"{block_label}_output" not in call["param_keys"], "self-output should have been filtered"
            assert call["self_output_key"] == f"{block_label}_output", "self_output_key not threaded correctly"

    def test_validator_directly_rejects_self_output(self) -> None:
        """Direct unit-level assertion (kept alongside the integration test):
        validator returns an error message identifying the self-output key."""
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
