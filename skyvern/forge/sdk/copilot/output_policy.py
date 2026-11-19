from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

from skyvern.forge.sdk.copilot.context import COPILOT_RESPONSE_TYPES, ResponseType
from skyvern.forge.sdk.copilot.request_policy import (
    RequestPolicy,
    contains_email_password_pair,
)
from skyvern.forge.sdk.schemas.copilot_turn_outcome import OutputPolicyReason
from skyvern.utils.raw_secret_redaction import (
    RAW_SECRET_PATTERNS,
    SECRET_KEYWORD_ASSIGNMENT_PATTERN,
    SECRET_KEYWORD_LABEL_PATTERN,
)

WORKFLOW_PRESENT_SENTINEL = object()
_PLACEHOLDER_MARKERS = ("{{", "{%", "[REDACTED_SECRET]")
# RHS of a secret-keyword assignment that references a bound value instead of carrying one:
# a `parameters`-rooted lookup (quoted-key subscript / .get / attribute), or an attribute
# chain ending in a credential field (`cred.password` / `await cred.otp()`), optionally wrapped in str(...).
# `totp` remains allowed for backward compatibility with old synthesized code;
# new Code-block OTP flows should use `await cred.otp()`.
# Fully anchored — only closing punctuation may follow, so a literal appended to a
# reference (`cred.password+"hunter2"`) or a dotted literal (a JWT) never passes.
_SANCTIONED_SECRET_REFERENCE_RE = re.compile(
    r"^(?:str\()?"
    r"(?:parameters(?:\[(?:'[^']*'|\"[^\"]*\")\]|\.get\((?:'[^']*'|\"[^\"]*\")\)|(?:\.[A-Za-z_][A-Za-z0-9_]*)+)"
    r"|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\.(?:username|password|totp)"
    r"|(?:await\s+)?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\.otp\(\))"
    r"[)\]\},;.'\"]*$"
)
# The RHS can be a multi-token expression such as `await login_credentials.otp()`.
# Callers pass the single line containing the match, so this is not expected to
# consume across embedded newlines.
_SECRET_ASSIGNMENT_RHS_RE = re.compile(r"[:=]\s*(.+)\s*$")


@dataclass(frozen=True)
class ResponseScaffoldingNormalization:
    response_type: ResponseType
    user_response: str | None
    changed: bool = False


class CopilotOutputKind(StrEnum):
    INFORMATIONAL_ANSWER = "informational_answer"
    CLARIFICATION_REQUEST = "clarification_request"
    REFUSAL = "refusal"
    WORKFLOW_DRAFT_PROPOSAL = "workflow_draft_proposal"
    WORKFLOW_UPDATE_PROPOSAL = "workflow_update_proposal"
    WORKFLOW_RUN_RESULT = "workflow_run_result"


@dataclass
class OutputPolicyVerdict:
    allowed: bool = True
    output_kind: CopilotOutputKind = CopilotOutputKind.INFORMATIONAL_ANSWER
    reason_codes: list[OutputPolicyReason] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.reason_codes:
            self.allowed = False

    def add(self, reason: OutputPolicyReason) -> None:
        if reason not in self.reason_codes:
            self.reason_codes.append(reason)
        self.allowed = False


_FINAL_OUTPUT_HARD_BLOCK_REASONS: frozenset[OutputPolicyReason] = frozenset(
    {
        OutputPolicyReason.RAW_SECRET_LEAK,
        OutputPolicyReason.PERSISTENCE_STATE_MISMATCH,
        OutputPolicyReason.OUTPUT_POLICY_CONTEXT_MISSING,
    }
)


def hard_block_output_policy_verdict(verdict: OutputPolicyVerdict) -> OutputPolicyVerdict:
    hard_reasons = [reason for reason in verdict.reason_codes if reason in _FINAL_OUTPUT_HARD_BLOCK_REASONS]
    return OutputPolicyVerdict(
        allowed=not hard_reasons,
        output_kind=verdict.output_kind,
        reason_codes=hard_reasons,
    )


def derive_output_kind(
    *,
    response_type: str,
    request_policy: RequestPolicy | None,
    updated_workflow: Any | None,
    workflow_was_persisted: bool,
    workflow_attempted: bool,
    unvalidated: bool,
) -> CopilotOutputKind:
    del request_policy
    if response_type == "ASK_QUESTION":
        return CopilotOutputKind.CLARIFICATION_REQUEST
    if updated_workflow is not None and workflow_attempted and not unvalidated:
        return CopilotOutputKind.WORKFLOW_RUN_RESULT
    if updated_workflow is not None and workflow_was_persisted:
        return CopilotOutputKind.WORKFLOW_UPDATE_PROPOSAL
    if updated_workflow is not None:
        return CopilotOutputKind.WORKFLOW_DRAFT_PROPOSAL
    if workflow_attempted:
        return CopilotOutputKind.WORKFLOW_RUN_RESULT
    return CopilotOutputKind.INFORMATIONAL_ANSWER


def normalize_response_scaffolding(response_type: str, user_response: str | None) -> ResponseScaffoldingNormalization:
    typed_response_type: ResponseType = (
        cast(ResponseType, response_type) if response_type in COPILOT_RESPONSE_TYPES else "REPLY"
    )
    label, stripped = _split_leading_response_label(user_response)
    if label is None:
        return ResponseScaffoldingNormalization(response_type=typed_response_type, user_response=user_response)
    normalized_type: ResponseType
    if label == "REPLACE_WORKFLOW":
        normalized_type = "REPLACE_WORKFLOW" if typed_response_type == "REPLACE_WORKFLOW" else "REPLY"
    else:
        normalized_type = label
    return ResponseScaffoldingNormalization(response_type=normalized_type, user_response=stripped, changed=True)


def _split_leading_response_label(text: str | None) -> tuple[ResponseType | None, str | None]:
    if not isinstance(text, str):
        return None, text
    candidate = text.lstrip()
    candidate_upper = candidate.upper()
    for response_type in sorted(COPILOT_RESPONSE_TYPES, key=len, reverse=True):
        if not candidate_upper.startswith(response_type):
            continue
        remainder = candidate[len(response_type) :]
        if not remainder:
            continue
        stripped = remainder.lstrip()
        if not stripped:
            return response_type, ""
        if stripped[0] in {":", ","}:
            return response_type, stripped[1:].lstrip()
        protocol_like_label = "_" in response_type or candidate[: len(response_type)].isupper()
        leading_whitespace = remainder[: len(remainder) - len(stripped)]
        if "\n" in leading_whitespace and not stripped.startswith(("{", "```")):
            return response_type, stripped
        if remainder[0].isspace() and protocol_like_label and not stripped.startswith(("{", "```")):
            return response_type, stripped
    return None, text


def output_policy_verdict_to_trace_data(
    verdict: OutputPolicyVerdict,
    *,
    surface: str,
    response_type: str | None = None,
    tool_name: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "surface": surface,
        "allowed": verdict.allowed,
        "output_kind": verdict.output_kind.value,
        "reason_codes": [reason.value for reason in verdict.reason_codes],
    }
    if response_type is not None:
        data["response_type"] = response_type
    if tool_name is not None:
        data["tool_name"] = tool_name
    return data


def build_output_policy_diagnostics(
    *,
    raw_verdict: OutputPolicyVerdict,
    final_verdict: OutputPolicyVerdict,
    final_output_kind: CopilotOutputKind,
    hard_block_reason_codes: list[OutputPolicyReason],
) -> dict[str, Any]:
    raw_would_have_failed = bool(raw_verdict.reason_codes)
    return {
        "raw_output_kind": raw_verdict.output_kind.value,
        "final_output_kind": final_output_kind.value,
        "raw_reason_codes": [reason.value for reason in raw_verdict.reason_codes],
        "hard_block_reason_codes": [reason.value for reason in hard_block_reason_codes],
        "raw_would_have_failed": raw_would_have_failed,
        "contained_failure": raw_would_have_failed and bool(hard_block_reason_codes),
        "final_output_policy_allowed": final_verdict.allowed,
    }


def output_policy_verdict_from_trace_data(data: Any) -> OutputPolicyVerdict:
    if not isinstance(data, dict):
        return OutputPolicyVerdict(
            allowed=False,
            reason_codes=[OutputPolicyReason.OUTPUT_POLICY_CONTEXT_MISSING],
        )
    reason_codes: list[OutputPolicyReason] = []
    for raw_reason in data.get("reason_codes") or []:
        try:
            reason_codes.append(OutputPolicyReason(str(raw_reason)))
        except ValueError:
            continue
    try:
        output_kind = CopilotOutputKind(str(data.get("output_kind")))
    except ValueError:
        output_kind = CopilotOutputKind.INFORMATIONAL_ANSWER
    return OutputPolicyVerdict(
        allowed=bool(data.get("allowed")) and not reason_codes,
        output_kind=output_kind,
        reason_codes=reason_codes,
    )


def evaluate_output_policy(
    *,
    request_policy: RequestPolicy | None,
    response_type: str = "REPLY",
    user_response: str | None = None,
    global_llm_context: str | None = None,
    has_workflow_proposal: bool = False,
    workflow_was_persisted: bool = False,
    workflow_attempted: bool = False,
    unvalidated: bool = False,
    output_kind: CopilotOutputKind | None = None,
) -> OutputPolicyVerdict:
    if output_kind is None:
        output_kind = derive_output_kind(
            response_type=response_type,
            request_policy=request_policy,
            updated_workflow=WORKFLOW_PRESENT_SENTINEL if has_workflow_proposal else None,
            workflow_was_persisted=workflow_was_persisted,
            workflow_attempted=workflow_attempted,
            unvalidated=unvalidated,
        )
    verdict = OutputPolicyVerdict(output_kind=output_kind)
    # Only the reply is scanned for a raw secret: rebinding and persistence scrubbing own what
    # reaches storage, and scanning a draft judged the YAML encoding rather than the value.
    if raw_secret_label(user_response) is not None:
        verdict.add(OutputPolicyReason.RAW_SECRET_LEAK)

    if output_kind == CopilotOutputKind.WORKFLOW_UPDATE_PROPOSAL and not workflow_was_persisted:
        verdict.add(OutputPolicyReason.PERSISTENCE_STATE_MISMATCH)
    elif output_kind == CopilotOutputKind.WORKFLOW_DRAFT_PROPOSAL and workflow_was_persisted:
        verdict.add(OutputPolicyReason.PERSISTENCE_STATE_MISMATCH)

    return verdict


def raw_secret_label(value: str | None) -> str | None:
    """None when the output check flags no raw secret; else the label the detector matched before its value, or ""."""
    if not value:
        return None
    if contains_email_password_pair(value):
        return ""
    for pattern in RAW_SECRET_PATTERNS:
        for match in pattern.finditer(value):
            matched = match.group(0)
            if any(marker in matched for marker in _PLACEHOLDER_MARKERS):
                continue
            if pattern is SECRET_KEYWORD_ASSIGNMENT_PATTERN and _keyword_assignment_is_exempt(value, match):
                continue
            label, separator, _ = matched.replace("=", ":").partition(":")
            label = label.strip()
            if not separator:
                code_label = re.fullmatch(r"([A-Za-z0-9 ]+?)(?:\s+is)?\s*\d{6,8}", matched)
                return code_label.group(1) if code_label else ""
            if "_" not in label:
                return label
            # Underscore-joined text before the keyword is free text that can carry part of a secret; keep the keyword.
            parts = label.split("_")
            suffixes = ("_".join(parts[i:]) for i in range(len(parts) - 1, -1, -1))
            return next((suffix for suffix in suffixes if SECRET_KEYWORD_LABEL_PATTERN.fullmatch(suffix)), "")
    return None


def _line_end_after_match(text: str, match: re.Match[str]) -> int:
    line_end = text.find("\n", match.end())
    return len(text) if line_end == -1 else line_end


def _line_containing_match(text: str, match: re.Match[str]) -> str:
    line_start = text.rfind("\n", 0, match.start()) + 1
    return text[line_start : _line_end_after_match(text, match)]


def _is_sanctioned_secret_reference(matched: str) -> bool:
    rhs_match = _SECRET_ASSIGNMENT_RHS_RE.search(matched)
    if rhs_match is None:
        return False
    return bool(_SANCTIONED_SECRET_REFERENCE_RE.match(rhs_match.group(1)))


# `token` is the one secret keyword with an unrelated English sense — a lexical token parsed out of
# page text. The other keywords (password, passcode, secret, api key, bearer, authorization) carry no
# such second meaning and keep the strict sanctioned-source rule.
_LEXICAL_TOKEN_LHS_RE = re.compile(r"^[A-Za-z0-9_]*token(?=\s*[:=])", re.I)
_UNAMBIGUOUS_SECRET_KEYWORD_RE = re.compile(r"password|passcode|api[_ -]?key|secret|bearer|authorization", re.I)


def _parse_chain_receiver(node: ast.expr) -> ast.expr | None:
    """The variable a read/index/method chain is rooted in, or None for anything else — `identity(...)`
    roots in a free function and `("ghp_...", parts)[0]` in a literal container, so neither counts."""
    while True:
        if isinstance(node, (ast.Subscript, ast.Attribute, ast.Await)):
            node = node.value
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Attribute):
                return None
            node = node.func.value
        else:
            return node


def _parses_a_value_out_of_something(rhs: str) -> bool:
    """Whether the assigned value is read out of a variable rather than written down: a credential
    reaches code as a literal, a lexical token is parsed off something in scope (`m.group(1)`).
    Anything that does not parse as one expression gets no verdict and keeps the keyword match."""
    candidate = rhs.strip()
    # On the last line of embedded code the scalar's own closing delimiter rides along.
    for text in (candidate, candidate.rstrip("\"'")):
        try:
            body = ast.parse(text, mode="eval").body
        except (SyntaxError, ValueError):
            continue
        # A bare dotted chain is how a JWT looks, so extraction has to involve an actual call or index.
        if not any(isinstance(node, (ast.Call, ast.Subscript)) for node in ast.walk(body)):
            return False
        if any(_reads_as_a_written_down_secret(node) for node in ast.walk(body)):
            return False
        return isinstance(_parse_chain_receiver(body), ast.Name)
    return False


# A delimiter or key a parse chain consumes is short, or carries whitespace or punctuation
# (`" logs found"`, `"access_token"`). A credential written into the chain is neither.
_SECRET_SHAPED_LITERAL_LENGTH = 16


def _reads_as_a_written_down_secret(node: ast.AST) -> bool:
    """Whether a literal inside an extraction chain carries a value in rather than naming a
    delimiter or key — `cache.get("ghp_...")` roots in a variable exactly as a real read does."""
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return False
    return len(node.value) >= _SECRET_SHAPED_LITERAL_LENGTH and all(
        char.isalnum() or char in "-_" for char in node.value
    )


def _keyword_assignment_is_exempt(text: str, match: re.Match[str]) -> bool:
    matched = match.group(0)
    line = _line_containing_match(text, match)
    if _is_sanctioned_secret_reference(matched) or _is_sanctioned_secret_reference(line):
        return True
    return any(_is_lexical_token_assignment(matched, reading) for reading in _assignment_readings(text, match))


def _assignment_readings(text: str, match: re.Match[str]) -> list[str]:
    """The assignment as the whole line, as the text from the keyword on, and — when the code is
    embedded in a quoted YAML scalar — as the single escaped segment carrying it.

    Only the YAML decoder knows whether an embedded ``\\n`` ends a line or is code data, so both
    readings are offered and either one showing an extraction exempts the match. Reading from the
    keyword matters because a YAML key's own colon precedes the code on a scalar's opening line.
    """
    from_keyword = text[match.start() : _line_end_after_match(text, match)]
    escaped_segment = from_keyword.split("\\n")[0].replace('\\"', '"').replace("\\'", "'")
    return [_line_containing_match(text, match), from_keyword, escaped_segment]


def _is_lexical_token_assignment(matched: str, assignment: str) -> bool:
    if not _LEXICAL_TOKEN_LHS_RE.match(matched) or _UNAMBIGUOUS_SECRET_KEYWORD_RE.search(assignment):
        return False
    # The keyword pattern ends at the first whitespace, so the rest of the line carries the expression.
    rhs_match = _SECRET_ASSIGNMENT_RHS_RE.search(assignment)
    return rhs_match is not None and _parses_a_value_out_of_something(rhs_match.group(1))
