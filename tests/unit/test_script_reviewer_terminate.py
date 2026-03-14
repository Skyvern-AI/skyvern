"""Tests for ScriptReviewer bare-terminate validation."""

from skyvern.services.script_reviewer import ScriptReviewer


class TestValidateBareTerminate:
    """Tests for _validate_bare_terminate."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_terminate_inside_if_passes(self) -> None:
        """page.terminate() inside an if block should pass validation."""
        code = """
async def block_fn(page, context):
    result = await page.extract(
        prompt='Is there an invoice available?',
        schema={'type': 'object', 'properties': {'available': {'type': 'boolean'}}}
    )
    if not result.get('available', True):
        await page.terminate(errors=["No invoice available"])
    else:
        await page.element_fallback(navigation_goal="Download the invoice")
"""
        assert self.reviewer._validate_bare_terminate(code) is None

    def test_terminate_inside_elif_passes(self) -> None:
        """page.terminate() inside an elif block should pass validation."""
        code = """
async def block_fn(page, context):
    state = await page.classify(
        options={"has_invoice": "invoice is available", "no_invoice": "no invoice found"},
        text_patterns={"has_invoice": "Download Invoice", "no_invoice": "No invoices"},
    )
    if state == "has_invoice":
        await page.click(selector='button:has-text("Download")', ai='fallback', prompt='Click download')
    elif state == "no_invoice":
        await page.terminate(errors=["No invoice available for this period"])
    else:
        await page.element_fallback(navigation_goal="Handle invoice page")
"""
        assert self.reviewer._validate_bare_terminate(code) is None

    def test_bare_terminate_at_function_body_rejected(self) -> None:
        """page.terminate() at function body level (unconditional) should be rejected."""
        code = """
async def block_fn(page, context):
    await page.terminate(errors=["Something went wrong"])
"""
        error = self.reviewer._validate_bare_terminate(code)
        assert error is not None
        assert "unconditional terminate rejected" in error
        assert "if/elif" in error

    def test_bare_terminate_after_other_calls_rejected(self) -> None:
        """page.terminate() at function body level after other calls should be rejected."""
        code = """
async def block_fn(page, context):
    await page.click(selector='button', ai='fallback', prompt='Click button')
    await page.terminate(errors=["Done but wrong"])
"""
        error = self.reviewer._validate_bare_terminate(code)
        assert error is not None
        assert "unconditional terminate rejected" in error

    def test_no_terminate_passes(self) -> None:
        """Code without any terminate call should pass."""
        code = """
async def block_fn(page, context):
    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='Click submit')
    await page.complete()
"""
        assert self.reviewer._validate_bare_terminate(code) is None

    def test_terminate_in_nested_if_passes(self) -> None:
        """page.terminate() inside a nested if should pass."""
        code = """
async def block_fn(page, context):
    result = await page.extract(
        prompt='What is the account status?',
        schema={'type': 'object', 'properties': {'status': {'type': 'string'}}}
    )
    if result.get('status') == 'active':
        data = await page.extract(
            prompt='Is there a balance?',
            schema={'type': 'object', 'properties': {'has_balance': {'type': 'boolean'}}}
        )
        if not data.get('has_balance', True):
            await page.terminate(errors=["No balance to process"])
        else:
            await page.click(selector='button:has-text("Pay")', ai='fallback', prompt='Click pay')
    else:
        await page.element_fallback(navigation_goal="Handle account status")
"""
        assert self.reviewer._validate_bare_terminate(code) is None

    def test_terminate_in_for_loop_without_conditional_rejected(self) -> None:
        """page.terminate() inside a for loop but not in an if should be rejected."""
        code = """
async def block_fn(page, context):
    for item in items:
        await page.terminate(errors=["Bad item"])
"""
        error = self.reviewer._validate_bare_terminate(code)
        assert error is not None
        assert "unconditional terminate rejected" in error

    def test_terminate_in_for_loop_inside_if_passes(self) -> None:
        """page.terminate() inside a for loop AND inside an if should pass."""
        code = """
async def block_fn(page, context):
    for item in items:
        if item.get('invalid'):
            await page.terminate(errors=["Invalid item found"])
        else:
            await page.click(selector='button', ai='fallback', prompt='Process item')
"""
        assert self.reviewer._validate_bare_terminate(code) is None

    def test_terminate_in_else_branch_passes(self) -> None:
        """page.terminate() inside an else branch still passes (it's inside a conditional).

        This test uses an extract-pattern example. The validator would also pass
        terminate in a classify-else branch -- classify-vs-extract else-branch
        semantics are enforced by the LLM prompt, not the structural validator.
        """
        code = """
async def block_fn(page, context):
    result = await page.extract(
        prompt='Check status',
        schema={'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}
    )
    if result.get('ok'):
        await page.complete()
    else:
        await page.terminate(errors=["Status check failed"])
"""
        assert self.reviewer._validate_bare_terminate(code) is None

    def test_bare_non_awaited_terminate_rejected(self) -> None:
        """page.terminate() without await at function body level should still be rejected."""
        code = """
async def block_fn(page, context):
    page.terminate(errors=["Something went wrong"])
"""
        error = self.reviewer._validate_bare_terminate(code)
        assert error is not None
        assert "unconditional terminate rejected" in error

    def test_non_awaited_terminate_inside_if_passes(self) -> None:
        """page.terminate() without await but inside an if block should pass."""
        code = """
async def block_fn(page, context):
    if some_condition:
        page.terminate(errors=["Condition met"])
    else:
        await page.element_fallback(navigation_goal="Handle it")
"""
        assert self.reviewer._validate_bare_terminate(code) is None

    def test_terminate_inside_match_case_passes(self) -> None:
        """page.terminate() inside a match/case body should pass (case is conditional)."""
        code = """
async def block_fn(page, context):
    result = await page.extract(
        prompt='What is the status?',
        schema={'type': 'object', 'properties': {'status': {'type': 'string'}}}
    )
    match result.get('status'):
        case 'active':
            await page.click(selector='button', ai='fallback', prompt='Click button')
        case 'cancelled':
            await page.terminate(errors=["Account cancelled — cannot proceed"])
        case _:
            await page.element_fallback(navigation_goal="Handle unknown status")
"""
        assert self.reviewer._validate_bare_terminate(code) is None

    def test_terminate_in_bare_try_rejected(self) -> None:
        """page.terminate() inside a try block but not in an if should be rejected."""
        code = """
async def block_fn(page, context):
    try:
        await page.terminate(errors=["Something failed"])
    except Exception:
        await page.element_fallback(navigation_goal="Handle error")
"""
        error = self.reviewer._validate_bare_terminate(code)
        assert error is not None
        assert "unconditional terminate rejected" in error

    def test_terminate_in_try_inside_if_passes(self) -> None:
        """page.terminate() inside try > if should pass."""
        code = """
async def block_fn(page, context):
    try:
        result = await page.extract(
            prompt='Check status',
            schema={'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}
        )
        if not result.get('ok'):
            await page.terminate(errors=["Status check failed"])
        else:
            await page.complete()
    except Exception:
        await page.element_fallback(navigation_goal="Handle error")
"""
        assert self.reviewer._validate_bare_terminate(code) is None

    def test_terminate_in_bare_except_rejected(self) -> None:
        """page.terminate() in an except handler (not inside if) should be rejected."""
        code = """
async def block_fn(page, context):
    try:
        await page.click(selector='button', ai='fallback', prompt='Click')
    except Exception:
        await page.terminate(errors=["Exception occurred"])
"""
        error = self.reviewer._validate_bare_terminate(code)
        assert error is not None
        assert "unconditional terminate rejected" in error

    def test_terminate_in_bare_with_rejected(self) -> None:
        """page.terminate() inside a with block but not in an if should be rejected."""
        code = """
async def block_fn(page, context):
    async with some_context_manager() as ctx:
        await page.terminate(errors=["Bad state"])
"""
        error = self.reviewer._validate_bare_terminate(code)
        assert error is not None
        assert "unconditional terminate rejected" in error

    def test_terminate_in_with_inside_if_passes(self) -> None:
        """page.terminate() inside with > if should pass."""
        code = """
async def block_fn(page, context):
    async with some_context_manager() as ctx:
        if ctx.failed:
            await page.terminate(errors=["Context failed"])
        else:
            await page.complete()
"""
        assert self.reviewer._validate_bare_terminate(code) is None

    def test_syntax_error_code_returns_none(self) -> None:
        """Code that doesn't parse should return None (syntax errors handled elsewhere)."""
        code = "this is not valid python at all {"
        assert self.reviewer._validate_bare_terminate(code) is None
