"""Tests for ScriptReviewer quality validators: proactive misuse, fragile selectors, hardcoded run data."""

from skyvern.services.script_reviewer import ScriptReviewer


class TestValidateProactiveMisuse:
    """Tests for _validate_proactive_misuse."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_fallback_is_fine(self) -> None:
        code = """
async def login(page, context):
    await page.fill(selector='input[name="email"]', value='test', ai='fallback', prompt='email')
    await page.click(selector='button', ai='fallback', prompt='submit')
"""
        assert self.reviewer._validate_proactive_misuse(code) is None

    def test_proactive_on_fill_flagged(self) -> None:
        code = """
async def login(page, context):
    await page.fill(selector='input[placeholder="Username"]', ai='proactive', prompt='username')
"""
        error = self.reviewer._validate_proactive_misuse(code)
        assert error is not None
        assert "page.fill()" in error
        assert "ai='fallback'" in error

    def test_proactive_on_click_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(selector='button:has-text("Submit")', ai='proactive', prompt='submit')
"""
        error = self.reviewer._validate_proactive_misuse(code)
        assert error is not None
        assert "page.click()" in error

    def test_proactive_on_select_option_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.select_option(selector='select[name="format"]', value='PDF', ai='proactive', prompt='format')
"""
        error = self.reviewer._validate_proactive_misuse(code)
        assert error is not None
        assert "page.select_option()" in error

    def test_proactive_on_type_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.type(selector='input[name="search"]', ai='proactive', prompt='search')
"""
        error = self.reviewer._validate_proactive_misuse(code)
        assert error is not None
        assert "page.type()" in error

    def test_proactive_on_extract_not_flagged(self) -> None:
        """ai='proactive' on extract is legitimate — extract doesn't have selectors."""
        code = """
async def block_fn(page, context):
    result = await page.extract(prompt='Get the invoice data', ai='proactive')
"""
        # extract is not in _INTERACTION_METHODS, so this should pass
        assert self.reviewer._validate_proactive_misuse(code) is None

    def test_comments_ignored(self) -> None:
        code = """
async def block_fn(page, context):
    # await page.fill(selector='input', ai='proactive', prompt='test')
    await page.fill(selector='input', value='x', ai='fallback', prompt='test')
"""
        assert self.reviewer._validate_proactive_misuse(code) is None

    def test_multiline_call_flagged(self) -> None:
        """ai='proactive' on a continuation line should still be caught."""
        code = """
async def login(page, context):
    await page.fill(
        selector='input[name="email"]',
        value='test',
        ai='proactive',
        prompt='email field',
    )
"""
        error = self.reviewer._validate_proactive_misuse(code)
        assert error is not None
        assert "page.fill()" in error

    def test_multiple_issues_reported(self) -> None:
        code = """
async def login(page, context):
    await page.fill(selector='#user', ai='proactive', prompt='user')
    await page.fill(selector='#pass', ai='proactive', prompt='pass')
    await page.click(selector='#submit', ai='proactive', prompt='submit')
"""
        error = self.reviewer._validate_proactive_misuse(code)
        assert error is not None
        # Should mention multiple occurrences
        assert "page.fill()" in error
        assert "page.click()" in error


class TestValidateFragileSelectors:
    """Tests for _validate_fragile_selectors."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_stable_selectors_pass(self) -> None:
        code = """
async def login(page, context):
    await page.fill(selector='input[name="email"]', value='test', ai='fallback', prompt='email')
    await page.click(selector='button:has-text("Sign In")', ai='fallback', prompt='sign in')
    await page.fill(selector='input[placeholder="Password"]', value='pass', ai='fallback', prompt='pass')
"""
        assert self.reviewer._validate_fragile_selectors(code) is None

    def test_dotnetnuke_id_flagged(self) -> None:
        code = """
async def login(page, context):
    await page.click(selector='#dnn_ctl00_aMyAccount', ai='fallback', prompt='account')
"""
        error = self.reviewer._validate_fragile_selectors(code)
        assert error is not None
        assert "dnn_" in error
        assert "DotNetNuke" in error

    def test_ember_id_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(selector='#ember-1234', ai='fallback', prompt='click')
"""
        error = self.reviewer._validate_fragile_selectors(code)
        assert error is not None
        assert "ember" in error.lower()

    def test_react_select_id_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(selector='#react-select-5-option-2', ai='fallback', prompt='select option')
"""
        error = self.reviewer._validate_fragile_selectors(code)
        assert error is not None
        assert "react-select" in error.lower()

    def test_css_in_js_class_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(selector='.css-1a2b3c', ai='fallback', prompt='click button')
"""
        error = self.reviewer._validate_fragile_selectors(code)
        assert error is not None
        assert "css-" in error.lower()

    def test_mui_class_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(selector='.MuiButton-root', ai='fallback', prompt='click button')
"""
        error = self.reviewer._validate_fragile_selectors(code)
        assert error is not None

    def test_extjs_id_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(selector='#ext-gen-456', ai='fallback', prompt='click')
"""
        error = self.reviewer._validate_fragile_selectors(code)
        assert error is not None
        assert "ext-gen" in error.lower()

    def test_comments_ignored(self) -> None:
        code = """
async def block_fn(page, context):
    # selector='#dnn_ctl00_aMyAccount' is fragile but this is a comment
    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
"""
        assert self.reviewer._validate_fragile_selectors(code) is None

    def test_multiline_call_flagged(self) -> None:
        """Fragile selector on a continuation line should still be caught."""
        code = """
async def block_fn(page, context):
    await page.click(
        selector='#dnn_ctl00_aMyAccount',
        ai='fallback',
        prompt='account link',
    )
"""
        error = self.reviewer._validate_fragile_selectors(code)
        assert error is not None
        assert "dnn_" in error

    def test_no_selector_passes(self) -> None:
        """Code without selector= kwargs should not trigger."""
        code = """
async def block_fn(page, context):
    await page.click(ai='fallback', prompt='click the submit button')
"""
        assert self.reviewer._validate_fragile_selectors(code) is None


class TestValidateHardcodedRunData:
    """Tests for _validate_hardcoded_run_data."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_clean_code_passes(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(selector='button:has-text("Download")', ai='fallback', prompt='download invoice')
"""
        assert self.reviewer._validate_hardcoded_run_data(code) is None

    def test_date_in_selector_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(selector='a:has-text("03/17/2026")', ai='fallback', prompt='click invoice')
"""
        error = self.reviewer._validate_hardcoded_run_data(code)
        assert error is not None
        assert "03/17/2026" in error
        assert "date" in error.lower()

    def test_iso_date_in_selector_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(selector='td:has-text("2026-03-17")', ai='fallback', prompt='click invoice')
"""
        error = self.reviewer._validate_hardcoded_run_data(code)
        assert error is not None
        assert "2026-03-17" in error

    def test_date_in_prompt_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.select_option(selector='select', value='PDF', ai='proactive', prompt='Select format for invoice dated 3/17/2026')
"""
        error = self.reviewer._validate_hardcoded_run_data(code)
        assert error is not None
        assert "3/17/2026" in error
        assert "prompt" in error.lower()

    def test_short_has_text_flagged(self) -> None:
        """a:has-text("6") is almost certainly hardcoded run data."""
        code = """
async def block_fn(page, context):
    await page.click(selector='a:has-text("6")', ai='fallback', prompt='click invoice')
"""
        error = self.reviewer._validate_hardcoded_run_data(code)
        assert error is not None
        assert ':has-text("6")' in error

    def test_short_has_text_number_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(selector='a:has-text("12")', ai='fallback', prompt='click row')
"""
        error = self.reviewer._validate_hardcoded_run_data(code)
        assert error is not None

    def test_stable_short_text_ok(self) -> None:
        """Common UI elements with short text like 'OK' or 'X' should pass."""
        code = """
async def block_fn(page, context):
    await page.click(selector='button:has-text("OK")', ai='fallback', prompt='confirm')
"""
        assert self.reviewer._validate_hardcoded_run_data(code) is None

    def test_long_has_text_ok(self) -> None:
        """:has-text with meaningful text (3+ chars) should pass."""
        code = """
async def block_fn(page, context):
    await page.click(selector='a:has-text("Download")', ai='fallback', prompt='download')
"""
        assert self.reviewer._validate_hardcoded_run_data(code) is None

    def test_comments_ignored(self) -> None:
        code = """
async def block_fn(page, context):
    # Don't use selector='a:has-text("6")' — it's hardcoded
    await page.click(selector='a:has-text("Download")', ai='fallback', prompt='download')
"""
        assert self.reviewer._validate_hardcoded_run_data(code) is None

    def test_multiline_date_in_selector_flagged(self) -> None:
        """Hardcoded date on a continuation line should still be caught."""
        code = """
async def block_fn(page, context):
    await page.click(
        selector='a:has-text("03/17/2026")',
        ai='fallback',
        prompt='click invoice',
    )
"""
        error = self.reviewer._validate_hardcoded_run_data(code)
        assert error is not None
        assert "03/17/2026" in error

    def test_multiline_date_in_prompt_flagged(self) -> None:
        """Hardcoded date in prompt on a continuation line should still be caught."""
        code = """
async def block_fn(page, context):
    await page.select_option(
        selector='select',
        value='PDF',
        ai='proactive',
        prompt='Select format for invoice dated 3/17/2026',
    )
"""
        error = self.reviewer._validate_hardcoded_run_data(code)
        assert error is not None
        assert "3/17/2026" in error

    def test_parameterized_date_ok(self) -> None:
        """Dates in context.parameters references should not trigger."""
        code = """
async def block_fn(page, context):
    start_date = context.parameters['download_start_date']
    await page.click(selector=f'a:has-text("{start_date}")', ai='fallback', prompt='click date')
"""
        # f-string selectors won't match the date regex in the selector value
        assert self.reviewer._validate_hardcoded_run_data(code) is None

    def test_email_in_text_patterns_flagged(self) -> None:
        """Email addresses in text_patterns are PII that should not be in cached scripts."""
        code = """
async def block_fn(page, context):
    state = await page.classify(
        options={"login": "login page", "dashboard": "dashboard"},
        text_patterns={
            "login": "Welcome to Portal, Username, Password, Sign in",
            "dashboard": "Logout, cmt.acme@example.com, John Smith, Billing",
        },
    )
"""
        error = self.reviewer._validate_hardcoded_run_data(code)
        assert error is not None
        assert "email" in error.lower()

    def test_text_patterns_without_email_ok(self) -> None:
        """Generic text_patterns without PII should pass."""
        code = """
async def block_fn(page, context):
    state = await page.classify(
        options={"login": "login page", "dashboard": "dashboard"},
        text_patterns={
            "login": "Welcome, Username, Password, Sign in",
            "dashboard": "Logout, Billing & Payments, Service Management",
        },
    )
"""
        assert self.reviewer._validate_hardcoded_run_data(code) is None


class TestValidateMissingSelectors:
    """Tests for _validate_missing_selectors."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_fallback_with_selector_is_fine(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
"""
        assert self.reviewer._validate_missing_selectors(code) is None

    def test_fallback_without_selector_flagged(self) -> None:
        """ai='fallback' with no selector= silently uses AI as primary path."""
        code = """
async def block_fn(page, context):
    await page.click(ai='fallback', prompt='Click Billing & Payments')
"""
        error = self.reviewer._validate_missing_selectors(code)
        assert error is not None
        assert "page.click()" in error
        assert "Missing selector" in error

    def test_fill_without_selector_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.fill(ai='fallback', prompt='Enter username', value='test')
"""
        error = self.reviewer._validate_missing_selectors(code)
        assert error is not None
        assert "page.fill()" in error

    def test_no_ai_arg_not_flagged(self) -> None:
        """Calls without ai='fallback' are not our concern here."""
        code = """
async def block_fn(page, context):
    await page.click(prompt='Click something')
"""
        assert self.reviewer._validate_missing_selectors(code) is None

    def test_proactive_without_selector_not_flagged(self) -> None:
        """ai='proactive' without selector is caught by _validate_proactive_misuse, not this validator."""
        code = """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='Click something')
"""
        assert self.reviewer._validate_missing_selectors(code) is None

    def test_multiline_call_flagged(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(
        ai='fallback',
        prompt='Click Billing & Payments',
    )
"""
        error = self.reviewer._validate_missing_selectors(code)
        assert error is not None
        assert "page.click()" in error

    def test_multiline_with_selector_ok(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(
        selector='a:has-text("Billing")',
        ai='fallback',
        prompt='Click billing link',
    )
"""
        assert self.reviewer._validate_missing_selectors(code) is None

    def test_comments_ignored(self) -> None:
        code = """
async def block_fn(page, context):
    # await page.click(ai='fallback', prompt='old code')
    await page.click(selector='button', ai='fallback', prompt='submit')
"""
        assert self.reviewer._validate_missing_selectors(code) is None

    def test_non_interaction_methods_ignored(self) -> None:
        """Methods like page.wait, page.complete are not interaction methods."""
        code = """
async def block_fn(page, context):
    await page.wait(ai='fallback', prompt='wait for page')
"""
        assert self.reviewer._validate_missing_selectors(code) is None
