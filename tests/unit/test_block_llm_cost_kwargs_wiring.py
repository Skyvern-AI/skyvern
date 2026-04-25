"""AST guard: every awaited LLM handler call inside the six
block-scoped methods below must pass both `workflow_run_block_id=`
and `organization_id=`. Catches call-site wiring regressions.
"""

from __future__ import annotations

import ast
import pathlib

BLOCK_PY = (
    pathlib.Path(__file__).resolve().parents[2] / "skyvern" / "forge" / "sdk" / "workflow" / "models" / "block.py"
)

# Methods that make block-scoped LLM calls. Each must pass
# workflow_run_block_id= and organization_id= to any LLM handler call
# in its body so the handler can associate the call with the block.
BLOCK_ATTRIBUTED_METHODS = {
    "_generate_workflow_run_block_description",  # BaseBlock description gen
    "send_prompt",  # TextPromptBlock
    "_parse_pdf_file",  # FileParserBlock PDF vision
    "_parse_image_file",  # FileParserBlock image OCR
    "_extract_with_ai",  # FileParserBlock schema extract
    "execute",  # PDFParserBlock (deprecated) execute
}

# Names that indicate an LLM handler call (both module-level handlers and
# locally-bound `llm_api_handler` from LLMAPIHandlerFactory.get_override_llm_api_handler).
LLM_HANDLER_NAME_FRAGMENTS = ("LLM_API_HANDLER", "llm_api_handler")


def _is_llm_handler_call(call: ast.Call) -> bool:
    """True if the call looks like `await (whatever).LLM_API_HANDLER(...)` or
    `await llm_api_handler(...)`."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return any(frag in func.attr for frag in LLM_HANDLER_NAME_FRAGMENTS)
    if isinstance(func, ast.Name):
        return any(frag in func.id for frag in LLM_HANDLER_NAME_FRAGMENTS)
    return False


def _find_llm_calls_in_method(method: ast.AsyncFunctionDef) -> list[ast.Call]:
    """Collect LLM handler INVOCATIONS (awaited calls) only — not factory
    lookups like `LLMAPIHandlerFactory.get_override_llm_api_handler(...)`
    which happen to contain `llm_api_handler` in the name but aren't the
    thing that makes an LLM request."""
    calls: list[ast.Call] = []
    for node in ast.walk(method):
        if not isinstance(node, ast.Await):
            continue
        inner = node.value
        if isinstance(inner, ast.Call) and _is_llm_handler_call(inner):
            calls.append(inner)
    return calls


def _kwarg_names(call: ast.Call) -> set[str]:
    return {kw.arg for kw in call.keywords if kw.arg is not None}


def test_every_block_scoped_llm_call_passes_both_cost_attribution_kwargs() -> None:
    tree = ast.parse(BLOCK_PY.read_text())

    offenders: list[str] = []
    methods_with_calls: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name not in BLOCK_ATTRIBUTED_METHODS:
            continue

        for call in _find_llm_calls_in_method(node):
            methods_with_calls.add(node.name)
            kwargs = _kwarg_names(call)
            missing = {"workflow_run_block_id", "organization_id"} - kwargs
            if missing:
                offenders.append(
                    f"{node.name} @ line {call.lineno}: missing {sorted(missing)} "
                    f"on LLM handler call (kwargs present: {sorted(kwargs)})"
                )

    # Sanity: every method in BLOCK_ATTRIBUTED_METHODS must have at least one
    # awaited LLM call. If our name-fragment matcher misses (e.g. a handler
    # rename), the kwargs check would silently pass with zero calls inspected.
    assert methods_with_calls == BLOCK_ATTRIBUTED_METHODS, (
        f"Expected every method in BLOCK_ATTRIBUTED_METHODS to contain at least one "
        f"awaited LLM handler call. Methods with no calls found: "
        f"{sorted(BLOCK_ATTRIBUTED_METHODS - methods_with_calls)}. "
        f"Either the matcher needs a new name fragment, or the method no longer "
        f"makes a block-scoped LLM call (remove from BLOCK_ATTRIBUTED_METHODS)."
    )

    assert not offenders, (
        "Block-scoped LLM calls are missing cost-attribution kwargs:\n  "
        + "\n  ".join(offenders)
        + "\n\nEvery LLM handler call inside these methods must pass both "
        "`workflow_run_block_id=` and `organization_id=` to correctly attribute "
        "cost to `workflow_run_blocks.llm_cost`."
    )
