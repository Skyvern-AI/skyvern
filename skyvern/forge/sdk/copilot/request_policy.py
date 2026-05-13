from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Literal, cast, get_args
from urllib.parse import urlparse

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.context import StructuredContext
from skyvern.forge.sdk.copilot.output_utils import parse_final_response
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.schemas.credentials import Credential
from skyvern.utils.strings import escape_code_fences

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
    "missing_conditional_condition",
    "missing_target_context",
]
_VALID_CLARIFICATION_REASONS: frozenset[ClarificationReason] = frozenset(get_args(ClarificationReason))
_PRE_RESOLUTION_CLARIFICATION_REASONS = {
    "credential_invention_requested",
    "ambiguous_loop_edit",
    "missing_conditional_condition",
    "missing_target_context",
}
_REASONS_OVERRIDDEN_BY_CREDENTIAL_REFS = {
    "ambiguous_loop_edit",
    "missing_conditional_condition",
    "missing_target_context",
}
_RAW_SECRET_QUESTION = (
    "Please do not paste raw login credentials or secrets in chat because they can enter model telemetry and execution traces. "
    "Store the credential in the Skyvern Credentials UI and reply with its exact saved credential name or a credential ID beginning with cred_. DO NOT PROVIDE RAW LOGIN/PASSWORD."
)
_SAVED_CREDENTIAL_NAME_QUESTION = "Which saved credential should I use? Please provide the exact credential name or a credential ID beginning with cred_."
_STORED_CREDENTIAL_URL_QUESTION = "Which website or login page should I use to look up the stored credential?"
_CREDENTIAL_ID_RE = re.compile(r"\bcred_[A-Za-z0-9][A-Za-z0-9_-]*\b")
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


def _clean_list(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _credential_ids(text: str) -> list[str]:
    return list(dict.fromkeys(_CREDENTIAL_ID_RE.findall(text or "")))


def _raw_secret_detected(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _RAW_SECRET_PATTERNS)


def _coerce_clarification_reason(value: Any) -> ClarificationReason:
    if value in _VALID_CLARIFICATION_REASONS:
        return cast(ClarificationReason, value)
    return "none"


def redact_raw_secrets_for_prompt(text: str) -> str:
    redacted = text or ""
    for pattern in _RAW_SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


_CHAT_HISTORY_TRUNCATION_MARKER = "\n...[earlier chat history truncated]...\n"


def _clip_chat_history_for_prompt(text: str, limit: int = 2048, head: int = 384) -> str:
    """Middle-truncate chat history so the most recent assistant turn survives.

    Plain `[:limit]` clips the tail in long conversations, hiding the latest
    assistant turn that the classifier's slot-fill rule depends on. The head
    boundary snaps back to the last newline so we do not cut a turn mid-line.
    """
    if len(text) <= limit:
        return text
    head_text = text[:head]
    snap = head_text.rfind("\n")
    if snap > head // 2:
        head_text = head_text[: snap + 1]
    tail = limit - len(head_text) - len(_CHAT_HISTORY_TRUNCATION_MARKER)
    if tail <= 0:
        return text[-limit:]
    return head_text + _CHAT_HISTORY_TRUNCATION_MARKER + text[-tail:]


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
    return policy


def _ground_completion_contract(user_message: str, value: str | None) -> str | None:
    if not value or not value.strip():
        return None

    contract = value.strip()
    message = user_message or ""
    if contract.lower() in message.lower():
        return contract

    return None


async def _classify_request(
    user_message: str, workflow_yaml: str, chat_history: str, global_llm_context: str, handler: Any
) -> RequestPolicy:
    ids = _credential_ids(user_message)
    if _raw_secret_detected(user_message):
        return RequestPolicy(
            credential_input_kind="raw_secret",
            credential_refs=ids,
            raw_secret_detected=True,
            clarification_reason="raw_secret",
        )
    if handler is None:
        return RequestPolicy(credential_input_kind="credential_id" if ids else "none", credential_refs=ids)

    # workflow_yaml and global_llm_context are head-truncated: the front matter
    # (title, parameters, first blocks) is what the classifier needs. chat_history
    # is middle-truncated because the slot-fill rule reads the latest assistant
    # turn at the tail.
    prompt = prompt_engine.load_prompt(
        template=PROMPT_NAME,
        user_message=escape_code_fences(user_message),
        workflow_yaml=escape_code_fences(redact_raw_secrets_for_prompt(workflow_yaml)[:2048]),
        chat_history=escape_code_fences(_clip_chat_history_for_prompt(redact_raw_secrets_for_prompt(chat_history))),
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


def _clarification_question(policy: RequestPolicy) -> str:
    if policy.clarification_reason == "raw_secret":
        return _RAW_SECRET_QUESTION
    if policy.clarification_reason == "credential_name_unresolved":
        if policy.credential_input_kind == "website_stored_credential":
            return _STORED_CREDENTIAL_URL_QUESTION
        return _SAVED_CREDENTIAL_NAME_QUESTION
    if policy.clarification_reason == "credential_invention_requested":
        return (
            "I cannot invent a credential ID. Please provide a valid saved credential ID, "
            "select an existing credential, or create one in the Credentials UI."
        )
    if policy.clarification_reason == "ambiguous_loop_edit":
        return "Which block or blocks should go inside the loop, and what should the loop iterate over or stop on?"
    if policy.clarification_reason == "missing_conditional_condition":
        return "What condition should trigger this conditional route?"
    if policy.clarification_reason == "missing_target_context":
        if policy.credential_input_kind == "website_stored_credential":
            return _STORED_CREDENTIAL_URL_QUESTION
        return "Which page or URL should the workflow go to?"
    if policy.credential_input_kind == "credential_name":
        return _SAVED_CREDENTIAL_NAME_QUESTION
    if policy.credential_input_kind == "website_stored_credential":
        return _STORED_CREDENTIAL_URL_QUESTION
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
        decision.startswith("request-policy clarification required:") and "/credential_name_unresolved" in decision
        for decision in structured.decisions_made
    )


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
        if policy.testing_intent == "skip_test" and policy.allow_missing_credentials_in_draft:
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


async def build_request_policy(
    *,
    user_message: str,
    workflow_yaml: str,
    chat_history: str,
    global_llm_context: str,
    organization_id: str,
    handler: Any,
) -> RequestPolicy:
    policy = await _classify_request(user_message, workflow_yaml, chat_history, global_llm_context, handler)
    policy.raw_secret_detected = policy.raw_secret_detected or policy.credential_input_kind == "raw_secret"
    _prioritize_credential_clarification(policy)
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

    if policy.raw_secret_detected:
        _block(
            policy,
            _RAW_SECRET_QUESTION,
            reason="raw_secret",
        )
    elif policy.requires_user_clarification and policy.clarification_reason in _PRE_RESOLUTION_CLARIFICATION_REASONS:
        _block(policy, _clarification_question(policy))
    elif policy.requires_user_clarification and not _has_resolvable_credential_scope(policy):
        _block(policy, _clarification_question(policy))
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
