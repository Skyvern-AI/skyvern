"""Preflight feasibility classifier for the workflow copilot.

Runs a single cheap LLM call before entering the main agent loop. If the
request looks obviously mismatched to the target site (e.g. asking a
sports-league site for regulatory filings), returns a clarifying question
so the copilot can bypass a ~7 minute browser-driven dead end.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Literal

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.output_utils import parse_final_response
from skyvern.forge.sdk.experimentation.llm_prompt_config import get_llm_handler_for_prompt_type
from skyvern.utils.strings import escape_code_fences

LOG = structlog.get_logger()

PROMPT_TEMPLATE_NAME = "feasibility-gate"
_WORKFLOW_YAML_MAX_CHARS = 2048


@dataclass(frozen=True)
class FeasibilityVerdict:
    verdict: Literal["proceed", "ask_clarification"]
    question: str | None = None
    rationale: str | None = None


_PROCEED = FeasibilityVerdict(verdict="proceed")


def _coerce_verdict(raw: Any) -> FeasibilityVerdict:
    """Normalize an LLM response into a FeasibilityVerdict.

    The expected shape is a dict matching the prompt schema; anything else
    is treated as classifier noise and falls through to _PROCEED. The str
    branch is a safety net for handlers that return a JSON-encoded string
    instead of a dict.
    """
    if isinstance(raw, str):
        raw = parse_final_response(raw)
    if not isinstance(raw, dict):
        return _PROCEED

    verdict_str = raw.get("verdict")
    if verdict_str not in ("proceed", "ask_clarification"):
        return _PROCEED

    if verdict_str == "proceed":
        rationale = raw.get("rationale")
        return FeasibilityVerdict(
            verdict="proceed",
            rationale=rationale if isinstance(rationale, str) else None,
        )

    question = raw.get("question")
    if not isinstance(question, str) or not question.strip():
        # ask_clarification without a question is malformed -- fall back to
        # proceed. Log at WARNING so a systematically misaligned prompt
        # producing empty questions is visible rather than silently
        # degrading the gate into a no-op classifier.
        LOG.warning("feasibility-gate ask_clarification verdict missing question, falling back to proceed")
        return _PROCEED

    rationale = raw.get("rationale")
    return FeasibilityVerdict(
        verdict="ask_clarification",
        question=question.strip(),
        rationale=rationale if isinstance(rationale, str) else None,
    )


async def run_feasibility_gate(
    user_message: str,
    workflow_yaml: str,
    chat_history: str,
    global_llm_context: str,
    distinct_id: str,
    organization_id: str | None,
) -> FeasibilityVerdict:
    """Classify the user's request. Returns FeasibilityVerdict; never raises.

    Any exception, timeout, or malformed response falls through to
    FeasibilityVerdict(verdict="proceed") so the main copilot loop runs.
    """
    if not isinstance(user_message, str) or not user_message.strip():
        return _PROCEED

    # Cap the workflow YAML so the "cheap preflight" call stays cheap. A large
    # workflow is not useful context for a feasibility decision -- we just need
    # enough to see whether the user's ask lines up with the current state.
    truncated_workflow_yaml = (workflow_yaml or "")[:_WORKFLOW_YAML_MAX_CHARS]

    try:
        prompt = prompt_engine.load_prompt(
            template=PROMPT_TEMPLATE_NAME,
            user_message=escape_code_fences(user_message),
            workflow_yaml=escape_code_fences(truncated_workflow_yaml),
            chat_history=escape_code_fences(chat_history or ""),
            global_llm_context=escape_code_fences(global_llm_context or ""),
        )
    except Exception as exc:
        LOG.warning("feasibility-gate prompt render failed, proceeding to main loop", error=str(exc))
        return _PROCEED

    try:
        handler = await get_llm_handler_for_prompt_type(PROMPT_TEMPLATE_NAME, distinct_id, organization_id)
    except Exception as exc:
        # Touches app.EXPERIMENTATION_PROVIDER; AppHolder raises RuntimeError
        # pre-startup and the provider can fail on network/payload errors.
        LOG.warning("feasibility-gate handler lookup failed, falling back", error=str(exc))
        handler = None
    if handler is None:
        # Use direct attribute access (not getattr-with-default) so the
        # intent of the except clause is unambiguous: the three-arg getattr
        # default only suppresses AttributeError, which looks like it
        # handles the AppHolder.__getattr__ RuntimeError but does not.
        # Catch both explicitly: RuntimeError (AppHolder pre-startup) and
        # AttributeError (app is some other object, e.g. in tests).
        try:
            handler = app.SECONDARY_LLM_API_HANDLER
        except (RuntimeError, AttributeError):
            handler = None
    if handler is None:
        LOG.info("feasibility-gate has no LLM handler available, proceeding to main loop")
        return _PROCEED

    try:
        response: Any = await asyncio.wait_for(
            handler(prompt=prompt, prompt_name=PROMPT_TEMPLATE_NAME),
            timeout=settings.COPILOT_FEASIBILITY_GATE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        LOG.warning(
            "feasibility-gate classifier timed out, proceeding to main loop",
            timeout=settings.COPILOT_FEASIBILITY_GATE_TIMEOUT_SECONDS,
        )
        return _PROCEED
    except Exception as exc:
        LOG.warning("feasibility-gate classifier failed, proceeding to main loop", error=str(exc))
        return _PROCEED

    # LLMAPIHandler Protocol's return type is dict[str, Any] | Any. With the
    # default force_dict=True every known adapter returns a dict, but an
    # adapter that bypasses that path (raw-HTTP provider, custom handler in
    # an experiment, future regression) can still return bytes. Normalize
    # here so the verdict survives instead of being dropped by
    # _coerce_verdict's fallthrough on an unknown shape.
    if isinstance(response, bytes):
        try:
            response = json.loads(response.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            LOG.warning("feasibility-gate failed to decode bytes response", error=str(exc))
            return _PROCEED

    verdict = _coerce_verdict(response)
    if verdict.verdict == "ask_clarification":
        # `question` is user-facing (displayed in the UI) so logging it at
        # INFO is fine. `rationale` is the LLM's internal reasoning and can
        # echo back user content under prompt injection -- drop it to DEBUG
        # so untrusted model output doesn't ship to every log aggregator.
        LOG.info(
            "feasibility-gate classifier asked for clarification",
            question=verdict.question,
        )
        LOG.debug("feasibility-gate clarification rationale", rationale=verdict.rationale)
    else:
        # Debug-level so latency regressions and skip-rate anomalies are
        # traceable without adding INFO noise on every copilot message.
        LOG.debug("feasibility-gate classifier returned proceed")
    return verdict
