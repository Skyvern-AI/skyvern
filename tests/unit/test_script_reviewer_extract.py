"""Tests for ScriptReviewer._extract_code_from_response and _response_to_text.

Covers SKY-9430: the multi-strategy text-first extractor that bypasses
parse_api_response by relying on raw_response=True from the LLM handler.
"""

import json

from structlog.testing import capture_logs

from skyvern.services.script_reviewer import ScriptReviewer


class TestResponseToText:
    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_string_returned_as_is(self) -> None:
        assert self.reviewer._response_to_text("foo") == "foo"

    def test_empty_string(self) -> None:
        assert self.reviewer._response_to_text("") == ""

    def test_choices_dict_extracts_content(self) -> None:
        response = {"choices": [{"message": {"content": "bar"}}]}
        assert self.reviewer._response_to_text(response) == "bar"

    def test_legacy_dict_round_trips_through_json(self) -> None:
        response = {"code": "async def foo(): pass"}
        text = self.reviewer._response_to_text(response)
        assert json.loads(text) == response

    def test_dict_without_choices_or_code_round_trips(self) -> None:
        response = {"foo": "bar"}
        text = self.reviewer._response_to_text(response)
        assert json.loads(text) == response

    def test_list_returns_empty(self) -> None:
        assert self.reviewer._response_to_text([1, 2, 3]) == ""

    def test_none_returns_empty(self) -> None:
        assert self.reviewer._response_to_text(None) == ""

    def test_choices_with_non_string_content_falls_back_to_dump(self) -> None:
        response = {"choices": [{"message": {"content": None}}]}
        text = self.reviewer._response_to_text(response)
        assert json.loads(text) == response


class TestExtractCodeFromResponse:
    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_none_returns_none(self) -> None:
        assert self.reviewer._extract_code_from_response(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert self.reviewer._extract_code_from_response("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert self.reviewer._extract_code_from_response("   \n  ") is None

    def test_garbage_text_returns_none(self) -> None:
        assert self.reviewer._extract_code_from_response("hello world") is None

    def test_cannot_convert_sentinel(self) -> None:
        assert self.reviewer._extract_code_from_response("CANNOT_CONVERT") == "CANNOT_CONVERT"

    def test_markdown_python_block(self) -> None:
        text = "```python\nasync def foo(): pass\n```"
        assert self.reviewer._extract_code_from_response(text) == "async def foo(): pass"

    def test_markdown_python_block_with_prose(self) -> None:
        text = "Here's the code:\n```python\nasync def foo(): pass\n```"
        assert self.reviewer._extract_code_from_response(text) == "async def foo(): pass"

    def test_bare_markdown_block(self) -> None:
        text = "```\nasync def foo(): pass\n```"
        assert self.reviewer._extract_code_from_response(text) == "async def foo(): pass"

    def test_bare_async_def(self) -> None:
        text = "async def foo(page, context):\n    pass"
        assert self.reviewer._extract_code_from_response(text) == text

    def test_strict_json_with_code_key(self) -> None:
        text = '{"code": "async def foo(): pass"}'
        assert self.reviewer._extract_code_from_response(text) == "async def foo(): pass"

    def test_dict_scan_finds_async_def_in_other_key(self) -> None:
        text = '{"updated_code": "async def foo(): pass"}'
        assert self.reviewer._extract_code_from_response(text) == "async def foo(): pass"

    def test_legacy_dict_input_returns_code(self) -> None:
        response = {"code": "async def foo(): pass"}
        assert self.reviewer._extract_code_from_response(response) == "async def foo(): pass"

    def test_legacy_choices_dict_with_markdown(self) -> None:
        response = {"choices": [{"message": {"content": "```python\nasync def foo(): pass\n```"}}]}
        assert self.reviewer._extract_code_from_response(response) == "async def foo(): pass"


class TestModeASignatureSplitRecovery:
    """SKY-9430 Mode A: LLM emits malformed JSON where unescaped quotes split
    the function signature across multiple top-level keys after json_repair."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_signature_split_recovers_compilable_code(self) -> None:
        text = '{"code": "async def fn(page: T", "context": "U):\\n    pass"}'
        result = self.reviewer._extract_code_from_response(text)
        assert result is not None
        assert result.startswith("async def fn(page: T, context: U):")
        compile(result, "<test>", "exec")

    def test_signature_split_via_legacy_dict(self) -> None:
        response = {
            "code": "async def fn(page: T",
            "context": "U):\n    pass",
        }
        result = self.reviewer._extract_code_from_response(response)
        assert result is not None
        compile(result, "<test>", "exec")

    def test_three_split_keys(self) -> None:
        text = '{"code": "async def fn(page: T", "ctx": "C", "third": "D):\\n    pass"}'
        result = self.reviewer._extract_code_from_response(text)
        assert result is not None
        compile(result, "<test>", "exec")


class TestMonotonicityGuards:
    """The recovery must never make a currently-passing case start failing."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_well_formed_with_extra_keys_returns_code_unchanged(self) -> None:
        text = '{"code": "async def X(): pass", "extra": "noise"}'
        result = self.reviewer._extract_code_from_response(text)
        assert result == "async def X(): pass"

    def test_string_paren_inside_well_formed_code_not_recovered(self) -> None:
        text = '{"code": "async def X(): print(\\"(\\")", "extra": "noise"}'
        result = self.reviewer._extract_code_from_response(text)
        assert result == 'async def X(): print("(")'
        compile(result, "<test>", "exec")

    def test_reconstruction_failed_compile_returns_original(self) -> None:
        text = '{"code": "async def X(", "oops": "))"}'
        result = self.reviewer._extract_code_from_response(text)
        assert result == "async def X("

    def test_non_string_extra_value_bails(self) -> None:
        text = '{"code": "async def X(", "context": 12345}'
        result = self.reviewer._extract_code_from_response(text)
        assert result == "async def X("


class TestModeBFieldMapNotRecovered:
    """SKY-9430 Mode B: LLM ignores 'return a function' instruction and emits
    a FIELD_MAP-shaped JSON. No async def anywhere — extractor returns None.
    Documented as deferred; needs prompt-side fix."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_field_map_response_returns_none(self) -> None:
        text = (
            '{"class_code": {"param": "class_code", "action": "fill"},'
            '"agency_program": {"param": null, "action": "fill"}}'
        )
        assert self.reviewer._extract_code_from_response(text) is None


class TestKwargsOptional:
    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_no_kwargs(self) -> None:
        text = "```python\nasync def foo(): pass\n```"
        assert self.reviewer._extract_code_from_response(text) == "async def foo(): pass"

    def test_with_kwargs(self) -> None:
        text = "```python\nasync def foo(): pass\n```"
        result = self.reviewer._extract_code_from_response(
            text, block_label="test_block", prompt_name="script-reviewer"
        )
        assert result == "async def foo(): pass"


class TestRecoveryLogging:
    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_recovery_failed_compile_warning_emitted(self) -> None:
        text = '{"code": "async def X(", "oops": "))"}'
        with capture_logs() as logs:
            result = self.reviewer._extract_code_from_response(
                text, block_label="bad_block", prompt_name="script-reviewer"
            )
        assert result == "async def X("
        failed = [e for e in logs if e.get("event") == "ScriptReviewer: malformed-dict recovery failed compile"]
        assert len(failed) == 1
        assert failed[0]["prompt_name"] == "script-reviewer"
        assert failed[0]["block_label"] == "bad_block"


class TestFenceWrappedJson:
    """SKY-9430 / debate round 2 / CORR-1: when the LLM wraps JSON inside a
    markdown fence, the extractor must fall through to the JSON parsing branch
    instead of returning the JSON text as code."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_well_formed_json_inside_python_fence(self) -> None:
        text = '```python\n{"code": "async def fn(): pass"}\n```'
        result = self.reviewer._extract_code_from_response(text)
        assert result == "async def fn(): pass"

    def test_well_formed_json_inside_bare_fence(self) -> None:
        text = '```\n{"code": "async def fn(): pass"}\n```'
        result = self.reviewer._extract_code_from_response(text)
        assert result == "async def fn(): pass"

    def test_mode_a_malformed_json_inside_python_fence_recovers(self) -> None:
        text = '```python\n{"code": "async def fn(page: T", "context": "U):\\n    pass"}\n```'
        result = self.reviewer._extract_code_from_response(text)
        assert result is not None
        assert result.startswith("async def fn(page: T, context: U):")
        compile(result, "<test>", "exec")

    def test_well_formed_json_inside_json_tagged_fence(self) -> None:
        text = '```json\n{"code": "async def fn(): pass"}\n```'
        result = self.reviewer._extract_code_from_response(text)
        assert result == "async def fn(): pass"

    def test_py_tagged_fence_extracts_python(self) -> None:
        text = "```py\nasync def foo(): pass\n```"
        result = self.reviewer._extract_code_from_response(text)
        assert result == "async def foo(): pass"

    def test_fence_with_unsupported_content_returns_none(self) -> None:
        text = "```python\nsome prose, no JSON, no function\n```"
        assert self.reviewer._extract_code_from_response(text) is None
