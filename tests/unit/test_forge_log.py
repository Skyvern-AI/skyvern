from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.forge_log import add_log_context


def test_add_log_context_tolerates_partial_context() -> None:
    # A partial context (e.g. a SimpleNamespace test double) exposes only some of the
    # fields add_log_context reads. It must fail-open on the missing ones rather than raise.
    context = SimpleNamespace(organization_id="org_1", task_id="task_1")
    with patch.object(skyvern_context, "current", return_value=context):
        event_dict = add_log_context(None, "warning", {"msg": "hi"})

    assert event_dict["organization_id"] == "org_1"
    assert event_dict["task_id"] == "task_1"
    assert "request_id" not in event_dict


def test_correlation_ids_are_searchable_in_msg() -> None:
    # Datadog free-text search only scans the message content; pasting an id like
    # pbs_x/wr_x must keep matching, so the ids are appended to msg (SKY-13848 kept
    # the arbitrary-kwarg copy out — only these bounded ids go in).
    context = SkyvernContext(
        organization_id="o_1",
        workflow_run_id="wr_1",
        browser_session_id="pbs_1",
    )
    with patch.object(skyvern_context, "current", return_value=context):
        event_dict = add_log_context(None, "info", {"msg": "Closing browser", "payload": "x" * 500})

    assert event_dict["msg"] == "Closing browser | organization_id=o_1, workflow_run_id=wr_1, browser_session_id=pbs_1"
    assert event_dict["browser_session_id"] == "pbs_1"


def test_kwarg_correlation_id_is_searchable_without_context() -> None:
    # Worker code paths log ids as kwargs before any skyvern_context exists; those must
    # be free-text searchable too.
    with patch.object(skyvern_context, "current", return_value=None):
        event_dict = add_log_context(None, "info", {"msg": "Begin browser session", "browser_session_id": "pbs_2"})

    assert event_dict["msg"] == "Begin browser session | browser_session_id=pbs_2"
