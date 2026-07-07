"""Skyvern MCP code-block tools."""

from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import Field

from skyvern.forge.sdk.copilot.code_block_preflight import (
    CodeBlockPreflightDiagnostic,
    author_time_code_block_diagnostics,
    preflight_code_block,
    sandbox_unresolved_name_diagnostics,
)
from skyvern.forge.sdk.copilot.code_block_security import (
    CodeBlockSecurityError,
    author_time_code_security_errors,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import synthesize_code_block

from ._common import ErrorCode, make_error, make_result


def _serialize_diagnostic(diagnostic: CodeBlockPreflightDiagnostic) -> dict[str, str]:
    return {"code": diagnostic.code, "message": diagnostic.message}


def _serialize_security_error(error: CodeBlockSecurityError) -> dict[str, str]:
    return {"reason_code": error.reason_code, "surface": error.surface, "message": str(error)}


def _code_safety_errors(code: str) -> list[dict[str, str]]:
    # Keep workflow model imports deferred for the lightweight-install import contract.
    from skyvern.forge.sdk.workflow.exceptions import InsecureCodeDetected  # noqa: PLC0415
    from skyvern.forge.sdk.workflow.models.block import CodeBlock  # noqa: PLC0415

    try:
        CodeBlock.is_safe_code(code)
    except SyntaxError:
        return []
    except InsecureCodeDetected as exc:
        return [{"message": str(exc)}]
    return []


async def skyvern_code_block_lint(
    code: Annotated[str, Field(description="Python source of the code block to lint")],
    parameter_keys: Annotated[
        list[str] | None,
        Field(description="Workflow parameter keys the block may reference (treated as defined names)"),
    ] = None,
    label: Annotated[str, Field(description="Block label, used in security-error messages")] = "code_block",
) -> dict[str, Any]:
    """Lint a Workflow Copilot `code` block with the copilot's own deterministic gates.

    Runs CodeBlock.is_safe_code(), the security denylist
    (page.request/context/evaluate/evaluate_handle), the sandbox unresolved-name analysis,
    static preflight, and author-time AST diagnostics. Returns a structured pass/fail result.
    No browser session or API call required.
    """
    action = "skyvern_code_block_lint"
    keys = tuple(parameter_keys or ())

    code_safety_errors = _code_safety_errors(code)
    security_errors = author_time_code_security_errors(label=label, code=code)
    preflight = preflight_code_block(code, parameter_keys=keys)
    sandbox = sandbox_unresolved_name_diagnostics(code, parameter_keys=keys)
    author_time = author_time_code_block_diagnostics(code)

    # Author-time diagnostics stay advisory and are deliberately excluded from ok.
    ok = not (code_safety_errors or security_errors or preflight or sandbox)
    return make_result(
        action,
        ok=ok,
        data={
            "lint_ok": ok,
            "code_safety_errors": code_safety_errors,
            "security_errors": [_serialize_security_error(error) for error in security_errors],
            "preflight_diagnostics": [_serialize_diagnostic(diagnostic) for diagnostic in preflight],
            "sandbox_diagnostics": [_serialize_diagnostic(diagnostic) for diagnostic in sandbox],
            "author_time_diagnostics": [_serialize_diagnostic(diagnostic) for diagnostic in author_time],
        },
        warnings=[diagnostic.message for diagnostic in author_time],
        error=(
            None
            if ok
            else make_error(
                ErrorCode.INVALID_INPUT,
                "Code block failed copilot lint gates",
                "Fix the listed security/preflight/sandbox issues before persisting the block",
            )
        ),
    )


async def skyvern_code_block_synthesize(
    trajectory_json: Annotated[
        str,
        Field(
            description=(
                "JSON array of captured interaction objects (the scout trajectory). Each object has "
                "tool_name plus selector/source_url/role/accessible_name/typed_value/value/key as "
                "applicable. This tool does NOT scout a live page - the trajectory is supplied by the caller."
            )
        ),
    ],
    strict_selectors: Annotated[
        bool,
        Field(description="Emit only the scout's captured selectors verbatim; drop ambiguous bare selectors"),
    ] = False,
) -> dict[str, Any]:
    """Deterministically synthesize a Playwright `code` block from a captured trajectory.

    Wraps the copilot's pure synthesize_code_block(): output is byte-identical per trajectory,
    with no LLM and no I/O. The trajectory is supplied by the caller - capturing a live trajectory
    (the scout) is out of scope for this tool.
    """
    action = "skyvern_code_block_synthesize"

    try:
        trajectory = json.loads(trajectory_json)
    except (json.JSONDecodeError, TypeError) as exc:
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid trajectory JSON: {exc}",
                "Provide a JSON array of interaction objects",
            ),
        )

    if not isinstance(trajectory, list):
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Expected a JSON array, got {type(trajectory).__name__}",
                "Provide a JSON array of interaction objects",
            ),
        )

    for index, item in enumerate(trajectory):
        if not isinstance(item, dict):
            return make_result(
                action,
                ok=False,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Expected trajectory item at index {index} to be an object, got {type(item).__name__}",
                    "Provide a JSON array of interaction objects",
                ),
            )

    synthesized = synthesize_code_block(trajectory, strict_selectors=strict_selectors)
    if synthesized is None:
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Trajectory produced no synthesizable steps",
                "Supply a non-empty trajectory with at least one actionable interaction",
            ),
        )

    return make_result(
        action,
        data={
            "code": synthesized.code,
            "parameters": synthesized.parameters,
            "steps": synthesized.steps,
            "notes": synthesized.notes,
            "emitted_interaction_count": synthesized.diagnostics.emitted_interaction_count,
            "truncated": synthesized.diagnostics.truncated,
        },
    )


__all__ = ["skyvern_code_block_lint", "skyvern_code_block_synthesize"]
