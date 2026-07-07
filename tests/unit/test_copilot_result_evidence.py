from __future__ import annotations

import json

from skyvern.forge.sdk.copilot.result_evidence import (
    _COMPOSITION_TARGET_SUMMARY_CHAR_BUDGET,
    loaded_result_composition_evidence_from_page,
    loaded_result_composition_target_summary,
)


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
    assert loaded_result_composition_target_summary(evidence)["targets"] == [
        {
            "selector": "#results",
            "is_table": True,
            "row_count": 2,
            "structure_signature": evidence.targets[0].structure_signature,
        },
        {
            "selector": "#cards",
            "is_table": False,
            "sample_rows": ["May 2026 statement available"],
            "structure_signature": evidence.targets[1].structure_signature,
        },
    ]


def test_loaded_result_target_summary_includes_bounded_table_rows() -> None:
    evidence = loaded_result_composition_evidence_from_page(
        {
            "result_containers": [
                {
                    "tag": "table",
                    "selector": "#results",
                    "row_selector": "tr.statement",
                    "row_count": 12,
                    "sample_rows": [
                        {"period": "May 2026", "amount": "$42.00", "download": "available"},
                        {"period": "April 2026", "amount": "$39.00", "download": "available"},
                        {"period": "March 2026", "amount": "$37.00", "download": "available"},
                        {"period": "February 2026", "amount": "$34.00", "download": "available"},
                    ],
                    "observation_id": "obs-1",
                    "evidence_source": "evaluate",
                }
            ],
        }
    )

    assert evidence is not None
    summary = loaded_result_composition_target_summary(evidence)
    assert summary["targets"] == [
        {
            "selector": "#results",
            "is_table": True,
            "row_selector": "tr.statement",
            "row_count": 12,
            "sample_rows": [
                '{"amount": "$42.00", "download": "available", "period": "May 2026"}',
                '{"amount": "$39.00", "download": "available", "period": "April 2026"}',
                '{"amount": "$37.00", "download": "available", "period": "March 2026"}',
            ],
            "structure_signature": evidence.targets[0].structure_signature,
            "evidence_source": "evaluate",
            "observation_id": "obs-1",
        }
    ]


def test_loaded_result_target_summary_includes_bounded_non_table_text() -> None:
    evidence = loaded_result_composition_evidence_from_page(
        {
            "result_containers": [
                {
                    "selector": "#cards",
                    "text": "May 2026 statement available. Download link ready." * 20,
                    "row_count": 1,
                }
            ],
        }
    )

    assert evidence is not None
    target = loaded_result_composition_target_summary(evidence)["targets"][0]
    assert target["selector"] == "#cards"
    assert target["is_table"] is False
    assert target["row_count"] == 1
    assert target["text_excerpt"].startswith("May 2026 statement available")
    assert len(target["text_excerpt"]) <= 240


def test_loaded_result_structure_signature_ignores_selector_presence_and_text() -> None:
    variants = [
        loaded_result_composition_evidence_from_page(
            {"result_containers": [{"tag": "table", "row_count": 1, "sample_rows": ["May 2026"]}]}
        ),
        loaded_result_composition_evidence_from_page(
            {
                "result_containers": [
                    {
                        "tag": "table",
                        "selector": "#results",
                        "row_count": 1,
                        "sample_rows": ["May 2026"],
                    }
                ]
            }
        ),
        loaded_result_composition_evidence_from_page(
            {
                "result_containers": [
                    {
                        "tag": "table",
                        "row_selector": "tr.statement",
                        "row_count": 1,
                        "sample_rows": ["May 2026"],
                    }
                ]
            }
        ),
        loaded_result_composition_evidence_from_page(
            {
                "result_containers": [
                    {
                        "tag": "table",
                        "selector": '#account-123456-JaneCustomer-results[data-customer="Jane Customer"]',
                        "row_selector": 'tr[data-account="987654321"][data-customer="Jane Customer"]',
                        "row_count": 1,
                        "sample_rows": ["May 2026"],
                    }
                ]
            }
        ),
    ]

    assert all(variant is not None for variant in variants)
    root_signatures = {variant.structure_signature for variant in variants if variant is not None}
    target_signatures = {variant.targets[0].structure_signature for variant in variants if variant is not None}
    assert len(root_signatures) == 1
    assert len(target_signatures) == 1


def test_loaded_result_structure_signature_ignores_row_selector_text_changes() -> None:
    base = loaded_result_composition_evidence_from_page(
        {
            "result_containers": [
                {
                    "selector": "#results",
                    "row_selector": "tr.statement",
                    "row_count": 1,
                    "sample_rows": ["May 2026"],
                }
            ]
        }
    )
    row_selector_changed = loaded_result_composition_evidence_from_page(
        {
            "result_containers": [
                {
                    "selector": "#results",
                    "row_selector": 'tr[data-account="987654321"][data-customer="Jane Customer"]',
                    "row_count": 1,
                    "sample_rows": ["May 2026"],
                }
            ]
        }
    )

    assert base is not None
    assert row_selector_changed is not None
    assert base.structure_signature == row_selector_changed.structure_signature
    assert base.targets[0].structure_signature == row_selector_changed.targets[0].structure_signature


def test_loaded_result_structure_signature_changes_for_structural_metadata() -> None:
    base = loaded_result_composition_evidence_from_page(
        {"result_containers": [{"selector": "#results", "row_count": 1, "sample_rows": ["May 2026"]}]}
    )
    row_count_changed = loaded_result_composition_evidence_from_page(
        {"result_containers": [{"selector": "#results", "row_count": 2, "sample_rows": ["May 2026"]}]}
    )
    table_shape_changed = loaded_result_composition_evidence_from_page(
        {
            "result_containers": [
                {
                    "tag": "table",
                    "selector": "#results",
                    "row_count": 1,
                    "sample_rows": ["May 2026"],
                }
            ]
        }
    )

    assert base is not None
    assert row_count_changed is not None
    assert table_shape_changed is not None
    assert base.structure_signature != row_count_changed.structure_signature
    assert base.targets[0].structure_signature != row_count_changed.targets[0].structure_signature
    assert base.structure_signature != table_shape_changed.structure_signature
    assert base.targets[0].structure_signature != table_shape_changed.targets[0].structure_signature


def test_loaded_result_target_signature_ignores_sample_rows_and_text() -> None:
    first = loaded_result_composition_evidence_from_page(
        {
            "result_containers": [
                {
                    "selector": "#results",
                    "row_selector": "tr",
                    "row_count": 1,
                    "sample_rows": ["Jane Customer account 123"],
                    "text": "Jane Customer balance due",
                }
            ]
        }
    )
    second = loaded_result_composition_evidence_from_page(
        {
            "result_containers": [
                {
                    "selector": "#results",
                    "row_selector": "tr",
                    "row_count": 1,
                    "sample_rows": ["Different Customer account 999"],
                    "text": "Different Customer balance due",
                }
            ]
        }
    )

    assert first is not None
    assert second is not None
    assert first.targets[0].structure_signature == second.targets[0].structure_signature


def test_loaded_result_target_signature_ignores_evidence_identity() -> None:
    first = loaded_result_composition_evidence_from_page(
        {
            "result_containers": [
                {
                    "selector": "#results",
                    "row_selector": "tr",
                    "row_count": 1,
                    "sample_rows": ["May 2026"],
                    "evidence_source": "evaluate",
                    "observation_id": "obs-1",
                }
            ]
        }
    )
    second = loaded_result_composition_evidence_from_page(
        {
            "result_containers": [
                {
                    "selector": "#results",
                    "row_selector": "tr",
                    "row_count": 1,
                    "sample_rows": ["May 2026"],
                    "evidence_source": "inspect_page",
                    "observation_id": "obs-2",
                }
            ]
        }
    )

    assert first is not None
    assert second is not None
    assert first.targets[0].structure_signature == second.targets[0].structure_signature


def test_loaded_result_target_summary_enforces_internal_budget_for_max_structural_target() -> None:
    evidence = loaded_result_composition_evidence_from_page(
        {
            "result_containers": [
                {
                    "tag": "table",
                    "selector": "#results-" + ("s" * 240),
                    "row_selector": "tr.statement-" + ("r" * 240),
                    "row_count": 12,
                    "sample_rows": ["Jane Customer account 123 " * 20],
                    "text": "Jane Customer statement ready " * 20,
                    "evidence_source": "evaluate-" + ("e" * 240),
                    "observation_id": "obs-" + ("o" * 240),
                }
            ],
        }
    )

    assert evidence is not None
    summary = loaded_result_composition_target_summary(evidence)
    target = summary["targets"][0]
    assert len(json.dumps(summary, default=str, separators=(",", ":"))) <= _COMPOSITION_TARGET_SUMMARY_CHAR_BUDGET
    assert target["selector"].startswith("#results-")
    assert target["is_table"] is True
    assert target["row_count"] == 12
    assert target["structure_signature"]
    assert summary["structure_signature"]
    assert "sample_rows" not in target
    assert "text_excerpt" not in target
    assert "evidence_source" not in target
    assert "observation_id" not in target


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
