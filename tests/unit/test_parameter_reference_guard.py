"""Tests for the context.parameters reference guard (SKY-8965 Phase 1)."""

from __future__ import annotations

import pytest

from skyvern.core.script_generations import generate_script as generate_script_module
from skyvern.core.script_generations.generate_script import (
    ScriptBlockSource,
    _collect_declared_param_keys,
    _collect_upstream_schema_keys,
    generate_workflow_script_python_code,
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


@pytest.mark.parametrize(
    ("code", "declared", "upstream", "synthesized"),
    [
        (SCRIPT_WITH_TWO_REFS, {"search_term", "other_key"}, set(), set()),
        (SCRIPT_WITH_PHANTOM, set(), set(), {"preprint_search_term"}),
        ("value=context.parameters['invoice_date']", set(), {"invoice_date"}, set()),
    ],
    ids=["declared", "synthesized", "upstream_schema"],
)
def test_guard_passes_when_every_ref_is_covered(
    code: str, declared: set[str], upstream: set[str], synthesized: set[str]
) -> None:
    result = validate_context_parameter_refs(
        code=code,
        declared_param_keys=frozenset(declared),
        upstream_schema_keys=frozenset(upstream),
        synthesized_keys=frozenset(synthesized),
    )
    assert result.valid
    assert result.undeclared_refs == []


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


CARRIED_FORWARD_FIELD = "what_is_the_full_name_of_the_person_to_search_for"

CACHED_BLOCK_CODE = f'''@skyvern.cached(cache_key="search_person")
async def search_person(page, context):
    await page.fill(
        selector="xpath=//input[@id='q']",
        value=context.parameters["{CARRIED_FORWARD_FIELD}"],
        prompt="What is the full name of the person to search for?",
    )
'''


@pytest.mark.asyncio
async def test_carried_forward_cached_block_does_not_fail_codegen() -> None:
    """SKY-13946: a cached block reused verbatim must not trip the guard.

    The block's actions did not run this time, so the schema this run synthesizes
    cannot declare the field its cached code references — enforcing against it
    failed codegen on every replay and permanently blocked caching.
    """
    workflow = {
        "workflow_permanent_id": "wpid_test",
        "title": "people search",
        "workflow_definition": {
            "parameters": [
                {"key": key, "parameter_type": "workflow", "workflow_parameter_type": "string"}
                for key in ("firstName", "lastName")
            ]
        },
    }
    cached_blocks = {
        "search_person": ScriptBlockSource(
            label="search_person",
            code=CACHED_BLOCK_CODE,
            run_signature="await search_person(page, context)",
            workflow_run_id="wr_previous",
            workflow_run_block_id="wrb_1",
            input_fields=[CARRIED_FORWARD_FIELD],
        )
    }

    result = await generate_workflow_script_python_code(
        file_name="main.py",
        workflow_run_request={"parameters": {}},
        workflow=workflow,
        blocks=[
            {
                "block_type": "task",
                "label": "review_results",
                "task_id": "tsk_review",
                "workflow_run_block_id": "wrb_2",
                "navigation_goal": "Open the first result",
                "url": "https://example.com/results",
            }
        ],
        actions_by_task={
            "tsk_review": [
                {
                    "action_type": "click",
                    "action_id": "act_1",
                    "xpath": "//a[1]",
                    "intention": "Open the first result",
                }
            ]
        },
        cached_blocks=cached_blocks,
        updated_block_labels={"review_results"},
    )

    assert CARRIED_FORWARD_FIELD in result.source_code


REUSED_FIELD = "enter_the_patient_id"
REUSED_BLOCK_CODE = f'''@skyvern.cached(cache_key="find_patient")
async def find_patient(page, context):
    await page.fill(
        selector="xpath=//input[@id='pid']",
        value=context.parameters["{REUSED_FIELD}"],
        prompt="Enter the patient ID",
    )
'''


@pytest.mark.asyncio
async def test_partial_regeneration_keeps_the_field_names_of_a_reused_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """A block reused from the cache keeps the input field its cached code reads; a regenerated one references the
    parameter its typed value came from."""
    saved: dict[str, list[str] | None] = {}

    async def record_block(**kwargs: object) -> bool:
        saved[str(kwargs["block_label"])] = kwargs.get("input_fields")  # type: ignore[assignment]
        return True

    monkeypatch.setattr(generate_script_module, "create_or_update_script_block", record_block)
    typed = {"action_type": "input_text", "xpath": "//input[@id='pid']", "text": "542-641-668"}
    result = await generate_workflow_script_python_code(
        file_name="main.py",
        workflow_run_request={"parameters": {"patient_id": "542-641-668"}},
        workflow={
            "workflow_permanent_id": "wpid_test",
            "title": "patient lookup",
            "workflow_definition": {
                "parameters": [{"key": "patient_id", "parameter_type": "workflow", "workflow_parameter_type": "string"}]
            },
        },
        blocks=[
            {"block_type": "task", "label": label, "task_id": f"tsk_{label}", "workflow_run_block_id": f"wrb_{label}"}
            | {"navigation_goal": "Enter the patient ID", "url": "https://example.com"}
            for label in ("find_patient", "open_chart")
        ],
        actions_by_task={
            "tsk_find_patient": [{**typed, "action_id": "act_1", "intention": "Enter the patient ID"}],
            "tsk_open_chart": [{**typed, "action_id": "act_2", "intention": "Enter the patient ID"}],
        },
        cached_blocks={
            "find_patient": ScriptBlockSource(
                label="find_patient",
                code=REUSED_BLOCK_CODE,
                run_signature="await find_patient(page, context)",
                workflow_run_id="wr_previous",
                workflow_run_block_id="wrb_find_patient",
                input_fields=[REUSED_FIELD],
            )
        },
        updated_block_labels={"open_chart"},
        script_id="s_1",
        script_revision_id="sr_1",
        organization_id="o_1",
    )

    assert saved["find_patient"] == [REUSED_FIELD]
    assert saved["open_chart"] == ["patient_id"]
    assert f'context.parameters["{REUSED_FIELD}"]' in result.source_code


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
