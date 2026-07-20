from __future__ import annotations

from types import SimpleNamespace

from skyvern.forge.sdk.copilot.composition_evidence import parse_composition_structured
from skyvern.forge.sdk.copilot.mcp_adapter import _restore_post_hook_context, _snapshot_post_hook_context
from skyvern.forge.sdk.copilot.output_extraction_plan import (
    LiveReadKind,
    ShapeExpectation,
    ValueCardinality,
    ValueShape,
    _exact_path,
    _key_value_shape_bindings,
    _table_shape_bindings,
    array_parent_path,
    derive_requested_output_extraction_plan,
    resolve_shape_expectations_by_path,
    value_matches_shape,
)

_SYNTHETIC_SHAPE_REGISTRY = {
    "widget_id": ShapeExpectation(ValueShape.NUMERIC_ID, ValueCardinality.SCALAR, id_digit_length=8),
    "depot": ShapeExpectation(ValueShape.POSTAL_ADDRESS, ValueCardinality.COLUMN),
    "phase": ShapeExpectation(ValueShape.CATEGORICAL_TOKEN, ValueCardinality.COLUMN),
    "final_phase": ShapeExpectation(ValueShape.CATEGORICAL_TOKEN, ValueCardinality.SCALAR),
}
_SYNTHETIC_REQUESTED_PATHS = {
    "output.widget_id",
    "output.sites",
    "output.sites[].depot",
    "output.sites[].phase",
    "output.final_phase",
}

LABELS_BY_PATH = {
    "output.record_id": ("Record Identifier",),
    "output.records[].detail": ("Detail",),
    "output.records[].state": ("State",),
    "output.overall_state": ("Overall State",),
}


def _flow_packet(*, visible: bool = True, ambiguous_id: bool = False, truncated: bool = False) -> dict[str, object]:
    id_relation = {
        "key_text": "Record Identifier",
        "container_selector": ".kv",
        "container_match_count": 7,
        "container_position": 0,
        "value_child_index": 1,
        "direct_child_count": 2,
        "visible": visible,
        "value_visible": visible,
    }
    overall_relation = dict(id_relation, key_text="Overall State", container_position=2)
    relations = [id_relation, overall_relation]
    if ambiguous_id:
        relations.append(dict(id_relation, container_position=1))
    return {
        "step": 4,
        "reached_via": "interaction",
        "had_bounded_schema": True,
        "evidence": {
            "source_tool": "scout_interaction",
            "interaction_tool": "click",
            "interaction_selector": "#show-details",
            "inspection_warnings": [],
            "result_containers_truncated": truncated,
            "key_value_relations_truncated": False,
            "key_value_relations": relations,
            "result_containers": [
                {
                    "tag": "table",
                    "selector": "#records",
                    "selector_match_count": 1,
                    "visible": visible,
                    "span_free": True,
                    "nested_table_free": True,
                    "headers": [
                        {"text": "Record", "column_index": 0},
                        {"text": "Detail", "column_index": 1},
                        {"text": "State", "column_index": 2},
                    ],
                    "row_selector": "#records tbody tr",
                    "row_count": 3,
                    "rows_truncated": False,
                    "sample_rows": [f"Record {row_index} Detail State" for row_index in range(3)],
                    "rows": [
                        {
                            "row_index": row_index,
                            "visible": True,
                            "has_row_header": False,
                            "cells": [
                                {"column_index": 0, "visible": True},
                                {"column_index": 1, "visible": True},
                                {"column_index": 2, "visible": True},
                            ],
                        }
                        for row_index in range(3)
                    ],
                }
            ],
        },
    }


def _derive(*, packet: dict[str, object] | None = None):
    return derive_requested_output_extraction_plan(
        flow_evidence=[packet or _flow_packet()],
        labels_by_path=LABELS_BY_PATH,
    )


def test_derives_complete_plan_from_one_visible_interaction_packet() -> None:
    plan = _derive()

    assert plan is not None
    assert plan.observation_step == 4
    assert plan.reveal.selector == "#show-details"
    assert {binding.output_path for binding in plan.live_reads} == {
        "output.record_id",
        "output.records[].detail",
        "output.records[].state",
        "output.overall_state",
    }
    assert {binding.kind for binding in plan.live_reads} == {LiveReadKind.KEY_VALUE, LiveReadKind.TABLE_COLUMN}
    assert plan.identity


def test_identical_reobservation_keeps_structural_candidate_identity() -> None:
    first = _flow_packet()
    second = _flow_packet()
    second["step"] = 5

    first_plan = _derive(packet=first)
    second_plan = _derive(packet=second)

    assert first_plan is not None
    assert second_plan is not None
    assert first_plan.observation_step != second_plan.observation_step
    assert first_plan.observation_identity == second_plan.observation_identity
    assert first_plan.identity == second_plan.identity


def test_derives_table_plan_through_production_structured_normalizer() -> None:
    raw_evidence = _flow_packet()["evidence"]
    parsed = parse_composition_structured(
        raw_evidence,
        inspected_url="https://example.com/records",
        current_url="https://example.com/records",
    )
    assert parsed is not None
    parsed.update(
        source_tool="scout_interaction",
        interaction_tool="click",
        interaction_selector="#show-details",
    )

    plan = derive_requested_output_extraction_plan(
        flow_evidence=[
            {
                "step": 4,
                "reached_via": "interaction",
                "had_bounded_schema": True,
                "evidence": parsed,
            }
        ],
        labels_by_path=LABELS_BY_PATH,
    )

    assert plan is not None
    assert {binding.output_path for binding in plan.live_reads if binding.kind == LiveReadKind.TABLE_COLUMN} == {
        "output.records[].detail",
        "output.records[].state",
    }


def test_hidden_ambiguous_truncated_and_mixed_packets_fail_closed() -> None:
    assert _derive(packet=_flow_packet(visible=False)) is None
    assert _derive(packet=_flow_packet(ambiguous_id=True)) is None
    assert _derive(packet=_flow_packet(truncated=True)) is None

    hidden_row = _flow_packet()
    hidden_row["evidence"]["result_containers"][0]["rows"][1]["visible"] = False
    assert _derive(packet=hidden_row) is None

    partial_id = _flow_packet()
    partial_id["evidence"] = dict(partial_id["evidence"], result_containers=[])
    partial_table = _flow_packet()
    partial_table["step"] = 5
    partial_table["evidence"] = dict(partial_table["evidence"], key_value_relations=[])
    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[partial_id, partial_table],
            labels_by_path={key: value for key, value in LABELS_BY_PATH.items() if key != "output.overall_state"},
        )
        is None
    )
    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[_flow_packet(), partial_table],
            labels_by_path={key: value for key, value in LABELS_BY_PATH.items() if key != "output.overall_state"},
        )
        is None
    )


def test_only_exact_configured_aliases_bind_observed_labels() -> None:
    fuzzy = _flow_packet()
    fuzzy["evidence"]["key_value_relations"][0]["key_text"] = "Approximate record value"

    assert _derive(packet=fuzzy) is None


def _reveal_relation(key_text: str, *, value_child_index: int, value_text: str = "Amount due: $3,927.75") -> dict:
    return {
        "key_text": key_text,
        "value_text": value_text,
        "container_selector": "#result",
        "container_match_count": 1,
        "container_position": 0,
        "value_child_index": value_child_index,
        "direct_child_count": 4,
        "visible": True,
        "value_visible": True,
    }


def _packet_with_reveal(*relations: dict) -> dict[str, object]:
    packet = _flow_packet()
    packet["evidence"]["key_value_relations"].extend(relations)
    return packet


def test_exact_path_rejects_empty_label_and_binds_configured_label() -> None:
    assert _exact_path("", LABELS_BY_PATH) is None
    assert _exact_path("Overall State", LABELS_BY_PATH) == "output.overall_state"


def test_multi_value_reveal_container_is_binder_inert_and_preserves_plan() -> None:
    base = _derive()
    with_reveal = _derive(
        packet=_packet_with_reveal(
            _reveal_relation("", value_child_index=1),
            _reveal_relation("", value_child_index=2, value_text="Billing period: Mar 1 - Mar 31, 2026"),
        )
    )

    assert base is not None
    assert with_reveal is not None
    assert with_reveal.identity == base.identity
    assert {binding.output_path for binding in with_reveal.live_reads} == {
        binding.output_path for binding in base.live_reads
    }


def test_single_value_reveal_heading_absent_from_labels_does_not_poison_plan() -> None:
    base = _derive()
    with_reveal = _derive(packet=_packet_with_reveal(_reveal_relation("March 2026 statement", value_child_index=1)))

    assert base is not None
    assert with_reveal is not None
    assert with_reveal.identity == base.identity


def test_single_value_reveal_heading_colliding_with_bound_label_yields_bounded_none() -> None:
    assert _derive(packet=_packet_with_reveal(_reveal_relation("Overall State", value_child_index=1))) is None


def test_reveal_truncation_signal_voids_plan_without_pass_one_flag() -> None:
    base = _derive()
    warned = _packet_with_reveal(
        _reveal_relation("", value_child_index=1),
        _reveal_relation("", value_child_index=2, value_text="Billing period: Mar 1 - Mar 31, 2026"),
    )
    warned_evidence = warned["evidence"]
    assert isinstance(warned_evidence, dict)
    warned_evidence["inspection_warnings"] = ["reveal_relations_truncated"]

    assert base is not None
    assert warned_evidence["key_value_relations_truncated"] is False
    assert _derive(packet=warned) is None


def test_table_identity_coordinate_and_shape_ambiguity_fail_closed() -> None:
    nested = _flow_packet()
    nested["evidence"]["result_containers"][0]["nested_table_free"] = False
    assert _derive(packet=nested) is None

    row_header = _flow_packet()
    row_header["evidence"]["result_containers"][0]["rows"][0]["has_row_header"] = True
    assert _derive(packet=row_header) is None

    shifted_cell = _flow_packet()
    shifted_cell["evidence"]["result_containers"][0]["rows"][1]["cells"][1]["column_index"] = 2
    assert _derive(packet=shifted_cell) is None

    span = _flow_packet()
    span["evidence"]["result_containers"][0]["span_free"] = False
    assert _derive(packet=span) is None


def test_jit_plan_cannot_see_flow_evidence_rolled_back_after_failed_hook() -> None:
    ctx = SimpleNamespace(flow_evidence=[])
    snapshot = _snapshot_post_hook_context(ctx)
    ctx.flow_evidence.append(_flow_packet())
    assert _derive(packet=ctx.flow_evidence[0]) is not None

    _restore_post_hook_context(ctx, snapshot)

    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=ctx.flow_evidence,
            labels_by_path={"output.record_id": ("Record Identifier",)},
        )
        is None
    )


def test_numeric_id_matches_only_exact_digit_length() -> None:
    expectation = _SYNTHETIC_SHAPE_REGISTRY["widget_id"]
    assert value_matches_shape("12345678", expectation) is True
    assert value_matches_shape("1234567", expectation) is False
    assert value_matches_shape("123456789", expectation) is False
    assert value_matches_shape("1234abcd", expectation) is False


def test_postal_address_requires_number_lead_alpha_and_region_token() -> None:
    expectation = _SYNTHETIC_SHAPE_REGISTRY["depot"]
    assert value_matches_shape("221 Baker Street Boston MA", expectation) is True
    assert value_matches_shape("500 Industrial Way Fremont CA", expectation) is True
    assert value_matches_shape("500 Industrial Way Fremont 94538", expectation) is True
    assert value_matches_shape("Industrial Way Fremont CA", expectation) is False
    assert value_matches_shape("500 Way", expectation) is False


def test_categorical_token_excludes_digits_commas_and_long_phrases() -> None:
    expectation = _SYNTHETIC_SHAPE_REGISTRY["final_phase"]
    assert value_matches_shape("Complete", expectation) is True
    assert value_matches_shape("In Progress", expectation) is True
    assert value_matches_shape("Not Yet Started Phase", expectation) is False
    assert value_matches_shape("Phase 2", expectation) is False
    assert value_matches_shape("Acme, Inc", expectation) is False


def test_free_text_expectation_never_matches() -> None:
    expectation = ShapeExpectation(ValueShape.FREE_TEXT, ValueCardinality.SCALAR)
    assert value_matches_shape("anything at all", expectation) is False


def test_resolve_maps_leaf_segments_and_enforces_cardinality_and_leaf_only() -> None:
    resolved = resolve_shape_expectations_by_path(_SYNTHETIC_REQUESTED_PATHS, _SYNTHETIC_SHAPE_REGISTRY)
    assert resolved == {
        "output.widget_id": _SYNTHETIC_SHAPE_REGISTRY["widget_id"],
        "output.sites[].depot": _SYNTHETIC_SHAPE_REGISTRY["depot"],
        "output.sites[].phase": _SYNTHETIC_SHAPE_REGISTRY["phase"],
        "output.final_phase": _SYNTHETIC_SHAPE_REGISTRY["final_phase"],
    }
    assert "output.sites" not in resolved


def test_resolve_excludes_cardinality_mismatch() -> None:
    registry = {"widget_id": ShapeExpectation(ValueShape.NUMERIC_ID, ValueCardinality.COLUMN, id_digit_length=8)}
    assert resolve_shape_expectations_by_path({"output.widget_id"}, registry) == {}


def test_resolve_returns_empty_without_registry() -> None:
    assert resolve_shape_expectations_by_path(_SYNTHETIC_REQUESTED_PATHS, None) == {}
    assert resolve_shape_expectations_by_path(_SYNTHETIC_REQUESTED_PATHS, {}) == {}


def test_array_parent_path_only_for_array_leaves() -> None:
    assert array_parent_path("output.sites[].depot") == "output.sites"
    assert array_parent_path("output.widget_id") is None


def _shape_kv_relation(value_text: str) -> dict[str, object]:
    return {
        "key_text": "Reference",
        "value_text": value_text,
        "container_selector": ".kv",
        "container_match_count": 1,
        "container_position": 0,
        "value_child_index": 1,
        "direct_child_count": 2,
        "visible": True,
        "value_visible": True,
    }


def test_key_value_shape_binding_matches_scalar_shape_on_value_text() -> None:
    resolved = resolve_shape_expectations_by_path(_SYNTHETIC_REQUESTED_PATHS, _SYNTHETIC_SHAPE_REGISTRY)
    bindings = _key_value_shape_bindings({"key_value_relations": [_shape_kv_relation("12345678")]}, resolved)
    assert [binding.output_path for binding in bindings] == ["output.widget_id"]
    assert bindings[0].kind == LiveReadKind.KEY_VALUE


def test_key_value_shape_binding_requires_present_value_text() -> None:
    resolved = resolve_shape_expectations_by_path(_SYNTHETIC_REQUESTED_PATHS, _SYNTHETIC_SHAPE_REGISTRY)
    relation = _shape_kv_relation("12345678")
    del relation["value_text"]
    assert _key_value_shape_bindings({"key_value_relations": [relation]}, resolved) == []


def _shape_table_packet() -> dict[str, object]:
    return {
        "result_containers": [
            {
                "tag": "table",
                "selector": "#sites",
                "selector_match_count": 1,
                "visible": True,
                "span_free": True,
                "nested_table_free": True,
                "row_selector": "#sites tbody tr",
                "headers": [
                    {"text": "Location", "column_index": 0},
                    {"text": "Stage", "column_index": 1},
                ],
                "row_count": 3,
                "rows_truncated": False,
                "sample_rows": ["r0", "r1", "r2"],
                "rows": [
                    {
                        "row_index": 0,
                        "visible": True,
                        "has_row_header": False,
                        "cells": [
                            {
                                "column_index": 0,
                                "visible": True,
                                "has_text": True,
                                "text": "221 Baker Street Boston MA",
                            },
                            {"column_index": 1, "visible": True, "has_text": True, "text": "Complete"},
                        ],
                    },
                    {
                        "row_index": 1,
                        "visible": True,
                        "has_row_header": False,
                        "cells": [
                            {"column_index": 0, "visible": True, "has_text": True, "text": "17 Elm Avenue Boston MA"},
                            {"column_index": 1, "visible": True, "has_text": True, "text": "Complete"},
                        ],
                    },
                    {
                        "row_index": 2,
                        "visible": True,
                        "has_row_header": False,
                        "cells": [
                            {"column_index": 0, "visible": True, "has_text": True, "text": "9 Oak Road Reno NV 89501"},
                            {"column_index": 1, "visible": True, "has_text": True, "text": "Pending"},
                        ],
                    },
                ],
            }
        ]
    }


def test_table_shape_bindings_match_columns_by_value_shape() -> None:
    resolved = resolve_shape_expectations_by_path(_SYNTHETIC_REQUESTED_PATHS, _SYNTHETIC_SHAPE_REGISTRY)
    bindings = _table_shape_bindings(_shape_table_packet(), resolved)
    bound = {binding.output_path: binding.column_index for binding in bindings}
    assert bound == {"output.sites[].depot": 0, "output.sites[].phase": 1}


def test_categorical_column_requires_repetition() -> None:
    resolved = resolve_shape_expectations_by_path(_SYNTHETIC_REQUESTED_PATHS, _SYNTHETIC_SHAPE_REGISTRY)
    packet = _shape_table_packet()
    packet["result_containers"][0]["rows"][1]["cells"][1]["text"] = "Started"
    bindings = _table_shape_bindings(packet, resolved)
    assert [binding.output_path for binding in bindings] == ["output.sites[].depot"]
