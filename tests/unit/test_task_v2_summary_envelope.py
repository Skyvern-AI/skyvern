"""task_v2 summary parsing tolerates envelope non-compliance so the deliverable is never dropped (SKY-14004).

When the user goal defines its own output contract, the summary model sometimes emits the
deliverable at the top level of its reply instead of inside the {"description", "output"}
envelope. The parser must recover that payload; production runs completed with
extracted_information=null because it did not. The LLM handler runs with force_dict=True,
so only dict replies reach the parser in production — non-dict handling is defensive.
"""

from __future__ import annotations

from datetime import timezone
from types import SimpleNamespace

import pytest

from skyvern.forge.prompts import prompt_engine
from skyvern.services import task_v2_service
from skyvern.services.task_v2_service import _parse_task_v2_summary_response


class _FakeTaskV2:
    observer_cruise_id = "tsk_v2_test"
    organization_id = "o_test"
    workflow_run_id = "wr_test"
    workflow_id = "w_test"
    workflow_permanent_id = "wpid_test"
    url = "https://example.com"
    prompt = "map the application form and emit the field inventory JSON"
    extracted_information_schema = None
    workflow_system_prompt = None


# Shape observed in production for the empty-output runs: the model followed the user
# goal's own schema (top-level field inventory) instead of the envelope.
_NON_ENVELOPE_RESPONSE = {
    "status": "success",
    "language": "en",
    "otpCodeRequired": False,
    "notes": "Single-page form; required-ness read from asterisks and validation attributes.",
    "fields": [
        {"name": "First name", "type": "text", "required": True, "validation": "maxlength: 127"},
        {"name": "Resume", "type": "file", "required": True},
    ],
}


def test_parser_keeps_envelope_verbatim() -> None:
    resp = {"description": "the memo", "output": {"rows": [1, 2]}}
    assert _parse_task_v2_summary_response(resp) == ("the memo", {"rows": [1, 2]})


def test_parser_description_only_yields_no_output() -> None:
    # Legit "no structured data" reply must stay None -- recovery only fires when keys
    # outside the envelope exist.
    assert _parse_task_v2_summary_response({"description": "navigated and submitted"}) == (
        "navigated and submitted",
        None,
    )
    assert _parse_task_v2_summary_response({"description": "done", "output": None}) == ("done", None)


def test_parser_recovers_top_level_payload() -> None:
    description, output = _parse_task_v2_summary_response(dict(_NON_ENVELOPE_RESPONSE))
    assert description is None
    # The whole non-envelope reply IS the deliverable.
    assert output == _NON_ENVELOPE_RESPONSE


def test_parser_recovery_preserves_user_schema_description_key() -> None:
    # A goal-defined contract may itself use "description"; recovery must not strip it.
    resp = {"description": "Widget", "price": 10}
    description, output = _parse_task_v2_summary_response(resp)
    assert description == "Widget"
    assert output == {"description": "Widget", "price": 10}


def test_parser_recovers_non_str_description_as_payload() -> None:
    # A non-str, non-null "description" is user data, not the envelope slot.
    assert _parse_task_v2_summary_response({"description": ["a", "b"]}) == (None, {"description": ["a", "b"]})
    # A null "description" alone carries no data.
    assert _parse_task_v2_summary_response({"description": None}) == (None, None)
    # Null envelope slots are dropped from a recovered payload.
    assert _parse_task_v2_summary_response({"description": None, "fields": [1]}) == (None, {"fields": [1]})


def test_parser_recovers_stray_keys_next_to_null_output() -> None:
    resp = {"description": "the memo", "output": None, "fields": [{"name": "Email"}]}
    description, output = _parse_task_v2_summary_response(resp)
    assert description == "the memo"
    # Only the null envelope "output" slot is dropped; every data key survives.
    assert output == {"description": "the memo", "fields": [{"name": "Email"}]}


def test_parser_tolerates_non_dict_and_non_str_description() -> None:
    # force_dict=True upstream makes non-dict unreachable in production; stay total anyway.
    assert _parse_task_v2_summary_response("just prose") == (None, None)
    assert _parse_task_v2_summary_response(None) == (None, None)
    assert _parse_task_v2_summary_response({}) == (None, None)
    # A non-str description must not reach the varchar summary column.
    description, output = _parse_task_v2_summary_response({"description": 42, "output": {"a": 1}})
    assert description is None
    assert output == {"a": 1}


def test_summary_prompt_pins_envelope_over_user_goal_schema() -> None:
    common = dict(
        user_goal="Emit the field inventory as a top-level JSON object.",
        task_history=[{"type": "extract", "status": "completed"}],
        extracted_information_schema=None,
        local_datetime="2026-08-13T12:00:00",
    )
    full = prompt_engine.load_prompt("task_v2_summary", is_partial=False, **common)
    partial = prompt_engine.load_prompt("task_v2_summary", is_partial=True, **common)
    for rendered in (full, partial):
        assert 'put that entire JSON inside the "output" key' in rendered


@pytest.mark.asyncio
async def test_generate_deliverable_recovers_non_envelope_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through _generate_task_v2_deliverable with the production failure shape."""
    thought_updates: dict[str, object] = {}

    class _FakeObserver:
        async def create_thought(self, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(observer_thought_id="ot_test")

        async def update_thought(self, **kwargs: object) -> None:
            thought_updates.update(kwargs)

    monkeypatch.setattr(task_v2_service.app, "DATABASE", SimpleNamespace(observer=_FakeObserver()))

    async def _llm_handler(**kwargs: object) -> dict:
        return dict(_NON_ENVELOPE_RESPONSE)

    monkeypatch.setattr(task_v2_service, "_get_task_v2_llm_api_handler", lambda task_v2: _llm_handler)

    description, output = await task_v2_service._generate_task_v2_deliverable(
        task_v2=_FakeTaskV2(),
        task_history=[{"type": "extract", "status": "completed", "extracted_data": {"fields": []}}],
        context=SimpleNamespace(tz_info=timezone.utc),
    )
    assert description is None
    assert output == _NON_ENVELOPE_RESPONSE
    # The raw reply still lands on the summarization thought for auditability/backfill.
    assert thought_updates["output"] == _NON_ENVELOPE_RESPONSE
    assert thought_updates["thought"] is None
