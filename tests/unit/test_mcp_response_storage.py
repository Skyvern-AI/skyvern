"""Unit tests for storage-aware MCP response distillation."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock

import pytest

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import storage as mcp_storage
from skyvern.cli.mcp_tools.response import response_transformed
from skyvern.cli.mcp_tools.response_distillation import TransformTier
from skyvern.cli.mcp_tools.response_storage import format_storage_response
from tests.unit._mcp_browser_fakes import make_mock_page, patch_get_page

_RECOVERY_HINT = "Call skyvern_get_session_storage with verbosity='full' to recover raw values."


def _storage_response(items: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "action": "get_session_storage",
        "data": {"items": items, "count": len(items)},
    }


def _wrapped_storage_tool() -> Callable[..., Awaitable[dict[str, Any]]]:
    return response_transformed(formatter=format_storage_response, recovery_hint=_RECOVERY_HINT)(
        mcp_storage.skyvern_get_session_storage
    )


def test_large_storage_map_has_deterministic_previews_and_exact_counts() -> None:
    items = {f"storage-key-{index:02d}": f"value-{index}" for index in range(30)}
    payload = _storage_response(items)
    snapshot = deepcopy(payload)

    first = format_storage_response(payload)
    second = format_storage_response(payload)

    assert first == second
    assert payload == snapshot
    assert first.tier is TransformTier.STRUCTURED
    assert first.complete is False
    assert first.value["data"]["count"] == 30
    previews = first.value["data"]["items"]
    assert previews["_key_count"] == 30
    assert previews["_omitted_keys"] == 8
    assert [key for key in previews if not key.startswith("_")] == [f"storage-key-{index:02d}" for index in range(22)]


def test_storage_formatter_compacts_json_and_yaml_strings() -> None:
    long_json_body = "x" * 300
    json_value = '{"rows": [' + ",".join(f'{{"id": {index}, "body": "{long_json_body}"}}' for index in range(8)) + "]}"
    long_yaml_body = "y" * 300
    yaml_value = "entries:\n" + "".join(f"  - id: item-{index}\n    body: {long_yaml_body}\n" for index in range(8))
    payload = _storage_response({"json-state": json_value, "yaml-state": yaml_value})

    result = format_storage_response(payload)

    assert result.tier is TransformTier.STRUCTURED
    assert result.complete is False
    assert result.fallback_reason == "content_summarized"
    assert result.value["data"]["count"] == 2
    assert set(result.value["data"]["items"]) == {"json-state", "yaml-state"}
    assert result.value["data"]["items"]["json-state"]["rows"]["_length"] == 8
    assert result.value["data"]["items"]["yaml-state"]["entries"]["_length"] == 8


def test_local_storage_shaped_fixture_is_supported_without_api_expansion() -> None:
    fixture = {
        "ok": True,
        "local_storage": {
            "cart": '[{"sku": "sku-1", "notes": "' + ("n" * 500) + '"}]',
            "locale": "en-US",
        },
        "local_storage_count": 2,
    }

    result = format_storage_response(fixture)

    assert result.tier is TransformTier.STRUCTURED
    assert result.value["local_storage_count"] == 2
    assert set(result.value["local_storage"]) == {"cart", "locale"}
    assert result.value["local_storage"]["locale"] == "en-US"
    assert result.value["local_storage"]["cart"][0]["sku"] == "sku-1"
    assert "skyvern_get_local_storage" not in vars(mcp_storage)


def test_small_storage_values_are_unchanged_and_never_expand() -> None:
    payload = _storage_response(
        {
            "token": "fake-token-for-test",
            "theme": "dark",
            "structured": {"enabled": True},
        }
    )

    result = format_storage_response(payload)

    assert result.complete is True
    assert result.value == payload
    assert result.value["data"]["items"]["token"] == "fake-token-for-test"


def test_ambiguous_values_fail_the_whole_map_closed() -> None:
    """A value whose content is ambiguous (duplicate keys) cannot be represented
    faithfully, so the whole map passes through untouched."""
    payload = _storage_response({"broken": '{"key": 1, "key": 2}' + (" " * 300), "other": '["valid", "but untouched"]'})

    result = format_storage_response(payload)

    assert result.tier is TransformTier.PASSTHROUGH
    assert result.value is payload
    assert result.complete is True
    assert result.fallback_reason == "ambiguous_duplicate_key"


@pytest.mark.asyncio
async def test_opaque_values_are_kept_verbatim_while_siblings_compact() -> None:
    opaque = "{" + ("not-json" * 80)
    compactible = json.dumps({"rows": [{"id": index, "value": "x" * 40} for index in range(30)]})
    payload = _storage_response({"broken": opaque, "big": compactible})

    @response_transformed(formatter=format_storage_response)
    async def tool() -> dict[str, Any]:
        return payload

    result = await tool()

    assert result["data"]["items"]["broken"] == opaque
    assert result["data"]["items"]["big"] != compactible


def test_targeted_structured_value_is_only_used_when_it_shrinks() -> None:
    small_structured = {"alpha": 1, "beta": [2, 3]}
    payload = _storage_response({"settings": small_structured})

    result = format_storage_response(payload)

    assert result.value["data"]["items"]["settings"] is small_structured
    assert result.value == payload


@pytest.mark.asyncio
async def test_default_summary_formats_session_storage_and_logs_no_values(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake_token = "fake-token-for-test"
    items = {f"key-{index:02d}": f"value-{index}-" + ("v" * 80) for index in range(30)}
    items["auth"] = fake_token
    page = make_mock_page()
    page.evaluate = AsyncMock(return_value=items)
    patch_get_page(monkeypatch, mcp_storage, page, BrowserContext(mode="local"))

    result = await _wrapped_storage_tool()()

    assert result["data"]["count"] == 31
    assert result["data"]["items"]["_key_count"] == 31
    assert result["_response_distillation"]["complete"] is False
    assert result["_response_distillation"]["recovery_hint"] == _RECOVERY_HINT
    assert fake_token not in caplog.text
    formatter_result = format_storage_response(_storage_response({"auth": fake_token}))
    metadata = (
        formatter_result.tier.value,
        formatter_result.complete,
        formatter_result.fallback_reason,
    )
    assert fake_token not in repr(metadata)


@pytest.mark.asyncio
async def test_storage_formatter_does_not_reinject_omitted_anchor_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    omitted = {
        "zz_oauth_code": "oauth-code-sentinel",
        "zz_org_id": "org-id-sentinel",
    }
    items = {f"storage-key-{index:02d}": f"value-{index}-" + ("v" * 80) for index in range(22)}
    items.update({key: sentinel + ("x" * 2_000) for key, sentinel in omitted.items()})
    page = make_mock_page()
    page.evaluate = AsyncMock(return_value=items)
    patch_get_page(monkeypatch, mcp_storage, page, BrowserContext(mode="local"))

    result = await _wrapped_storage_tool()()
    response_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
    anchors_json = json.dumps(result.get("_response_anchors", {}), ensure_ascii=False, sort_keys=True)

    for key, sentinel in omitted.items():
        assert key not in response_json
        assert sentinel not in response_json
        assert key not in anchors_json
        assert sentinel not in anchors_json


@pytest.mark.asyncio
async def test_full_verbosity_recovers_raw_session_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_items = {
        "auth": "fake-token-for-test",
        "state": '{"rows": [' + ",".join(str(index) for index in range(100)) + "]}",
    }
    page = make_mock_page()
    page.evaluate = AsyncMock(return_value=raw_items)
    patch_get_page(monkeypatch, mcp_storage, page, BrowserContext(mode="local"))

    result = await _wrapped_storage_tool()(verbosity="full")

    assert result["data"]["items"] == raw_items
    assert result["data"]["count"] == 2
    assert "_response_distillation" not in result
