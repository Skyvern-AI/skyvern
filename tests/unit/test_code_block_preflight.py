"""Tests for author-time render validation of copilot code blocks.

OSS-synced: only example.* placeholder targets and synthetic labels.
"""

from __future__ import annotations

import pytest

from skyvern.forge.sdk.copilot.code_block_preflight import (
    RENDER_TEMPLATE_SYNTAX_REASON_CODE,
    RENDER_UNDEFINED_NAME_REASON_CODE,
    CodeBlockRenderDiagnostic,
    code_block_render_diagnostic,
)

_BOUND_NAMES = frozenset({"business_name", "contact_email", "submit_request", "submit_request_output"})


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
