from __future__ import annotations

from dataclasses import replace

from skyvern.forge.sdk.copilot.composition_evidence import SCOUT_INTERACTION_EVIDENCE_TOOL, has_witnessed_value_content
from skyvern.forge.sdk.copilot.output_extraction_plan import ShapeExpectation, ValueCardinality, ValueShape
from skyvern.forge.sdk.copilot.result_evidence import (
    _SCOUT_INTERACTION_SOURCE_TOOL,
    ScoutObservedBinding,
    mint_scout_observation_contract,
    scout_observation_bound_paths,
    scout_observation_contract_valid,
)

_SHAPE_REGISTRY_BY_PATH = {
    "output.widget_id": ShapeExpectation(ValueShape.NUMERIC_ID, ValueCardinality.SCALAR, id_digit_length=8),
    "output.sites[].depot": ShapeExpectation(ValueShape.POSTAL_ADDRESS, ValueCardinality.COLUMN),
    "output.sites[].phase": ShapeExpectation(ValueShape.CATEGORICAL_TOKEN, ValueCardinality.COLUMN),
}
_SHAPE_LABELS_BY_PATH = {
    "output.widget_id": ("the eight digit widget identifier",),
    "output.sites": ("the list of build sites",),
    "output.sites[].depot": ("each site depot postal address",),
    "output.sites[].phase": ("each site current build phase",),
}


def _kv_relation(key_text: str, *, position: int = 0) -> dict[str, object]:
    return {
        "key_text": key_text,
        "container_selector": ".kv",
        "container_match_count": 7,
        "container_position": position,
        "value_child_index": 1,
        "direct_child_count": 2,
        "visible": True,
        "value_visible": True,
    }


def _kv_page_evidence(*, key_text: str = "Overall Credentialing Result") -> dict[str, object]:
    return {
        "current_url": "https://example.com/provider",
        "inspection_warnings": [],
        "result_containers_truncated": False,
        "key_value_relations_truncated": False,
        "key_value_relations": [_kv_relation(key_text)],
        "result_containers": [],
    }


def _table_row(row_index: int, *, status_has_text: bool) -> dict[str, object]:
    return {
        "row_index": row_index,
        "visible": True,
        "has_row_header": False,
        "cells": [
            {"column_index": 0, "visible": True, "has_text": True},
            {"column_index": 1, "visible": True, "has_text": status_has_text},
        ],
    }


def _table_page_evidence(*, status_has_text: bool = True) -> dict[str, object]:
    return {
        "current_url": "https://example.com/records",
        "inspection_warnings": [],
        "result_containers_truncated": False,
        "key_value_relations_truncated": False,
        "key_value_relations": [],
        "result_containers": [
            {
                "tag": "table",
                "selector": "#records",
                "selector_match_count": 1,
                "visible": True,
                "span_free": True,
                "nested_table_free": True,
                "headers": [
                    {"text": "Record", "column_index": 0},
                    {"text": "Status", "column_index": 1},
                ],
                "row_selector": "#records tbody tr",
                "row_count": 2,
                "rows_truncated": False,
                "sample_rows": ["Row 0 Active", "Row 1 Active"],
                "rows": [
                    _table_row(0, status_has_text=status_has_text),
                    _table_row(1, status_has_text=status_has_text),
                ],
            }
        ],
    }


def test_mint_binds_key_value_path_via_capture_invariant_without_bounded_schema() -> None:
    contract = mint_scout_observation_contract(
        _kv_page_evidence(),
        labels_by_path={"output.overall_credentialing_result": ("Overall Credentialing Result",)},
        url="https://example.com/provider",
        has_bounded_page_schema=False,
    )

    assert contract is not None
    assert scout_observation_contract_valid(contract)
    assert scout_observation_bound_paths(contract) == {"output.overall_credentialing_result"}
    assert contract.has_bounded_page_schema is False
    assert contract.bindings[0].value_witness == "capture_nonempty_value"
    assert contract.bindings[0].kind == "key_value"


def test_mint_binds_table_column_only_with_cell_text_witness() -> None:
    contract = mint_scout_observation_contract(
        _table_page_evidence(status_has_text=True),
        labels_by_path={"output.statuses": ("Status",)},
        url="https://example.com/records",
        has_bounded_page_schema=True,
    )

    assert contract is not None
    assert scout_observation_bound_paths(contract) == {"output.statuses"}
    assert contract.bindings[0].value_witness == "cell_text_present"
    assert contract.bindings[0].kind == "table_column"


def test_mint_excludes_table_column_when_bound_cells_lack_text() -> None:
    contract = mint_scout_observation_contract(
        _table_page_evidence(status_has_text=False),
        labels_by_path={"output.statuses": ("Status",)},
        url="https://example.com/records",
        has_bounded_page_schema=True,
    )

    assert contract is None


def test_mint_binds_only_labeled_paths_and_excludes_unbound() -> None:
    contract = mint_scout_observation_contract(
        _kv_page_evidence(),
        labels_by_path={
            "output.overall_credentialing_result": ("Overall Credentialing Result",),
            "output.npi": ("NPI",),
        },
        url="https://example.com/provider",
        has_bounded_page_schema=False,
    )

    assert contract is not None
    assert scout_observation_bound_paths(contract) == {"output.overall_credentialing_result"}


def test_mint_excludes_ambiguous_path_with_two_candidates() -> None:
    evidence = _table_page_evidence(status_has_text=True)
    evidence["key_value_relations"] = [_kv_relation("Status")]
    contract = mint_scout_observation_contract(
        evidence,
        labels_by_path={"output.statuses": ("Status",)},
        url="https://example.com/records",
        has_bounded_page_schema=True,
    )

    assert contract is None


def _reveal_shape_relation(
    *, value_text: str, value_child_index: int, key_text: str = "March 2026 statement"
) -> dict[str, object]:
    return {
        "key_text": key_text,
        "value_text": value_text,
        "container_selector": "#result",
        "container_match_count": 1,
        "container_position": 0,
        "value_child_index": value_child_index,
        "direct_child_count": 3,
        "visible": True,
        "value_visible": True,
    }


def _reveal_shape_page_evidence(relations: list[dict[str, object]]) -> dict[str, object]:
    return {
        "current_url": "https://portal.example.com/statement",
        "inspection_warnings": [],
        "result_containers_truncated": False,
        "key_value_relations_truncated": False,
        "key_value_relations": relations,
        "result_containers": [],
    }


def test_mint_binds_single_reveal_shape_relation() -> None:
    evidence = _reveal_shape_page_evidence(
        [_reveal_shape_relation(value_text="Amount due: $3,927.75", value_child_index=1)]
    )
    contract = mint_scout_observation_contract(
        evidence,
        labels_by_path={"output.amount": ("March 2026 statement",)},
        url="https://portal.example.com/statement",
        has_bounded_page_schema=False,
    )

    assert contract is not None
    assert scout_observation_bound_paths(contract) == {"output.amount"}
    assert contract.bindings[0].kind == "key_value"
    assert contract.bindings[0].value_witness == "capture_nonempty_value"


def test_mint_excludes_ambiguous_reveal_shape_siblings() -> None:
    evidence = _reveal_shape_page_evidence(
        [
            _reveal_shape_relation(value_text="Amount due: $3,927.75", value_child_index=1),
            _reveal_shape_relation(value_text="Billing period: Mar 1 - Mar 31, 2026", value_child_index=2),
        ]
    )
    contract = mint_scout_observation_contract(
        evidence,
        labels_by_path={"output.amount": ("March 2026 statement",)},
        url="https://portal.example.com/statement",
        has_bounded_page_schema=False,
    )

    assert contract is None


def test_reveal_shape_relation_witnesses_value_content() -> None:
    evidence = _reveal_shape_page_evidence(
        [_reveal_shape_relation(value_text="Amount due: $3,927.75", value_child_index=1)]
    )
    assert has_witnessed_value_content(evidence) is True


def test_mint_ignores_empty_key_reveal_siblings_but_still_witnesses() -> None:
    evidence = _reveal_shape_page_evidence(
        [
            _reveal_shape_relation(value_text="Amount due: $3,927.75", value_child_index=1, key_text=""),
            _reveal_shape_relation(value_text="Billing period: Mar 1 - Mar 31, 2026", value_child_index=2, key_text=""),
        ]
    )
    contract = mint_scout_observation_contract(
        evidence,
        labels_by_path={"output.amount": ("March 2026 statement",)},
        url="https://portal.example.com/statement",
        has_bounded_page_schema=False,
    )

    assert contract is None
    assert has_witnessed_value_content(evidence) is True


def test_mint_returns_none_on_truncated_or_warned_capture() -> None:
    truncated = _kv_page_evidence()
    truncated["result_containers_truncated"] = True
    warned = _kv_page_evidence()
    warned["inspection_warnings"] = ["partial capture"]
    size_compacted = _kv_page_evidence()
    size_compacted["size_compaction"] = {
        "original_char_count": 130_000,
        "omissions": [{"category": "key_value_relations", "omitted_count": 1, "unit": "entries"}],
    }
    labels = {"output.overall_credentialing_result": ("Overall Credentialing Result",)}

    assert (
        mint_scout_observation_contract(
            truncated, labels_by_path=labels, url="https://example.com/x", has_bounded_page_schema=True
        )
        is None
    )
    assert (
        mint_scout_observation_contract(
            warned, labels_by_path=labels, url="https://example.com/x", has_bounded_page_schema=True
        )
        is None
    )
    assert size_compacted["key_value_relations_truncated"] is False
    assert (
        mint_scout_observation_contract(
            size_compacted,
            labels_by_path=labels,
            url="https://example.com/x",
            has_bounded_page_schema=True,
        )
        is None
    )


def test_mint_returns_none_on_reveal_truncation_signal() -> None:
    # A single-binding reveal packet binds non-None with no warning; the ONLY change below is the
    # reveal_relations_truncated signal, so this proves that token is load-bearing (a cap-drop on an
    # otherwise-clean reveal voids the bind) rather than the bind already being None for another reason.
    single_binding = [_reveal_shape_relation(value_text="Amount due: $3,927.75", value_child_index=1)]
    labels = {"output.amount": ("March 2026 statement",)}

    baseline = _reveal_shape_page_evidence(single_binding)
    baseline_contract = mint_scout_observation_contract(
        baseline,
        labels_by_path=labels,
        url="https://portal.example.com/statement",
        has_bounded_page_schema=False,
    )
    assert baseline_contract is not None
    assert scout_observation_bound_paths(baseline_contract) == {"output.amount"}
    assert has_witnessed_value_content(baseline) is True

    warned = _reveal_shape_page_evidence(single_binding)
    warned["inspection_warnings"] = ["reveal_relations_truncated"]
    assert warned["key_value_relations_truncated"] is False
    assert (
        mint_scout_observation_contract(
            warned,
            labels_by_path=labels,
            url="https://portal.example.com/statement",
            has_bounded_page_schema=False,
        )
        is None
    )
    assert has_witnessed_value_content(warned) is False


def test_mint_returns_none_without_url_or_labels() -> None:
    labels = {"output.overall_credentialing_result": ("Overall Credentialing Result",)}
    assert (
        mint_scout_observation_contract(
            _kv_page_evidence(), labels_by_path=labels, url="  ", has_bounded_page_schema=True
        )
        is None
    )
    assert (
        mint_scout_observation_contract(
            _kv_page_evidence(), labels_by_path={}, url="https://example.com/x", has_bounded_page_schema=True
        )
        is None
    )


def test_tampered_contract_is_rejected_by_validation_and_bound_paths() -> None:
    contract = mint_scout_observation_contract(
        _kv_page_evidence(),
        labels_by_path={"output.overall_credentialing_result": ("Overall Credentialing Result",)},
        url="https://example.com/provider",
        has_bounded_page_schema=True,
    )
    assert contract is not None

    forged_binding = ScoutObservedBinding(
        output_path="output.injected",
        kind="key_value",
        selector=".kv",
        selector_index=0,
        matched_label="Injected",
        value_witness="capture_nonempty_value",
    )
    tampered = replace(contract, bindings=contract.bindings + (forged_binding,))

    assert scout_observation_contract_valid(tampered) is False
    assert scout_observation_bound_paths(tampered) == set()


def test_scout_interaction_source_tool_constant_matches_production() -> None:
    assert _SCOUT_INTERACTION_SOURCE_TOOL == SCOUT_INTERACTION_EVIDENCE_TOOL


def _shape_table_container() -> dict[str, object]:
    def _row(row_index: int, depot: str, phase: str) -> dict[str, object]:
        return {
            "row_index": row_index,
            "visible": True,
            "has_row_header": False,
            "cells": [
                {"column_index": 0, "visible": True, "has_text": True, "text": depot},
                {"column_index": 1, "visible": True, "has_text": True, "text": phase},
            ],
        }

    return {
        "tag": "table",
        "selector": "#sites",
        "selector_match_count": 1,
        "visible": True,
        "span_free": True,
        "nested_table_free": True,
        "headers": [
            {"text": "Loc", "column_index": 0},
            {"text": "Stage", "column_index": 1},
        ],
        "row_selector": "#sites tbody tr",
        "row_count": 3,
        "rows_truncated": False,
        "sample_rows": ["r0", "r1", "r2"],
        "rows": [
            _row(0, "12 Main Street Reno NV 89501", "Complete"),
            _row(1, "8 Oak Avenue Boston MA", "Complete"),
            _row(2, "40 Pine Road Fremont CA", "Pending"),
        ],
    }


def _shape_interaction_packet(**overrides: object) -> dict[str, object]:
    packet: dict[str, object] = {
        "current_url": "https://example.com/sites",
        "source_tool": SCOUT_INTERACTION_EVIDENCE_TOOL,
        "interaction_selector": "#reveal",
        "inspection_warnings": [],
        "result_containers_truncated": False,
        "key_value_relations_truncated": False,
        "key_value_relations": [
            {
                "key_text": "Ref Code",
                "value_text": "12345678",
                "container_selector": ".kv",
                "container_match_count": 1,
                "container_position": 0,
                "value_child_index": 1,
                "direct_child_count": 2,
                "visible": True,
                "value_visible": True,
            }
        ],
        "result_containers": [_shape_table_container()],
    }
    packet.update(overrides)
    return packet


def _mint_shape(
    packet: dict[str, object],
    *,
    registry: dict[str, ShapeExpectation] | None = _SHAPE_REGISTRY_BY_PATH,
    bounded: bool = True,
) -> object:
    return mint_scout_observation_contract(
        packet,
        labels_by_path=_SHAPE_LABELS_BY_PATH,
        url="https://example.com/sites",
        has_bounded_page_schema=bounded,
        shape_expectations_by_path=registry,
    )


def test_shape_channel_binds_zero_overlap_paths_and_lexical_alone_zero_binds() -> None:
    packet = _shape_interaction_packet()

    assert _mint_shape(packet, registry=None) is None

    contract = _mint_shape(packet)
    assert contract is not None
    assert scout_observation_bound_paths(contract) == {
        "output.widget_id",
        "output.sites",
        "output.sites[].depot",
        "output.sites[].phase",
    }


def test_shape_channel_activates_regardless_of_interaction_but_requires_witnessed_content() -> None:
    # No interaction ordinal is required: a witnessed structured first-load capture binds by value shape.
    contract = _mint_shape(_shape_interaction_packet())
    assert contract is not None
    assert scout_observation_bound_paths(contract) == {
        "output.widget_id",
        "output.sites",
        "output.sites[].depot",
        "output.sites[].phase",
    }
    # Witnessed value content (or a bounded schema) is still required.
    content_free = _shape_interaction_packet(key_value_relations=[], result_containers=[])
    assert _mint_shape(content_free, bounded=False) is None


def test_shape_channel_activates_on_content_witnessed_reveal_without_bounded_schema() -> None:
    contract = _mint_shape(_shape_interaction_packet(), bounded=False)
    assert contract is not None
    assert scout_observation_bound_paths(contract) == {
        "output.widget_id",
        "output.sites",
        "output.sites[].depot",
        "output.sites[].phase",
    }


def test_shape_binding_below_quorum_drops() -> None:
    single_path_registry = {"output.widget_id": _SHAPE_REGISTRY_BY_PATH["output.widget_id"]}
    packet = _shape_interaction_packet(result_containers=[])
    assert _mint_shape(packet, registry=single_path_registry) is None


def test_ambiguous_shape_value_drops_path() -> None:
    packet = _shape_interaction_packet(
        key_value_relations=[
            {
                "key_text": "Ref Code",
                "value_text": "12345678",
                "container_selector": ".kv",
                "container_match_count": 2,
                "container_position": 0,
                "value_child_index": 1,
                "direct_child_count": 2,
                "visible": True,
                "value_visible": True,
            },
            {
                "key_text": "Alt Code",
                "value_text": "87654321",
                "container_selector": ".kv",
                "container_match_count": 2,
                "container_position": 1,
                "value_child_index": 1,
                "direct_child_count": 2,
                "visible": True,
                "value_visible": True,
            },
        ]
    )
    contract = _mint_shape(packet)
    assert contract is not None
    assert "output.widget_id" not in scout_observation_bound_paths(contract)
    assert "output.sites[].depot" in scout_observation_bound_paths(contract)


def test_present_but_empty_value_text_drops_kv_while_absent_key_still_mints() -> None:
    empty_value = _shape_interaction_packet()
    empty_value["key_value_relations"][0]["value_text"] = "   "
    contract = _mint_shape(empty_value)
    assert contract is not None
    assert "output.widget_id" not in scout_observation_bound_paths(contract)

    lexical_labels = {"output.overall_result": ("Ref Code",)}
    legacy_relation = {
        "key_text": "Ref Code",
        "container_selector": ".kv",
        "container_match_count": 1,
        "container_position": 0,
        "value_child_index": 1,
        "direct_child_count": 2,
        "visible": True,
        "value_visible": True,
    }
    legacy_contract = mint_scout_observation_contract(
        {
            "current_url": "https://example.com/x",
            "inspection_warnings": [],
            "result_containers_truncated": False,
            "key_value_relations_truncated": False,
            "key_value_relations": [legacy_relation],
            "result_containers": [],
        },
        labels_by_path=lexical_labels,
        url="https://example.com/x",
        has_bounded_page_schema=False,
    )
    assert legacy_contract is not None
    assert scout_observation_bound_paths(legacy_contract) == {"output.overall_result"}


def test_lexical_precedence_keeps_lexical_binding_over_disagreeing_shape() -> None:
    labels = dict(_SHAPE_LABELS_BY_PATH)
    labels["output.widget_id"] = ("Ref Code",)
    packet = _shape_interaction_packet(
        key_value_relations=[
            {
                "key_text": "Ref Code",
                "value_text": "not-an-id",
                "container_selector": ".kv",
                "container_match_count": 1,
                "container_position": 0,
                "value_child_index": 1,
                "direct_child_count": 2,
                "visible": True,
                "value_visible": True,
            },
            {
                "key_text": "Serial",
                "value_text": "12345678",
                "container_selector": ".serial",
                "container_match_count": 1,
                "container_position": 0,
                "value_child_index": 1,
                "direct_child_count": 2,
                "visible": True,
                "value_visible": True,
            },
        ]
    )
    contract = mint_scout_observation_contract(
        packet,
        labels_by_path=labels,
        url="https://example.com/sites",
        has_bounded_page_schema=True,
        shape_expectations_by_path=_SHAPE_REGISTRY_BY_PATH,
    )
    assert contract is not None
    widget_binding = next(b for b in contract.bindings if b.output_path == "output.widget_id")
    assert widget_binding.selector == ".kv"
    assert widget_binding.matched_label == "Ref Code"
