"""Tests for the context.parameters reference guard (SKY-8965 Phase 1)."""

from __future__ import annotations

import pytest

from skyvern.core.script_generations.generate_script import (
    _collect_declared_param_keys,
    _collect_upstream_schema_keys,
)
from skyvern.core.script_generations.parameter_reference_guard import (
    HallucinatedParameterError,
    log_or_raise_guard_result,
    validate_context_parameter_refs,
)

SCRIPT_WITH_TWO_REFS = """
async def block_fn(page, context):
    await page.fill(value=context.parameters['search_term'])
    await page.fill(value=context.parameters['other_key'])
"""

SCRIPT_WITH_PHANTOM = """
async def block_fn(page, context):
    await page.fill(value=context.parameters['preprint_search_term'])
"""

SCRIPT_WITH_COMMENT = """
# comment reference: context.parameters['ignored']
await page.fill(value=context.parameters['real'])
"""


def test_guard_passes_when_all_refs_declared() -> None:
    result = validate_context_parameter_refs(
        code=SCRIPT_WITH_TWO_REFS,
        declared_param_keys=frozenset({"search_term", "other_key"}),
        upstream_schema_keys=frozenset(),
        synthesized_keys=frozenset(),
    )
    assert result.valid
    assert result.undeclared_refs == []


def test_guard_accepts_via_synthesized_keys() -> None:
    """Phase 1: synthesized `GeneratedWorkflowParameters` fields count as valid."""
    result = validate_context_parameter_refs(
        code=SCRIPT_WITH_PHANTOM,
        declared_param_keys=frozenset(),
        upstream_schema_keys=frozenset(),
        synthesized_keys=frozenset({"preprint_search_term"}),
    )
    assert result.valid


def test_guard_accepts_via_upstream_schema() -> None:
    code = "value=context.parameters['invoice_date']"
    result = validate_context_parameter_refs(
        code=code,
        declared_param_keys=frozenset(),
        upstream_schema_keys=frozenset({"invoice_date"}),
        synthesized_keys=frozenset(),
    )
    assert result.valid


def test_guard_detects_phantom_param_when_nothing_covers_it() -> None:
    result = validate_context_parameter_refs(
        code=SCRIPT_WITH_PHANTOM,
        declared_param_keys=frozenset(),
        upstream_schema_keys=frozenset(),
        synthesized_keys=frozenset(),
    )
    assert not result.valid
    assert len(result.undeclared_refs) == 1
    assert result.undeclared_refs[0].key == "preprint_search_term"


def test_guard_skips_references_in_comments() -> None:
    result = validate_context_parameter_refs(
        code=SCRIPT_WITH_COMMENT,
        declared_param_keys=frozenset({"real"}),
        upstream_schema_keys=frozenset(),
        synthesized_keys=frozenset(),
    )
    assert result.valid, "commented-out reference must not count"


def test_guard_collects_multiple_undeclared_refs() -> None:
    code = """
    await page.fill(value=context.parameters['a'])
    await page.fill(value=context.parameters['b'])
    await page.fill(value=context.parameters['c'])
    """
    result = validate_context_parameter_refs(
        code=code,
        declared_param_keys=frozenset({"a"}),
        upstream_schema_keys=frozenset({"b"}),
        synthesized_keys=frozenset(),
    )
    assert not result.valid
    assert [r.key for r in result.undeclared_refs] == ["c"]


def test_guard_catches_get_access_form() -> None:
    """Regex matches both subscript and `.get()` access patterns."""
    code = """
    a = context.parameters.get('ok_key')
    b = context.parameters.get("with_default", "fallback")
    c = context.parameters['also_ok']
    d = context.parameters.get('phantom')
    """
    result = validate_context_parameter_refs(
        code=code,
        declared_param_keys=frozenset({"ok_key", "with_default", "also_ok"}),
        upstream_schema_keys=frozenset(),
        synthesized_keys=frozenset(),
    )
    assert not result.valid
    assert [r.key for r in result.undeclared_refs] == ["phantom"]


def test_guard_handles_double_and_single_quotes() -> None:
    code = """
    x = context.parameters["double"]
    y = context.parameters['single']
    """
    result = validate_context_parameter_refs(
        code=code,
        declared_param_keys=frozenset({"double", "single"}),
        upstream_schema_keys=frozenset(),
        synthesized_keys=frozenset(),
    )
    assert result.valid


def test_format_error_contains_invalid_and_valid_keys() -> None:
    result = validate_context_parameter_refs(
        code="context.parameters['phantom']",
        declared_param_keys=frozenset({"real_key"}),
        upstream_schema_keys=frozenset(),
        synthesized_keys=frozenset(),
    )
    msg = result.format_error()
    assert "'phantom'" in msg
    assert "'real_key'" in msg
    assert "SKY-8965" in msg


# --- log_or_raise_guard_result --------------------------------------------


def test_log_or_raise_noop_on_valid_result() -> None:
    result = validate_context_parameter_refs(
        code="no refs here",
        declared_param_keys=frozenset(),
        upstream_schema_keys=frozenset(),
        synthesized_keys=frozenset(),
    )
    # Should not raise even with raise_on_violation=True
    log_or_raise_guard_result(result, raise_on_violation=True)


def test_log_or_raise_does_not_raise_phase_1() -> None:
    """Phase 1 behaviour: raise_on_violation=False → log only.

    The log assertion is verified manually via Datadog in production — structlog
    doesn't propagate to pytest's caplog without extra fixture setup, so we
    assert the no-raise contract here and rely on integration / production
    observability for the log payload.
    """
    result = validate_context_parameter_refs(
        code="context.parameters['phantom']",
        declared_param_keys=frozenset(),
        upstream_schema_keys=frozenset(),
        synthesized_keys=frozenset(),
    )
    # Does not raise
    log_or_raise_guard_result(
        result,
        raise_on_violation=False,
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
    )


def test_log_or_raise_raises_phase_2() -> None:
    """Phase 2 behaviour: raise_on_violation=True → throws HallucinatedParameterError."""
    result = validate_context_parameter_refs(
        code="context.parameters['phantom']",
        declared_param_keys=frozenset(),
        upstream_schema_keys=frozenset(),
        synthesized_keys=frozenset(),
    )
    with pytest.raises(HallucinatedParameterError) as exc_info:
        log_or_raise_guard_result(result, raise_on_violation=True)
    assert "'phantom'" in str(exc_info.value)
    assert exc_info.value.result is result


def test_collect_declared_params_includes_all_parameter_types() -> None:
    workflow = {
        "workflow_definition": {
            "parameters": [
                {"parameter_type": "workflow", "key": "search_term"},
                {"parameter_type": "output", "key": "extracted_date"},
                {"parameter_type": "context", "key": "loop_var"},
                {"parameter_type": "aws_secret", "key": "api_token"},
            ]
        }
    }
    keys = _collect_declared_param_keys(workflow)
    assert keys == frozenset({"search_term", "extracted_date", "loop_var", "api_token"})


def test_collect_declared_params_returns_empty_on_non_dict_definition() -> None:
    assert _collect_declared_param_keys({"workflow_definition": "not a dict"}) == frozenset()
    assert _collect_declared_param_keys({}) == frozenset()


def test_collect_upstream_schema_keys_parses_json_string_schema() -> None:
    blocks = [{"data_schema": '{"properties": {"invoice_date": {"type": "string"}, "total": {"type": "number"}}}'}]
    keys = _collect_upstream_schema_keys(blocks)
    assert keys == frozenset({"invoice_date", "total"})


def test_collect_upstream_schema_keys_ignores_invalid_json_string_schema() -> None:
    blocks = [{"data_schema": "this is not json"}]
    assert _collect_upstream_schema_keys(blocks) == frozenset()


def test_collect_upstream_schema_keys_recurses_into_loop_blocks() -> None:
    blocks = [
        {
            "block_type": "for_loop",
            "loop_blocks": [
                {
                    "data_schema": {"properties": {"nested_invoice_id": {"type": "string"}}},
                },
            ],
        },
        {"data_schema": {"properties": {"outer_key": {"type": "string"}}}},
    ]
    keys = _collect_upstream_schema_keys(blocks)
    assert keys == frozenset({"nested_invoice_id", "outer_key"})
