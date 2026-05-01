"""Guard test for MCP workflow raw-HTTP route prefix choices.

Keeps public vs internal route prefix assignments stable:
- list / create / update / update_folder → ``v1/workflows`` (public, mirrors the
  Fern-generated raw client at ``skyvern/client/raw_client.py``).
- get / run-status → ``api/v1/workflows`` (internal, used only where no public
  Fern SDK equivalent exists yet).

If you're adding a new MCP workflow raw helper and this test fails, either
(a) your new helper uses the wrong prefix for its responsibility, or
(b) this test needs a new assertion entry for the new helper.

See ``cloud_docs/fern-sdk/README.md`` for the rationale behind the split.
"""

from __future__ import annotations

import ast
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.mcp_tools import _workflow_http


@pytest.fixture
def capture_request(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace httpx.request with a 200 response.

    The default JSON payload is a dict for dict-returning helpers; list tests
    override it to ``[]``.
    """
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {}

    mock_skyvern = MagicMock()
    mock_skyvern._client_wrapper = MagicMock()
    mock_skyvern._client_wrapper.httpx_client = MagicMock()
    mock_skyvern._client_wrapper.httpx_client.request = AsyncMock(return_value=response)

    monkeypatch.setattr(_workflow_http, "get_skyvern", lambda: mock_skyvern)
    return mock_skyvern._client_wrapper.httpx_client.request


def _route(captured: MagicMock) -> str:
    """Extract the route string passed as the first positional arg to httpx.request."""
    assert captured.await_count == 1, "helper must make exactly one HTTP call"
    return captured.call_args[0][0]


@pytest.mark.asyncio
async def test_list_workflows_uses_public_route(capture_request: MagicMock) -> None:
    capture_request.return_value.json.return_value = []
    await _workflow_http.list_workflows_raw(search=None, page=1, page_size=10, only_workflows=False)
    assert _route(capture_request) == _workflow_http.PUBLIC_WORKFLOW_ROUTE


@pytest.mark.asyncio
async def test_create_workflow_uses_public_route(capture_request: MagicMock) -> None:
    await _workflow_http.create_workflow_raw(
        json_definition={"title": "x"},
        yaml_definition=None,
        folder_id=None,
    )
    assert _route(capture_request) == _workflow_http.PUBLIC_WORKFLOW_ROUTE


@pytest.mark.asyncio
async def test_update_workflow_uses_public_route(capture_request: MagicMock) -> None:
    await _workflow_http.update_workflow_raw(
        "wpid_x",
        json_definition={"title": "x"},
        yaml_definition=None,
    )
    assert _route(capture_request).startswith(_workflow_http.PUBLIC_WORKFLOW_ROUTE + "/")


@pytest.mark.asyncio
async def test_update_workflow_folder_uses_public_route(capture_request: MagicMock) -> None:
    await _workflow_http.update_workflow_folder_raw("wpid_x", folder_id=None)
    assert _route(capture_request).startswith(_workflow_http.PUBLIC_WORKFLOW_ROUTE + "/")


@pytest.mark.asyncio
async def test_get_workflow_by_id_uses_internal_route(capture_request: MagicMock) -> None:
    await _workflow_http.get_workflow_by_id("wpid_x")
    assert _route(capture_request).startswith(_workflow_http.INTERNAL_WORKFLOW_ROUTE + "/")


@pytest.mark.asyncio
async def test_get_workflow_run_status_uses_internal_route(capture_request: MagicMock) -> None:
    await _workflow_http.get_workflow_run_status("wr_x", include_output_details=False)
    assert _route(capture_request).startswith(_workflow_http.INTERNAL_WORKFLOW_ROUTE + "/")


def test_prefix_constants_are_stable() -> None:
    """Lock the string values so a rename doesn't silently invalidate the split."""
    assert _workflow_http.PUBLIC_WORKFLOW_ROUTE == "v1/workflows"
    assert _workflow_http.INTERNAL_WORKFLOW_ROUTE == "api/v1/workflows"
    assert _workflow_http.PUBLIC_WORKFLOW_ROUTE != _workflow_http.INTERNAL_WORKFLOW_ROUTE


def test_no_hardcoded_workflow_routes_outside_constants() -> None:
    """AST guard: every ``v1/...`` or ``api/v1/...`` string literal in _workflow_http.py
    must be the named constant, not a bypass.

    This catches the case where a future contributor adds a new helper and
    hardcodes ``"api/v1/workflows/runs/foo"`` instead of using
    ``INTERNAL_WORKFLOW_ROUTE``. The presence-only tests above only cover the
    helpers that exist today; this test covers the *shape* of the module.
    """
    allowed = {
        _workflow_http.PUBLIC_WORKFLOW_ROUTE,
        _workflow_http.INTERNAL_WORKFLOW_ROUTE,
    }
    tree = ast.parse(inspect.getsource(_workflow_http))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if value.startswith(("v1/", "api/v1/")) and value not in allowed:
                offenders.append(f"line {node.lineno}: {value!r}")

    assert not offenders, (
        "Hardcoded workflow route literal(s) detected in _workflow_http.py:\n"
        + "\n".join(f"  {o}" for o in offenders)
        + "\n\nUse PUBLIC_WORKFLOW_ROUTE or INTERNAL_WORKFLOW_ROUTE constants instead. "
        "See cloud_docs/fern-sdk/README.md."
    )
