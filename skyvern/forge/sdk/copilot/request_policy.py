from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Literal, cast, get_args
from urllib.parse import urlparse

import structlog
from email_validator import EmailNotValidError, validate_email

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.context import StructuredContext
from skyvern.forge.sdk.copilot.output_utils import parse_final_response
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.schemas.credentials import Credential
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
)
from skyvern.forge.sdk.workflow.models.parameter import ParameterType
from skyvern.utils.strings import escape_code_fences
from skyvern.utils.yaml_loader import safe_load_no_dates

LOG = structlog.get_logger()
PROMPT_NAME = "workflow-copilot-request-policy"
_TESTING_INTENTS = {"require_test", "skip_test", "unspecified"}
_KINDS = {"none", "raw_secret", "credential_id", "credential_name", "website_stored_credential", "placeholder"}
ClarificationReason = Literal[
    "none",
    "raw_secret",
    "credential_name_unresolved",
    "credential_invention_requested",
    "ambiguous_loop_edit",
    "invalid_conditional_container",
    "missing_conditional_condition",
    "missing_target_context",
    "workflow_credential_inputs_unbound",
]
_VALID_CLARIFICATION_REASONS: frozenset[ClarificationReason] = frozenset(get_args(ClarificationReason))
_PRE_RESOLUTION_CLARIFICATION_REASONS = {
    "credential_invention_requested",
    "ambiguous_loop_edit",
    "invalid_conditional_container",
    "missing_conditional_condition",
    "missing_target_context",
}
_REASONS_OVERRIDDEN_BY_CREDENTIAL_REFS = {
    "ambiguous_loop_edit",
    "invalid_conditional_container",
    "missing_conditional_condition",
    "missing_target_context",
}
_CREDENTIALS_UI_DIRECTIONS = (
    f"You can find or add saved credentials at {settings.SKYVERN_APP_URL.rstrip('/')}/credentials."
)
_RAW_SECRET_QUESTION = (
    "Please do not paste raw login credentials or secrets in chat because they can enter model telemetry and execution traces. "
    "Store the credential in the Skyvern Credentials UI and reply with its exact saved credential name or a credential ID beginning with cred_. "
    f"{_CREDENTIALS_UI_DIRECTIONS} "
    "DO NOT PROVIDE RAW LOGIN/PASSWORD."
)
_SAVED_CREDENTIAL_NAME_QUESTION_STABLE_PREFIX = "Which saved credential should I use? Please provide the exact credential name or a credential ID beginning with cred_."
_SAVED_CREDENTIAL_NAME_QUESTION = f"{_SAVED_CREDENTIAL_NAME_QUESTION_STABLE_PREFIX} {_CREDENTIALS_UI_DIRECTIONS}"
_STORED_CREDENTIAL_URL_QUESTION = (
    f"Which website or login page should I use to look up the stored credential? {_CREDENTIALS_UI_DIRECTIONS}"
)
_CREDENTIAL_ID_RE = re.compile(r"\bcred_[A-Za-z0-9][A-Za-z0-9_-]*\b")
_JINJA_TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_CREDENTIAL_PARAM_METADATA_FIELDS = frozenset(
    {
        "parameter_type",
        "key",
        "description",
        "workflow_id",
        "created_at",
        "modified_at",
        "deleted_at",
    }
)
_LOGIN_CREDENTIAL_REQUIRED_KEY_FIELDS = ("username_key", "password_key")
_WORKFLOW_CREDENTIAL_INPUTS_UNBOUND_QUESTION = (
    "I couldn't find the required credentials for the existing workflow. "
    f"Please add them via the Credentials UI and I can try again. {_CREDENTIALS_UI_DIRECTIONS}"
)
_RAW_SECRET_PATTERNS = (
    re.compile(r"\b(?:password|passcode|api[_ -]?key|secret|token|bearer|authorization)\s*[:=]\s*\S+", re.I),
    re.compile(
        r"\b(?:otp|totp|mfa|2fa|verification|auth(?:entication)? code)(?:\s+code)?\s*(?:is|[:=])?\s*\d{6,8}\b",
        re.I,
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
# Reused by output-policy guardrails as syntactic leak backstops.
RAW_SECRET_PATTERNS = _RAW_SECRET_PATTERNS
_COLON_DELIMITED_SECRET_SEGMENT_SEPARATORS = (",", ";", "|")
_COLON_DELIMITED_SECRET_EDGE_CHARS = "\"'`()[]{}<>"
_INVALID_CONDITIONAL_CONTAINER_MARKERS = (
    "into the conditional",
    "inside the conditional",
    "within the conditional",
    "into conditional",
    "inside conditional",
    "within conditional",
)


@dataclass
class RequestPolicy:
    testing_intent: str = "unspecified"
    credential_input_kind: str = "none"
    credential_refs: list[str] = field(default_factory=list)
    login_page_urls: list[str] = field(default_factory=list)
    requires_user_clarification: bool = False
    allow_update_workflow: bool = True
    allow_run_blocks: bool = True
    allow_missing_credentials_in_draft: bool = False
    user_response_policy: str = "proceed"
    completion_contract: str | None = None
    resolved_credentials: list[Credential] = field(default_factory=list)
    invalid_credential_ids: list[str] = field(default_factory=list)
    clarification_question: str | None = None
    raw_secret_detected: bool = False
    clarification_reason: ClarificationReason = "none"

    def to_trace_data(self) -> dict[str, Any]:
        return {
            "testing_intent": self.testing_intent,
            "credential_input_kind": self.credential_input_kind,
            "clarification_reason": self.clarification_reason,
            "allow_update_workflow": self.allow_update_workflow,
            "allow_run_blocks": self.allow_run_blocks,
            "allow_missing_credentials_in_draft": self.allow_missing_credentials_in_draft,
            "resolved_credential_count": len(self.resolved_credentials),
            "has_completion_contract": bool(self.completion_contract),
            "raw_secret_detected": self.raw_secret_detected,
        }

    def prompt_summary(self) -> str:
        lines = [
            f"testing_intent: {self.testing_intent}",
            f"credential_input_kind: {self.credential_input_kind}",
            f"clarification_reason: {self.clarification_reason}",
            f"allow_update_workflow: {self.allow_update_workflow}",
            f"allow_run_blocks: {self.allow_run_blocks}",
            f"allow_missing_credentials_in_draft: {self.allow_missing_credentials_in_draft}",
        ]
        if self.completion_contract:
            lines.append(f"completion_contract: {self.completion_contract}")
        if self.resolved_credentials:
            lines += [
                "resolved_credentials:",
                *[f"- {_safe_label(credential)}" for credential in self.resolved_credentials],
            ]
        if self.invalid_credential_ids:
            lines.append("invalid_credential_ids: " + ", ".join(f"`{cid}`" for cid in self.invalid_credential_ids))
        return "\n".join(lines)


_TRANSCRIPT_TOTAL_CHAR_BUDGET = 2048
TRANSCRIPT_ANCHOR_CHAR_CAP = 512
_TRANSCRIPT_RETAINED_MIN_CHARS = 512
_TRANSCRIPT_MARKER_RESERVE = 32
_EMPTY_SLOT_SENTINEL = "(none)"


@dataclass(frozen=True)
class RequestPolicyTranscriptContext:
    earliest_user_turn: str
    latest_prior_user_turn: str
    latest_assistant_turn: str
    retained_history: str
    omitted_any: bool


def _middle_truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    keep = max(cap - 32, 16)
    head_len = keep // 2
    tail_len = keep - head_len
    omitted = len(text) - keep
    return f"{text[:head_len]}<…{omitted} chars truncated…>{text[-tail_len:]}"


def _safe_slot(text: str | None, cap: int) -> str:
    if not text:
        return _EMPTY_SLOT_SENTINEL
    # Truncate the raw text first to bound regex work on pasted large messages.
    # A secret that straddles the head/tail splice would not be caught here, but
    # `_middle_truncate` later collapses the rendered output anyway, so any such
    # value already cannot leak intact to the prompt.
    bounded = text if len(text) <= cap * 4 else text[: cap * 2] + text[-cap * 2 :]
    return _middle_truncate(escape_code_fences(redact_raw_secrets_for_prompt(bounded)), cap)


def build_transcript_context(
    messages: list[WorkflowCopilotChatHistoryMessage],
    current_user_message: str,
    *,
    total_char_budget: int = _TRANSCRIPT_TOTAL_CHAR_BUDGET,
    anchor_char_cap: int = TRANSCRIPT_ANCHOR_CHAR_CAP,
    retained_min_chars: int = _TRANSCRIPT_RETAINED_MIN_CHARS,
) -> RequestPolicyTranscriptContext:
    """Shape chat history into structural anchors for the request-policy classifier.

    The chat window is already truncated upstream (see
    `CHAT_HISTORY_CONTEXT_MESSAGES` in `skyvern/forge/sdk/routes/workflow_copilot.py`),
    so `earliest_user_turn` is the head of the retained tail rather than the
    original conversation goal — the prompt header surfaces that caveat.
    """
    filtered: list[WorkflowCopilotChatHistoryMessage] = [m for m in messages if (m.content or "").strip()]
    current_stripped = (current_user_message or "").strip()
    if (
        filtered
        and filtered[-1].sender == WorkflowCopilotChatSender.USER
        and (filtered[-1].content or "").strip() == current_stripped
        and current_stripped
    ):
        filtered = filtered[:-1]

    user_indices = [i for i, m in enumerate(filtered) if m.sender == WorkflowCopilotChatSender.USER]
    ai_indices = [i for i, m in enumerate(filtered) if m.sender == WorkflowCopilotChatSender.AI]

    earliest_idx = user_indices[0] if user_indices else None
    latest_user_idx = user_indices[-1] if user_indices else None
    latest_ai_idx = ai_indices[-1] if ai_indices else None
    anchor_indices = {idx for idx in (earliest_idx, latest_user_idx, latest_ai_idx) if idx is not None}

    earliest_text = filtered[earliest_idx].content if earliest_idx is not None else None
    latest_user_text = filtered[latest_user_idx].content if latest_user_idx is not None else None
    latest_ai_text = filtered[latest_ai_idx].content if latest_ai_idx is not None else None

    earliest_slot = _safe_slot(earliest_text, anchor_char_cap)
    latest_user_slot = _safe_slot(latest_user_text, anchor_char_cap)
    latest_ai_slot = _safe_slot(latest_ai_text, anchor_char_cap)

    consumed = sum(len(slot) for slot in (earliest_slot, latest_user_slot, latest_ai_slot))
    retained_budget = max(total_char_budget - consumed, retained_min_chars)
    # Inner max() floors the retained block at `retained_min_chars // 2` when
    # the marker reserve would otherwise drive it negative; outer min() caps it
    # by total_char_budget so retained_history cannot exceed the declared ceiling
    # even when retained_min_chars is configured above total_char_budget.
    content_budget = min(
        max(retained_budget - _TRANSCRIPT_MARKER_RESERVE, retained_min_chars // 2),
        total_char_budget,
    )

    candidate_lines: list[tuple[int, str]] = []
    for i, message in enumerate(filtered):
        if i in anchor_indices:
            continue
        role = "user" if message.sender == WorkflowCopilotChatSender.USER else "assistant"
        candidate_lines.append((i, f"[{i + 1}] {role}: {_safe_slot(message.content, anchor_char_cap)}"))

    keep: list[str] = []
    used = 0
    # break (not continue) on overflow: drops the oldest tail entirely instead
    # of skipping a large recent turn to fit smaller older ones.
    for _i, line in reversed(candidate_lines):
        cost = len(line) + 1
        if used + cost > content_budget:
            break
        keep.append(line)
        used += cost
    omitted_count = len(candidate_lines) - len(keep)
    keep.reverse()

    if omitted_count:
        keep.insert(0, f"<omitted {omitted_count} entries>")

    retained = "\n".join(keep) if keep else _EMPTY_SLOT_SENTINEL
    return RequestPolicyTranscriptContext(
        earliest_user_turn=earliest_slot,
        latest_prior_user_turn=latest_user_slot,
        latest_assistant_turn=latest_ai_slot,
        retained_history=retained,
        omitted_any=bool(omitted_count),
    )


def _clean_list(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _credential_ids(text: str) -> list[str]:
    return list(dict.fromkeys(_CREDENTIAL_ID_RE.findall(text or "")))


def _raw_secret_detected(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _RAW_SECRET_PATTERNS) or contains_email_password_pair(text)


def _candidate_secret_segments(text: str) -> list[str]:
    segments: list[str] = []
    for raw_token in (text or "").split():
        token_segments = [raw_token]
        for separator in _COLON_DELIMITED_SECRET_SEGMENT_SEPARATORS:
            token_segments = [part for segment in token_segments for part in segment.split(separator)]
        segments.extend(segment.strip(_COLON_DELIMITED_SECRET_EDGE_CHARS) for segment in token_segments)
    return [segment for segment in segments if segment]


def _is_valid_account_row_email(value: str) -> bool:
    if any(char.isspace() for char in value) or "/" in value or ":" in value:
        return False
    try:
        validate_email(value, check_deliverability=False, test_environment=True)
    except EmailNotValidError:
        return False
    return True


def _looks_like_colon_delimited_secret_value(value: str) -> bool:
    if len(value) < 4:
        return False
    if any(char.isspace() for char in value):
        return False
    if any(char in value for char in ("/", "?", "#")):
        return False
    if value.isdigit() and len(value) <= 5:
        return False
    return True


def _email_password_pair_segments(text: str) -> list[str]:
    pairs: list[str] = []
    for segment in _candidate_secret_segments(text):
        email, separator, secret_value = segment.partition(":")
        if not separator:
            continue
        if _is_valid_account_row_email(email) and _looks_like_colon_delimited_secret_value(secret_value):
            pairs.append(segment)
    return pairs


def contains_email_password_pair(text: str) -> bool:
    # Privacy backstop for pasted account dumps. The request-policy classifier
    # owns ambiguous credential semantics; this parser keeps high-confidence raw
    # values out of model prompts and output surfaces without a broad regex rule.
    return bool(_email_password_pair_segments(text))


def _coerce_clarification_reason(value: Any) -> ClarificationReason:
    if value in _VALID_CLARIFICATION_REASONS:
        return cast(ClarificationReason, value)
    return "none"


def redact_raw_secrets_for_prompt(text: str) -> str:
    redacted = text or ""
    for pattern in _RAW_SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    for segment in _email_password_pair_segments(redacted):
        redacted = redacted.replace(segment, "[REDACTED_SECRET]")
    return redacted


def _classification_from_raw(raw: Any) -> RequestPolicy:
    if isinstance(raw, str):
        raw = parse_final_response(raw)
    if not isinstance(raw, dict):
        return RequestPolicy()
    testing_intent = raw.get("testing_intent")
    credential_input_kind = raw.get("credential_input_kind")
    completion_contract_raw = raw.get("completion_contract")
    completion_contract = completion_contract_raw.strip() if isinstance(completion_contract_raw, str) else None
    policy = RequestPolicy(
        testing_intent=testing_intent if testing_intent in _TESTING_INTENTS else "unspecified",
        credential_input_kind=credential_input_kind if credential_input_kind in _KINDS else "none",
        credential_refs=_clean_list(raw.get("credential_refs") or []),
        login_page_urls=_clean_list(raw.get("login_page_urls") or []),
        requires_user_clarification=bool(raw.get("requires_user_clarification")),
        completion_contract=completion_contract or None,
        clarification_reason=_coerce_clarification_reason(raw.get("clarification_reason")),
    )
    if policy.credential_input_kind == "raw_secret":
        policy.clarification_reason = "raw_secret"
    if policy.clarification_reason in _PRE_RESOLUTION_CLARIFICATION_REASONS:
        policy.requires_user_clarification = True
    return policy


def _structural_clarification_reason(user_message: str) -> ClarificationReason:
    normalized = " ".join((user_message or "").lower().split())
    if "loop" in normalized and "conditional" in normalized:
        if any(marker in normalized for marker in _INVALID_CONDITIONAL_CONTAINER_MARKERS):
            return "invalid_conditional_container"
    return "none"


def _ground_completion_contract(user_message: str, value: str | None) -> str | None:
    if not value or not value.strip():
        return None

    contract = value.strip()
    message = user_message or ""
    if contract.lower() in message.lower():
        return contract

    return None


async def _classify_request(
    user_message: str,
    workflow_yaml: str,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    global_llm_context: str,
    handler: Any,
) -> RequestPolicy:
    ids = _credential_ids(user_message)
    if _raw_secret_detected(user_message):
        return RequestPolicy(
            credential_input_kind="raw_secret",
            credential_refs=ids,
            raw_secret_detected=True,
            clarification_reason="raw_secret",
        )
    structural_reason = _structural_clarification_reason(user_message)
    if structural_reason != "none":
        return RequestPolicy(
            credential_input_kind="credential_id" if ids else "none",
            credential_refs=ids,
            requires_user_clarification=True,
            clarification_reason=structural_reason,
        )
    if handler is None:
        return RequestPolicy(credential_input_kind="credential_id" if ids else "none", credential_refs=ids)

    transcript = build_transcript_context(chat_history, user_message)
    prompt = prompt_engine.load_prompt(
        template=PROMPT_NAME,
        user_message=escape_code_fences(user_message),
        workflow_yaml=escape_code_fences(redact_raw_secrets_for_prompt(workflow_yaml)[:2048]),
        earliest_user_turn=transcript.earliest_user_turn,
        latest_prior_user_turn=transcript.latest_prior_user_turn,
        latest_assistant_turn=transcript.latest_assistant_turn,
        retained_history=transcript.retained_history,
        global_llm_context=escape_code_fences(redact_raw_secrets_for_prompt(global_llm_context)[:2048]),
    )
    try:
        raw = await asyncio.wait_for(
            handler(prompt=prompt, prompt_name=PROMPT_NAME),
            timeout=settings.COPILOT_FEASIBILITY_GATE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        LOG.warning("request-policy classifier timed out")
        return RequestPolicy(credential_input_kind="credential_id" if ids else "none", credential_refs=ids)
    except Exception as exc:
        LOG.warning("request-policy classifier failed", error=str(exc))
        return RequestPolicy(credential_input_kind="credential_id" if ids else "none", credential_refs=ids)

    policy = _classification_from_raw(raw)
    policy.completion_contract = _ground_completion_contract(user_message, policy.completion_contract)
    policy.credential_refs = _clean_list(policy.credential_refs + ids)
    if policy.testing_intent == "skip_test" and policy.completion_contract:
        policy.testing_intent = "unspecified"
    if ids and policy.credential_input_kind in ("none", "placeholder"):
        policy.credential_input_kind = "credential_id"
    return policy


async def _load_credentials(organization_id: str) -> list[Credential]:
    page = 1
    credentials: list[Credential] = []
    while True:
        items = await app.DATABASE.credentials.get_credentials(organization_id=organization_id, page=page, page_size=50)
        credentials.extend(items)
        if len(items) < 50:
            return sorted(credentials, key=lambda c: getattr(c, "created_at", None) or "", reverse=True)
        page += 1


def _safe_label(credential: Credential) -> str:
    parts = [f"`{credential.credential_id}`", credential.name]
    parts += [f"Login Page URL: {credential.tested_url}"] if credential.tested_url else []
    return " - ".join(parts)


def _block(
    policy: RequestPolicy,
    question: str,
    candidates: list[Credential] | None = None,
    *,
    reason: ClarificationReason | None = None,
) -> None:
    policy.requires_user_clarification = True
    policy.user_response_policy = "ask_clarification"
    policy.allow_update_workflow = policy.allow_run_blocks = False
    if reason is not None:
        policy.clarification_reason = reason
    if candidates:
        question += "\n\nSafe matches:\n" + "\n".join(f"- {_safe_label(candidate)}" for candidate in candidates)
    policy.clarification_question = question


def _url_parts(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if not parsed.netloc:
        return None
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{host}{path}", f"{parsed.scheme.lower()}://{host}"


def _match_by_url(credentials: list[Credential], urls: list[str]) -> list[Credential]:
    indexed = [
        (credential, parts)
        for credential in credentials
        if credential.tested_url and (parts := _url_parts(credential.tested_url))
    ]
    requested = [parts for url in urls if (parts := _url_parts(url))]
    for index in range(2):
        matches = [
            credential for credential, parts in indexed if any(parts[index] == target[index] for target in requested)
        ]
        if matches:
            return matches
    return []


_CLARIFICATION_DECISION_PREFIX = "request-policy clarification required:"
_PRIOR_CREDENTIAL_CLARIFICATION_REASONS = frozenset(
    {
        "credential_name_unresolved",
        "credential_invention_requested",
        "workflow_credential_inputs_unbound",
        "raw_secret",
    }
)


def _prior_turn_was_credential_clarification(global_llm_context: str) -> bool:
    # Walk in reverse so a stale credential clarification followed by later non-credential
    # work does not keep routing the generic fallback into credential-help text.
    structured = StructuredContext.from_json_str(global_llm_context)
    for decision in reversed(structured.decisions_made):
        if not decision.startswith(_CLARIFICATION_DECISION_PREFIX):
            continue
        return any(decision.endswith(f"/{reason}") for reason in _PRIOR_CREDENTIAL_CLARIFICATION_REASONS)
    return False


def _clarification_question(policy: RequestPolicy, global_llm_context: str = "") -> str:
    if policy.clarification_reason == "raw_secret":
        return _RAW_SECRET_QUESTION
    if policy.clarification_reason == "credential_name_unresolved":
        if policy.credential_input_kind == "website_stored_credential":
            return _STORED_CREDENTIAL_URL_QUESTION
        return _SAVED_CREDENTIAL_NAME_QUESTION
    if policy.clarification_reason == "credential_invention_requested":
        return (
            "I cannot invent a credential ID. Please provide a valid saved credential ID, "
            f"select an existing credential, or create one in the Credentials UI. {_CREDENTIALS_UI_DIRECTIONS}"
        )
    if policy.clarification_reason == "ambiguous_loop_edit":
        return "Which block or blocks should go inside the loop, and what should the loop iterate over or stop on?"
    if policy.clarification_reason == "invalid_conditional_container":
        return (
            "Conditional blocks route to other blocks; they do not contain loop blocks. "
            "What condition should route into the loop, and should any default branch skip it?"
        )
    if policy.clarification_reason == "missing_conditional_condition":
        return "What condition should trigger this conditional route?"
    if policy.clarification_reason == "missing_target_context":
        if policy.credential_input_kind == "website_stored_credential":
            return _STORED_CREDENTIAL_URL_QUESTION
        return "Which page or URL should the workflow go to?"
    if policy.clarification_reason == "workflow_credential_inputs_unbound":
        return _WORKFLOW_CREDENTIAL_INPUTS_UNBOUND_QUESTION
    if policy.credential_input_kind == "credential_name":
        return _SAVED_CREDENTIAL_NAME_QUESTION
    if policy.credential_input_kind == "website_stored_credential":
        return _STORED_CREDENTIAL_URL_QUESTION
    if _prior_turn_was_credential_clarification(global_llm_context):
        return _SAVED_CREDENTIAL_NAME_QUESTION
    return "I need one more detail before I can build and test this workflow safely."


def _has_resolvable_credential_scope(policy: RequestPolicy) -> bool:
    if policy.credential_input_kind == "credential_id":
        return any(ref.startswith("cred_") for ref in policy.credential_refs)
    if policy.credential_input_kind == "credential_name":
        return bool(policy.credential_refs)
    if policy.credential_input_kind == "website_stored_credential":
        return bool(policy.login_page_urls)
    return False


def _prioritize_credential_clarification(policy: RequestPolicy) -> None:
    if policy.credential_input_kind not in ("credential_id", "credential_name"):
        return
    if not policy.credential_refs:
        return
    if policy.clarification_reason not in _REASONS_OVERRIDDEN_BY_CREDENTIAL_REFS:
        return
    policy.clarification_reason = "credential_name_unresolved"


def _previous_credential_clarification_was_asked(global_llm_context: str) -> bool:
    structured = StructuredContext.from_json_str(global_llm_context)
    return any(
        decision.startswith(_CLARIFICATION_DECISION_PREFIX) and "/credential_name_unresolved" in decision
        for decision in structured.decisions_made
    )


def _last_assistant_message_was_saved_credential_question(
    chat_history: list[WorkflowCopilotChatHistoryMessage],
) -> bool:
    for message in reversed(chat_history):
        if message.sender == WorkflowCopilotChatSender.AI:
            return _SAVED_CREDENTIAL_NAME_QUESTION_STABLE_PREFIX in message.content
    return False


def _can_defer_unresolved_credential_name_for_draft(
    policy: RequestPolicy,
    *,
    global_llm_context: str,
) -> bool:
    if policy.clarification_reason != "credential_name_unresolved":
        return False
    if _has_resolvable_credential_scope(policy):
        return True
    if _previous_credential_clarification_was_asked(global_llm_context):
        return True
    return False


def _should_defer_repeated_unresolved_credential_question(
    policy: RequestPolicy,
    *,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
) -> bool:
    return (
        policy.credential_input_kind in ("none", "credential_name")
        and policy.clarification_reason == "credential_name_unresolved"
        and not _has_resolvable_credential_scope(policy)
        and _last_assistant_message_was_saved_credential_question(chat_history)
    )


async def _resolve_credentials(policy: RequestPolicy, organization_id: str) -> None:
    if policy.credential_input_kind == "credential_id":
        ids = _clean_list([ref for ref in policy.credential_refs if ref.startswith("cred_")])
        if not ids:
            return
        existing = await app.DATABASE.credentials.get_credentials_by_ids(ids, organization_id=organization_id)
        found = {credential.credential_id for credential in existing}
        policy.resolved_credentials = existing
        policy.invalid_credential_ids = [credential_id for credential_id in ids if credential_id not in found]
        if policy.invalid_credential_ids and policy.testing_intent != "skip_test":
            formatted = ", ".join(f"`{credential_id}`" for credential_id in policy.invalid_credential_ids)
            _block(
                policy,
                f"The credential ID(s) {formatted} were not found in this organization. Please provide a valid saved credential ID or explicitly ask for an unvalidated draft that will not be run yet.",
                reason="credential_name_unresolved",
            )
        elif policy.invalid_credential_ids:
            policy.allow_run_blocks = False
            policy.allow_missing_credentials_in_draft = True
        return

    if policy.credential_input_kind == "credential_name" and not policy.credential_refs:
        if policy.allow_missing_credentials_in_draft:
            policy.allow_run_blocks = False
            return
        _block(
            policy,
            _SAVED_CREDENTIAL_NAME_QUESTION,
            reason="credential_name_unresolved",
        )
        return
    if policy.credential_input_kind == "website_stored_credential" and not policy.login_page_urls:
        _block(
            policy,
            _STORED_CREDENTIAL_URL_QUESTION,
            reason="missing_target_context",
        )
        return
    if policy.credential_input_kind not in ("credential_name", "website_stored_credential"):
        return

    credentials = await _load_credentials(organization_id)
    if policy.credential_input_kind == "credential_name":
        for ref in policy.credential_refs:
            matches = [credential for credential in credentials if credential.name == ref]
            if len(matches) == 1:
                policy.resolved_credentials.append(matches[0])
            elif matches:
                _block(
                    policy,
                    "I found multiple stored credentials with that exact name. Which one should I use?",
                    matches,
                    reason="credential_name_unresolved",
                )
                return
            elif policy.testing_intent == "skip_test":
                policy.allow_run_blocks, policy.allow_missing_credentials_in_draft = False, True
            else:
                _block(
                    policy,
                    f"I could not find a stored credential named `{ref}`. Please choose an existing credential by exact name or a credential ID beginning with cred_.",
                    reason="credential_name_unresolved",
                )
                return
        return

    matches = _match_by_url(credentials, policy.login_page_urls)
    if len(matches) == 1:
        policy.resolved_credentials = matches
    elif matches:
        _block(
            policy,
            "I found multiple stored credentials for that login page. Which one should I use?",
            matches,
            reason="credential_name_unresolved",
        )
    else:
        _block(
            policy,
            "I could not find a stored credential for that login page. Please select a saved credential by exact name or a credential ID beginning with cred_, or create one in the Credentials UI.",
            reason="credential_name_unresolved",
        )


def _is_login_credential_param(param: dict[str, Any]) -> bool:
    raw = param.get("parameter_type")
    if not isinstance(raw, str):
        return False
    try:
        return ParameterType(raw).is_login_credential()
    except (ValueError, TypeError):
        return False


def _iter_login_credential_params(parsed: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    workflow_def = parsed.get("workflow_definition")
    if not isinstance(workflow_def, dict):
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for param in workflow_def.get("parameters") or []:
        if isinstance(param, dict) and _is_login_credential_param(param):
            out.append(("workflow", param))

    def _walk(block_list: Any) -> None:
        if not isinstance(block_list, list):
            return
        for block in block_list:
            if not isinstance(block, dict):
                continue
            label = str(block.get("label") or "<unlabeled>")
            for param in block.get("parameters") or []:
                if isinstance(param, dict) and _is_login_credential_param(param):
                    out.append((label, param))
            _walk(block.get("loop_blocks"))

    _walk(workflow_def.get("blocks"))
    return out


def _value_resolves_at_init(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _workflow_credential_inputs_unbound(workflow_yaml: str) -> list[dict[str, str]]:
    if not workflow_yaml:
        return []
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except Exception:
        return []
    if not isinstance(parsed, dict):
        return []

    workflow_def = parsed.get("workflow_definition")
    if not isinstance(workflow_def, dict):
        return []

    workflow_param_bound: dict[str, bool] = {}
    for param in workflow_def.get("parameters") or []:
        if isinstance(param, dict) and param.get("parameter_type") == "workflow":
            key = param.get("key")
            if isinstance(key, str):
                workflow_param_bound[key] = _value_resolves_at_init(param.get("default_value"))

    findings: list[dict[str, str]] = []
    for location, param in _iter_login_credential_params(parsed):
        for field_name in _LOGIN_CREDENTIAL_REQUIRED_KEY_FIELDS:
            value = param.get(field_name)
            if not isinstance(value, str) or value.strip():
                continue
            findings.append(
                {"location": location, "field": field_name, "missing": "<empty>", "kind": "credential_empty"}
            )
        for field_name, value in param.items():
            if field_name in _CREDENTIAL_PARAM_METADATA_FIELDS:
                continue
            if not isinstance(value, str):
                continue
            for jinja_key in _JINJA_TEMPLATE_VAR_RE.findall(value):
                if jinja_key in workflow_param_bound:
                    if not workflow_param_bound[jinja_key]:
                        findings.append(
                            {
                                "location": location,
                                "field": field_name,
                                "missing": jinja_key,
                                "kind": "credential_template_unbound",
                            }
                        )
                else:
                    findings.append(
                        {
                            "location": location,
                            "field": field_name,
                            "missing": jinja_key,
                            "kind": "credential_template_undefined",
                        }
                    )
    return findings


async def build_request_policy(
    *,
    user_message: str,
    workflow_yaml: str,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    global_llm_context: str,
    organization_id: str,
    handler: Any,
) -> RequestPolicy:
    policy = await _classify_request(user_message, workflow_yaml, chat_history, global_llm_context, handler)
    policy.raw_secret_detected = policy.raw_secret_detected or policy.credential_input_kind == "raw_secret"
    _prioritize_credential_clarification(policy)

    if policy.clarification_reason == "none" and not policy.raw_secret_detected:
        if _workflow_credential_inputs_unbound(workflow_yaml):
            policy.clarification_reason = "workflow_credential_inputs_unbound"
            policy.allow_run_blocks = False
            policy.allow_missing_credentials_in_draft = True
    if policy.testing_intent == "skip_test":
        policy.allow_run_blocks = False
        if (
            policy.credential_input_kind != "raw_secret"
            and policy.clarification_reason not in _PRE_RESOLUTION_CLARIFICATION_REASONS
        ):
            if (
                policy.clarification_reason == "credential_name_unresolved"
                and not _can_defer_unresolved_credential_name_for_draft(
                    policy,
                    global_llm_context=global_llm_context,
                )
            ):
                policy.requires_user_clarification = True
                policy.allow_update_workflow = False
            else:
                policy.requires_user_clarification = False
                policy.allow_missing_credentials_in_draft = True

    if _should_defer_repeated_unresolved_credential_question(
        policy,
        chat_history=chat_history,
    ):
        policy.requires_user_clarification = False
        policy.allow_run_blocks = False
        policy.allow_missing_credentials_in_draft = True

    if policy.raw_secret_detected:
        _block(
            policy,
            _RAW_SECRET_QUESTION,
            reason="raw_secret",
        )
    elif policy.requires_user_clarification and policy.clarification_reason in _PRE_RESOLUTION_CLARIFICATION_REASONS:
        _block(policy, _clarification_question(policy, global_llm_context))
    elif policy.requires_user_clarification and not _has_resolvable_credential_scope(policy):
        _block(policy, _clarification_question(policy, global_llm_context))
    else:
        try:
            # A resolvable credential scope can override the classifier's
            # conservative clarification flag; _resolve_credentials will block
            # again if the lookup is missing or ambiguous.
            policy.requires_user_clarification = False
            await _resolve_credentials(policy, organization_id)
        except Exception:
            LOG.warning(
                "request-policy credential resolution failed",
                organization_id=organization_id,
                credential_input_kind=policy.credential_input_kind,
                exc_info=True,
            )
            _block(
                policy,
                "I could not verify the requested credential metadata for this organization. Please provide a valid saved credential by exact name or a credential ID beginning with cred_.",
            )

    with copilot_span("request_policy", data=policy.to_trace_data()):
        LOG.info("request-policy decision", **policy.to_trace_data())
    return policy
