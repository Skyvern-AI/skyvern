import unicodedata
from typing import Any

from structlog.testing import capture_logs

from skyvern.forge.sdk.workflow.service import _merge_workflow_run_errors

SHORT_SECRET = "587"
MASK = "[redacted]"


def _typed_error(code: str, reasoning: str) -> dict[str, Any]:
    return {
        "error_code": code,
        "reasoning": reasoning,
        "confidence_float": 1.0,
        "error_type": "USER_DEFINED_ERROR",
    }


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _string_values(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _string_values(item)]
    return []


def test_aggregation_scrubs_short_secret_from_typed_reasoning() -> None:
    errors = _merge_workflow_run_errors(
        [],
        [("block", [], None, {"errors": [_typed_error("typed", f"PIN {SHORT_SECRET} leaked")]}, "code")],
        registered_secret_values={SHORT_SECRET},
    )

    assert errors == [_typed_error("typed", f"PIN {MASK} leaked")]


def test_aggregation_scrubs_short_secret_from_legacy_reasoning() -> None:
    errors = _merge_workflow_run_errors(
        [],
        [("block", ["legacy"], f"PIN {SHORT_SECRET} leaked", None, "code")],
        registered_secret_values={SHORT_SECRET},
    )

    assert errors == [{"error_code": "legacy", "reasoning": f"PIN {MASK} leaked", "confidence_float": 1.0}]


def test_aggregation_normalizes_then_scrubs_secret_from_legacy_reasoning() -> None:
    errors = _merge_workflow_run_errors(
        [],
        [("block", ["legacy"], "PIN 5\u200b87 leaked", None, "code")],
        registered_secret_values={SHORT_SECRET},
    )

    reasoning = errors[0]["reasoning"]
    assert reasoning == f"PIN {MASK} leaked"
    assert SHORT_SECRET not in "".join(
        character for character in reasoning if unicodedata.category(character)[0] != "C"
    )
    assert all(unicodedata.category(character)[0] != "C" for character in reasoning)


def test_aggregation_scrubs_short_card_secrets_from_all_reasoning_sinks() -> None:
    errors = _merge_workflow_run_errors(
        [],
        [
            (
                "block",
                ["invalid_dob"],
                "user retried 12 times",
                {"errors": [_typed_error("invalid_dob", "user retried 12 times")]},
                "code",
            )
        ],
        registered_secret_values={"12", "587"},
    )

    assert errors == [_typed_error("invalid_dob", "user retried [redacted] times")]
    # Short secrets can collide with numeric metadata in repr; inspect recursively collected strings only.
    assert all(secret not in text for secret in ("12", "587") for text in _string_values(errors))


def test_aggregation_skips_code_containing_short_secret_and_warns() -> None:
    with capture_logs() as logs:
        errors = _merge_workflow_run_errors(
            [],
            [
                ("legacy", [f"legacy_{SHORT_SECRET}"], "failure", None, "code"),
                ("typed", [], None, {"errors": [_typed_error(f"typed_{SHORT_SECRET}", "failure")]}, "code"),
            ],
            registered_secret_values={SHORT_SECRET},
            workflow_run_id="wr_test",
        )

    assert errors == []
    assert len(logs) == 2
    assert {record["row_origin"] for record in logs} == {"legacy", "typed"}
    assert {record["workflow_run_block_id"] for record in logs} == {"legacy", "typed"}
    assert all(record["block_label"] is None for record in logs)
    assert all(record["workflow_run_id"] == "wr_test" for record in logs)
    # Short secrets can collide with numeric log metadata in repr; inspect recursively collected strings only.
    assert all(SHORT_SECRET not in text for record in logs for text in _string_values(record))


def test_aggregation_normalizes_then_drops_code_containing_short_secret_and_warns() -> None:
    with capture_logs() as logs:
        errors = _merge_workflow_run_errors(
            [],
            [("legacy", ["ERR_5\u200b87"], "failure", None, "code")],
            registered_secret_values={SHORT_SECRET},
            workflow_run_id="wr_test",
        )

    assert errors == []
    assert len(logs) == 1
    assert logs[0]["event"] == "Dropped workflow error row because its code contains a registered secret"
    assert logs[0]["row_origin"] == "legacy"


def test_aggregation_preserves_non_secret_category_c_reasoning_byte_for_byte() -> None:
    reasoning = "network\u200b failure"

    errors = _merge_workflow_run_errors(
        [],
        [("block", ["legacy"], reasoning, None, "code")],
        registered_secret_values={SHORT_SECRET},
    )

    assert errors[0]["reasoning"] == reasoning


def test_aggregation_preserves_only_category_c_reasoning_byte_for_byte() -> None:
    reasoning = "\u200b"

    errors = _merge_workflow_run_errors(
        [],
        [("block", ["legacy"], reasoning, None, "code")],
        registered_secret_values={SHORT_SECRET},
    )

    assert errors[0]["reasoning"] == reasoning


def test_aggregation_preserves_empty_legacy_reasoning_with_registered_secrets() -> None:
    for failure_reason in (None, ""):
        errors = _merge_workflow_run_errors(
            [],
            [("block", ["legacy"], failure_reason, None, "code")],
            registered_secret_values={SHORT_SECRET},
        )

        assert errors == [{"error_code": "legacy", "reasoning": "", "confidence_float": 1.0}]


def test_aggregation_replaces_reasoning_that_is_exactly_a_registered_secret() -> None:
    errors = _merge_workflow_run_errors(
        [],
        [("block", ["legacy"], SHORT_SECRET, None, "code")],
        registered_secret_values={SHORT_SECRET},
    )

    assert errors[0]["reasoning"] == MASK


def test_aggregation_without_context_or_registry_passes_through() -> None:
    typed = _typed_error(f"typed_{SHORT_SECRET}", f"PIN {SHORT_SECRET} leaked")

    errors = _merge_workflow_run_errors(
        [],
        [("block", [f"legacy_{SHORT_SECRET}"], f"PIN {SHORT_SECRET} leaked", {"errors": [typed]}, "code")],
    )

    assert errors == [
        {"error_code": f"legacy_{SHORT_SECRET}", "reasoning": f"PIN {SHORT_SECRET} leaked", "confidence_float": 1.0},
        typed,
    ]


def test_secret_scrub_preserves_order_dedupe_upgrade_and_100_cap() -> None:
    task_errors = [
        {"error_code": f"task_{index}", "reasoning": f"task {index}", "confidence_float": 1.0} for index in range(98)
    ]
    task_errors.append(dict(task_errors[0]))
    typed = _typed_error("upgrade", f"typed {SHORT_SECRET}")

    errors = _merge_workflow_run_errors(
        task_errors,
        [
            ("block", ["upgrade", "last"], f"legacy {SHORT_SECRET}", {"errors": [typed]}, "code"),
            ("capped", ["excluded"], "excluded", None, "code"),
        ],
        registered_secret_values={SHORT_SECRET},
    )

    assert len(errors) == 100
    assert [error["error_code"] for error in errors[:98]] == [f"task_{index}" for index in range(98)]
    assert errors[98] == _typed_error("upgrade", f"typed {MASK}")
    assert errors[99] == {"error_code": "last", "reasoning": f"legacy {MASK}", "confidence_float": 1.0}
