"""How an expression handed to evaluate is classified before it reaches Chrome.

``Runtime.callFunctionOn`` needs a function *declaration*. Callers pass three different shapes -- a
real function, a bare expression, and an immediately-invoked function expression -- and only the
first can be forwarded as-is. Getting this wrong is not a graceful failure: Chrome rejects the whole
payload with a syntax error that names a token rather than the mistake.

The IIFE case is why this file exists. Skyvern injects its DOM utilities as ``(() => { ... })()``,
and a heuristic that merely looks for an arrow anywhere in the string classifies that as a function
and sends it straight to Chrome, which refuses it.
"""

from __future__ import annotations

import pytest

from skyvern.webeye.skycdp.facade.evaluation import looks_like_function, wrap_as_function


@pytest.mark.parametrize(
    "source",
    [
        "() => 1",
        "()=>1",
        "(element) => element.blur()",
        "(a, b) => a + b",
        "element => element.id",
        "async () => await fetch('/x')",
        "async (element) => element.id",
        "function() { return 1; }",
        "function (element) { return element.id; }",
        "async function() { return 1; }",
        """function() {
            return this.textContent;
        }""",
    ],
)
def test_real_functions_are_forwarded_unchanged(source: str) -> None:
    assert looks_like_function(source) is True
    assert wrap_as_function(source) == source


@pytest.mark.parametrize(
    "source",
    [
        # The shape Skyvern's injected DOM utilities take.
        "(() => { let x = 1; return x; })()",
        "(function () { return 1; })()",
        "(async () => { return 1; })()",
        # Bare expressions.
        "1 + 1",
        "document.title",
        "window.__marker",
        "[1, 2, 3].map(n => n * 2)",
        "({a: 1})",
    ],
)
def test_everything_else_is_wrapped_so_chrome_receives_a_declaration(source: str) -> None:
    assert looks_like_function(source) is False
    wrapped = wrap_as_function(source)
    assert source in wrapped
    assert wrapped.startswith("() => (")


def test_an_iife_is_not_mistaken_for_an_arrow_function() -> None:
    """The specific regression: an arrow inside an IIFE is not the IIFE's own signature."""
    iife = "(() => { return document.title; })()"
    assert looks_like_function(iife) is False, "an IIFE is a call expression, not a function"


def test_a_callable_expression_returning_a_function_is_still_wrapped() -> None:
    assert looks_like_function("makeHandler()") is False


def test_a_script_that_opens_with_a_comment_survives_wrapping() -> None:
    """The regression that broke the real scraper.

    An injected script file starts with a licence or explanatory comment. Wrapped on one line, the
    `//` swallows the opening parenthesis and Chrome rejects the whole payload.
    """
    source = "// we only use chromium browser for now\nlet x = 1;\nreturn x;"
    wrapped = wrap_as_function(source)
    first_line = wrapped.splitlines()[0]
    assert "//" not in first_line, f"the wrapper's opening line is commented out: {first_line!r}"


def test_a_script_that_ends_with_a_comment_survives_wrapping() -> None:
    source = "let x = 1;\n// trailing note"
    wrapped = wrap_as_function(source)
    assert wrapped.splitlines()[-1].strip() == ")", "the closing parenthesis was swallowed by a comment"
