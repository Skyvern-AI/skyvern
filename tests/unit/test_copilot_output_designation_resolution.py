"""Tests for the page-evidence-triggered designation resolver (SKY-13485).

OSS-synced: only RFC-2606 placeholder data (example.com).
"""

from __future__ import annotations

from typing import Any

from skyvern.forge.sdk.copilot.output_designation_resolution import (
    DesignationCandidate,
    coerce_resolution,
    designation_opportunity,
)

PATH = "output.failed_records"


def _relation(key_text: str, value_text: str) -> dict[str, Any]:
    return {
        "key_text": key_text,
        "value_text": value_text,
        "container_selector": f".tile-{key_text.replace(' ', '-')}",
        "container_match_count": 1,
        "container_position": 0,
        "value_child_index": 0,
        "direct_child_count": 2,
        "label_child_index": 1,
        "visible": True,
        "value_visible": True,
    }


def _entry(*relations: dict[str, Any]) -> dict[str, Any]:
    return {
        "step": 12,
        "reached_via": "current_page",
        "had_bounded_schema": True,
        "evidence": {
            "source_tool": "inspect_page_for_composition",
            "current_url": "https://dashboard.example.com/records",
            "inspection_warnings": [],
            "result_containers_truncated": False,
            "key_value_relations_truncated": False,
            "key_value_relations": list(relations),
            "result_containers": [],
        },
    }


def _tile_evidence() -> list[dict[str, Any]]:
    return [_entry(_relation("records found", "1.42K"), _relation("78.9%", "of total records"))]


def test_a_packet_offering_candidates_for_an_unbound_path_is_an_opportunity() -> None:
    opportunity = designation_opportunity(
        unbound_paths={PATH}, flow_evidence=_tile_evidence(), resolved_fingerprints=set()
    )

    assert opportunity is not None
    assert opportunity.unbound_paths == (PATH,)
    assert DesignationCandidate("records found", "1.42K") in opportunity.candidates


def test_a_packet_with_no_candidates_is_not_an_opportunity() -> None:
    # The login page: nothing to designate, so the resolver must not fire there.
    assert designation_opportunity(unbound_paths={PATH}, flow_evidence=[_entry()], resolved_fingerprints=set()) is None


def test_a_bound_path_is_not_an_opportunity() -> None:
    assert (
        designation_opportunity(unbound_paths=set(), flow_evidence=_tile_evidence(), resolved_fingerprints=set())
        is None
    )


def test_the_same_decision_fires_once() -> None:
    first = designation_opportunity(unbound_paths={PATH}, flow_evidence=_tile_evidence(), resolved_fingerprints=set())
    assert first is not None

    assert (
        designation_opportunity(
            unbound_paths={PATH}, flow_evidence=_tile_evidence(), resolved_fingerprints={first.fingerprint}
        )
        is None
    )


def test_a_ticking_metric_does_not_re_arm_the_same_decision() -> None:
    # Identity is the labels on offer, not their values; a live counter re-renders constantly.
    first = designation_opportunity(unbound_paths={PATH}, flow_evidence=_tile_evidence(), resolved_fingerprints=set())
    assert first is not None
    ticked = [_entry(_relation("records found", "1.43K"), _relation("78.9%", "of total records"))]

    assert (
        designation_opportunity(unbound_paths={PATH}, flow_evidence=ticked, resolved_fingerprints={first.fingerprint})
        is None
    )


def test_the_same_labels_on_a_differently_filtered_page_re_arm_it() -> None:
    # The page's query is an input to the decision, so "records found" unfiltered and "records found"
    # filtered to the requested subject are two different questions, not one already answered.
    first = designation_opportunity(unbound_paths={PATH}, flow_evidence=_tile_evidence(), resolved_fingerprints=set())
    assert first is not None
    filtered = _tile_evidence()
    filtered[0]["evidence"]["current_url"] = "https://dashboard.example.com/records?query=failed"

    assert (
        designation_opportunity(unbound_paths={PATH}, flow_evidence=filtered, resolved_fingerprints={first.fingerprint})
        is not None
    )


def test_a_structurally_new_page_re_arms_it() -> None:
    first = designation_opportunity(unbound_paths={PATH}, flow_evidence=_tile_evidence(), resolved_fingerprints=set())
    assert first is not None
    widened = [_entry(_relation("records found", "1.42K"), _relation("open alerts", "42"))]

    assert (
        designation_opportunity(unbound_paths={PATH}, flow_evidence=widened, resolved_fingerprints={first.fingerprint})
        is not None
    )


def _opportunity():
    opportunity = designation_opportunity(
        unbound_paths={PATH}, flow_evidence=_tile_evidence(), resolved_fingerprints=set()
    )
    assert opportunity is not None
    return opportunity


def test_a_selection_becomes_a_read_of_the_value_as_rendered() -> None:
    reads = coerce_resolution({"selections": [{"output_path": PATH, "candidate_index": 0}]}, _opportunity())

    assert reads == [{"output_path": PATH, "value_text": "1.42K", "label": "records found"}]


def test_abstaining_yields_no_read() -> None:
    assert coerce_resolution({"selections": []}, _opportunity()) == []


def test_an_index_outside_the_offer_yields_no_read() -> None:
    assert coerce_resolution({"selections": [{"output_path": PATH, "candidate_index": 7}]}, _opportunity()) == []


def test_a_path_that_was_not_asked_about_yields_no_read() -> None:
    resolution = {"selections": [{"output_path": "output.something_else", "candidate_index": 0}]}

    assert coerce_resolution(resolution, _opportunity()) == []


def test_one_path_cannot_claim_two_candidates() -> None:
    opportunity = _opportunity()
    resolution = {
        "selections": [
            {"output_path": PATH, "candidate_index": 0},
            {"output_path": PATH, "candidate_index": 1},
        ]
    }

    assert coerce_resolution(resolution, opportunity) == [
        {"output_path": PATH, "value_text": "1.42K", "label": "records found"}
    ]


def test_two_paths_cannot_claim_one_candidate() -> None:
    # One tile answers at most one requested output; two paths on it means one of them is wrong.
    opportunity = designation_opportunity(
        unbound_paths={PATH, "output.other"}, flow_evidence=_tile_evidence(), resolved_fingerprints=set()
    )
    assert opportunity is not None
    resolution = {
        "selections": [
            {"output_path": PATH, "candidate_index": 0},
            {"output_path": "output.other", "candidate_index": 0},
        ]
    }

    reads = coerce_resolution(resolution, opportunity)

    assert [read["output_path"] for read in reads] == [PATH]


def test_a_boolean_index_is_not_candidate_one() -> None:
    # bool subclasses int, so an unguarded isinstance check would read `true` as index 1.
    resolution = {"selections": [{"output_path": PATH, "candidate_index": True}]}

    assert coerce_resolution(resolution, _opportunity()) == []


def test_a_malformed_answer_is_an_abstention() -> None:
    for raw in ("not json", {"selections": "nope"}, {}, None, [1, 2]):
        assert coerce_resolution(raw, _opportunity()) == []
