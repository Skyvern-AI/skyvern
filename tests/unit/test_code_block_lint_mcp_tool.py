"""Tests for the MCP code-block lint tool."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from skyvern.cli.mcp_tools.code_block import skyvern_code_block_lint


@pytest.fixture(autouse=True)
def _stub_mypy_for_non_mypy_lint_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_mypy = ModuleType("mypy")
    fake_mypy.__dict__["api"] = SimpleNamespace(run=lambda _args: ("", "", 0))
    monkeypatch.setitem(sys.modules, "mypy", fake_mypy)


def _has_security_error(result: dict, *, reason_code: str, surface: str | None = None) -> bool:
    return any(
        error["reason_code"] == reason_code and (surface is None or error["surface"] == surface)
        for error in result["data"]["security_errors"]
    )


def _has_diagnostic(result: dict, *, section: str, code: str) -> bool:
    return any(diagnostic["code"] == code for diagnostic in result["data"][section])


@pytest.mark.asyncio
async def test_clean_code_block_lints_ok() -> None:
    code = 'await page.goto("https://example.com", wait_until="domcontentloaded")\nreturn {"ok": True}'

    result = await skyvern_code_block_lint(code=code)

    assert result["ok"] is True
    assert result["data"]["lint_ok"] is True
    assert result["data"]["code_safety_errors"] == []
    assert result["data"]["security_errors"] == []
    assert result["data"]["preflight_diagnostics"] == []
    assert result["data"]["sandbox_diagnostics"] == []
    assert result["data"]["author_time_diagnostics"] == []


@pytest.mark.asyncio
async def test_dunder_access_is_blocked_by_code_safety_gate() -> None:
    result = await skyvern_code_block_lint(code="x = page.__class__\nreturn {}")

    assert result["ok"] is False
    assert result["data"]["lint_ok"] is False
    assert result["data"]["code_safety_errors"]
    assert "private methods or attributes" in result["data"]["code_safety_errors"][0]["message"]


@pytest.mark.asyncio
async def test_import_is_blocked_by_code_safety_gate() -> None:
    result = await skyvern_code_block_lint(code="import os\nreturn {}")

    assert result["ok"] is False
    assert result["data"]["lint_ok"] is False
    assert result["data"]["code_safety_errors"]
    assert "Not allowed to import" in result["data"]["code_safety_errors"][0]["message"]


@pytest.mark.asyncio
async def test_page_evaluate_is_blocked_by_security_denylist() -> None:
    result = await skyvern_code_block_lint(code='await page.evaluate("1+1")')

    assert result["ok"] is False
    assert result["data"]["lint_ok"] is False
    assert _has_security_error(result, reason_code="AUTHOR_PAGE_EVALUATE", surface="page.evaluate")


@pytest.mark.asyncio
async def test_page_request_is_blocked_by_security_denylist() -> None:
    result = await skyvern_code_block_lint(code='await page.request.get("https://example.com")')

    assert result["ok"] is False
    assert result["data"]["lint_ok"] is False
    assert _has_security_error(result, reason_code="AUTHOR_PAGE_REQUEST")


@pytest.mark.asyncio
async def test_undefined_name_is_caught_by_sandbox_analyzer() -> None:
    result = await skyvern_code_block_lint(code="x = undefined_thing + 1\nreturn {}")

    assert result["ok"] is False
    assert result["data"]["lint_ok"] is False
    assert _has_diagnostic(result, section="sandbox_diagnostics", code="SANDBOX_UNRESOLVED_NAME")


@pytest.mark.asyncio
async def test_parameter_key_is_treated_as_defined_by_sandbox_analyzer() -> None:
    result = await skyvern_code_block_lint(code="x = query + 1\nreturn {}", parameter_keys=["query"])

    assert not any("query" in diagnostic["message"] for diagnostic in result["data"]["sandbox_diagnostics"])


@pytest.mark.asyncio
async def test_syntax_error_is_caught_by_preflight() -> None:
    result = await skyvern_code_block_lint(code="await page.goto(  # unbalanced paren")

    assert result["ok"] is False
    assert result["data"]["lint_ok"] is False
    assert _has_diagnostic(result, section="preflight_diagnostics", code="SYNTAX_ERROR")
