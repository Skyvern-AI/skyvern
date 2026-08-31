from __future__ import annotations

import subprocess
import sys
import textwrap
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


def test_codeblock_execution_path_is_a_field_not_a_msg_suffix() -> None:
    # The arm is a grouping facet for the secure-vs-legacy monitors, not a correlation id,
    # so it must stay out of the searchable-id suffix (SKY-13848 bounds what goes into msg).
    context = SkyvernContext(workflow_run_id="wr_1", codeblock_execution_path="secure_runner")
    with patch.object(skyvern_context, "current", return_value=context):
        event_dict = add_log_context(None, "warning", {"msg": "Block failed"})

    assert event_dict["codeblock_execution_path"] == "secure_runner"
    assert event_dict["msg"] == "Block failed | workflow_run_id=wr_1"


def test_a_dropped_coroutine_warning_names_its_call_site() -> None:
    """CPython emits "coroutine ... was never awaited" from the coroutine's __del__, so the
    file:line it carries is wherever the collector ran, never the code that dropped it. Origin
    tracking is what puts the creating frame in the warning (SKY-15069).

    Run out of process: setup_logger() replaces the root handlers, the structlog configuration and
    several logger levels, and a subprocess also puts the warning on the same stderr stream the log
    collector reads in production.
    """
    program = textwrap.dedent(
        """
        import gc

        from skyvern.forge.sdk.forge_log import setup_logger

        setup_logger()

        async def dropped_coroutine() -> None:
            return None

        def sig_handler() -> None:
            dropped_coroutine()

        sig_handler()
        gc.collect()
        """
    )
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, check=True)

    assert "was never awaited" in result.stderr
    assert "Coroutine created at" in result.stderr
    assert "in sig_handler" in result.stderr
