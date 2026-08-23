"""Tests for the code-artifact-metadata validator returning every violation at once.

OSS-synced: only example.* / RFC-2606 placeholder targets and synthetic labels.
"""

from __future__ import annotations

import re
import textwrap

from skyvern.forge.sdk.copilot.outcome_verification_trace import (
    finalize_outcome_verification_trace,
    record_code_artifact_violations,
)
from skyvern.forge.sdk.copilot.output_utils import _sanitize_failure_text
from skyvern.forge.sdk.copilot.tools import _normalize_code_artifact_metadata
from skyvern.forge.sdk.copilot.tools.workflow_update import (
    _code_artifact_metadata_shape_errors,
    _code_block_returns_flat_string,
    _code_block_returns_uninvoked_structured_function,
    _download_descriptor_leak_finding,
    _normalize_code_artifact_metadata_detailed,
)


def _code_block_yaml(label: str) -> str:
    return textwrap.dedent(
        f"""
        workflow_definition:
          blocks:
            - block_type: code
              label: {label}
              code: |
                await page.goto("https://example.com/")
                return {{"records": [{{"number": "123"}}]}}
        """
    ).strip()


def _violation_count(error: str) -> int:
    return len(re.findall(r"^\d+\.", error, flags=re.M))


def _valid_metadata(label: str) -> dict:
    return {
        "block_label": label,
        "artifact_id": f"code_artifact:{label}",
        "declared_goal": "g",
        "claimed_outcomes": [
            {
                "id": "claim:x",
                "scope": "outcome",
                "text": "x",
                "status": "observed_not_verified",
                "depends_on": ["dependency:p"],
                "covered_criteria": ["criterion:c"],
                "goal_value_paths": ["records[].number"],
                "observation_refs": ["obs1"],
            }
        ],
        "page_dependencies": [
            {"id": "dependency:p", "scope": "page", "status": "observed_not_verified", "observation_refs": ["obs1"]}
        ],
        "completion_criteria": [{"id": "criterion:c", "text": "c", "level": "terminal"}],
        "terminal_verifier_expectations": [
            {"id": "exp", "text": "e", "criteria_ids": ["criterion:c"], "goal_value_paths": ["records[].number"]}
        ],
        "observation_refs": [
            {
                "observation_ref": "obs1",
                "dependency_id": "dependency:p",
                "status": "observed_not_verified",
                "source_tool": "scout_interaction",
            }
        ],
    }


def _broken_metadata(label: str) -> dict:
    return {
        "block_label": label,
        "artifact_id": "not-prefixed",
        "declared_goal": "do the thing",
        "claimed_outcomes": [{"id": "claim:x", "scope": "outcome", "text": "x", "status": "satisfied"}],
        "page_dependencies": [{"id": "dependency:p", "scope": "page", "status": "satisfied"}],
        "completion_criteria": [{"id": "criterion:c", "text": "c", "level": "terminal"}],
        "terminal_verifier_expectations": [{"id": "exp", "text": "e"}],
        "observation_refs": [{"observation_ref": "obs1", "status": "satisfied", "checkpoint_next_mode": "advance"}],
    }


class TestAccumulateAllViolations:
    def test_every_violation_returned_at_once(self) -> None:
        normalized, error = _normalize_code_artifact_metadata(
            [_broken_metadata("my_block")], _code_block_yaml("my_block")
        )
        assert normalized == {}
        assert error is not None
        # The 5+ sequential failures from the repair loop now surface together.
        assert _violation_count(error) >= 5
        assert error.startswith("Artifact metadata has ")
        assert "fix all of them in one update" in error
        assert "requires `source_tool`" in error
        assert "requires `depends_on`" in error
        assert "is `satisfied` but has no" in error

    def test_single_violation_is_not_numbered(self) -> None:
        metadata = _valid_metadata("my_block")
        metadata["claimed_outcomes"][0].pop("depends_on")
        normalized, error = _normalize_code_artifact_metadata([metadata], _code_block_yaml("my_block"))
        assert normalized == {}
        assert error is not None
        assert _violation_count(error) == 0
        assert error == "Artifact metadata claim `claim:x` for `my_block` requires `depends_on`."

    def test_violations_aggregate_across_multiple_artifacts(self) -> None:
        yaml = textwrap.dedent(
            """
            workflow_definition:
              blocks:
                - block_type: code
                  label: block_one
                  code: |
                    await page.goto("https://example.com/")
                    return {"records": [{"number": "123"}]}
                - block_type: code
                  label: block_two
                  code: |
                    await page.goto("https://example.com/")
                    return {"records": [{"number": "123"}]}
            """
        ).strip()
        normalized, error = _normalize_code_artifact_metadata(
            [_broken_metadata("block_one"), _broken_metadata("block_two")], yaml
        )
        assert normalized == {}
        assert error is not None
        assert "block_one" in error
        assert "block_two" in error

    def test_unknown_label_is_rejected_and_other_artifacts_still_validated(self) -> None:
        normalized, error = _normalize_code_artifact_metadata(
            [_broken_metadata("ghost_label"), _broken_metadata("my_block")], _code_block_yaml("my_block")
        )
        assert normalized == {}
        assert error is not None
        # The stale identity and the anchored artifact's shape violations are both surfaced;
        # the server never silently drops or rekeys the submitted row.
        assert "ghost_label" in error
        assert "requires `source_tool`" in error

    def test_valid_metadata_passes(self) -> None:
        normalized, error = _normalize_code_artifact_metadata(
            [_valid_metadata("my_block")], _code_block_yaml("my_block")
        )
        assert error is None
        assert list(normalized.keys()) == ["my_block"]

    def test_terminal_goal_value_path_placeholders_are_rejected(self) -> None:
        metadata = _valid_metadata("my_block")
        metadata["claimed_outcomes"][0]["goal_value_paths"] = [
            "<fill: output JSON path(s) carrying requested goal values>"
        ]
        metadata["terminal_verifier_expectations"][0]["goal_value_paths"] = [
            "<fill: output JSON path(s) carrying requested goal values>"
        ]

        errors = _code_artifact_metadata_shape_errors("my_block", metadata, reject_unfilled_goal_value_paths=True)

        assert any("claim `claim:x`" in error for error in errors)
        assert any("terminal verifier expectation `exp`" in error for error in errors)
        assert all("has unfilled `goal_value_paths`" in error for error in errors)

    def test_empty_metadata_is_noop(self) -> None:
        assert _normalize_code_artifact_metadata(None, _code_block_yaml("my_block")) == ({}, None)
        assert _normalize_code_artifact_metadata([], _code_block_yaml("my_block")) == ({}, None)


class TestPerLabelSalvage:
    def test_conforming_label_survives_offending_label(self) -> None:
        yaml = textwrap.dedent(
            """
            workflow_definition:
              blocks:
                - block_type: code
                  label: block_one
                  code: |
                    await page.goto("https://example.com/")
                - block_type: code
                  label: block_two
                  code: |
                    await page.goto("https://example.com/")
            """
        ).strip()
        bad = _valid_metadata("block_two")
        bad["claimed_outcomes"][0]["status"] = "satisfied"
        normalized, error = _normalize_code_artifact_metadata([_valid_metadata("block_one"), bad], yaml)
        assert list(normalized.keys()) == ["block_one"]
        assert error is not None
        assert "block_two" in error
        assert "block_one" not in error

    def test_unknown_label_rejected_without_discarding_valid_sibling(self) -> None:
        normalized, error = _normalize_code_artifact_metadata(
            [_valid_metadata("ghost_label"), _valid_metadata("my_block")], _code_block_yaml("my_block")
        )
        assert list(normalized.keys()) == ["my_block"]
        assert error is not None
        assert "ghost_label" in error


def _extraction_code_block_yaml(label: str, code: str) -> str:
    indented = textwrap.indent(textwrap.dedent(code).strip(), " " * 16)
    return textwrap.dedent(
        f"""
        workflow_definition:
          blocks:
            - block_type: code
              label: {label}
              code: |
{indented}
        """
    ).strip()


def _extraction_metadata(label: str, goal_value_paths: list[str]) -> dict:
    metadata = _valid_metadata(label)
    metadata["claimed_outcomes"][0]["goal_value_paths"] = list(goal_value_paths)
    metadata["terminal_verifier_expectations"][0]["goal_value_paths"] = list(goal_value_paths)
    return metadata


def _non_extraction_metadata(label: str) -> dict:
    return {
        "block_label": label,
        "artifact_id": f"code_artifact:{label}",
        "declared_goal": "click submit",
        "claimed_outcomes": [
            {
                "id": "claim:x",
                "scope": "outcome",
                "text": "submitted",
                "status": "observed_not_verified",
                "depends_on": ["dependency:p"],
                "covered_criteria": ["criterion:c"],
                "observation_refs": ["obs1"],
            }
        ],
        "page_dependencies": [
            {"id": "dependency:p", "scope": "page", "status": "observed_not_verified", "observation_refs": ["obs1"]}
        ],
        "completion_criteria": [{"id": "criterion:c", "text": "submitted", "level": "outcome", "terminal": False}],
        "terminal_verifier_expectations": [{"id": "exp", "text": "e", "criteria_ids": ["criterion:c"]}],
        "observation_refs": [
            {
                "observation_ref": "obs1",
                "dependency_id": "dependency:p",
                "status": "observed_not_verified",
                "source_tool": "scout_interaction",
            }
        ],
    }


class TestExtractionReturnShape:
    def test_flat_inner_text_return_is_rejected(self) -> None:
        code = """
        await page.goto("https://example.com/")
        return page.inner_text("#results")
        """
        normalized, error = _normalize_code_artifact_metadata(
            [_extraction_metadata("my_block", ["records[].number"])],
            _extraction_code_block_yaml("my_block", code),
        )
        assert normalized == {}
        assert error is not None
        assert "flat text blob" in error
        assert "array of objects" in error

    def test_flat_string_local_return_is_rejected(self) -> None:
        code = """
        await page.goto("https://example.com/")
        text = await page.locator("#results").inner_text()
        return text
        """
        normalized, error = _normalize_code_artifact_metadata(
            [_extraction_metadata("my_block", ["records[].number"])],
            _extraction_code_block_yaml("my_block", code),
        )
        assert normalized == {}
        assert error is not None
        assert "flat text blob" in error

    def test_keyed_dict_return_passes(self) -> None:
        code = """
        await page.goto("https://example.com/")
        return {"records": [{"number": "REC-001"}]}
        """
        normalized, error = _normalize_code_artifact_metadata(
            [_extraction_metadata("my_block", ["records[].number"])],
            _extraction_code_block_yaml("my_block", code),
        )
        assert error is None
        assert list(normalized.keys()) == ["my_block"]

    def test_array_of_objects_comprehension_return_passes(self) -> None:
        code = """
        await page.goto("https://example.com/")
        rows = await page.locator(".row").all()
        return [{"number": await row.inner_text()} for row in rows]
        """
        normalized, error = _normalize_code_artifact_metadata(
            [_extraction_metadata("my_block", ["records[].number"])],
            _extraction_code_block_yaml("my_block", code),
        )
        assert error is None
        assert list(normalized.keys()) == ["my_block"]

    def test_single_scalar_passes_as_keyed_field_without_array_wrapping(self) -> None:
        code = """
        await page.goto("https://example.com/")
        return {"total": 5}
        """
        normalized, error = _normalize_code_artifact_metadata(
            [_extraction_metadata("my_block", ["total"])],
            _extraction_code_block_yaml("my_block", code),
        )
        assert error is None
        assert list(normalized.keys()) == ["my_block"]

    def test_non_extraction_block_with_flat_return_is_not_rejected(self) -> None:
        code = """
        await page.goto("https://example.com/")
        return page.inner_text("#status")
        """
        normalized, error = _normalize_code_artifact_metadata(
            [_non_extraction_metadata("my_block")],
            _extraction_code_block_yaml("my_block", code),
        )
        assert error is None
        assert list(normalized.keys()) == ["my_block"]


class TestExtractionUninvokedNestedReturn:
    def test_uninvoked_nested_structured_function_is_rejected(self) -> None:
        code = """
        async def run(page):
            result = {"records": [{"number": "REC-001"}]}
            return result
        """
        normalized, error = _normalize_code_artifact_metadata(
            [_extraction_metadata("my_block", ["records[].number"])],
            _extraction_code_block_yaml("my_block", code),
        )
        assert normalized == {}
        assert error is not None
        assert "nested function" in error
        assert "captures the function object" in error

    def test_invoked_and_returned_nested_function_passes(self) -> None:
        code = """
        async def run(page):
            return {"records": [{"number": "REC-001"}]}
        return await run(page)
        """
        normalized, error = _normalize_code_artifact_metadata(
            [_extraction_metadata("my_block", ["records[].number"])],
            _extraction_code_block_yaml("my_block", code),
        )
        assert error is None
        assert list(normalized.keys()) == ["my_block"]

    def test_invoked_and_bound_nested_function_passes(self) -> None:
        code = """
        async def run(page):
            return {"records": [{"number": "REC-001"}]}
        data = await run(page)
        """
        normalized, error = _normalize_code_artifact_metadata(
            [_extraction_metadata("my_block", ["records[].number"])],
            _extraction_code_block_yaml("my_block", code),
        )
        assert error is None
        assert list(normalized.keys()) == ["my_block"]

    def test_top_level_structured_local_passes(self) -> None:
        code = """
        await page.goto("https://example.com/")
        records = [{"number": "REC-001"}]
        """
        normalized, error = _normalize_code_artifact_metadata(
            [_extraction_metadata("my_block", ["records[].number"])],
            _extraction_code_block_yaml("my_block", code),
        )
        assert error is None
        assert list(normalized.keys()) == ["my_block"]

    def test_indeterminate_nested_function_is_not_flagged(self) -> None:
        code = """
        def helper():
            return "text"
        await page.goto("https://example.com/")
        """
        normalized, error = _normalize_code_artifact_metadata(
            [_extraction_metadata("my_block", ["records[].number"])],
            _extraction_code_block_yaml("my_block", code),
        )
        assert error is None
        assert list(normalized.keys()) == ["my_block"]

    def test_structured_return_inside_except_block_passes(self) -> None:
        code = """
        try:
            rows = await page.locator(".row").all()
        except Exception:
            rows = []
        return {"records": [{"number": "REC-001"}]}
        """
        normalized, error = _normalize_code_artifact_metadata(
            [_extraction_metadata("my_block", ["records[].number"])],
            _extraction_code_block_yaml("my_block", code),
        )
        assert error is None
        assert list(normalized.keys()) == ["my_block"]


class TestUninvokedStructuredFunctionClassifier:
    def test_uninvoked_structured_function_with_literal_return_is_flagged(self) -> None:
        assert _code_block_returns_uninvoked_structured_function("def run():\n    return {'a': 1}") is True

    def test_uninvoked_structured_function_with_local_return_is_flagged(self) -> None:
        code = """
        async def run(page):
            result = {"records": []}
            return result
        """
        assert _code_block_returns_uninvoked_structured_function(textwrap.dedent(code)) is True

    def test_invoked_function_is_not_flagged(self) -> None:
        code = """
        def run():
            return {"a": 1}
        data = run()
        return data
        """
        assert _code_block_returns_uninvoked_structured_function(textwrap.dedent(code)) is False

    def test_top_level_structured_return_is_not_flagged(self) -> None:
        code = """
        async def run(page):
            return {"x": 1}
        return {"records": []}
        """
        assert _code_block_returns_uninvoked_structured_function(textwrap.dedent(code)) is False

    def test_top_level_structured_assignment_is_not_flagged(self) -> None:
        assert _code_block_returns_uninvoked_structured_function("records = [{'number': '1'}]") is False

    def test_function_returning_string_is_not_flagged(self) -> None:
        code = """
        def run():
            return "text"
        """
        assert _code_block_returns_uninvoked_structured_function(textwrap.dedent(code)) is False

    def test_function_referenced_but_not_called_is_not_flagged(self) -> None:
        code = """
        def build():
            return {"records": []}
        callbacks = [build]
        """
        assert _code_block_returns_uninvoked_structured_function(textwrap.dedent(code)) is False

    def test_outer_with_only_deeper_nested_structured_return_is_not_flagged(self) -> None:
        # The structured return lives in a doubly-nested helper, not in the
        # uninvoked outer's own scope, so the outer must not look structured.
        code = """
        async def run(page):
            def helper():
                return {"records": []}
            await page.goto("https://example.com/")
        """
        assert _code_block_returns_uninvoked_structured_function(textwrap.dedent(code)) is False

    def test_uninvoked_outer_with_own_structured_return_is_still_flagged(self) -> None:
        code = """
        async def run(page):
            def helper():
                return {"x": 1}
            result = {"records": []}
            return result
        """
        assert _code_block_returns_uninvoked_structured_function(textwrap.dedent(code)) is True

    def test_top_level_structured_return_in_except_is_not_flagged(self) -> None:
        code = """
        try:
            rows = page.locator(".row")
        except Exception:
            return {"records": []}
        """
        assert _code_block_returns_uninvoked_structured_function(textwrap.dedent(code)) is False

    def test_nested_function_structured_local_rebound_to_flat_is_not_flagged(self) -> None:
        # The nested function rebinds its structured local to a flat read before
        # returning it, so it does not actually return structure and must not be flagged.
        code = """
        def run(page):
            result = {"records": []}
            result = page.inner_text("body")
            return result
        """
        assert _code_block_returns_uninvoked_structured_function(textwrap.dedent(code)) is False


class TestFlatStringClassifier:
    def test_string_literal_return_is_flat(self) -> None:
        assert _code_block_returns_flat_string('return "hello"') is True

    def test_fstring_return_is_flat(self) -> None:
        assert _code_block_returns_flat_string('return f"{a} {b}"') is True

    def test_join_return_is_flat(self) -> None:
        assert _code_block_returns_flat_string('return " ".join(parts)') is True

    def test_dict_return_is_not_flat(self) -> None:
        assert _code_block_returns_flat_string('return {"a": 1}') is False

    def test_list_return_is_not_flat(self) -> None:
        assert _code_block_returns_flat_string("return [1, 2, 3]") is False

    def test_unknown_name_return_is_indeterminate_not_flat(self) -> None:
        assert _code_block_returns_flat_string("return some_unknown") is False

    def test_no_return_is_not_flat(self) -> None:
        assert _code_block_returns_flat_string('await page.goto("https://example.com/")') is False

    def test_mixed_structured_and_flat_returns_are_not_flagged(self) -> None:
        code = """
        if condition:
            return {"records": []}
        return page.inner_text("#x")
        """
        assert _code_block_returns_flat_string(textwrap.dedent(code)) is False

    def test_capture_then_wrap_rebind_under_same_name_is_not_flat(self) -> None:
        # Re-binding a flat local to a structured value must clear it from
        # string_locals so the final structured return is not falsely rejected.
        code = """
        text = await page.inner_text("body")
        text = {"records": [{"number": "REC-001"}]}
        return text
        """
        assert _code_block_returns_flat_string(textwrap.dedent(code)) is False

    def test_rebound_still_flat_local_is_flat(self) -> None:
        code = """
        text = "seed"
        text = await page.inner_text("body")
        return text
        """
        assert _code_block_returns_flat_string(textwrap.dedent(code)) is True

    def test_flat_return_inside_except_block_is_flat(self) -> None:
        code = """
        try:
            data = page.locator("#x")
        except Exception:
            return page.inner_text("body")
        """
        assert _code_block_returns_flat_string(textwrap.dedent(code)) is True

    def test_flat_return_inside_match_case_is_flat(self) -> None:
        code = """
        match mode:
            case "x":
                return page.inner_text("body")
        """
        assert _code_block_returns_flat_string(textwrap.dedent(code)) is True

    def test_structured_return_inside_except_block_is_not_flat(self) -> None:
        code = """
        try:
            rows = page.locator(".row")
        except Exception:
            return {"records": []}
        """
        assert _code_block_returns_flat_string(textwrap.dedent(code)) is False


def _two_code_block_yaml(first: str, second: str) -> str:
    return textwrap.dedent(
        f"""
        workflow_definition:
          blocks:
            - block_type: code
              label: {first}
              code: |
                await page.goto("https://example.com/")
            - block_type: code
              label: {second}
              code: |
                await page.goto("https://example.com/")
        """
    ).strip()


class _FakeSpan:
    def __init__(self) -> None:
        self.attrs: dict = {}

    def set_attributes(self, fields: dict) -> None:
        self.attrs.update(fields)


def _record_and_flush(violations: list[str], offending_labels: list[str]) -> dict:
    ctx = type("Ctx", (), {})()
    record_code_artifact_violations(ctx, violations, offending_labels)
    span = _FakeSpan()
    finalize_outcome_verification_trace(ctx, span)
    return span.attrs


class TestViolationBatchIsDurablyRecoverable:
    def test_full_batch_recoverable_from_span_even_with_credential_labels(self) -> None:
        yaml = _two_code_block_yaml("credential_login", "credential_vault")
        result = _normalize_code_artifact_metadata_detailed(
            [_broken_metadata("credential_login"), _broken_metadata("credential_vault")], yaml
        )
        assert result.error is not None
        attrs = _record_and_flush(result.violations, result.offending_labels)

        assert attrs["copilot.code_artifact_violations"] == result.violations
        assert attrs["copilot.code_artifact_violation_count"] == len(result.violations)
        assert attrs["copilot.code_artifact_violation_block_labels"] == ["credential_login", "credential_vault"]
        # Every numbered line from the batched error survives as its own element.
        numbered = [line.split(". ", 1)[1] for line in result.error.splitlines() if re.match(r"^\d+\.", line)]
        assert numbered == result.violations

    def test_malformed_only_batch_records_count_without_labels_or_values(self) -> None:
        secret = "SUPER_SECRET_VALUE_12345"
        result = _normalize_code_artifact_metadata_detailed(
            [{"block_label": "credential_x", "claimed_outcomes": secret}], _code_block_yaml("credential_x")
        )
        assert result.error is not None
        assert result.offending_labels == []
        assert all(secret not in violation for violation in result.violations)
        attrs = _record_and_flush(result.violations, result.offending_labels)
        assert attrs["copilot.code_artifact_violation_count"] == len(result.violations)
        assert attrs["copilot.code_artifact_violation_block_labels"] == []
        assert all(secret not in violation for violation in attrs["copilot.code_artifact_violations"])

    def test_span_keeps_violations_the_backend_log_summary_truncates_away(self) -> None:
        yaml = _two_code_block_yaml("credential_login", "credential_vault")
        result = _normalize_code_artifact_metadata_detailed(
            [_broken_metadata("credential_login"), _broken_metadata("credential_vault")], yaml
        )
        bounded = _sanitize_failure_text(result.error)
        assert len(bounded) <= 120
        assert len(result.violations) > 1
        attrs = _record_and_flush(result.violations, result.offending_labels)
        # The bounded summary loses all but the first violation; the span keeps them all.
        assert attrs["copilot.code_artifact_violations"][-1] not in bounded
        assert len(attrs["copilot.code_artifact_violations"]) == len(result.violations)

    def test_empty_batch_is_a_noop(self) -> None:
        ctx = type("Ctx", (), {})()
        record_code_artifact_violations(ctx, [], [])
        span = _FakeSpan()
        finalize_outcome_verification_trace(ctx, span)
        assert "copilot.code_artifact_violations" not in span.attrs

    def test_latest_batch_wins_on_retry(self) -> None:
        ctx = type("Ctx", (), {})()
        record_code_artifact_violations(ctx, ["v1", "v2", "v3"], ["a"])
        record_code_artifact_violations(ctx, ["only_one"], ["b"])
        span = _FakeSpan()
        finalize_outcome_verification_trace(ctx, span)
        assert span.attrs["copilot.code_artifact_violations"] == ["only_one"]
        assert span.attrs["copilot.code_artifact_violation_count"] == 1
        assert span.attrs["copilot.code_artifact_violation_block_labels"] == ["b"]


def _download_intent_metadata(label: str) -> dict:
    metadata = _non_extraction_metadata(label)
    metadata["claimed_outcomes"][0]["goal_value_paths"] = ["downloaded_files"]
    metadata["terminal_verifier_expectations"][0]["goal_value_paths"] = ["downloaded_files"]
    return metadata


class TestDownloadDescriptorLeak:
    """A run cannot reveal this arm: the run succeeds and the path lands in workflow output.
    The registration-detection arms were deleted precisely because a run does reveal those."""

    def test_returned_path_or_url_is_flagged(self) -> None:
        for code in (
            'return {"downloaded_file_path": p}',
            'return {"download_url": u}',
            'out = {"download_url": u}\nreturn out',
        ):
            assert _download_descriptor_leak_finding("b", code) is not None

    def test_clean_descriptor_and_registration_keys_are_not_flagged(self) -> None:
        assert _download_descriptor_leak_finding("b", 'return {"saved_as": n}') is None
        assert _download_descriptor_leak_finding("b", 'return {"downloaded_files": [f]}') is None


class TestDownloadShapesThatMustNotBeFlagged:
    """Negative space for the surviving descriptor-leak arm: a clean descriptor, an extraction
    block, and a non-download block must all normalize without a violation."""

    def test_expect_download_idiom_with_descriptor_passes(self) -> None:
        code = """
        async with page.expect_download() as dl_info:
            await page.click("a#statement-pdf")
        return {"saved_as": dl_info.value.suggested_filename}
        """
        normalized, error = _normalize_code_artifact_metadata(
            [_download_intent_metadata("dl_block")],
            _extraction_code_block_yaml("dl_block", code),
        )
        assert error is None
        assert list(normalized.keys()) == ["dl_block"]

    def test_extraction_block_is_not_treated_as_download_intent(self) -> None:
        code = """
        await page.goto("https://example.com/")
        return {"records": [{"number": "1"}]}
        """
        normalized, error = _normalize_code_artifact_metadata(
            [_extraction_metadata("ex_block", ["records[].number"])],
            _extraction_code_block_yaml("ex_block", code),
        )
        assert error is None
        assert list(normalized.keys()) == ["ex_block"]

    def test_non_download_non_extraction_block_passes(self) -> None:
        code = """
        await page.click("a#statement-pdf")
        return {"clicked": True}
        """
        normalized, error = _normalize_code_artifact_metadata(
            [_non_extraction_metadata("dl_block")],
            _extraction_code_block_yaml("dl_block", code),
        )
        assert error is None
        assert list(normalized.keys()) == ["dl_block"]
