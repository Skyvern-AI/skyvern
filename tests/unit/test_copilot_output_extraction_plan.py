from __future__ import annotations

from types import SimpleNamespace

from skyvern.forge.sdk.copilot.composition_evidence import parse_composition_html, parse_composition_structured
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
    bindable_candidate_headings,
    derivation_bail_reason,
    derive_requested_output_extraction_plan,
    plan_from_designations,
    resolve_shape_expectations_by_path,
    unbound_candidate_relations,
    value_matches_shape,
)
from skyvern.forge.sdk.copilot.tools._shared import _append_flow_evidence

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


def _derive(*, packet: dict[str, object] | None = None, labels_by_path: dict[str, tuple[str, ...]] | None = None):
    return derive_requested_output_extraction_plan(
        flow_evidence=[packet or _flow_packet()],
        labels_by_path=LABELS_BY_PATH if labels_by_path is None else labels_by_path,
    )


def _custody_relation(key_text: str, value_text: str, *, selector: str, child_index: int, child_count: int) -> dict:
    return {
        "key_text": key_text,
        "value_text": value_text,
        "container_selector": selector,
        "container_match_count": 1,
        "container_position": 0,
        "value_child_index": child_index,
        "direct_child_count": child_count,
        "visible": True,
        "value_visible": True,
    }


def _custody_packet(*relations: dict) -> dict[str, object]:
    return {
        "step": 4,
        "reached_via": "interaction",
        "had_bounded_schema": True,
        "evidence": {
            "source_tool": "scout_interaction",
            "interaction_selector": "#show-dashboard",
            "inspection_warnings": [],
            "result_containers_truncated": False,
            "key_value_relations_truncated": False,
            "key_value_relations": list(relations),
            "result_containers": [],
        },
    }


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


def _navigated_packet(
    *,
    source_tool: str = "inspect_page_for_composition",
    reached_via: str = "current_page",
    **overrides: object,
) -> dict[str, object]:
    packet = _flow_packet()
    evidence = dict(packet["evidence"], source_tool=source_tool)  # type: ignore[call-overload]
    for key in ("interaction_tool", "interaction_selector"):
        evidence.pop(key, None)
    evidence.update(overrides)
    return {"step": 4, "reached_via": reached_via, "had_bounded_schema": True, "evidence": evidence}


def test_deep_link_inspection_stamped_navigate_binds_without_an_anchor() -> None:
    # A target_url inspection navigates there itself and is stamped "navigate", which is the live
    # shape of the ticket's deep-link case (wr_557574..., 8 containers / 24 relations, plan underived).
    plan = derive_requested_output_extraction_plan(
        flow_evidence=[_navigated_packet(reached_via="navigate")], labels_by_path=LABELS_BY_PATH
    )

    assert plan is not None
    assert plan.reveal is None


def test_post_run_observation_never_binds_a_plan() -> None:
    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[_navigated_packet(reached_via="post_run")], labels_by_path=LABELS_BY_PATH
        )
        is None
    )


def test_derives_plan_from_a_navigated_page_with_no_reveal_interaction() -> None:
    plan = derive_requested_output_extraction_plan(flow_evidence=[_navigated_packet()], labels_by_path=LABELS_BY_PATH)

    assert plan is not None
    assert plan.reveal is None
    assert {binding.output_path for binding in plan.live_reads} == {
        "output.record_id",
        "output.records[].detail",
        "output.records[].state",
        "output.overall_state",
    }


def test_navigated_packet_binds_after_login_interactions_precede_it() -> None:
    login = _flow_packet()
    login["step"] = 2
    login["evidence"] = dict(login["evidence"], key_value_relations=[], result_containers=[])  # type: ignore[call-overload]
    dashboard = _navigated_packet()
    dashboard["step"] = 9

    plan = derive_requested_output_extraction_plan(flow_evidence=[login, dashboard], labels_by_path=LABELS_BY_PATH)

    assert plan is not None
    assert plan.observation_step == 9
    assert plan.reveal is None


def test_navigated_packet_keeps_every_completeness_guard() -> None:
    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[_navigated_packet(result_containers_truncated=True)], labels_by_path=LABELS_BY_PATH
        )
        is None
    )
    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[_navigated_packet(inspection_warnings=["stale frame"])], labels_by_path=LABELS_BY_PATH
        )
        is None
    )
    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[_navigated_packet(source_tool="self_heal_verify")], labels_by_path=LABELS_BY_PATH
        )
        is None
    )


def test_interaction_packet_without_an_anchor_still_fails_closed() -> None:
    orphan = _flow_packet()
    orphan["evidence"] = {  # type: ignore[assignment]
        key: value
        for key, value in orphan["evidence"].items()  # type: ignore[union-attr]
        if key not in {"interaction_selector", "interaction_role", "interaction_accessible_name"}
    }

    assert derive_requested_output_extraction_plan(flow_evidence=[orphan], labels_by_path=LABELS_BY_PATH) is None


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


def test_an_entry_carrying_no_schema_is_not_the_one_entry_derivation_spends() -> None:
    # Live turns bailed entry-unbounded-schema while a bindable packet sat one entry back: the
    # freshest entry was chosen for having a bindable reached_via, then rejected for having no schema.
    labels = {"output.visitors": ("Visitors",)}
    bindable = _bail_entry([_bail_relation("Visitors", selector=".tile")])
    schemaless = _bail_entry([_bail_relation("Visitors", selector=".tile")])
    schemaless["had_bounded_schema"] = False

    assert derive_requested_output_extraction_plan(flow_evidence=[bindable], labels_by_path=labels) is not None

    plan = derive_requested_output_extraction_plan(flow_evidence=[bindable, schemaless], labels_by_path=labels)

    assert plan is not None
    assert [binding.output_path for binding in plan.live_reads] == ["output.visitors"]


def test_a_witnessed_value_binds_the_relation_still_showing_it_when_no_label_matches() -> None:
    # The requested label and the page heading share nothing, which is the shape exact label equality
    # cannot bind; the value the scout already read identifies the element on its own.
    labels = {"output.visitors": ("the number of visitors in the last week is returned",)}
    tile = _bail_relation("Visitors", selector=".tile")
    tile["value_text"] = "8.7K"
    delta = _bail_relation("Change", selector=".delta")
    delta["value_text"] = "-8.0%"
    entry = _bail_entry([tile, delta])

    assert derive_requested_output_extraction_plan(flow_evidence=[entry], labels_by_path=labels) is None

    plan = derive_requested_output_extraction_plan(
        flow_evidence=[entry], labels_by_path=labels, witnessed_by_path={"output.visitors": "8.7K"}
    )
    assert plan is not None
    assert [binding.output_path for binding in plan.live_reads] == ["output.visitors"]
    assert [binding.relation_label for binding in plan.live_reads] == ["Visitors"]


def test_a_witnessed_value_matching_two_relations_binds_nothing() -> None:
    labels = {"output.visitors": ("the number of visitors in the last week is returned",)}
    first = _bail_relation("Visitors", selector=".tile")
    first["value_text"] = "8.7K"
    second = _bail_relation("Sessions", selector=".other")
    second["value_text"] = "8.7K"

    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[_bail_entry([first, second])],
            labels_by_path=labels,
            witnessed_by_path={"output.visitors": "8.7K"},
        )
        is None
    )


def test_key_value_bind_survives_a_truncated_container_channel() -> None:
    # A live dashboard capped its container capture while every tile relation was still captured;
    # vetoing the whole packet withheld a tile bind whose own channel was complete.
    labels = {"output.visitors": ("Visitors",)}
    entry = _bail_entry([_bail_relation("Visitors", selector=".tile-header")])
    entry["evidence"]["result_containers_truncated"] = True

    plan = derive_requested_output_extraction_plan(flow_evidence=[entry], labels_by_path=labels)
    assert plan is not None
    assert [binding.output_path for binding in plan.live_reads] == ["output.visitors"]

    entry["evidence"]["key_value_relations_truncated"] = True
    assert derive_requested_output_extraction_plan(flow_evidence=[entry], labels_by_path=labels) is None


def test_bail_reason_names_the_truncated_channel_behind_an_unbound_path() -> None:
    labels = {"output.visitors": ("visitors",)}
    entry = _bail_entry([_bail_relation("Sessions", selector=".other")])
    entry["evidence"]["result_containers_truncated"] = True

    assert derivation_bail_reason(flow_evidence=[entry], labels_by_path=labels) == (
        "bindings[output.visitors:witness-not-declared truncated=['result_containers']]"
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


_TYPED_LABEL_BY_PATH = {"output.visitors_last_7_days": ("visitors",)}
_OUTCOME_PROSE_BY_PATH = {"output.visitors_last_7_days": ("the number of visitors in the last 7 days is output",)}
_LIVE_COUNTER_ROW = _custody_relation("recently online", "4", selector="#live-counter", child_index=1, child_count=3)
_CHART_AXIS_ROW = _custody_relation("", "0", selector="#trend-chart", child_index=1, child_count=4)
_METRIC_TILE_ROW = _custody_relation("Visitors", "8.45K", selector="#visitors-tile", child_index=2, child_count=4)


def test_typed_requested_output_label_binds_the_metric_tile_not_the_counter_or_axis() -> None:
    plan = _derive(
        packet=_custody_packet(_LIVE_COUNTER_ROW, _CHART_AXIS_ROW, _METRIC_TILE_ROW),
        labels_by_path=_TYPED_LABEL_BY_PATH,
    )

    assert plan is not None
    assert [(binding.output_path, binding.relation_label, binding.selector) for binding in plan.live_reads] == [
        ("output.visitors_last_7_days", "Visitors", "#visitors-tile")
    ]
    assert _derive(packet=_custody_packet(_LIVE_COUNTER_ROW), labels_by_path=_TYPED_LABEL_BY_PATH) is None
    assert _derive(packet=_custody_packet(_CHART_AXIS_ROW), labels_by_path=_TYPED_LABEL_BY_PATH) is None


def test_metric_tile_binds_on_the_typed_label_and_never_on_the_outcome_sentence() -> None:
    assert _derive(packet=_custody_packet(_METRIC_TILE_ROW), labels_by_path=_OUTCOME_PROSE_BY_PATH) is None


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


def _bail_entry(
    relations: list[dict], *, reached_via: str = "current_page", drop: str | tuple[str, ...] | None = None
) -> dict:
    evidence = {
        "source_tool": "inspect_page_for_composition",
        "result_containers_truncated": False,
        "key_value_relations_truncated": False,
        "inspection_warnings": [],
        "key_value_relations": relations,
        "result_containers": [],
    }
    for key in (drop,) if isinstance(drop, str) else drop or ():
        evidence.pop(key)
    return {"step": 4, "reached_via": reached_via, "had_bounded_schema": True, "evidence": evidence}


def _bail_relation(key: str, *, selector: str, matches: int = 1, position: int = 0) -> dict:
    return {
        "key_text": key,
        "value_text": "-8.5%",
        "container_selector": selector,
        "container_match_count": matches,
        "container_position": position,
        "value_child_index": 1,
        "direct_child_count": 2,
        "visible": True,
        "value_visible": True,
    }


def test_bail_reason_names_each_guard_distinctly() -> None:
    # Ten live runs produced derived=0 with the tile on screen; each cause previously cost a
    # ~15-minute run to distinguish. The reason string must separate them in one log line.
    labels = {"output.visitors": ("visitors",)}
    tile = _bail_relation("Visitors", selector=".tile-header")
    sidebar = _bail_relation("Visitors", selector=".sidebar-item", matches=3)

    assert derivation_bail_reason(flow_evidence=[], labels_by_path={}) == "no-authoritative-paths"
    assert derivation_bail_reason(flow_evidence=[], labels_by_path=labels) == "no-bindable-entry"
    assert (
        derivation_bail_reason(flow_evidence=[_bail_entry([tile], reached_via="post_run")], labels_by_path=labels)
        == "no-bindable-entry"
    )
    assert derivation_bail_reason(flow_evidence=[_bail_entry([tile])], labels_by_path=labels) == (
        "table-consistency-or-derived"
    )
    assert "ambiguous=['output.visitors(n=2)']" in derivation_bail_reason(
        flow_evidence=[_bail_entry([tile, sidebar])], labels_by_path=labels
    )
    assert "output.visitors:witness-not-declared" in derivation_bail_reason(
        flow_evidence=[_bail_entry([_bail_relation("Sessions", selector=".other")])], labels_by_path=labels
    )
    # Truncation stopped being terminal once a witness could bind through it, so it is reported as
    # context beside the per-path reason rather than as the answer.
    truncated_reason = derivation_bail_reason(
        flow_evidence=[_bail_entry([tile], drop=("result_containers_truncated", "key_value_relations_truncated"))],
        labels_by_path=labels,
    )
    assert "output.visitors:witness-not-declared" in truncated_reason
    assert "key_value_relations" in truncated_reason
    assert derivation_bail_reason(
        flow_evidence=[_bail_entry([tile], reached_via="interaction")], labels_by_path=labels
    ).startswith("packet-source-tool[interaction:")


def _truncated_entry(relations: list[dict]) -> dict:
    return {
        "step": 1,
        "reached_via": "current_page",
        "had_bounded_schema": True,
        "evidence": {
            "source_tool": "inspect_page_for_composition",
            "inspection_warnings": [],
            "key_value_relations": relations,
            "result_containers": [],
            "key_value_relations_truncated": True,
            "result_containers_truncated": True,
        },
    }


def _counted_relation(key: str, value: str, selector: str, page_count: int) -> dict:
    relation = _bail_relation(key, selector=selector)
    relation["value_text"] = value
    relation["key_text_walked_count"] = page_count
    return relation


def test_a_label_counted_once_page_wide_binds_from_a_truncated_channel() -> None:
    # A real dashboard trips both capture caps, so every requested output was refused before any
    # binding was attempted, even though the label it needed was captured and unique.
    unique = _counted_relation("Visitors", "8.7K", ".tile", 1)

    plan = derive_requested_output_extraction_plan(
        flow_evidence=[_truncated_entry([unique])], labels_by_path={"output.visitors": ("Visitors",)}
    )

    assert plan is not None
    assert [binding.output_path for binding in plan.live_reads] == ["output.visitors"]


def test_a_label_the_page_repeats_beyond_the_capture_still_refuses() -> None:
    repeated = _counted_relation("agents", "v0", ".row", 34)

    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[_truncated_entry([repeated])], labels_by_path={"output.agents": ("agents",)}
        )
        is None
    )


def test_two_aliases_for_one_path_refuse_even_when_each_is_counted_once() -> None:
    # Each alias occurring once still yields two bindings for the path, so the count alone is not
    # licence to bind.
    first = _counted_relation("Visitors", "8.7K", ".tile", 1)
    second = _counted_relation("Unique visitors", "8.7K", ".other", 1)

    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[_truncated_entry([first, second])],
            labels_by_path={"output.visitors": ("Visitors", "Unique visitors")},
        )
        is None
    )


def test_one_relation_two_paths_name_refuses_rather_than_reporting_one_tile_twice() -> None:
    # Counting is per path, so without a cross-path check both paths bind the single "Total" tile and
    # the block reports one number as two different outputs.
    only = _counted_relation("Total", "8.7K", ".tile", 1)

    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[_truncated_entry([only])],
            labels_by_path={"output.visitors": ("Total",), "output.sessions": ("Total",)},
        )
        is None
    )


def _metric_card(label: str, figure: str, index: int) -> str:
    # A bare heading rather than a label/delta pair, so nothing here is two-child shaped and the cap
    # can only be reached through the metric-card pass.
    return f'<div id="card-{index}"><h3>{label}</h3><div>{figure}</div><div>vs prior</div></div>'


def _capped_dashboard_packet() -> dict:
    # 25 cards past a 24-relation cap: "Visitors" is captured and walked once, "Total" is captured
    # once but walked twice because its twin sits beyond the cap.
    cards = [_metric_card("Visitors", "8.83K", 0), _metric_card("Total", "10.7K", 1)]
    cards += [_metric_card(f"Metric {index}", f"{index}.0K", index + 2) for index in range(22)]
    cards.append(_metric_card("Total", "42.0K", 24))
    parsed = parse_composition_html(
        f"<body><main>{''.join(cards)}</main></body>",
        inspected_url="https://example.test/web",
        current_url="https://example.test/web",
    )
    return {"step": 1, "reached_via": "current_page", "had_bounded_schema": True, "evidence": parsed}


def test_the_extractor_reports_truncation_when_the_cap_drops_a_metric_card() -> None:
    # A channel that loses relations without saying so reads as complete, and a complete list is the
    # one thing the binder is entitled to trust.
    packet = _capped_dashboard_packet()

    assert packet["evidence"]["key_value_relations_truncated"] is True


def test_a_walked_once_label_binds_and_its_walked_twice_neighbour_refuses_from_one_real_capture() -> None:
    packet = _capped_dashboard_packet()

    bound = derive_requested_output_extraction_plan(
        flow_evidence=[packet], labels_by_path={"output.visitors": ("Visitors",)}
    )
    repeated = derive_requested_output_extraction_plan(
        flow_evidence=[packet], labels_by_path={"output.total": ("Total",)}
    )

    assert bound is not None
    assert [binding.relation_label for binding in bound.live_reads] == ["Visitors"]
    assert repeated is None


def test_a_shared_label_does_not_take_down_an_unrelated_path_in_the_same_packet() -> None:
    shared = _counted_relation("Total", "8.7K", ".tile", 1)
    distinct = _counted_relation("Visitors", "4.3K", ".other", 1)

    plan = derive_requested_output_extraction_plan(
        flow_evidence=[_truncated_entry([shared, distinct])],
        labels_by_path={"output.visitors": ("Visitors",)},
    )

    assert plan is not None
    assert [binding.relation_label for binding in plan.live_reads] == ["Visitors"]


def test_a_page_that_never_uses_the_requested_wording_still_offers_what_it_shows() -> None:
    # Live shape (SKY-13226): the request asked for "azure errors" while the page labelled the count
    # "logs found", so exact label binding could never join them however plainly the value was shown.
    packet = _truncated_entry(
        [
            _counted_relation("logs found", "1.22K", ".count", 1),
            _counted_relation("76.1%", "of total errors", ".pct", 1),
        ]
    )

    assert unbound_candidate_relations([packet]) == [("logs found", "1.22K"), ("76.1%", "of total errors")]


def test_an_unreadable_relation_is_not_offered_as_a_candidate() -> None:
    hidden = dict(_counted_relation("logs found", "1.22K", ".count", 1), visible=False)

    assert unbound_candidate_relations([_truncated_entry([hidden])]) == []


def test_a_dialog_only_capture_does_not_outrank_the_page_it_covered() -> None:
    # Live shape (SKY-13226): a time-zone dialog left the capture on the log page describing only its
    # own two buttons. Three walks each choose "the freshest bindable packet", so excluding it from
    # one still let the other two select it; driven through the shared append seam because the
    # discriminator alone proves nothing about which packet a walk then picks.
    ctx = SimpleNamespace(flow_evidence=[])
    dismiss = "No, keep the current setting"
    page = _truncated_entry([_counted_relation("logs found", "1.31K", ".count", 1)])["evidence"]
    dialog = dict(
        page,
        key_value_relations=[_counted_relation(dismiss, "Yes, switch", ".dialog", 1)],
        modal_overlays=[{"dismiss_controls": [{"text": dismiss}, {"text": "Yes, switch"}]}],
    )

    _append_flow_evidence(ctx, page, reached_via="current_page")
    _append_flow_evidence(ctx, dialog, reached_via="current_page")

    assert ctx.flow_evidence[1]["obstructed"] is True
    assert unbound_candidate_relations(ctx.flow_evidence) == [("logs found", "1.31K")]
    assert bindable_candidate_headings(ctx.flow_evidence) == ["logs found"]


def test_a_live_counter_binds_through_the_packet_its_witness_came_from() -> None:
    # Live shape (SKY-13226): the page recounts between the read and the next capture, so the freshest
    # packet shows 1.42K while the read witnessed 1.41K, and a join on the freshest packet alone can
    # never succeed for a moving figure. The witnessed value names its own contemporaneous packet.
    def _intact(entry: dict, step: int) -> dict:
        entry["evidence"] = dict(entry["evidence"], key_value_relations_truncated=False)
        return dict(entry, step=step)

    at_read = _intact(_truncated_entry([_counted_relation("logs found", "1.41K", ".count", 1)]), 1)
    ticked = _intact(_truncated_entry([_counted_relation("logs found", "1.42K", ".count", 1)]), 2)

    plan = derive_requested_output_extraction_plan(
        flow_evidence=[at_read, ticked],
        labels_by_path={"output.azure_error_count": ("azure",)},
        witnessed_by_path={"output.azure_error_count": "1.41K"},
    )

    assert plan is not None
    assert [b.relation_label for b in plan.live_reads] == ["logs found"]
    assert plan.observation_step == at_read["step"]


def test_without_a_witness_only_the_freshest_packet_is_tried() -> None:
    stale = _truncated_entry([_counted_relation("azure", "7", ".tile", 1)])
    fresh = dict(_truncated_entry([_counted_relation("unrelated", "9", ".other", 1)]), step=2)

    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[stale, fresh], labels_by_path={"output.azure_error_count": ("azure",)}
        )
        is None
    )


# --- witness admission matrix (SKY-13226) -------------------------------------------------------
#
# The value witness binds on the value a read observed, so it is the route for a page whose wording
# shares nothing with the request. These pin what admits such a binding and what still refuses it.


def _witness_relation(key: str, value: str, *, selector: str, child: int, count: int, walked: int = 1) -> dict:
    return {
        "key_text": key,
        "value_text": value,
        "container_selector": selector,
        "container_match_count": 1,
        "container_position": 0,
        "value_child_index": child,
        "direct_child_count": count,
        "value_text_walked_count": walked,
        "visible": True,
        "value_visible": True,
    }


def _witness_entry(relations: list[dict], *, truncated: bool = False) -> dict:
    return {
        "step": 1,
        "reached_via": "current_page",
        "had_bounded_schema": True,
        "evidence": {
            "source_tool": "inspect_page_for_composition",
            "inspection_warnings": [],
            "key_value_relations": relations,
            "result_containers": [],
            "key_value_relations_truncated": truncated,
            "result_containers_truncated": False,
        },
    }


def test_a_witness_binds_a_page_whose_wording_the_request_never_uses() -> None:
    # Live shape (SKY-13226): the page labels the requested quantity "logs found" while the request
    # says "azure errors", so no label can join them however plainly the value is shown.
    entry = _witness_entry([_witness_relation("logs found", "1.41K", selector=".count", child=0, count=2)])

    plan = derive_requested_output_extraction_plan(
        flow_evidence=[entry],
        labels_by_path={},
        witnessed_by_path={"output.azure_error_count": "1.41K"},
        requested_paths={"output.azure_error_count"},
    )

    assert plan is not None
    assert [(b.relation_label, b.child_index, b.child_count) for b in plan.live_reads] == [("logs found", 0, 2)]
    # Goal completion checks the requested paths are a subset of this; built from the labels it was
    # empty here, so a label-free bind produced a plan the turn could never call complete.
    assert plan.requested_output_paths == ("output.azure_error_count",)


def test_a_designated_value_binds_the_tile_no_label_and_no_scalar_read_could_reach() -> None:
    # Live shape (SKY-13226): the page labels the tile "Visitors" while the request calls it
    # "the number of visitors in the last week", and the model's own read returned the label and
    # the delta chip without the figure. The value the page confirmed is what joins them.
    entry = _witness_entry([_witness_relation("Visitors", "7.82K", selector=".card", child=1, count=3)])

    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[entry],
            labels_by_path={"output.visitor_count": ("the number of visitors in the last week",)},
            witnessed_by_path={"output.visitor_count": "Visitors\n-18.0%"},
            requested_paths={"output.visitor_count"},
        )
        is None
    )

    designated = derive_requested_output_extraction_plan(
        flow_evidence=[entry],
        labels_by_path={},
        witnessed_by_path={"output.visitor_count": "7.82K"},
        requested_paths={"output.visitor_count"},
    )

    assert designated is not None
    binding = designated.live_reads[0]
    # Bound through the relation channel, so the generated read keeps its runtime heading proof
    # rather than pinning the leaf directly.
    assert (binding.relation_label, binding.child_index, binding.child_count) == ("Visitors", 1, 3)


def test_a_designation_pins_the_element_when_no_packet_relation_carries_it() -> None:
    plan = plan_from_designations(
        [{"output_path": "output.total", "selector": ".val", "match_count": 1, "position": 0, "text": "7.82K"}],
        {"output.total"},
    )

    assert plan is not None
    assert plan.requested_output_paths == ("output.total",)
    binding = plan.live_reads[0]
    # child_count 0 is what tells synthesis to read the element itself rather than walk a child slot.
    assert (binding.selector, binding.selector_count, binding.child_count) == (".val", 1, 0)


def test_a_designation_the_page_saw_but_could_not_pin_still_witnesses_its_value() -> None:
    # Live shape (SKY-13226): the log explorer prints "1.44K" on three visible leaves, so the probe
    # cannot resolve one element and rejects. The page still shows that value in exactly one
    # relation, and the model naming it is what the witness needs.
    entry = _witness_entry([_witness_relation("logs found", "1.44K", selector=".count", child=0, count=2)])
    value_only = [{"output_path": "output.azure_error_count", "selector": "", "text": "1.44K"}]

    # It pins nothing on its own — there is no element to read.
    assert plan_from_designations(value_only, {"output.azure_error_count"}) is None

    plan = derive_requested_output_extraction_plan(
        flow_evidence=[entry],
        labels_by_path={},
        witnessed_by_path={d["output_path"]: d["text"] for d in value_only},
        requested_paths={"output.azure_error_count"},
    )

    assert plan is not None
    binding = plan.live_reads[0]
    assert (binding.relation_label, binding.child_index, binding.child_count) == ("logs found", 0, 2)


def test_a_value_only_designation_leaves_the_path_uncovered_rather_than_half_pinned() -> None:
    pinned_a = {"output_path": "output.a", "selector": ".a", "match_count": 1, "position": 0, "text": "1"}
    pinned_b = {"output_path": "output.b", "selector": ".b", "match_count": 1, "position": 0, "text": "2"}
    value_only_b = {"output_path": "output.b", "selector": "", "text": "2"}
    scope = {"output.a", "output.b"}

    # Withheld, so the uncovered path falls through to the witness channel rather than shipping a
    # plan that reads one of the two requested values.
    assert plan_from_designations([pinned_a, value_only_b], scope) is None
    # The value-only entry is skipped, not fatal: pinning both still builds the plan.
    plan = plan_from_designations([pinned_a, pinned_b], scope)
    assert plan is not None
    assert [b.output_path for b in plan.live_reads] == ["output.a", "output.b"]


def test_a_designation_the_page_could_not_pin_uniquely_binds_nothing() -> None:
    assert (
        plan_from_designations(
            [{"output_path": "output.total", "selector": ".val", "match_count": 1, "position": 3}],
            {"output.total"},
        )
        is None
    )
    assert (
        plan_from_designations(
            [{"output_path": "output.other", "selector": ".val", "match_count": 1, "position": 0}],
            {"output.total"},
        )
        is None
    )


def test_a_witness_binds_through_a_truncated_channel_when_its_value_is_page_unique() -> None:
    # A dashboard always trips the relation cap, so requiring an intact channel switched the witness
    # off for every page rich enough to need it. Page-wide uniqueness of the value is the proof.
    entry = _witness_entry(
        [_witness_relation("Visitors", "7.82K", selector=".tile", child=1, count=3, walked=1)], truncated=True
    )

    plan = derive_requested_output_extraction_plan(
        flow_evidence=[entry],
        labels_by_path={"output.visitors": ("the number of visitors is returned",)},
        witnessed_by_path={"output.visitors": "7.82K"},
        requested_paths={"output.visitors"},
    )

    assert plan is not None
    assert [(b.relation_label, b.child_index, b.child_count) for b in plan.live_reads] == [("Visitors", 1, 3)]


def test_a_truncated_channel_refuses_a_witness_whose_page_count_is_unproved() -> None:
    entry = _witness_entry(
        [
            dict(
                _witness_relation("Visitors", "7.82K", selector=".tile", child=1, count=3), value_text_walked_count=None
            )
        ],
        truncated=True,
    )

    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[entry],
            labels_by_path={},
            witnessed_by_path={"output.visitors": "7.82K"},
            requested_paths={"output.visitors"},
        )
        is None
    )


def test_a_truncated_channel_refuses_a_witness_the_page_shows_twice() -> None:
    entry = _witness_entry(
        [_witness_relation("Visitors", "7.82K", selector=".tile", child=1, count=3, walked=2)], truncated=True
    )

    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[entry],
            labels_by_path={},
            witnessed_by_path={"output.visitors": "7.82K"},
            requested_paths={"output.visitors"},
        )
        is None
    )


def test_one_relation_cannot_answer_two_requested_outputs() -> None:
    entry = _witness_entry([_witness_relation("Visitors", "7.82K", selector=".tile", child=1, count=3)])

    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[entry],
            labels_by_path={},
            witnessed_by_path={"output.a": "7.82K", "output.b": "7.82K"},
            requested_paths={"output.a", "output.b"},
        )
        is None
    )


def test_a_requested_path_left_unwitnessed_withholds_the_whole_plan() -> None:
    entry = _witness_entry(
        [
            _witness_relation("Visitors", "7.82K", selector=".a", child=1, count=3),
            _witness_relation("Sessions", "9.39K", selector=".b", child=1, count=3),
        ]
    )

    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[entry],
            labels_by_path={},
            witnessed_by_path={"output.visitors": "7.82K"},
            requested_paths={"output.visitors", "output.sessions"},
        )
        is None
    )


def test_a_witness_binds_values_that_are_not_quantities() -> None:
    # Extraction is not always a number, and the join is exact-value equality, so an identifier, an
    # address and a date bind on the same evidence a figure would.
    entry = _witness_entry(
        [
            _witness_relation("Order", "ORD-88213", selector=".o", child=1, count=2),
            _witness_relation("Ship to", "12 Harbor Way, Portsmouth", selector=".s", child=1, count=2),
            _witness_relation("Placed", "2026-07-30", selector=".p", child=1, count=2),
        ]
    )

    plan = derive_requested_output_extraction_plan(
        flow_evidence=[entry],
        labels_by_path={},
        witnessed_by_path={"output.order_id": "ORD-88213"},
        requested_paths={"output.order_id"},
    )

    assert plan is not None
    assert [b.relation_label for b in plan.live_reads] == ["Order"]


def test_a_value_repeated_across_rows_witnesses_nothing() -> None:
    entry = _witness_entry(
        [
            _witness_relation("Row 1 status", "Shipped", selector=".r1", child=1, count=2),
            _witness_relation("Row 2 status", "Shipped", selector=".r2", child=1, count=2),
        ]
    )

    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[entry],
            labels_by_path={},
            witnessed_by_path={"output.status": "Shipped"},
            requested_paths={"output.status"},
        )
        is None
    )


def test_bail_reason_separates_why_each_witness_channel_could_not_answer() -> None:
    # A vocabulary miss, an undeclared read and a value the page shows twice want three different
    # fixes, and each previously read as the same "no-labels" (SKY-13226).
    shown_twice = [
        _bail_relation("Row 1", selector=".r1"),
        _bail_relation("Row 2", selector=".r2"),
    ]
    for relation in shown_twice:
        relation["value_text"] = "Shipped"
    entry = _bail_entry(shown_twice)
    paths = {"output.status"}

    assert "witness-not-declared" in derivation_bail_reason(
        flow_evidence=[entry], labels_by_path={}, requested_paths=paths
    )
    assert "witness-not-present" in derivation_bail_reason(
        flow_evidence=[entry],
        labels_by_path={},
        witnessed_by_path={"output.status": "Delivered"},
        requested_paths=paths,
    )
    assert "witness-ambiguous[n=2]" in derivation_bail_reason(
        flow_evidence=[entry], labels_by_path={}, witnessed_by_path={"output.status": "Shipped"}, requested_paths=paths
    )
