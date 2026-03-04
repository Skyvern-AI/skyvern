"""Tests for ScriptReviewer validation methods."""

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
        assert "context.parameters" in error

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
