"""Tests for the MCP code-block lint tool."""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from skyvern.cli.mcp_tools.code_block import skyvern_code_block_lint
from skyvern.forge import app
from skyvern.forge.sdk.copilot import code_block_preflight as code_block_preflight_module
from skyvern.forge.sdk.copilot.code_block_preflight import CodeBlockScanFinding


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
    assert "sandbox_diagnostics" not in result["data"]
    assert result["data"]["author_time_diagnostics"] == []


@pytest.mark.asyncio
async def test_unknown_runtime_name_and_builtin_exception_are_not_lint_failures() -> None:
    result = await skyvern_code_block_lint(
        code="try:\n    value = unavailable_at_runtime\nexcept ValueError:\n    value = None",
    )

    assert result["ok"] is True
    assert result["data"]["lint_ok"] is True
    assert "sandbox_diagnostics" not in result["data"]


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
async def test_page_evaluate_is_not_blocked_by_security_denylist() -> None:
    result = await skyvern_code_block_lint(code='await page.evaluate("1+1")')

    assert result["data"]["security_errors"] == []


@pytest.mark.asyncio
async def test_page_request_is_blocked_by_security_denylist() -> None:
    result = await skyvern_code_block_lint(code='await page.request.get("https://example.com")')

    assert result["ok"] is False
    assert result["data"]["lint_ok"] is False
    assert _has_security_error(result, reason_code="AUTHOR_PAGE_REQUEST")


@pytest.mark.asyncio
async def test_syntax_error_is_caught_by_preflight() -> None:
    result = await skyvern_code_block_lint(code="await page.goto(  # unbalanced paren")

    assert result["ok"] is False
    assert result["data"]["lint_ok"] is False
    assert _has_diagnostic(result, section="preflight_diagnostics", code="SYNTAX_ERROR")


@pytest.mark.asyncio
async def test_body_readiness_advisory_warns_without_failing_the_lint_gate() -> None:
    code = 'body = page.locator("body")\nawait body.wait_for(state="visible", timeout=30000)\nreturn {"ok": True}'

    result = await skyvern_code_block_lint(code=code)

    assert result["ok"] is True
    assert result["data"]["lint_ok"] is True
    assert result["data"]["preflight_diagnostics"] == []
    assert _has_diagnostic(result, section="author_time_diagnostics", code="ROOT_CONTAINER_READINESS_WAIT")


def _install_scanner_stub(
    monkeypatch: pytest.MonkeyPatch,
    findings: list[CodeBlockScanFinding] | Exception,
) -> None:
    async def _scan(
        code: str, *, organization_id: str | None = None, timeout_seconds: float = 3.0
    ) -> list[CodeBlockScanFinding]:
        if isinstance(findings, Exception):
            raise findings
        return findings

    monkeypatch.setattr(app.AGENT_FUNCTION, "scan_code_block_source", _scan)


@pytest.mark.asyncio
async def test_scanner_advisory_warns_without_failing_the_lint_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    finding = CodeBlockScanFinding(rule_id="obfuscated-exec", line=2, message="Executes decoded code.")
    _install_scanner_stub(monkeypatch, [finding])
    code = 'await page.goto("https://example.com")\nreturn {"ok": True}'

    result = await skyvern_code_block_lint(code=code)

    assert result["ok"] is True
    assert result["data"]["lint_ok"] is True
    assert result["data"]["code_safety_errors"] == []
    assert result["data"]["preflight_diagnostics"] == []
    advisories = [d for d in result["data"]["author_time_diagnostics"] if d["code"] == "SCANNER_ADVISORY"]
    assert advisories == [
        {
            "code": "SCANNER_ADVISORY",
            "message": "Flagged by scanner rule `obfuscated-exec` at line 2. Executes decoded code.",
        }
    ]
    assert advisories[0]["message"] in result.get("warnings", [])


@pytest.mark.asyncio
async def test_scanner_advisory_never_echoes_matched_snippet_text(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = "SNIPPET_MARKER_b64_payload_xyz"
    _install_scanner_stub(monkeypatch, [CodeBlockScanFinding(rule_id="rule-id", line=1)])

    result = await skyvern_code_block_lint(code=f'value = "{marker}"\nreturn {{"ok": True}}')

    assert marker not in json.dumps(result["data"]["author_time_diagnostics"])
    assert marker not in json.dumps(result.get("warnings", []))


@pytest.mark.asyncio
async def test_scanner_advisory_is_distinct_from_hard_code_safety_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_scanner_stub(monkeypatch, [CodeBlockScanFinding(rule_id="rule-id", line=1)])

    result = await skyvern_code_block_lint(code="import os\nreturn {}")

    assert result["ok"] is False
    assert result["data"]["code_safety_errors"]
    assert all("SCANNER_ADVISORY" not in error["message"] for error in result["data"]["code_safety_errors"])
    assert _has_diagnostic(result, section="author_time_diagnostics", code="SCANNER_ADVISORY")


@pytest.mark.asyncio
async def test_scanner_error_is_silently_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_scanner_stub(monkeypatch, RuntimeError("scanner unavailable"))

    result = await skyvern_code_block_lint(code='await page.goto("https://example.com")\nreturn {"ok": True}')

    assert result["ok"] is True
    assert result["data"]["author_time_diagnostics"] == []


@pytest.mark.asyncio
async def test_scanner_timeout_is_silently_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_block_preflight_module, "SCANNER_ADVISORY_TIMEOUT_SECONDS", 0.05)

    async def _hang(code: str) -> list[CodeBlockScanFinding]:
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(app.AGENT_FUNCTION, "scan_code_block_source", _hang)

    result = await skyvern_code_block_lint(code='await page.goto("https://example.com")\nreturn {"ok": True}')

    assert result["ok"] is True
    assert result["data"]["author_time_diagnostics"] == []
