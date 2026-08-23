from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.workflow.exceptions import FailedToFormatJinjaStyleParameter
from skyvern.forge.sdk.workflow.models._jinja import (
    mask_jinja_in_python_comments,
    restore_jinja_masked_comments,
)
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType


def _round_trip(source: str) -> str:
    masked, comments = mask_jinja_in_python_comments(source)
    return restore_jinja_masked_comments(masked, comments)


def test_comment_with_jinja_delimiters_is_hidden_from_the_template() -> None:
    source = "# code blocks use {{ }} templating for the same reason.\nvalue = 1\n"

    masked, comments = mask_jinja_in_python_comments(source)

    assert "{{" not in masked
    assert len(comments) == 1
    assert restore_jinja_masked_comments(masked, comments) == source


def test_masking_preserves_line_count_and_code_columns() -> None:
    source = "a = 1  # {{ }}\nb = 2\n"

    masked, _ = mask_jinja_in_python_comments(source)

    assert masked.splitlines()[0].startswith("a = 1  #")
    assert len(masked.splitlines()) == len(source.splitlines())


def test_source_without_jinja_delimiters_is_untouched() -> None:
    source = "# a plain comment\nvalue = {'a': 1}\n"

    masked, comments = mask_jinja_in_python_comments(source)

    assert masked == source
    assert comments == {}


def test_comment_without_delimiters_is_left_in_place() -> None:
    source = "# plain\nraw = '''{{ email }}'''  # keep me\n"

    masked, comments = mask_jinja_in_python_comments(source)

    assert "# plain" in masked
    assert "# keep me" in masked
    assert comments == {}
    assert "{{ email }}" in masked


def test_hash_inside_a_string_literal_is_not_masked() -> None:
    source = 'selector = "#id-{{ }}"\n'

    masked, comments = mask_jinja_in_python_comments(source)

    assert masked == source
    assert comments == {}


def test_untokenizable_source_is_returned_unchanged() -> None:
    source = "def broken(:\n  # {{ }}\n"

    masked, comments = mask_jinja_in_python_comments(source)

    assert masked == source
    assert comments == {}


def test_multiple_masked_comments_round_trip() -> None:
    source = "# first {{ }}\nvalue = 1  # second {% bad %}\n# third {# unclosed\n"

    assert _round_trip(source) == source


def test_restore_handles_a_duplicated_sentinel() -> None:
    masked, comments = mask_jinja_in_python_comments("# {{ }}\n")
    sentinel = next(iter(comments))

    restored = restore_jinja_masked_comments(f"{sentinel}\n{sentinel}\n", comments)

    assert restored == "# {{ }}\n# {{ }}\n"


def _code_block(code: str) -> CodeBlock:
    now = datetime.now()
    return CodeBlock(
        label="prefill",
        code=code,
        output_parameter=OutputParameter(
            parameter_type=ParameterType.OUTPUT,
            key="prefill_output",
            description=None,
            output_parameter_id="op-1",
            workflow_id="w-1",
            created_at=now,
            modified_at=now,
            deleted_at=None,
        ),
    )


def _mock_context(values: dict[str, object]) -> MagicMock:
    context = MagicMock()
    context.values = values
    context.secrets = {}
    context.include_secrets_in_templates = False
    context.get_block_metadata = MagicMock(return_value={})
    return context


def test_code_block_renders_despite_bare_jinja_braces_in_a_comment() -> None:
    block = _code_block(
        "# code blocks use {{ }} templating for the same reason.\nraw_email = r'''{{ email }}'''\nvalue = 1\n"
    )

    block.format_potential_template_parameters(_mock_context({"email": "a@b.com"}))

    assert "raw_email = r'''a@b.com'''" in block.code
    assert "# code blocks use {{ }} templating for the same reason." in block.code


def test_code_block_does_not_substitute_values_into_comments() -> None:
    block = _code_block("# Plain {{ email }} renders a value.\nraw_email = r'''{{ email }}'''\n")

    block.format_potential_template_parameters(_mock_context({"email": "a@b.com"}))

    assert "# Plain {{ email }} renders a value." in block.code
    assert "raw_email = r'''a@b.com'''" in block.code


def test_code_block_still_raises_when_the_executable_source_is_unrenderable() -> None:
    block = _code_block("raw = r'''{{ }}'''\n")

    with pytest.raises(FailedToFormatJinjaStyleParameter):
        block.format_potential_template_parameters(_mock_context({}))
