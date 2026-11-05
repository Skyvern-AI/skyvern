"""The validation-evidence-router prompt carries the criterion and payload and asks for the routing schema."""

from __future__ import annotations

import json

from skyvern.forge.prompts import prompt_engine


def test_router_prompt_carries_its_inputs_and_asks_for_the_routing_schema() -> None:
    rendered = prompt_engine.load_prompt(
        "validation-evidence-router",
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str=json.dumps({"invoice_amount": 100}),
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
    )

    assert "extracted_amount equals invoice_amount" in rendered
    assert '"invoice_amount": 100' in rendered
    for schema_field in ("evidence_kind", "confidence", "rationale"):
        assert schema_field in rendered
    for page_evidence in ("{{ elements", "{{ current_url", "{{ action_history", "<html", "screenshot"):
        assert page_evidence not in rendered.lower()
