from __future__ import annotations

from skyvern.forge.sdk.copilot.result_evidence import loaded_result_composition_evidence_from_page


def test_loaded_result_evidence_counts_populated_tables() -> None:
    evidence = loaded_result_composition_evidence_from_page(
        {
            "result_containers": [
                {"tag": "table", "selector": "#results", "row_count": 2},
                {"selector": "#cards", "sample_rows": ["May 2026 statement available"]},
            ],
        }
    )

    assert evidence is not None
    assert evidence.result_container_count == 2
    assert evidence.table_result_container_count == 1


def test_caption_only_result_container_is_not_composition_evidence() -> None:
    evidence = loaded_result_composition_evidence_from_page(
        {
            "result_containers": [
                {"selector": "#count", "text": "12 matching records"},
                {"selector": "#results", "text": "3 matching records"},
                {"selector": "#none", "text": "No results found"},
                {"selector": "#empty", "text": "0 results found"},
            ],
        }
    )

    assert evidence is None


def test_result_container_text_uses_non_ui_chrome_tokens() -> None:
    evidence = loaded_result_composition_evidence_from_page(
        {"result_containers": [{"selector": "#results", "text": "Statement May 2026 available"}]}
    )

    assert evidence is not None
    assert evidence.result_container_count == 1


def test_standalone_no_can_be_result_payload_text() -> None:
    evidence = loaded_result_composition_evidence_from_page(
        {"result_containers": [{"selector": "#results", "text": "No"}]}
    )

    assert evidence is not None
    assert evidence.result_container_count == 1


def test_transient_status_result_container_is_not_composition_evidence() -> None:
    evidence = loaded_result_composition_evidence_from_page(
        {
            "result_containers": [
                {"selector": "#loading", "text": "Loading..."},
                {"selector": "#wait", "text": "Please wait"},
                {"selector": "#pagination", "text": "Showing 1-20 of 100"},
            ],
        }
    )

    assert evidence is None


def test_deep_result_container_payload_does_not_recurse_unbounded() -> None:
    nested: object = "value"
    for _ in range(20):
        nested = [nested]

    evidence = loaded_result_composition_evidence_from_page(
        {"result_containers": [{"selector": "#results", "sample_rows": [nested]}]}
    )

    assert evidence is None
