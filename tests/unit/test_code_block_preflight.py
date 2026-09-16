"""Tests for author-time render validation of copilot code blocks.

OSS-synced: only example.* placeholder targets and synthetic labels.
"""

from __future__ import annotations

import ast
import textwrap

import pytest

from skyvern.forge.sdk.copilot.code_block_preflight import (
    RENDER_TEMPLATE_SYNTAX_REASON_CODE,
    RENDER_UNDEFINED_NAME_REASON_CODE,
    WRAPPER_SCOPE_GLOBAL_REASON_CODE,
    CodeBlockRenderDiagnostic,
    _build_typed_module,
    advisory_code_block_diagnostics,
    code_block_render_diagnostic,
    wrapper_scope_diagnostics,
    wrapper_scope_facts,
)

_BOUND_NAMES = frozenset({"business_name", "contact_email", "submit_request", "submit_request_output"})

# The shape of a real saved block: top-level counters, a nested helper that declares them
# ``global`` and increments them, and a loop calling the helper. Selectors and text are generic.
_GLOBAL_IN_HELPER_HEAD = """\
links = page.locator("nav a[href*='/items/']")
link_count = await links.count()
if link_count == 0:
    raise RuntimeError("No items were visible")

item_urls = []
for index in range(link_count):
    href = await links.nth(index).get_attribute("href")
    if href and href not in item_urls:
        item_urls.append(href)

items_completed = 0
items_started = 0
titles = []

async def open_item_if_present():
"""
_GLOBAL_IN_HELPER_TAIL = """\
    play_button = page.locator("button[aria-label='Play']")
    if not await play_button.count():
        return False
    await play_button.first.click()
    items_started += 1
    titles.append(await page.title())
    if bool(setup_only):
        return True
    await page.wait_for_timeout(500)
    items_completed += 1
    return True

for item_url in item_urls:
    await page.goto("https://example.com" + item_url, wait_until="domcontentloaded")
    opened = await open_item_if_present()
    if opened and bool(setup_only):
        return {"items_started": items_started, "items_completed": items_completed}

return {"items_started": items_started, "items_completed": items_completed, "titles": titles}
"""
_GLOBAL_IN_HELPER = _GLOBAL_IN_HELPER_HEAD + "    global items_completed, items_started\n" + _GLOBAL_IN_HELPER_TAIL
_NONLOCAL_IN_HELPER = _GLOBAL_IN_HELPER_HEAD + "    nonlocal items_completed, items_started\n" + _GLOBAL_IN_HELPER_TAIL
_RETURN_VALUE_HELPER = """\
items_started = 0

async def open_item():
    await page.locator("button[aria-label='Play']").first.click()
    return 1

for _ in range(3):
    items_started += await open_item()
return {"items_started": items_started}
"""
_ACCUMULATOR_HELPER = """\
counts = {"started": 0}

async def open_item(acc):
    await page.locator("button[aria-label='Play']").first.click()
    acc["started"] += 1

for _ in range(3):
    await open_item(counts)
return counts
"""
_GLOBAL_FOR_UNBOUND_NAME = """\
async def helper():
    global request_count
    request_count = 1

await helper()
return {"ok": True}
"""
_WRAPPER_LEVEL_GLOBAL = """\
global items_started
items_started = 0

async def helper():
    global items_started
    items_started += 1

await helper()
return {"items_started": items_started}
"""


class TestCodeBlockRenderDiagnosticRejects:
    def test_parameters_namespace_reference_is_unrenderable(self) -> None:
        code = (
            "# Workflow input bindings: {{ parameters.business_name }}\n"
            'await page.goto("https://example.com/request")\n'
            'await page.locator("#company").fill(str(business_name).strip())\n'
        )
        diagnostic = code_block_render_diagnostic(code, _BOUND_NAMES)
        assert diagnostic is not None
        assert diagnostic.code == RENDER_UNDEFINED_NAME_REASON_CODE
        assert diagnostic.failing_expression == "{{ parameters.business_name }}"
        assert "{{ business_name }}" in diagnostic.message

    def test_undeclared_root_is_unrenderable(self) -> None:
        diagnostic = code_block_render_diagnostic("value = str({{ frobnicator }})", _BOUND_NAMES)
        assert diagnostic is not None
        assert diagnostic.code == RENDER_UNDEFINED_NAME_REASON_CODE
        assert diagnostic.failing_expression == "{{ frobnicator }}"
        assert "frobnicator" in diagnostic.message

    def test_template_syntax_error_is_unrenderable(self) -> None:
        diagnostic = code_block_render_diagnostic("value = {{ business_name\nother = 1", _BOUND_NAMES)
        assert diagnostic is not None
        assert diagnostic.code == RENDER_TEMPLATE_SYNTAX_REASON_CODE
        assert diagnostic.message

    def test_statement_only_undeclared_root_is_attributed(self) -> None:
        code = "{% if unknown_flag %}\nvalue = 1\n{% endif %}"
        diagnostic = code_block_render_diagnostic(code, _BOUND_NAMES)
        assert diagnostic is not None
        assert "unknown_flag" in diagnostic.failing_expression

    def test_unattributable_undefined_yields_diagnostic_not_exception(self) -> None:
        code = "{% macro helper() %}{{ caller() }}{% endmacro %}\nvalue = {{ helper() }}"
        diagnostic = code_block_render_diagnostic(code, _BOUND_NAMES)
        assert isinstance(diagnostic, CodeBlockRenderDiagnostic)
        assert diagnostic.code == RENDER_UNDEFINED_NAME_REASON_CODE

    @pytest.mark.parametrize(
        "gadget",
        [
            "value = {{ ''.__class__.__mro__[1].__subclasses__() }}",
            "value = {{ business_name.__class__.__init__.__globals__ }}",
        ],
    )
    def test_ssti_gadget_is_rejected_without_executing(self, gadget: str) -> None:
        diagnostic = code_block_render_diagnostic(gadget, _BOUND_NAMES)
        assert diagnostic is not None
        assert diagnostic.code == RENDER_UNDEFINED_NAME_REASON_CODE

    def test_loop_names_outside_loop_scope_are_unrenderable(self) -> None:
        diagnostic = code_block_render_diagnostic("value = {{ current_item }}", _BOUND_NAMES)
        assert diagnostic is not None
        assert diagnostic.code == RENDER_UNDEFINED_NAME_REASON_CODE
        assert "current_item" in diagnostic.message


class TestCodeBlockRenderDiagnosticPasses:
    @pytest.mark.parametrize(
        "code",
        [
            'await page.locator("#company").fill("{{ business_name }}")',
            "value = {{ submit_request_output.field }}",
            "value = {{ submit_request_output['nested'][0] }}",
            "today = {{ current_date }}",
            "payload = {{ business_name | json }}",
            "{% for item in workflow_run_outputs %}{{ item }}{% endfor %}",
        ],
    )
    def test_renderable_templates_pass(self, code: str) -> None:
        assert code_block_render_diagnostic(code, _BOUND_NAMES) is None

    @pytest.mark.parametrize(
        "code",
        [
            "{% for item in workflow_run_outputs %}{{ current_index }}{% endfor %}",
            "{% for item in workflow_run_outputs %}\nvalue = {{ current_item }}\n{% endfor %}",
        ],
    )
    def test_loop_names_pass_inside_loop(self, code: str) -> None:
        assert code_block_render_diagnostic(code, _BOUND_NAMES) is None

    def test_jinja_free_code_passes(self) -> None:
        code = 'await page.goto("https://example.com")\nreturn {"output": {"a": 1}}'
        assert code_block_render_diagnostic(code, _BOUND_NAMES) is None


def test_preflight_declares_normalized_parameters_once_and_omits_normalized_keywords() -> None:
    source = _build_typed_module("result = ﬁle", parameter_keys=["ﬁle", "file", "ｉｆ", "_＿private"])
    tree = ast.parse(source)
    declared = [node.target.id for node in tree.body if isinstance(node, ast.AnnAssign)]
    assert declared.count("file") == 1
    assert "if" not in declared
    assert "__private" not in declared


class TestWrapperScopeGlobalAdvisory:
    def test_global_for_a_top_level_name_in_a_nested_helper_is_flagged_with_its_line(self) -> None:
        diagnostics = advisory_code_block_diagnostics(_GLOBAL_IN_HELPER)
        scope = [diagnostic for diagnostic in diagnostics if diagnostic.code == WRAPPER_SCOPE_GLOBAL_REASON_CODE]
        assert len(scope) == 1
        assert "`global items_completed, items_started` at line 17 inside `open_item_if_present`" in scope[0].message

    def test_global_line_follows_the_runtime_when_the_block_starts_with_blank_lines(self) -> None:
        diagnostics = wrapper_scope_diagnostics("\n\n" + _GLOBAL_IN_HELPER)
        assert len(diagnostics) == 1
        assert "at line 19 inside" in diagnostics[0].message

    @pytest.mark.parametrize(
        "code",
        [
            _NONLOCAL_IN_HELPER,
            _RETURN_VALUE_HELPER,
            _ACCUMULATOR_HELPER,
            _GLOBAL_FOR_UNBOUND_NAME,
            _WRAPPER_LEVEL_GLOBAL,
        ],
        ids=["nonlocal", "return_value", "mutable_accumulator", "never_bound_name", "wrapper_level_global"],
    )
    def test_sanctioned_and_exact_negative_shapes_yield_no_advisory(self, code: str) -> None:
        assert [d for d in advisory_code_block_diagnostics(code) if d.code == WRAPPER_SCOPE_GLOBAL_REASON_CODE] == []

    def test_a_global_for_a_parameter_key_is_advised_only_when_the_keys_are_known(self) -> None:
        code = "async def helper():\n    global setup_only\n    setup_only = False\n\nawait helper()\nreturn {}\n"
        assert advisory_code_block_diagnostics(code) == []
        diagnostics = advisory_code_block_diagnostics(code, parameter_keys=["setup_only"])
        assert [d.code for d in diagnostics] == [WRAPPER_SCOPE_GLOBAL_REASON_CODE]
        assert "`global setup_only` at line 2 inside `helper` for a workflow parameter" in diagnostics[0].message
        assert "NameError" not in diagnostics[0].message

    def test_a_mixed_global_statement_names_only_the_block_bound_name(self) -> None:
        code = textwrap.dedent(
            """\
            count = 0

            async def helper():
                global count, phantom
                count += 1

            await helper()
            return {"count": count}
            """
        )
        diagnostics = advisory_code_block_diagnostics(code)
        assert [d.code for d in diagnostics] == [WRAPPER_SCOPE_GLOBAL_REASON_CODE]
        assert "`global count` at line 4 inside `helper`" in diagnostics[0].message
        assert "phantom" not in diagnostics[0].message

    def test_a_helper_nested_in_a_helper_yields_one_diagnostic_owned_by_the_inner_helper(self) -> None:
        code = textwrap.dedent(
            """\
            count = 0

            async def outer():
                async def inner():
                    global count
                    count += 1

                await inner()

            await outer()
            return {"count": count}
            """
        )
        diagnostics = advisory_code_block_diagnostics(code)
        assert [d.code for d in diagnostics] == [WRAPPER_SCOPE_GLOBAL_REASON_CODE]
        assert "`global count` at line 5 inside `inner`" in diagnostics[0].message
        facts = wrapper_scope_facts(code)
        assert facts is not None
        assert {helper.name: helper.global_names for helper in facts.helpers} == {
            "outer": frozenset(),
            "inner": frozenset({"count"}),
        }
