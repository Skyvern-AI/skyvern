"""Shared validators for generated cached-script code.

Both the script generator (`generate_script.py`) and the script reviewer
(`script_reviewer.py`) need to enforce the same code-quality rules. Keeping
the validators here avoids drift between the two paths.

Validators are AST-based to correctly distinguish kwargs from text inside
string literals (e.g. a `prompt='No selector= available'` must not look
like the call has a selector).
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass

INTERACTION_METHODS: frozenset[str] = frozenset({"click", "fill", "fill_autocomplete", "type", "select_option"})

PAGE_CALL_RE: re.Pattern[str] = re.compile(r"""\bpage\.(\w+)\s*\(""")


@dataclass(frozen=True)
class InteractionCall:
    method: str
    lineno: int
    has_selector: bool
    has_prompt: bool
    ai_value: str | None
    recoverable_marker_id: int | None = None
    sorted_kwarg_names: tuple[str, ...] = ()
    # Frozen tuple of (kwarg_name, ast_dump) pairs for kwargs whose value
    # we want to compare verbatim across input/output (prompt, value, intention).
    semantic_kwargs: tuple[tuple[str, str], ...] = ()


def iter_interaction_calls(code: str) -> list[InteractionCall]:
    """Walk `code` and yield each `await page.<interaction_method>(...)` call.

    Returns an empty list on parse failure rather than raising — the validator
    itself must never block codegen.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    out: list[InteractionCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "page"):
            continue
        method = func.attr
        if method not in INTERACTION_METHODS:
            continue
        has_selector = any(kw.arg == "selector" for kw in node.keywords)
        has_prompt = any(kw.arg == "prompt" for kw in node.keywords)
        ai_value: str | None = None
        marker_id: int | None = None
        for kw in node.keywords:
            if kw.arg == "ai" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                ai_value = kw.value.value
            elif (
                kw.arg == "recoverable_marker_id"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, int)
            ):
                marker_id = kw.value.value
        sorted_names = tuple(sorted(kw.arg for kw in node.keywords if kw.arg))
        # Capture verbatim ast.dump of EVERY kwarg so the safety validator
        # detects any semantic edit (prompt text, value source, timeout, data, etc.).
        semantic_kwargs = tuple(sorted((kw.arg, ast.dump(kw.value)) for kw in node.keywords if kw.arg))
        out.append(
            InteractionCall(
                method=method,
                lineno=node.lineno,
                has_selector=has_selector,
                has_prompt=has_prompt,
                ai_value=ai_value,
                recoverable_marker_id=marker_id,
                sorted_kwarg_names=sorted_names,
                semantic_kwargs=semantic_kwargs,
            )
        )
    return out


def validate_missing_selectors(code: str) -> str | None:
    """Flag interaction methods that lack a `selector=` argument.

    Two cases are flagged:
    1. `ai='fallback'` but no selector — the CSS-try block is skipped entirely
       and AI fires as the primary path on every run, burning LLM tokens
       silently. Worse, on some Playwright code paths a missing selector
       raises `Locator.fill: selector: expected string, got undefined`.
    2. No `ai=` argument at all and no selector — the call has no
       deterministic path and no explicit AI strategy.

    `ai='proactive'` without a selector is intentional (AI always generates
    the value, no caching benefit but no crash) and is NOT flagged.

    Returns an error message describing the offending sites, or None if clean.
    """
    issues: list[str] = []
    for call in iter_interaction_calls(code):
        if call.has_selector:
            continue
        if not call.has_prompt:
            issues.append(
                f"page.{call.method}() on line {call.lineno} (no selector= AND no prompt= — runtime will raise)"
            )
            continue
        if call.ai_value == "proactive":
            continue
        suffix = "" if call.ai_value else " (no ai= argument)"
        issues.append(f"page.{call.method}() on line {call.lineno}{suffix}")

    if not issues:
        return None

    return (
        f"Missing selector on interaction methods: {', '.join(issues[:5])}. "
        f"Interaction methods without a selector= argument have no deterministic path — "
        f"they silently invoke the LLM on every run, burning tokens with no fallback "
        f"episode created. Add a selector= argument with a stable CSS selector "
        f"(aria-label, placeholder, name, role, :has-text()) and set ai='fallback' "
        f"so the element is found without an LLM call."
    )


def validate_proactive_misuse(code: str) -> str | None:
    """Flag `ai='proactive'` on interaction methods that ALSO supply a `selector=`.

    `ai='proactive'` WITH `selector=` defeats caching — the LLM is always invoked
    even though a deterministic selector is available.

    Two intentional exceptions are NOT flagged:
    - `ai='proactive'` WITHOUT `selector=` (the SKY-9436 escape hatch).
    - `page.select_option(selector=..., ai='proactive')` without a `value=` — the
      generator emits this when a no-value select_option has a stable label-based
      selector. The selector locates the dropdown but the LLM must still pick the
      option text at runtime, so proactive is unavoidable here.

    Returns an error message or None if no issues found.
    """
    issues: list[str] = []
    for call in iter_interaction_calls(code):
        if call.ai_value != "proactive" or not call.has_selector:
            continue
        if call.method == "select_option" and "value" not in call.sorted_kwarg_names:
            continue
        issues.append(f"page.{call.method}() on line {call.lineno}")

    if not issues:
        return None

    return (
        f"ai='proactive' combined with selector= on interaction methods: {', '.join(issues[:5])}. "
        f"When a selector is present, ai='proactive' still ALWAYS invokes the LLM, defeating the "
        f"zero-LLM-cost goal of caching. Change to ai='fallback' — this tries the selector first "
        f"and only invokes the LLM if the selector fails. (Note: ai='proactive' WITHOUT selector= "
        f"is the documented escape hatch when no semantic selector is feasible — that is allowed.)"
    )


@dataclass(frozen=True)
class RecoverableProactiveCandidate:
    method: str
    lineno: int
    marker_id: int


def find_recoverable_proactive_candidates(code: str) -> list[RecoverableProactiveCandidate]:
    """Find selectorless `ai='proactive'` interaction calls that carry a
    `recoverable_marker_id` kwarg (SKY-9436 escape hatch).

    These are the calls the script reviewer's Rule 8f can upgrade to
    `selector=, ai='fallback'` when a recovery episode with the same marker_id
    is available. Marker presence is the sole disambiguator vs intentional
    selectorless proactive (essay/fuzzy/ambiguous cases).

    Per-method value-precondition for upgrade:
    - click: always safe.
    - fill / type: only when value= is present.

    Only click/fill/type are admitted — the runtime threads `recoverable_marker_id`
    only for these methods, so a marked select_option / hover / etc. could never
    have produced a recovery episode and must never have been emitted.

    The reviewer's prompt receives this list to focus its rewrites; not a hard
    error itself.
    """
    out: list[RecoverableProactiveCandidate] = []
    for call in iter_interaction_calls(code):
        if call.has_selector or call.ai_value != "proactive":
            continue
        if call.recoverable_marker_id is None:
            continue
        if call.method not in {"click", "fill", "type"}:
            continue
        if call.method != "click" and "value" not in call.sorted_kwarg_names:
            continue
        out.append(
            RecoverableProactiveCandidate(
                method=call.method,
                lineno=call.lineno,
                marker_id=call.recoverable_marker_id,
            )
        )
    return out


def validate_marker_kwarg_only_on_recoverable_proactive(code: str) -> str | None:
    """`recoverable_marker_id` is only valid on `ai='proactive'` interaction calls
    with no `selector=`. Any other shape means the marker leaked through a rewrite
    that should have removed it (Rule 8f) — the runtime forwards unknown kwargs
    to Playwright, which can crash or silently change behavior.

    Returns an error message or None if clean.
    """
    issues: list[str] = []
    for call in iter_interaction_calls(code):
        if call.recoverable_marker_id is None:
            continue
        if call.ai_value == "proactive" and not call.has_selector:
            continue
        issues.append(f"page.{call.method}() on line {call.lineno}")

    if not issues:
        return None

    return (
        f"`recoverable_marker_id` kwarg present on non-recoverable interaction calls: "
        f"{', '.join(issues[:5])}. The marker is valid only on `ai='proactive'` calls "
        f"WITHOUT a selector= (the SKY-9436 escape hatch). On any other shape, remove "
        f"the kwarg — Rule 8f explicitly says to drop it on upgrade to fallback+selector."
    )


def validate_unmarked_proactive_unchanged(input_code: str, output_code: str) -> str | None:
    """Hard safety check: any `ai='proactive'` call in `input_code` that lacks
    a `recoverable_marker_id` kwarg MUST be unchanged in `output_code`.

    Identity tuple covers structure AND value semantics: method, ai value,
    selector presence, marker presence, kwarg-name set, AND the verbatim AST
    of `prompt=`, `value=`, `intention=` kwargs. Detects both structural and
    semantic mutations. Multiplicity preserved via Counter so removing one
    of N identical calls is caught.

    Returns an error message or None if clean.
    """
    input_calls = iter_interaction_calls(input_code)
    output_calls = iter_interaction_calls(output_code)

    def _identity(c: InteractionCall) -> tuple:
        return (
            c.method,
            c.ai_value,
            c.has_selector,
            c.recoverable_marker_id is not None,
            c.sorted_kwarg_names,
            c.semantic_kwargs,
        )

    unmarked_proactive = [c for c in input_calls if c.ai_value == "proactive" and c.recoverable_marker_id is None]
    output_identity_counts = Counter(_identity(c) for c in output_calls)

    violations: list[str] = []
    for call in unmarked_proactive:
        ident = _identity(call)
        if output_identity_counts[ident] <= 0:
            violations.append(f"page.{call.method}() on line {call.lineno}")
        else:
            output_identity_counts[ident] -= 1

    if not violations:
        return None

    return (
        f"Reviewer modified unmarked ai='proactive' calls: {', '.join(violations[:5])}. "
        f"Selectorless proactive calls without `recoverable_marker_id` are intentional "
        f"(essay generation, fuzzy matching, ambiguous targets) and MUST be left unchanged "
        f"— including their prompt= and value= text. Only proactive calls with "
        f"`recoverable_marker_id` are eligible for upgrade (Rule 8f)."
    )
