"""Shared helpers for MCP workflow tool tests.

Extracted in SKY-9227 so the four MCP test files that mock ``get_skyvern``
don't drift in how they patch both ``workflow_tools`` and the
``_workflow_http`` module introduced by the raw-HTTP centralization.
"""

from __future__ import annotations

import pytest

import skyvern.cli.mcp_tools.workflow as workflow_tools
from skyvern.cli.mcp_tools import _workflow_http


def patch_skyvern_client(monkeypatch: pytest.MonkeyPatch, fake_client: object) -> None:
    """Patch ``get_skyvern`` on both ``workflow_tools`` and ``_workflow_http``.

    Each module has its own import-time binding for ``get_skyvern``; tests must
    redirect both, otherwise the raw-HTTP code path reaches a real client and
    either errors obscurely (no API key) or makes a stray HTTP call.
    """

    def _factory() -> object:
        return fake_client

    monkeypatch.setattr(workflow_tools, "get_skyvern", _factory)
    monkeypatch.setattr(_workflow_http, "get_skyvern", _factory)


def patch_get_workflow_by_id(monkeypatch: pytest.MonkeyPatch, fake_get_workflow_by_id: object) -> None:
    """Patch workflow lookup in both modules that bind it at import time."""
    monkeypatch.setattr(workflow_tools, "get_workflow_by_id", fake_get_workflow_by_id)
    monkeypatch.setattr(_workflow_http, "get_workflow_by_id", fake_get_workflow_by_id)
