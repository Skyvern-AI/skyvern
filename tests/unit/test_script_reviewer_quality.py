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

    def test_no_ai_arg_without_selector_flagged(self) -> None:
        """Bare calls with no ai= and no selector= silently burn LLM tokens."""
        code = """
async def block_fn(page, context):
    await page.click(prompt='Click something')
"""
        error = self.reviewer._validate_missing_selectors(code)
        assert error is not None
        assert "page.click()" in error
        assert "no ai= argument" in error

    def test_proactive_without_selector_not_flagged(self) -> None:
        """ai='proactive' without selector is intentional — AI always generates the value."""
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


class TestExtractCachedBlocks:
    """Tests for the shared block extraction utilities."""

    def test_extract_all_blocks(self):
        from skyvern.services.workflow_script_service import extract_cached_blocks_from_source

        source = """import skyvern

@skyvern.cached(cache_key = 'login')
async def login(page, context):
    await page.goto('https://example.com')
    await page.complete()


@skyvern.cached(cache_key = 'block_1')
async def block_1(page, context):
    await page.click(selector='button')
    await page.complete()
"""
        result = extract_cached_blocks_from_source(source)
        assert set(result.keys()) == {"login", "block_1"}
        assert "page.goto" in result["login"]
        assert "page.click" in result["block_1"]
        # login block should NOT contain block_1 code
        assert "page.click" not in result["login"]

    def test_extract_single_block(self):
        from skyvern.services.workflow_script_service import extract_single_cached_block

        source = """import skyvern

@skyvern.cached(cache_key = 'login')
async def login(page, context):
    await page.goto('https://example.com')


@skyvern.cached(cache_key = 'block_1')
async def block_1(page, context):
    await page.click(selector='button')
"""
        result = extract_single_cached_block(source, "block_1")
        assert result is not None
        assert "page.click" in result
        assert "page.goto" not in result

    def test_extract_first_block(self):
        from skyvern.services.workflow_script_service import extract_single_cached_block

        source = """import skyvern

@skyvern.cached(cache_key = 'login')
async def login(page, context):
    await page.goto('https://example.com')


@skyvern.cached(cache_key = 'block_1')
async def block_1(page, context):
    await page.click(selector='button')
"""
        result = extract_single_cached_block(source, "login")
        assert result is not None
        assert "page.goto" in result
        assert "page.click" not in result

    def test_extract_single_block_not_found(self):
        from skyvern.services.workflow_script_service import extract_single_cached_block

        source = "@skyvern.cached(cache_key = 'login')\nasync def login(page, context): pass\n"
        result = extract_single_cached_block(source, "nonexistent")
        assert result is None

    def test_extract_empty_source(self):
        from skyvern.services.workflow_script_service import extract_cached_blocks_from_source

        assert extract_cached_blocks_from_source("") == {}
        assert extract_cached_blocks_from_source("import skyvern\n") == {}

    def test_extract_last_block_goes_to_eof(self):
        from skyvern.services.workflow_script_service import extract_cached_blocks_from_source

        source = "@skyvern.cached(cache_key = 'only_block')\nasync def only(page, ctx):\n    pass\n"
        result = extract_cached_blocks_from_source(source)
        assert "only_block" in result
        assert "pass" in result["only_block"]


class TestClassifyBlockStrategy:
    """Tests for _classify_block_strategy template selection."""

    def setup_method(self):
        from skyvern.services.script_reviewer import ScriptReviewer

        self.reviewer = ScriptReviewer()

    def test_extraction_block(self):
        """Block with page.extract() and no page.click() → extraction."""
        code = "async def block(page, ctx):\n    return await page.extract(prompt='...')\n"
        result = self.reviewer._classify_block_strategy(existing_code=code)
        assert result == "extraction"

    def test_extraction_with_click_is_sequential(self):
        """Block with both extract and click → sequential (not extraction)."""
        code = (
            "async def block(page, ctx):\n    await page.click('#btn')\n    return await page.extract(prompt='...')\n"
        )
        result = self.reviewer._classify_block_strategy(existing_code=code)
        assert result == "sequential"

    def test_existing_fill_form_stays_form_filling(self):
        """Block already using page.fill_form() stays form_filling."""
        code = "async def block(page, ctx):\n    await page.fill_form(ctx.parameters, prompt='...')\n"
        result = self.reviewer._classify_block_strategy(existing_code=code)
        assert result == "form_filling"

    def test_download_block_is_sequential(self):
        """Download block should be sequential, never form_filling."""
        code = "async def download(page, ctx):\n    await page.download_file(prompt='...')\n    await page.complete()\n"
        result = self.reviewer._classify_block_strategy(existing_code=code)
        assert result == "sequential"

    def test_navigation_block_with_form_fields_is_sequential(self):
        """Block on a page with form fields should still be sequential (not form_filling).

        Regression guard: this data (5 form fields + form keywords in goal + form
        action types) would have triggered the removed heuristic. Verifies it's gone.
        """
        code = "async def block(page, ctx):\n    await page.click('#btn')\n    await page.complete()\n"
        result = self.reviewer._classify_block_strategy(existing_code=code)
        assert result == "sequential"

    def test_default_is_sequential(self):
        """Empty code, no special signals → sequential."""
        result = self.reviewer._classify_block_strategy(existing_code="")
        assert result == "sequential"
