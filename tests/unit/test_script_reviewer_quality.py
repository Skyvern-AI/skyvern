"""Tests for ScriptReviewer quality validators: proactive misuse, fragile selectors, hardcoded run data."""

from pathlib import Path

import pytest

from skyvern.core.script_generations.script_validators import validate_missing_selectors
from skyvern.services.script_reviewer import ScriptReviewer

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "skyvern" / "forge" / "prompts" / "skyvern" / "script-reviewer.j2"


class TestSelectorReplacementGuidance:
    """Pin Rule 8b's multi-row form guidance against accidental deletion."""

    def test_rule_8b_covers_multi_row_full_block_case(self) -> None:
        text = _PROMPT_PATH.read_text(encoding="utf-8")
        assert "multi-row" in text.lower()
        assert "full_block" in text
        assert "REMOVE" in text
        assert "label:has-text" in text


class TestValidateProactiveMisuse:
    """Tests for _validate_proactive_misuse."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    @pytest.mark.parametrize(
        ("cases",),
        [
            pytest.param(
                [
                    (
                        "fallback_is_fine",
                        """
async def login(page, context):
    await page.fill(selector='input[name="email"]', value='test', ai='fallback', prompt='email')
    await page.click(selector='button', ai='fallback', prompt='submit')
""",
                        None,
                    ),
                    (
                        "proactive_on_extract_not_flagged",
                        """
async def block_fn(page, context):
    result = await page.extract(prompt='Get the invoice data', ai='proactive')
""",
                        None,
                    ),
                    (
                        "proactive_without_selector_not_flagged",
                        """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='Click the next-step button')
    await page.fill(value='hello', ai='proactive', prompt='Fill the field')
""",
                        None,
                    ),
                    (
                        "selector_inside_prompt_does_not_falsely_flag",
                        """
async def block_fn(page, context):
    await page.click(ai='proactive', prompt='No selector= available for this widget')
""",
                        None,
                    ),
                    (
                        "comments_ignored",
                        """
async def block_fn(page, context):
    # await page.fill(selector='input', ai='proactive', prompt='test')
    await page.fill(selector='input', value='x', ai='fallback', prompt='test')
""",
                        None,
                    ),
                ],
                id="allowed-proactive-shapes",
            ),
            pytest.param(
                [
                    (
                        "proactive_on_fill_flagged",
                        """
async def login(page, context):
    await page.fill(selector='input[placeholder="Username"]', ai='proactive', prompt='username')
""",
                        ("page.fill()", "ai='fallback'"),
                    ),
                    (
                        "proactive_on_click_flagged",
                        """
async def block_fn(page, context):
    await page.click(selector='button:has-text("Submit")', ai='proactive', prompt='submit')
""",
                        ("page.click()",),
                    ),
                    (
                        "proactive_on_select_option_flagged",
                        """
async def block_fn(page, context):
    await page.select_option(selector='select[name="format"]', value='PDF', ai='proactive', prompt='format')
""",
                        ("page.select_option()",),
                    ),
                    (
                        "proactive_on_type_flagged",
                        """
async def block_fn(page, context):
    await page.type(selector='input[name="search"]', ai='proactive', prompt='search')
""",
                        ("page.type()",),
                    ),
                ],
                id="selector-plus-proactive-is-flagged",
            ),
            pytest.param(
                [
                    (
                        "multiline_call_flagged",
                        """
async def login(page, context):
    await page.fill(
        selector='input[name="email"]',
        value='test',
        ai='proactive',
        prompt='email field',
    )
""",
                        ("page.fill()",),
                    )
                ],
                id="multiline-call",
            ),
            pytest.param(
                [
                    (
                        "multiple_issues_reported",
                        """
async def login(page, context):
    await page.fill(selector='#user', ai='proactive', prompt='user')
    await page.fill(selector='#pass', ai='proactive', prompt='pass')
    await page.click(selector='#submit', ai='proactive', prompt='submit')
""",
                        ("page.fill()", "page.click()"),
                    )
                ],
                id="multiple-issues",
            ),
        ],
    )
    def test_proactive_misuse_cases(self, cases: list[tuple[str, str, tuple[str, ...] | None]]) -> None:
        for case_id, code, expected_substrings in cases:
            error = self.reviewer._validate_proactive_misuse(code)
            if expected_substrings is None:
                assert error is None, case_id
                continue
            assert error is not None, case_id
            for expected in expected_substrings:
                assert expected in error, case_id


class TestValidateFragileSelectors:
    """Tests for _validate_fragile_selectors."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    @pytest.mark.parametrize(
        ("cases",),
        [
            pytest.param(
                [
                    (
                        "stable_selectors_pass",
                        """
async def login(page, context):
    await page.fill(selector='input[name="email"]', value='test', ai='fallback', prompt='email')
    await page.click(selector='button:has-text("Sign In")', ai='fallback', prompt='sign in')
    await page.fill(selector='input[placeholder="Password"]', value='pass', ai='fallback', prompt='pass')
""",
                        None,
                    ),
                    (
                        "comments_ignored",
                        """
async def block_fn(page, context):
    # selector='#dnn_ctl00_aMyAccount' is fragile but this is a comment
    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
""",
                        None,
                    ),
                    (
                        "no_selector_passes",
                        """
async def block_fn(page, context):
    await page.click(ai='fallback', prompt='click the submit button')
""",
                        None,
                    ),
                ],
                id="stable-and-ignored",
            ),
            pytest.param(
                [
                    (
                        "dotnetnuke_id_flagged",
                        """
async def login(page, context):
    await page.click(selector='#dnn_ctl00_aMyAccount', ai='fallback', prompt='account')
""",
                        ("dnn_", "DotNetNuke"),
                    ),
                    (
                        "ember_id_flagged",
                        """
async def block_fn(page, context):
    await page.click(selector='#ember-1234', ai='fallback', prompt='click')
""",
                        ("ember",),
                    ),
                    (
                        "react_select_id_flagged",
                        """
async def block_fn(page, context):
    await page.click(selector='#react-select-5-option-2', ai='fallback', prompt='select option')
""",
                        ("react-select",),
                    ),
                    (
                        "css_in_js_class_flagged",
                        """
async def block_fn(page, context):
    await page.click(selector='.css-1a2b3c', ai='fallback', prompt='click button')
""",
                        ("css-",),
                    ),
                    (
                        "mui_class_flagged",
                        """
async def block_fn(page, context):
    await page.click(selector='.MuiButton-root', ai='fallback', prompt='click button')
""",
                        (),
                    ),
                    (
                        "extjs_id_flagged",
                        """
async def block_fn(page, context):
    await page.click(selector='#ext-gen-456', ai='fallback', prompt='click')
""",
                        ("ext-gen",),
                    ),
                ],
                id="fragile-framework-patterns",
            ),
            pytest.param(
                [
                    (
                        "multiline_call_flagged",
                        """
async def block_fn(page, context):
    await page.click(
        selector='#dnn_ctl00_aMyAccount',
        ai='fallback',
        prompt='account link',
    )
""",
                        ("dnn_",),
                    )
                ],
                id="multiline-selector",
            ),
        ],
    )
    def test_fragile_selector_cases(self, cases: list[tuple[str, str, tuple[str, ...] | None]]) -> None:
        for case_id, code, expected_substrings in cases:
            error = self.reviewer._validate_fragile_selectors(code)
            if expected_substrings is None:
                assert error is None, case_id
                continue
            assert error is not None, case_id
            for expected in expected_substrings:
                assert expected.lower() in error.lower(), case_id


class TestValidateHardcodedRunData:
    """Tests for _validate_hardcoded_run_data."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    @pytest.mark.parametrize(
        ("cases",),
        [
            pytest.param(
                [
                    (
                        "clean_code_passes",
                        """
async def block_fn(page, context):
    await page.click(selector='button:has-text("Download")', ai='fallback', prompt='download invoice')
""",
                        None,
                    ),
                    (
                        "stable_short_text_ok",
                        """
async def block_fn(page, context):
    await page.click(selector='button:has-text("OK")', ai='fallback', prompt='confirm')
""",
                        None,
                    ),
                    (
                        "long_has_text_ok",
                        """
async def block_fn(page, context):
    await page.click(selector='a:has-text("Download")', ai='fallback', prompt='download')
""",
                        None,
                    ),
                    (
                        "comments_ignored",
                        """
async def block_fn(page, context):
    # Don't use selector='a:has-text("6")' — it's hardcoded
    await page.click(selector='a:has-text("Download")', ai='fallback', prompt='download')
""",
                        None,
                    ),
                    (
                        "parameterized_date_ok",
                        """
async def block_fn(page, context):
    start_date = context.parameters['download_start_date']
    await page.click(selector=f'a:has-text("{start_date}")', ai='fallback', prompt='click date')
""",
                        None,
                    ),
                    (
                        "text_patterns_without_email_ok",
                        """
async def block_fn(page, context):
    state = await page.classify(
        options={"login": "login page", "dashboard": "dashboard"},
        text_patterns={
            "login": "Welcome, Username, Password, Sign in",
            "dashboard": "Logout, Billing & Payments, Service Management",
        },
    )
""",
                        None,
                    ),
                ],
                id="allowed-run-data-shapes",
            ),
            pytest.param(
                [
                    (
                        "date_in_selector_flagged",
                        """
async def block_fn(page, context):
    await page.click(selector='a:has-text("03/17/2026")', ai='fallback', prompt='click invoice')
""",
                        ("03/17/2026", "date"),
                    ),
                    (
                        "iso_date_in_selector_flagged",
                        """
async def block_fn(page, context):
    await page.click(selector='td:has-text("2026-03-17")', ai='fallback', prompt='click invoice')
""",
                        ("2026-03-17",),
                    ),
                    (
                        "date_in_prompt_flagged",
                        """
async def block_fn(page, context):
    await page.select_option(selector='select', value='PDF', ai='proactive', prompt='Select format for invoice dated 3/17/2026')
""",
                        ("3/17/2026", "prompt"),
                    ),
                    (
                        "multiline_date_in_selector_flagged",
                        """
async def block_fn(page, context):
    await page.click(
        selector='a:has-text("03/17/2026")',
        ai='fallback',
        prompt='click invoice',
    )
""",
                        ("03/17/2026",),
                    ),
                    (
                        "multiline_date_in_prompt_flagged",
                        """
async def block_fn(page, context):
    await page.select_option(
        selector='select',
        value='PDF',
        ai='proactive',
        prompt='Select format for invoice dated 3/17/2026',
    )
""",
                        ("3/17/2026",),
                    ),
                ],
                id="date-literals-flagged",
            ),
            pytest.param(
                [
                    (
                        "short_has_text_flagged",
                        """
async def block_fn(page, context):
    await page.click(selector='a:has-text("6")', ai='fallback', prompt='click invoice')
""",
                        (':has-text("6")',),
                    ),
                    (
                        "short_has_text_number_flagged",
                        """
async def block_fn(page, context):
    await page.click(selector='a:has-text("12")', ai='fallback', prompt='click row')
""",
                        (),
                    ),
                ],
                id="short-has-text-flagged",
            ),
            pytest.param(
                [
                    (
                        "email_in_text_patterns_flagged",
                        """
async def block_fn(page, context):
    state = await page.classify(
        options={"login": "login page", "dashboard": "dashboard"},
        text_patterns={
            "login": "Welcome to Portal, Username, Password, Sign in",
            "dashboard": "Logout, cmt.acme@example.com, John Smith, Billing",
        },
    )
""",
                        ("email",),
                    )
                ],
                id="text-patterns-pii",
            ),
        ],
    )
    def test_hardcoded_run_data_cases(self, cases: list[tuple[str, str, tuple[str, ...] | None]]) -> None:
        for case_id, code, expected_substrings in cases:
            error = self.reviewer._validate_hardcoded_run_data(code)
            if expected_substrings is None:
                assert error is None, case_id
                continue
            assert error is not None, case_id
            for expected in expected_substrings:
                assert expected.lower() in error.lower(), case_id


class TestValidateMissingSelectors:
    """Tests for _validate_missing_selectors."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_delegates_to_shared_validator(self) -> None:
        code = """
async def block_fn(page, context):
    await page.click(
        ai='fallback',
        prompt='Click Billing & Payments',
    )
"""
        result = self.reviewer._validate_missing_selectors(code)
        assert result is not None
        assert result == validate_missing_selectors(code)


class TestExtractCachedBlocks:
    """Tests for the shared block extraction utilities."""

    def test_extract_cached_block_cases(self) -> None:
        from skyvern.services.workflow_script_service import (
            extract_cached_blocks_from_source,
            extract_single_cached_block,
        )

        multi_block_source = """import skyvern

@skyvern.cached(cache_key = 'login')
async def login(page, context):
    await page.goto('https://example.com')
    await page.complete()


@skyvern.cached(cache_key = 'block_1')
async def block_1(page, context):
    await page.click(selector='button')
    await page.complete()
"""
        result = extract_cached_blocks_from_source(multi_block_source)
        assert set(result.keys()) == {"login", "block_1"}
        assert "page.goto" in result["login"]  # nosemgrep: incomplete-url-substring-sanitization
        assert "page.click" in result["block_1"]  # nosemgrep: incomplete-url-substring-sanitization
        assert "page.click" not in result["login"]

        single_block = extract_single_cached_block(multi_block_source, "block_1")
        assert single_block is not None
        assert "page.click" in single_block  # nosemgrep: incomplete-url-substring-sanitization
        assert "page.goto" not in single_block

        first_block = extract_single_cached_block(multi_block_source, "login")
        assert first_block is not None
        assert "page.goto" in first_block  # nosemgrep: incomplete-url-substring-sanitization
        assert "page.click" not in first_block

        assert (
            extract_single_cached_block(
                "@skyvern.cached(cache_key = 'login')\nasync def login(page, context): pass\n",
                "nonexistent",
            )
            is None
        )
        assert extract_cached_blocks_from_source("") == {}
        assert extract_cached_blocks_from_source("import skyvern\n") == {}

        last_block_source = "@skyvern.cached(cache_key = 'only_block')\nasync def only(page, ctx):\n    pass\n"
        last_block = extract_cached_blocks_from_source(last_block_source)
        assert "only_block" in last_block
        assert "pass" in last_block["only_block"]


class TestClassifyBlockStrategy:
    """Tests for _classify_block_strategy template selection."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            pytest.param(
                "async def block(page, ctx):\n    return await page.extract(prompt='...')\n",
                "extraction",
                id="extract-only",
            ),
            pytest.param(
                "async def block(page, ctx):\n    await page.click('#btn')\n    return await page.extract(prompt='...')\n",
                "sequential",
                id="extract-with-click",
            ),
            pytest.param(
                "async def block(page, ctx):\n    await page.fill_form(ctx.parameters, prompt='...')\n",
                "form_filling",
                id="existing-fill-form",
            ),
            pytest.param(
                "async def download(page, ctx):\n    await page.download_file(prompt='...')\n    await page.complete()\n",
                "sequential",
                id="download",
            ),
            pytest.param(
                "async def block(page, ctx):\n    await page.click('#btn')\n    await page.complete()\n",
                "sequential",
                id="navigation-with-form-fields",
            ),
            pytest.param("", "sequential", id="default"),
        ],
    )
    def test_classify_block_strategy_cases(self, code: str, expected: str) -> None:
        assert self.reviewer._classify_block_strategy(existing_code=code) == expected
