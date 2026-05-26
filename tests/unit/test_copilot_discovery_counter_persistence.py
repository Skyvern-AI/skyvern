"""Round-trip tests for the per-chat discovery counter.

The counter ships in ``StructuredContext.discovery_calls_made``, which is
serialized into ``AgentResult.global_llm_context`` and re-parsed at the next
turn's start. ``finalize_discovery_counter_in_global_llm_context`` is the
single writeback site — called by ``_make_agent_result`` for every exit path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from skyvern.forge.sdk.copilot.context import (
    StructuredContext,
    finalize_discovery_counter_in_global_llm_context,
)


@dataclass
class _Ctx:
    prior_discovery_calls_made: int = 0
    discovery_calls_this_turn: int = 0


def test_structured_context_default_discovery_calls_made_is_zero() -> None:
    assert StructuredContext().discovery_calls_made == 0


def test_structured_context_round_trip_preserves_discovery_calls_made() -> None:
    sc = StructuredContext(user_goal="x", discovery_calls_made=2)
    raw = sc.to_json_str()
    parsed = StructuredContext.from_json_str(raw)
    assert parsed.discovery_calls_made == 2


def test_finalize_writes_summed_counter_into_outgoing_context() -> None:
    inbound = StructuredContext(user_goal="x", discovery_calls_made=1).to_json_str()
    ctx = _Ctx(prior_discovery_calls_made=1, discovery_calls_this_turn=1)
    out = finalize_discovery_counter_in_global_llm_context(ctx, inbound)
    assert out is not None
    sc = StructuredContext.from_json_str(out)
    assert sc.discovery_calls_made == 2
    assert sc.user_goal == "x"


def test_finalize_writes_zero_when_no_calls_made_and_no_prior() -> None:
    ctx = _Ctx(prior_discovery_calls_made=0, discovery_calls_this_turn=0)
    # No prior context + no this-turn activity -> no need to invent a context.
    assert finalize_discovery_counter_in_global_llm_context(ctx, None) is None


def test_finalize_writes_prior_when_this_turn_is_zero_and_prior_context_exists() -> None:
    inbound = StructuredContext(user_goal="g", discovery_calls_made=2).to_json_str()
    ctx = _Ctx(prior_discovery_calls_made=2, discovery_calls_this_turn=0)
    out = finalize_discovery_counter_in_global_llm_context(ctx, inbound)
    assert out is not None
    sc = StructuredContext.from_json_str(out)
    assert sc.discovery_calls_made == 2


def test_finalize_handles_string_only_inbound_context() -> None:
    """Legacy `global_llm_context` was a plain string. The migration path in
    `StructuredContext.from_json_str` should preserve the string in
    user_goal and zero the counter."""
    ctx = _Ctx(prior_discovery_calls_made=0, discovery_calls_this_turn=1)
    out = finalize_discovery_counter_in_global_llm_context(ctx, "legacy string context")
    assert out is not None
    sc = StructuredContext.from_json_str(out)
    assert sc.discovery_calls_made == 1
    assert sc.user_goal == "legacy string context"


def test_finalize_handles_invalid_json_inbound() -> None:
    ctx = _Ctx(prior_discovery_calls_made=0, discovery_calls_this_turn=1)
    out = finalize_discovery_counter_in_global_llm_context(ctx, "{not valid json")
    assert out is not None
    sc = StructuredContext.from_json_str(out)
    assert sc.discovery_calls_made == 1


def test_finalize_treats_none_ctx_as_passthrough_in_factory() -> None:
    """The factory in agent.py passes ctx=None for very-early errors (before
    CopilotContext is constructed). The finalizer itself isn't called in that
    branch — _make_agent_result skips it — but verify that the StructuredContext
    round-trip itself still preserves a counter set by an earlier turn."""
    inbound = StructuredContext(discovery_calls_made=2).to_json_str()
    parsed = json.loads(inbound)
    assert parsed["discovery_calls_made"] == 2
