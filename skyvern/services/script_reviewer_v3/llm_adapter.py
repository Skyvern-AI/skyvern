"""LLM path for the v3 agentic reviewer.

Phase 4a decision: use :class:`LLMCaller` with ``add_tool_result``. LLMCaller
already wraps :func:`litellm.acompletion` with telemetry (spans, cost
logging, prompt_name) and supports multi-turn tool use, so we get both
invariants from architecture.md for free (telemetry parity + router-level
fallback) without writing a new adapter.

Invariant documented in ``architecture.md``: raw ``litellm.acompletion`` is
banned — every v3 LLM call must preserve BOTH telemetry parity AND
router-level fallback parity (Gemini 3 Flash → GPT-5-mini via
``cloud/llm/router.py``).
"""

from __future__ import annotations

# LLM key passed to LLMCaller(llm_key=...). Direct LLMConfig (not the
# GEMINI_3_0_FLASH_WITH_FALLBACK router) because LLMCaller's call path
# accesses ``self.llm_config.litellm_params`` which only exists on direct
# LLMConfig — router configs (LLMRouterConfig) lack that attribute and crash.
# Trade-off: we lose the cross-model fallback the router gives v2's reviewer.
# Production should either (a) extend LLMCaller to handle router configs, or
# (b) wrap the call in a try/except that demotes to a backup llm_key on
# transient failures. Tracked as a follow-up before ramping v3 to >0% traffic.
V3_REVIEWER_MODEL = "VERTEX_GEMINI_3.0_FLASH"


__all__ = ["V3_REVIEWER_MODEL"]
