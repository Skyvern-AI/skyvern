"""Build-phase machinery for the copilot agent loop.

The phase machine separates build-time discovery from workflow composition.
TurnIntent.mode stays BUILD; phase is loop runtime state on ctx. The
deterministic orchestrator owns transitions — an agent-emitted phase signal
does not by itself unlock mutation authority.

Phases:
- INITIAL: BUILD turn with no known entrypoint URL. Discovery is available;
  mutation and direct browser primitives are gated off.
- DISCOVERING: discover_workflow_entrypoint is running. Same gate as INITIAL.
- COMPOSING: entrypoint resolved. Mutation tools and direct browser primitives
  are available; discovery is no longer available.
- TESTING: post-update; same authority as COMPOSING.
"""

from __future__ import annotations

import re
import time
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog
import yaml

from skyvern.utils.yaml_loader import safe_load_no_dates

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.context import CopilotContext
    from skyvern.forge.sdk.copilot.turn_intent import TurnIntent

LOG = structlog.get_logger()

# Mode values that explicitly rule out discovery. Other modes (BUILD,
# DRAFT_ONLY, UNKNOWN) may enter INITIAL phase when no URL signal exists —
# the TurnIntent keyword classifier gates NEW_BROWSER_TASK_TERMS on
# `not has_workflow`, so a fresh build turn with an empty-blocks YAML can
# legitimately classify as UNKNOWN even though discovery is the right next
# step. The phase gate keeps mutation blocked until discovery returns a
# candidate (or the model ASK_QUESTIONs for a URL).
_PHASE_NON_BUILD_MODE_VALUES: frozenset[str] = frozenset({"edit", "diagnose", "docs_answer", "clarify", "refuse"})


class BuildPhase(StrEnum):
    INITIAL = "initial"
    DISCOVERING = "discovering"
    COMPOSING = "composing"
    TESTING = "testing"


DISCOVERY_PERMITTED_PHASES: frozenset[BuildPhase] = frozenset({BuildPhase.INITIAL, BuildPhase.DISCOVERING})
MUTATION_PERMITTED_PHASES: frozenset[BuildPhase] = frozenset({BuildPhase.COMPOSING, BuildPhase.TESTING})

# Tools whose call paths must be gated by phase. Names match the
# function_tool name_override / MCP overlay names registered elsewhere.
_BROWSER_PRIMITIVE_TOOLS: frozenset[str] = frozenset(
    {
        "navigate_browser",
        "evaluate",
        "click",
        "type_text",
        "scroll",
        "select_option",
        "press_key",
        "console_messages",
        "get_browser_screenshot",
    }
)
_MUTATION_TOOLS: frozenset[str] = frozenset(
    {"update_workflow", "update_and_run_blocks", "run_blocks_and_collect_debug"}
)
_DISCOVERY_TOOLS: frozenset[str] = frozenset({"discover_workflow_entrypoint"})

_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"\)]+", re.IGNORECASE)


def _yaml_has_target_url(workflow_yaml: str | None) -> bool:
    """True when the YAML carries a goto_url or navigation block with a non-empty url."""
    if not workflow_yaml:
        return False
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except yaml.YAMLError:
        return False
    if not isinstance(parsed, dict):
        return False
    definition = parsed.get("workflow_definition")
    if not isinstance(definition, dict):
        return False
    blocks = definition.get("blocks")
    if not isinstance(blocks, list):
        return False
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("block_type")
        if block_type not in {"goto_url", "navigation"}:
            continue
        url = block.get("url")
        if isinstance(url, str) and url.strip():
            return True
    return False


def initial_build_phase(
    turn_intent: TurnIntent | None,
    user_message: str,
    agent_user_message: str,
    workflow_yaml: str | None,
) -> BuildPhase:
    """Decide the initial build phase for this turn.

    Returns COMPOSING (sentinel — harmless because the existing TurnIntent gate
    already blocks mutation for non-BUILD modes) when the mode is one of the
    explicitly-non-build values. For BUILD / DRAFT_ONLY / UNKNOWN, returns
    COMPOSING when this turn already has a URL signal, else INITIAL.

    UNKNOWN deliberately enters INITIAL when no URL is present: a fresh chat
    whose latest message dodges every keyword heuristic (e.g. "go to X" with
    an empty-blocks scaffold workflow that flips `has_workflow=True` and
    suppresses NEW_BROWSER_TASK_TERMS) should still get discovery — the
    alternative is to let mutation through on an ambiguous turn, which is
    worse than asking discovery to resolve or falling through to ASK_QUESTION.

    Signals considered (this-turn only — never prior visited URLs or chat
    history, which can leak stale URLs across unrelated turns):
    - The raw latest user message.
    - The rewritten agent input (request-policy may carry the prior request).
    - The current workflow YAML's first goto_url/navigation block.
    """
    mode_value = getattr(getattr(turn_intent, "mode", None), "value", None)
    if mode_value in _PHASE_NON_BUILD_MODE_VALUES:
        return BuildPhase.COMPOSING
    if _URL_IN_TEXT_RE.search(user_message or ""):
        return BuildPhase.COMPOSING
    if _URL_IN_TEXT_RE.search(agent_user_message or ""):
        return BuildPhase.COMPOSING
    if _yaml_has_target_url(workflow_yaml):
        return BuildPhase.COMPOSING
    return BuildPhase.INITIAL


def _log_transition(ctx: CopilotContext, *, prev: BuildPhase, new: BuildPhase, reason: str) -> None:
    LOG.info(
        "copilot.build_phase_transition",
        prev_phase=prev.value,
        new_phase=new.value,
        transition_reason=reason,
        workflow_permanent_id=getattr(ctx, "workflow_permanent_id", None),
    )


def advance_to_discovering(ctx: CopilotContext) -> None:
    if ctx.build_phase != BuildPhase.INITIAL:
        raise ValueError(f"advance_to_discovering called from {ctx.build_phase.value}; expected INITIAL")
    if ctx.discovery_started_monotonic is None:
        ctx.discovery_started_monotonic = time.monotonic()
    prev = ctx.build_phase
    ctx.build_phase = BuildPhase.DISCOVERING
    _log_transition(ctx, prev=prev, new=ctx.build_phase, reason="discovery_started")


def advance_to_composing(ctx: CopilotContext, *, reason: str) -> None:
    if ctx.build_phase not in (BuildPhase.INITIAL, BuildPhase.DISCOVERING):
        raise ValueError(f"advance_to_composing called from {ctx.build_phase.value}; expected INITIAL or DISCOVERING")
    prev = ctx.build_phase
    ctx.build_phase = BuildPhase.COMPOSING
    _log_transition(ctx, prev=prev, new=ctx.build_phase, reason=reason)


def advance_to_testing(ctx: CopilotContext) -> None:
    if ctx.build_phase == BuildPhase.TESTING:
        # Already in TESTING (or post-TESTING composer follow-up) is benign.
        return
    if ctx.build_phase != BuildPhase.COMPOSING:
        raise ValueError(f"advance_to_testing called from {ctx.build_phase.value}; expected COMPOSING or TESTING")
    prev = ctx.build_phase
    ctx.build_phase = BuildPhase.TESTING
    _log_transition(ctx, prev=prev, new=ctx.build_phase, reason="update_workflow_succeeded")


def _phase_tool_error(ctx: Any, tool_name: str) -> str | None:
    """Shared phase-aware authority error. Returns the steering error string
    when the (phase, tool) pair is forbidden; None otherwise.

    Called from both the native gate (`_authority_tool_error` in tools.py)
    and the MCP-adapter gate (`SkyvernOverlayMCPServer.call_tool` pre-pre-hook).
    Errors flow back to the LLM as next-step steering, per the ai-agents
    'errors as steering' guidance.
    """
    phase = getattr(ctx, "build_phase", None)
    if not isinstance(phase, BuildPhase):
        return None

    in_discovery = phase in DISCOVERY_PERMITTED_PHASES
    in_mutation = phase in MUTATION_PERMITTED_PHASES

    if tool_name in _DISCOVERY_TOOLS and in_mutation:
        return (
            "discover_workflow_entrypoint is only available before composition. "
            "The workflow already has a target URL — proceed with update_workflow or update_and_run_blocks. "
            "safe_reason_code=build_phase_discovery_disallowed_post_compose."
        )

    if tool_name in _BROWSER_PRIMITIVE_TOOLS and in_discovery:
        return (
            "Direct browser tools are not callable before composition. "
            "Call discover_workflow_entrypoint to resolve the entrypoint URL, or ASK_QUESTION for a URL. "
            "safe_reason_code=build_phase_browser_blocked_pre_compose."
        )

    if tool_name in _MUTATION_TOOLS and in_discovery:
        return (
            "Workflow mutation is gated to composition. "
            "Call discover_workflow_entrypoint to resolve the entrypoint URL, or ASK_QUESTION for a URL first. "
            "safe_reason_code=build_phase_mutation_blocked_pre_compose."
        )

    return None
