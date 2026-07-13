from __future__ import annotations

import json

import pytest

from skyvern.forge.sdk.copilot.result_evidence import loaded_result_source_producible
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tools import scouting


def _packet() -> dict:
    return {
        "navigation_targets": [
            {"selector": "#print", "text": "View Printable Statement"},
            {"selector": "#download", "text": "Download"},
        ],
        "forms": [],
        "result_containers": [],
    }


def _full_packet() -> dict:
    return {
        "forms": [
            {
                "fields": [{"selector": "#user", "text": "Username"}],
                "submit_controls": [{"selector": "#login", "text": "Log In"}],
            }
        ],
        "navigation_targets": [{"selector": "#print", "text": "View Printable Statement"}],
        "result_containers": [{"selector": "#results", "text": "Results"}],
        "modal_overlays": [{"dismiss_controls": [{"selector": "#close", "text": "Close"}]}],
    }


def _result_table_packet() -> dict:
    return {
        "forms": [],
        "navigation_targets": [{"selector": "#details", "text": "Details", "href": "/details"}],
        "result_containers": [
            {
                "tag": "table",
                "selector": "#results",
                "text": "Results",
                "row_count": 2,
                "sample_rows": ["Result A May 2026 $42.00", "Result B May 2026 $51.00"],
            }
        ],
    }


def _row_result_packet() -> dict:
    return {
        "forms": [],
        "navigation_targets": [],
        "result_containers": [
            {
                "selector": "#results",
                "row_count": 1,
                "sample_rows": ["May 2026 statement available"],
            }
        ],
    }


def _max_loaded_result_packet() -> dict:
    large_text = "Jane Customer account 123 statement ready " * 40
    return {
        "forms": [],
        "navigation_targets": [{"selector": "#details", "text": "Details", "href": "/details"}],
        "result_containers": [
            {
                "tag": "table",
                "selector": f"#results-{index}-" + ("x" * 220),
                "row_selector": f"tr.statement-{index}-" + ("y" * 220),
                "text": large_text,
                "row_count": 100 + index,
                "sample_rows": [
                    {"name": f"Jane Customer {index}", "account": "1234567890", "amount": "$999.99"},
                    {"name": f"John Customer {index}", "account": "0987654321", "amount": "$888.88"},
                    {"name": f"Pat Customer {index}", "account": "1111111111", "amount": "$777.77"},
                ],
            }
            for index in range(8)
        ],
    }


def _single_max_structural_loaded_result_packet() -> dict:
    return {
        "forms": [],
        "navigation_targets": [],
        "result_containers": [
            {
                "tag": "table",
                "selector": "#single-result-" + ("s" * 240),
                "row_selector": "tr.single-row-" + ("r" * 240),
                "row_count": 1,
                "sample_rows": [{"name": "Jane Customer", "account": "1234567890", "amount": "$999.99"}],
                "text": "Jane Customer account 123 statement ready " * 30,
                "evidence_source": "evaluate-" + ("e" * 240),
                "observation_id": "obs-" + ("o" * 240),
            }
        ],
    }


def _ctx() -> AgentContext:
    ctx = AgentContext.__new__(AgentContext)
    ctx.scout_trajectory = []
    return ctx


@pytest.mark.asyncio
async def test_evaluate_mints_current_card_and_table_source_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = _packet()
    packet["result_containers"] = [
        {"selector": "#coastalCard", "text": "Provider credentialing status is visible"},
        {"tag": "table", "selector": "#results", "row_count": 2, "sample_rows": ["A", "B"]},
    ]
    ctx = _ctx()
    ctx.discovery_mcp_server = object()

    async def fake_structured_evidence(*args: object, **kwargs: object) -> dict[str, object]:
        return packet

    monkeypatch.setattr(scouting, "_composition_get_structured_evidence", fake_structured_evidence)

    page_evidence = await scouting._scout_act_observe_page_evidence(ctx, url="https://example.com/results")
    assert getattr(ctx, "latest_evaluate_result_composition_steer", None) is None
    result = {"ok": True, "data": {"url": "https://example.com/results"}}
    await scouting._maybe_steer_evaluate_to_action(
        ctx, result, url="https://example.com/results", page_evidence=page_evidence
    )
    first_carrier = ctx.latest_evaluate_result_composition_steer
    await scouting._maybe_steer_evaluate_to_action(
        ctx, result, url="https://example.com/results", page_evidence=page_evidence
    )

    assert first_carrier == ctx.latest_evaluate_result_composition_steer
    assert [target.selector for target in first_carrier.targets] == ["#coastalCard", "#results"]
    assert first_carrier.source_tool == "evaluate"
    assert first_carrier.source_url == "https://example.com/results"
    assert loaded_result_source_producible(first_carrier) is True
    assert loaded_result_source_producible(first_carrier, target_code='await page.locator("#coastalCard")') is True
    assert loaded_result_source_producible(first_carrier, target_code='await page.locator("#otherCard")') is False


@pytest.mark.asyncio
async def test_scout_interaction_observation_does_not_mint_evaluate_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = _packet()
    packet["result_containers"] = [{"selector": "#coastalCard", "text": "Provider result is visible"}]
    ctx = _ctx()
    ctx.discovery_mcp_server = object()

    async def fake_structured_evidence(*args: object, **kwargs: object) -> dict[str, object]:
        return packet

    monkeypatch.setattr(scouting, "_composition_get_structured_evidence", fake_structured_evidence)

    await scouting._register_scout_interaction_observation(
        ctx,
        tool_name="click",
        selector="#search",
        source_url="https://example.com/search",
        url="https://example.com/results",
    )

    assert getattr(ctx, "latest_evaluate_result_composition_steer", None) is None


async def _seed(ctx, monkeypatch, packet, url):
    async def fake(c, *, url):
        return packet

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    result = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, result, url=url)
    return result


def test_signature_stable_across_equal_identity_sets() -> None:
    a = scouting._actionable_target_identities(_packet())
    b = scouting._actionable_target_identities(_packet())
    assert scouting._actionable_target_signature(a) == scouting._actionable_target_signature(b)


def test_signature_changes_when_controls_change() -> None:
    base = scouting._actionable_target_identities(_packet())
    mutated_packet = _packet()
    mutated_packet["navigation_targets"][0]["selector"] = "#print-v2"
    mutated = scouting._actionable_target_identities(mutated_packet)
    assert scouting._actionable_target_signature(base) != scouting._actionable_target_signature(mutated)


@pytest.mark.asyncio
async def test_standalone_controls_only_page_surfaces_grounded_targets(monkeypatch) -> None:
    async def fake_evidence(ctx, *, url):
        return {
            "forms": [],
            "navigation_targets": [],
            "result_containers": [],
            "clickable_controls": [{"selector": "#biz-tile", "text": "Business"}],
        }

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake_evidence)
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None
    result = {"ok": True, "data": {"url": "https://example.com/account"}}
    await scouting._maybe_steer_evaluate_to_action(ctx, result, url="https://example.com/account")
    assert result["data"]["actionable_targets"] == [{"selector": "#biz-tile", "text": "Business"}]


@pytest.mark.asyncio
async def test_first_evaluate_names_targets_without_next_action(monkeypatch) -> None:
    async def fake_evidence(ctx, *, url):
        return _packet()

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake_evidence)
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None
    result = {"ok": True, "data": {"url": "https://example.com/bill"}}
    await scouting._maybe_steer_evaluate_to_action(ctx, result, url="https://example.com/bill")
    assert "actionable_targets" in result["data"]
    assert "next_action" not in result["data"]


@pytest.mark.asyncio
async def test_first_loaded_result_table_steers_to_composition_not_click_even_with_actionable_targets(
    monkeypatch,
) -> None:
    async def fake_evidence(ctx, *, url):
        return _result_table_packet()

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake_evidence)
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = "stale"
    ctx.last_evaluate_actionable_url = "https://example.com/previous"
    result = {
        "ok": True,
        "data": {
            "url": "https://example.com/results",
            "actionable_targets": [{"selector": "#details", "text": "Details"}],
        },
    }

    await scouting._maybe_steer_evaluate_to_action(ctx, result, url="https://example.com/results")

    assert result["data"].get("next_action") == "compose_extraction"
    assert "extract" in result["data"]["next_action_reason"]
    assert "click" not in result["data"]["next_action_reason"].lower()
    assert "actionable_targets" not in result["data"]
    assert result["data"]["composition_targets"] == {
        "result_container_count": 1,
        "table_result_container_count": 1,
        "targets": [
            {
                "selector": "#results",
                "is_table": True,
                "row_count": 2,
                "sample_rows": ["Result A May 2026 $42.00", "Result B May 2026 $51.00"],
                "text_excerpt": "Results",
                "structure_signature": ctx.latest_evaluate_result_composition_steer.targets[0].structure_signature,
            }
        ],
        "structure_signature": ctx.latest_evaluate_result_composition_steer.structure_signature,
    }
    assert ctx.latest_evaluate_result_composition_steer.result_container_count == 1
    assert ctx.latest_evaluate_result_composition_steer.targets[0].selector == "#results"
    assert ctx.latest_evaluate_result_composition_steer.source_tool == "evaluate"
    assert ctx.latest_evaluate_result_composition_steer.source_url == "https://example.com/results"
    assert ctx.last_evaluate_actionable_signature is None
    assert ctx.last_evaluate_actionable_url is None


@pytest.mark.asyncio
async def test_first_loaded_result_rows_without_table_marker_steers_to_composition(monkeypatch) -> None:
    async def fake_evidence(ctx, *, url):
        return _row_result_packet()

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake_evidence)
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None
    result = {"ok": True, "data": {"url": "https://example.com/results"}}

    await scouting._maybe_steer_evaluate_to_action(ctx, result, url="https://example.com/results")

    assert result["data"].get("next_action") == "compose_extraction"
    assert result["data"]["composition_targets"] == {
        "result_container_count": 1,
        "table_result_container_count": 0,
        "targets": [
            {
                "selector": "#results",
                "is_table": False,
                "row_count": 1,
                "sample_rows": ["May 2026 statement available"],
                "structure_signature": ctx.latest_evaluate_result_composition_steer.targets[0].structure_signature,
            }
        ],
        "structure_signature": ctx.latest_evaluate_result_composition_steer.structure_signature,
    }


@pytest.mark.asyncio
async def test_repeat_evaluate_same_page_steers_to_click(monkeypatch) -> None:
    async def fake_evidence(ctx, *, url):
        return _packet()

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake_evidence)
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None
    url = "https://example.com/bill"
    first = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, first, url=url)
    second = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, second, url=url)
    assert second["data"].get("next_action") == "click"
    assert second["data"]["actionable_targets"]


@pytest.mark.asyncio
async def test_changed_signature_not_steered(monkeypatch) -> None:
    packets = [
        _packet(),
        {"navigation_targets": [{"selector": "#other", "text": "Next"}], "forms": [], "result_containers": []},
    ]
    calls = {"i": 0}

    async def fake_evidence(ctx, *, url):
        packet = packets[calls["i"]]
        calls["i"] += 1
        return packet

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake_evidence)
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None
    url = "https://example.com/bill"
    first = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, first, url=url)
    second = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, second, url=url)
    assert "next_action" not in second["data"]


@pytest.mark.asyncio
async def test_same_signature_different_url_not_steered(monkeypatch) -> None:
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None
    await _seed(ctx, monkeypatch, _packet(), "https://app.example.com/a")
    second = await _seed(ctx, monkeypatch, _packet(), "https://app.example.com/b")
    assert "next_action" not in second["data"]


@pytest.mark.asyncio
async def test_hash_route_change_not_steered(monkeypatch) -> None:
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None
    await _seed(ctx, monkeypatch, _packet(), "https://app.example.com/wizard#/step1")
    second = await _seed(ctx, monkeypatch, _packet(), "https://app.example.com/wizard#/step2")
    assert "next_action" not in second["data"]


@pytest.mark.asyncio
async def test_repeat_scalar_result_sheds_under_cap(monkeypatch) -> None:
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None
    url = "https://app.example.com/bill"

    async def fake(c, *, url):
        return _packet()

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    big = "x" * 6000
    first = {"ok": True, "data": {"url": url, "result": big}}
    await scouting._maybe_steer_evaluate_to_action(ctx, first, url=url)
    second = {"ok": True, "data": {"url": url, "result": big}}
    await scouting._maybe_steer_evaluate_to_action(ctx, second, url=url)
    serialized = json.dumps(second, default=str)
    assert second["data"].get("next_action") == "click"
    assert second["data"].get("actionable_targets")
    assert len(serialized) <= scouting._RECENT_TOOL_OUTPUT_CHAR_CAP
    assert '"next_action"' in serialized[: scouting._RECENT_TOOL_OUTPUT_CHAR_CAP]


@pytest.mark.asyncio
async def test_first_loaded_result_table_sheds_large_payload_but_keeps_composition_steer(monkeypatch) -> None:
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None
    url = "https://app.example.com/results"

    async def fake(c, *, url):
        return _result_table_packet()

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    big = "x" * 10000
    result = {"ok": True, "data": {"url": url, "result": {"html": big, "text": big}}}
    await scouting._maybe_steer_evaluate_to_action(ctx, result, url=url)
    serialized = json.dumps(result, default=str)
    assert result["data"].get("next_action") == "compose_extraction"
    assert result["data"].get("composition_targets")
    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.phase == "scout_evaluate"
    assert ctx.latest_recorded_build_test_outcome.reason_code == "loaded_result_targets_observed"
    assert ctx.latest_recorded_build_test_outcome.structural_key is not None
    assert len(serialized) <= scouting._RECENT_TOOL_OUTPUT_CHAR_CAP
    assert '"next_action"' in serialized[: scouting._RECENT_TOOL_OUTPUT_CHAR_CAP]
    assert "compose_extraction" in serialized


@pytest.mark.asyncio
async def test_loaded_result_composition_targets_are_cap_aware_for_max_shaped_evaluate(monkeypatch) -> None:
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None
    url = "https://app.example.com/results"

    async def fake(c, *, url):
        return _max_loaded_result_packet()

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    big = "z" * 10000
    result = {
        "ok": True,
        "data": {
            "url": url,
            "title": "Loaded results",
            "result": {"html": big, "text": big},
        },
    }

    await scouting._maybe_steer_evaluate_to_action(ctx, result, url=url)

    serialized = json.dumps(result, default=str)
    composition_targets = result["data"]["composition_targets"]
    target = composition_targets["targets"][0]
    assert len(serialized) <= scouting._RECENT_TOOL_OUTPUT_CHAR_CAP
    assert result["data"]["next_action"] == "compose_extraction"
    assert composition_targets["result_container_count"] == 8
    assert composition_targets["table_result_container_count"] == 8
    assert len(composition_targets["targets"]) == 1
    assert target["selector"].startswith("#results-0-")
    assert target["is_table"] is True
    assert target["row_selector"].startswith("tr.statement-0-")
    assert target["row_count"] == 100
    assert target["structure_signature"]
    assert composition_targets["structure_signature"]
    assert "sample_rows" not in target
    assert "text_excerpt" not in target
    assert "Jane Customer" not in serialized


@pytest.mark.asyncio
async def test_single_loaded_result_structural_target_fits_evaluate_cap_with_url_title(monkeypatch) -> None:
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None
    url = "https://app.example.com/results?" + ("query=value&" * 20)

    async def fake(c, *, url):
        return _single_max_structural_loaded_result_packet()

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    big = "z" * 10000
    result = {
        "ok": True,
        "data": {
            "url": url,
            "title": "Loaded result " + ("summary " * 20),
            "result": {"html": big, "text": big},
        },
    }

    await scouting._maybe_steer_evaluate_to_action(ctx, result, url=url)

    serialized = json.dumps(result, default=str)
    composition_targets = result["data"]["composition_targets"]
    target = composition_targets["targets"][0]
    assert len(serialized) <= scouting._RECENT_TOOL_OUTPUT_CHAR_CAP
    assert result["data"]["next_action"] == "compose_extraction"
    assert composition_targets["result_container_count"] == 1
    assert composition_targets["table_result_container_count"] == 1
    assert len(composition_targets["targets"]) == 1
    assert target["selector"].startswith("#single-result-")
    assert target["is_table"] is True
    assert target["row_count"] == 1
    assert target["structure_signature"]
    assert composition_targets["structure_signature"]
    assert "sample_rows" not in target
    assert "text_excerpt" not in target
    assert "evidence_source" not in target
    assert "observation_id" not in target
    assert "Jane Customer" not in serialized


@pytest.mark.asyncio
async def test_repeat_nested_result_sheds_under_cap(monkeypatch) -> None:
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None
    url = "https://app.example.com/bill"

    async def fake(c, *, url):
        return _packet()

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)

    def payload() -> dict:
        return {
            "url": url,
            "result": {
                "html": "<html>" + ("<div>row</div>" * 600) + "</html>",
                "text": "May 5 2026 $4,210.55 " * 80,
                "buttons": [f"button-{i}" for i in range(50)],
            },
        }

    first = {"ok": True, "data": payload()}
    assert len(json.dumps(first, default=str)) > 8000
    await scouting._maybe_steer_evaluate_to_action(ctx, first, url=url)
    second = {"ok": True, "data": payload()}
    await scouting._maybe_steer_evaluate_to_action(ctx, second, url=url)
    serialized = json.dumps(second, default=str)
    assert second["data"].get("next_action") == "click"
    assert second["data"].get("actionable_targets")
    assert len(serialized) <= scouting._RECENT_TOOL_OUTPUT_CHAR_CAP
    assert '"next_action"' in serialized[: scouting._RECENT_TOOL_OUTPUT_CHAR_CAP]


@pytest.mark.asyncio
async def test_first_evaluate_large_result_not_shed(monkeypatch) -> None:
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None
    url = "https://app.example.com/bill"

    async def fake(c, *, url):
        return _packet()

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    big = "x" * 6000
    first = {"ok": True, "data": {"url": url, "result": big}}
    await scouting._maybe_steer_evaluate_to_action(ctx, first, url=url)
    assert first["data"]["result"] == big
    assert "next_action" not in first["data"]


@pytest.mark.asyncio
async def test_non_serializable_data_does_not_raise(monkeypatch) -> None:
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = "seed"
    ctx.last_evaluate_actionable_url = "https://app.example.com/bill"

    async def fake(c, *, url):
        raise TypeError("boom")

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    result = {"ok": True, "data": {"url": "https://app.example.com/bill"}}
    await scouting._maybe_steer_evaluate_to_action(ctx, result, url="https://app.example.com/bill")
    assert "actionable_targets" not in result["data"]
    assert "next_action" not in result["data"]
    assert ctx.last_evaluate_actionable_signature is None


def test_identities_cover_all_collections() -> None:
    identities = scouting._actionable_target_identities(_full_packet())
    selectors = {sel for sel, _ in identities}
    assert {"#user", "#login", "#print", "#results", "#close"} <= selectors


def test_identities_include_clickable_controls() -> None:
    packet = {
        "forms": [],
        "navigation_targets": [],
        "result_containers": [],
        "clickable_controls": [
            {"selector": "#biz-tile", "text": "Business"},
            {"text": "Residential"},
        ],
    }
    identities = scouting._actionable_target_identities(packet)
    assert ("#biz-tile", "Business") in identities
    assert ("", "Residential") in identities


def test_identities_order_affordances_before_fields_and_selectors_first() -> None:
    packet = {
        "forms": [
            {
                "fields": [{"selector": "#user", "text": "Username"}],
                "submit_controls": [{"selector": "#login", "text": "Log In"}],
            }
        ],
        "navigation_targets": [],
        "result_containers": [],
        "clickable_controls": [
            {"text": "Text only tile"},
            {"selector": "#biz-tile", "text": "Business"},
        ],
    }
    identities = scouting._actionable_target_identities(packet)
    positions = {ident: index for index, ident in enumerate(identities)}
    field_pos = positions[("#user", "Username")]
    # Click affordances (selector-bearing and text-only) precede the plain input field.
    assert positions[("#login", "Log In")] < field_pos
    assert positions[("#biz-tile", "Business")] < field_pos
    assert positions[("", "Text only tile")] < field_pos
    # Selector-bearing affordances precede text-only ones.
    assert positions[("#biz-tile", "Business")] < positions[("", "Text only tile")]


def test_click_affordance_identities_only_selector_bearing_affordances() -> None:
    packet = {
        "forms": [
            {
                "fields": [{"selector": "#user", "text": "Username"}],
                "submit_controls": [{"selector": "#login", "text": "Log In"}],
            }
        ],
        "navigation_targets": [{"selector": "a.nav", "text": "Nav"}],
        "result_containers": [{"selector": "#results", "text": "Results"}],
        "clickable_controls": [
            {"selector": "#tile", "text": "Tile"},
            {"text": "Text only"},
        ],
        "modal_overlays": [{"dismiss_controls": [{"selector": "#close", "text": "Close"}]}],
    }
    identities = scouting._click_affordance_target_identities(packet)
    selectors = {selector for selector, _ in identities}
    assert selectors == {"#login", "a.nav", "#tile", "#close"}
    assert all(selector for selector, _ in identities)


@pytest.mark.asyncio
async def test_probe_none_leaves_data_untouched_and_resets(monkeypatch) -> None:
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = "seed"
    ctx.last_evaluate_actionable_url = "https://app.example.com/x"

    async def fake(c, *, url):
        return None

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    result = {"ok": True, "data": {"url": "https://app.example.com/x"}}
    await scouting._maybe_steer_evaluate_to_action(ctx, result, url="https://app.example.com/x")
    assert result["data"] == {"url": "https://app.example.com/x"}
    assert ctx.last_evaluate_actionable_signature is None


@pytest.mark.asyncio
async def test_probe_empty_collections_no_steer(monkeypatch) -> None:
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = "seed"
    ctx.last_evaluate_actionable_url = None
    ctx.latest_evaluate_result_composition_steer = object()

    async def fake(c, *, url):
        return {"forms": [], "navigation_targets": [], "result_containers": []}

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    result = {"ok": True, "data": {"url": "https://app.example.com/x"}}
    await scouting._maybe_steer_evaluate_to_action(ctx, result, url="https://app.example.com/x")
    assert "actionable_targets" not in result["data"]
    assert ctx.latest_evaluate_result_composition_steer is None


@pytest.mark.asyncio
async def test_interaction_between_evaluates_suppresses_steer(monkeypatch) -> None:
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None
    ctx.scouted_interactions = []
    ctx.scout_trajectory = []
    url = "https://app.example.com/bill"
    await _seed(ctx, monkeypatch, _packet(), url)
    scouting._record_scouted_interaction(ctx, tool_name="click", selector="#print", source_url=url)
    second = await _seed(ctx, monkeypatch, _packet(), url)
    assert "next_action" not in second["data"]


@pytest.mark.asyncio
async def test_steer_front_runs_loop_guard(monkeypatch) -> None:
    from skyvern.forge.sdk.copilot.loop_detection import MAX_CONSECUTIVE_SAME_TOOL, detect_tool_loop

    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None
    url = "https://app.example.com/bill"
    await _seed(ctx, monkeypatch, _packet(), url)
    second = await _seed(ctx, monkeypatch, _packet(), url)
    assert second["data"].get("next_action") == "click"

    tracker: list[str] = []
    assert detect_tool_loop(tracker, "evaluate") is None  # evaluate #1 post-hook runs
    assert detect_tool_loop(tracker, "evaluate") is None  # evaluate #2 post-hook runs + steers
    assert detect_tool_loop(tracker, "evaluate") is not None  # evaluate #3 trips the guard
    assert MAX_CONSECUTIVE_SAME_TOOL == 3


class _FakeDiscoveryServer:
    def __init__(self, click_result: dict) -> None:
        self.click_result = click_result
        self.calls: list[tuple[str, dict]] = []

    async def call_internal_tool(self, tool_name: str, args: dict) -> dict:
        self.calls.append((tool_name, args))
        return self.click_result


def _auto_act_ctx(server: _FakeDiscoveryServer) -> AgentContext:
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None
    ctx.last_auto_acted_signature = None
    ctx.browser_session_id = None
    ctx.scouted_interactions = []
    ctx.scout_trajectory = []
    ctx.discovery_mcp_server = server
    return ctx


def _single_link_packet() -> dict:
    return {
        "forms": [],
        "navigation_targets": [
            {"selector": "#stmt", "text": "Download Statement", "href": "https://apexbiz.example.com/apexbiz.pdf"}
        ],
        "result_containers": [],
    }


def _two_link_packet() -> dict:
    return {
        "forms": [],
        "navigation_targets": [
            {"selector": "#a", "text": "First Doc", "href": "https://apexbiz.example.com/a.pdf"},
            {"selector": "#b", "text": "Second Doc", "href": "https://apexbiz.example.com/b.pdf"},
        ],
        "result_containers": [],
    }


def _pay_submit_packet() -> dict:
    return {
        "forms": [
            {
                "method": "post",
                "fields": [],
                "submit_controls": [{"selector": "#pay", "text": "Pay $4,210.55", "type": "submit"}],
            }
        ],
        "navigation_targets": [],
        "result_containers": [],
    }


def _bare_button_submit_packet() -> dict:
    # `<button>Continue</button>` (HTML-default submit) inside a method=post form:
    # the structured-evidence producer reports type='button', so the writing-submit
    # guard never fires. Nav-only candidacy is what keeps it from being auto-acted.
    return {
        "forms": [
            {
                "method": "post",
                "fields": [],
                "submit_controls": [{"selector": "#continue", "text": "Continue", "type": "button"}],
            }
        ],
        "navigation_targets": [],
        "result_containers": [],
    }


def _type_button_packet() -> dict:
    return {
        "forms": [
            {
                "method": "post",
                "fields": [],
                "submit_controls": [{"selector": "#go", "text": "Go", "type": "button"}],
            }
        ],
        "navigation_targets": [],
        "result_containers": [],
    }


def _empty_text_link_packet() -> dict:
    return {
        "forms": [],
        "navigation_targets": [{"selector": "#icon", "text": "   ", "href": "https://apexbiz.example.com/x"}],
        "result_containers": [],
    }


def _post_click_packet() -> dict:
    return {
        "page_title": "Statement",
        "forms": [],
        "navigation_targets": [{"selector": "#back", "text": "Back", "href": "https://apexbiz.example.com/home"}],
        "result_containers": [],
    }


def test_auto_act_candidate_rejects_multiple() -> None:
    assert scouting._auto_act_candidate(_two_link_packet()) is None


def test_auto_act_candidate_rejects_high_tier_submit() -> None:
    assert scouting._auto_act_candidate(_pay_submit_packet()) is None


def test_auto_act_candidate_rejects_bare_button_submit() -> None:
    # A lone low-tier submit in a method=post form is a form submit, never a nav link.
    assert scouting._auto_act_candidate(_bare_button_submit_packet()) is None


def test_auto_act_candidate_rejects_type_button() -> None:
    assert scouting._auto_act_candidate(_type_button_packet()) is None


def test_auto_act_candidate_rejects_empty_text_link() -> None:
    assert scouting._auto_act_candidate(_empty_text_link_packet()) is None


def test_auto_act_candidate_accepts_single_low_tier_link() -> None:
    candidate = scouting._auto_act_candidate(_single_link_packet())
    assert candidate == {"selector": "#stmt", "text": "Download Statement"}


def test_auto_act_candidate_rejects_javascript_href() -> None:
    packet = _single_link_packet()
    packet["navigation_targets"][0]["href"] = "javascript:void(0)"
    assert scouting._auto_act_candidate(packet) is None


@pytest.mark.asyncio
async def test_multiple_candidates_no_auto_act(monkeypatch) -> None:
    server = _FakeDiscoveryServer({"ok": True, "data": {}})
    ctx = _auto_act_ctx(server)
    url = "https://apexbiz.example.com/bill"

    async def fake(c, *, url):
        return _two_link_packet()

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    first = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, first, url=url)
    second = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, second, url=url)
    assert server.calls == []
    assert "auto_acted" not in second["data"]
    assert second["data"].get("next_action") == "click"


@pytest.mark.asyncio
async def test_high_tier_no_auto_act(monkeypatch) -> None:
    server = _FakeDiscoveryServer({"ok": True, "data": {}})
    ctx = _auto_act_ctx(server)
    url = "https://apexbiz.example.com/bill"

    async def fake(c, *, url):
        return _pay_submit_packet()

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    first = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, first, url=url)
    second = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, second, url=url)
    assert server.calls == []
    assert "auto_acted" not in second["data"]
    assert second["data"].get("next_action") == "click"


@pytest.mark.asyncio
async def test_bare_button_submit_no_auto_act(monkeypatch) -> None:
    server = _FakeDiscoveryServer({"ok": True, "data": {}})
    ctx = _auto_act_ctx(server)
    url = "https://apexbiz.example.com/checkout"

    async def fake(c, *, url):
        return _bare_button_submit_packet()

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    first = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, first, url=url)
    second = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, second, url=url)
    assert server.calls == []
    assert "auto_acted" not in second["data"]
    assert second["data"].get("next_action") == "click"


@pytest.mark.asyncio
async def test_empty_text_link_no_auto_act(monkeypatch) -> None:
    server = _FakeDiscoveryServer({"ok": True, "data": {}})
    ctx = _auto_act_ctx(server)
    url = "https://apexbiz.example.com/bill"

    async def fake(c, *, url):
        return _empty_text_link_packet()

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    first = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, first, url=url)
    second = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, second, url=url)
    assert server.calls == []
    assert "auto_acted" not in second["data"]
    assert second["data"].get("next_action") == "click"


@pytest.mark.asyncio
async def test_single_low_tier_link_auto_acts(monkeypatch) -> None:
    server = _FakeDiscoveryServer({"ok": True, "data": {"selector": "#stmt"}})
    ctx = _auto_act_ctx(server)
    url = "https://apexbiz.example.com/bill"
    packets = [_single_link_packet(), _single_link_packet(), _post_click_packet()]
    idx = {"i": 0}

    async def fake(c, *, url):
        packet = packets[idx["i"]]
        idx["i"] += 1
        return packet

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    first = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, first, url=url)
    second = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, second, url=url)
    # count, not list-equality: _resolve_scout_role_name may add a browser read before the click
    assert [call[0] for call in server.calls].count("skyvern_click") == 1
    assert server.calls[0] == ("skyvern_click", {"selector": "#stmt", "selector_mode": "direct"})
    assert second["data"]["auto_acted"] == {"tool": "click", "selector": "#stmt", "text": "Download Statement"}
    assert second["data"]["page"]["page_title"] == "Statement"
    assert "next_action" not in second["data"]
    assert "actionable_targets" not in second["data"]
    assert len(json.dumps(second, default=str)) <= scouting._RECENT_TOOL_OUTPUT_CHAR_CAP


@pytest.mark.asyncio
async def test_auto_act_post_evidence_none_sheds_bulky_result_under_cap(monkeypatch) -> None:
    server = _FakeDiscoveryServer({"ok": True, "data": {"selector": "#stmt"}})
    ctx = _auto_act_ctx(server)
    url = "https://apexbiz.example.com/bill"
    # First two calls feed the steer probe; the third is the post-click extractor (None).
    evidence_queue: list[dict | None] = [_single_link_packet(), _single_link_packet(), None]
    idx = {"i": 0}

    async def fake(c, *, url):
        packet = evidence_queue[idx["i"]]
        idx["i"] += 1
        return packet

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    big = "x" * 10000
    first = {"ok": True, "data": {"url": url, "result": big}}
    await scouting._maybe_steer_evaluate_to_action(ctx, first, url=url)
    second = {"ok": True, "data": {"url": url, "result": big}}
    await scouting._maybe_steer_evaluate_to_action(ctx, second, url=url)
    assert [call[0] for call in server.calls].count("skyvern_click") == 1
    assert "auto_acted" in second["data"]
    assert second["data"]["auto_acted"]["selector"] == "#stmt"
    assert second["data"]["auto_acted"].get("note")
    assert "page" not in second["data"]
    assert len(json.dumps(second, default=str)) <= scouting._RECENT_TOOL_OUTPUT_CHAR_CAP


@pytest.mark.asyncio
async def test_auto_act_success_branch_sheds_oversized_page_under_cap(monkeypatch) -> None:
    server = _FakeDiscoveryServer({"ok": True, "data": {"selector": "#stmt"}})
    ctx = _auto_act_ctx(server)
    url = "https://apexbiz.example.com/bill"

    def fat_post_click() -> dict:
        return {
            "page_title": "Statement",
            "forms": [
                {
                    "fields": [
                        {"selector": f"#f{i}", "label": f"Field number {i} with a long label"} for i in range(40)
                    ],
                    "submit_controls": [{"selector": f"#s{i}", "text": f"Submit option {i}"} for i in range(20)],
                }
            ],
            "navigation_targets": [
                {"selector": f"#n{i}", "text": f"Navigation link number {i} on the page"} for i in range(40)
            ],
            "result_containers": [],
            "modal_overlays": [
                {"dismiss_controls": [{"selector": f"#d{i}", "text": f"Dismiss control {i}"} for i in range(10)]}
            ],
        }

    evidence_queue: list[dict] = [_single_link_packet(), _single_link_packet(), fat_post_click()]
    idx = {"i": 0}

    async def fake(c, *, url):
        packet = evidence_queue[idx["i"]]
        idx["i"] += 1
        return packet

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    big = "x" * 10000
    first = {"ok": True, "data": {"url": url, "result": big}}
    await scouting._maybe_steer_evaluate_to_action(ctx, first, url=url)
    second = {"ok": True, "data": {"url": url, "result": big}}
    await scouting._maybe_steer_evaluate_to_action(ctx, second, url=url)
    assert [call[0] for call in server.calls].count("skyvern_click") == 1
    assert "auto_acted" in second["data"]
    assert "page" in second["data"]
    assert len(json.dumps(second, default=str)) <= scouting._RECENT_TOOL_OUTPUT_CHAR_CAP


@pytest.mark.asyncio
async def test_auto_act_re_arms_on_changed_signature(monkeypatch) -> None:
    server = _FakeDiscoveryServer({"ok": True, "data": {"selector": "#stmt"}})
    ctx = _auto_act_ctx(server)
    url = "https://apexbiz.example.com/bill"

    def second_page_link() -> dict:
        return {
            "forms": [],
            "navigation_targets": [
                {"selector": "#next-stmt", "text": "Next Statement", "href": "https://apexbiz.example.com/next.pdf"}
            ],
            "result_containers": [],
        }

    # eval1+eval2: page A (auto-acts) -> post-click evidence;
    # eval3+eval4: page B (a genuinely changed page) re-arms and auto-acts again.
    evidence_queue: list[dict] = [
        _single_link_packet(),
        _single_link_packet(),
        _post_click_packet(),
        second_page_link(),
        second_page_link(),
        _post_click_packet(),
    ]
    idx = {"i": 0}

    async def fake(c, *, url):
        packet = evidence_queue[idx["i"]]
        idx["i"] += 1
        return packet

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    for _ in range(4):
        result = {"ok": True, "data": {"url": url}}
        await scouting._maybe_steer_evaluate_to_action(ctx, result, url=url)
    clicked = [call[1]["selector"] for call in server.calls if call[0] == "skyvern_click"]
    assert clicked == ["#stmt", "#next-stmt"]


@pytest.mark.asyncio
async def test_auto_act_idempotent_on_unchanged_signature(monkeypatch) -> None:
    server = _FakeDiscoveryServer({"ok": True, "data": {"selector": "#stmt"}})
    ctx = _auto_act_ctx(server)
    url = "https://apexbiz.example.com/bill"

    async def fake(c, *, url):
        return _single_link_packet()

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    for _ in range(3):
        result = {"ok": True, "data": {"url": url}}
        await scouting._maybe_steer_evaluate_to_action(ctx, result, url=url)
    assert [call[0] for call in server.calls].count("skyvern_click") == 1


@pytest.mark.asyncio
async def test_auto_act_click_failure_degrades_to_advisory(monkeypatch) -> None:
    server = _FakeDiscoveryServer({"ok": False, "error": "element not found"})
    ctx = _auto_act_ctx(server)
    url = "https://apexbiz.example.com/bill"

    async def fake(c, *, url):
        return _single_link_packet()

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    first = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, first, url=url)
    second = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, second, url=url)
    assert [call[0] for call in server.calls] == ["skyvern_click"]
    assert "auto_acted" not in second["data"]
    assert second["data"].get("next_action") == "click"
    assert second["data"].get("actionable_targets")


@pytest.mark.asyncio
async def test_auto_act_front_runs_the_third_evaluate(monkeypatch) -> None:
    from skyvern.forge.sdk.copilot.loop_detection import detect_tool_loop

    server = _FakeDiscoveryServer({"ok": True, "data": {"selector": "#stmt"}})
    ctx = _auto_act_ctx(server)
    url = "https://apexbiz.example.com/bill"
    packets = [_single_link_packet(), _single_link_packet(), _post_click_packet()]
    idx = {"i": 0}

    async def fake(c, *, url):
        packet = packets[idx["i"]]
        idx["i"] += 1
        return packet

    monkeypatch.setattr(scouting, "_scout_act_observe_page_evidence", fake)
    first = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, first, url=url)
    second = {"ok": True, "data": {"url": url}}
    await scouting._maybe_steer_evaluate_to_action(ctx, second, url=url)
    assert second["data"]["auto_acted"]["selector"] == "#stmt"
    assert ctx.scouted_interactions and ctx.scouted_interactions[-1]["tool_name"] == "click"

    # The auto-act click runs via call_internal_tool, which bypasses detect_tool_loop
    # and never appends to the consecutive-tool tracker, so it does NOT reset the streak.
    # The real front-run: the click changes the page, so evaluate #2 returns the post-click
    # auto_acted result and the model sees new content rather than issuing a 3rd identical
    # evaluate. If it did, SKY-10982's consecutive-same-tool guard still trips on evaluate #3
    # as the safe backstop, shown here.
    tripping: list[str] = []
    detect_tool_loop(tripping, "evaluate")
    detect_tool_loop(tripping, "evaluate")
    assert detect_tool_loop(tripping, "evaluate") is not None
