from __future__ import annotations

from types import SimpleNamespace

from skyvern.forge.sdk.copilot.composition_evidence import parse_composition_structured
from skyvern.forge.sdk.copilot.mcp_adapter import _restore_post_hook_context, _snapshot_post_hook_context
from skyvern.forge.sdk.copilot.output_extraction_plan import (
    LiveReadKind,
    derive_requested_output_extraction_plan,
)

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
