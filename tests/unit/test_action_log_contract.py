import ast
import inspect
from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from skyvern.cli.mcp_tools import browser as mcp_browser
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.schemas.action_log import (
    ACTION_LOG_ALLOWED_TOOLS,
    ACTION_LOG_MAX_EVENTS_PER_BATCH,
    ActionLogBatchRequest,
    ActionLogOutcome,
    project_action_event,
)


def test_action_log_allowed_tools_match_instrumented_browser_actions() -> None:
    browser_tree = ast.parse(inspect.getsource(mcp_browser))
    instrumented_tools = frozenset(
        node.args[0].value
        for node in ast.walk(browser_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "action_result"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    )

    assert ACTION_LOG_ALLOWED_TOOLS == instrumented_tools


def test_action_log_contract_and_privacy_projection() -> None:
    selector_secret = "sk-test-selector-secret"
    path_secret = "person@example.test:opaque-value"
    event = project_action_event(
        event_id=UUID("00000000-0000-0000-0000-000000000001"),
        tool="skyvern_type",
        selector=f'input[value="{selector_secret}"][data-kind="search"]',
        value="widget",
        source_url=f"https://example.test/products/verify/{path_secret}/confirm?session=raw-secret&q=widgets",
        occurred_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        timing_ms={"total": 12},
        outcome=ActionLogOutcome.SUCCESS,
        index=1,
        artifact_ref="sk-test-artifact-secret",
    )

    assert ArtifactType.BROWSER_SESSION_ACTION_LOG == "browser_session_action_log"
    assert event.schema_version == 1
    assert event.selector == 'input[value="__redacted__"][data-kind="search"]'
    assert event.value == "widget"
    assert event.source_url == (
        "https://example.test/products/verify/__redacted__/confirm?session=__redacted__&q=widgets"
    )
    assert event.artifact_ref == "__redacted__"
    assert event.order_key == (event.occurred_at, event.index, str(event.event_id))
    same_ms_earlier = event.model_copy(update={"index": 0, "event_id": UUID(int=0xFF)})
    assert same_ms_earlier.order_key < event.order_key
    assert selector_secret not in event.model_dump_json()
    assert path_secret not in event.model_dump_json()

    normal_path_event = project_action_event(
        event_id=UUID("00000000-0000-0000-0000-000000000007"),
        tool="skyvern_navigate",
        source_url="https://example.test/products/123/search-results",
        occurred_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        timing_ms={},
        outcome=ActionLogOutcome.SUCCESS,
        index=5,
    )
    assert normal_path_event.source_url == "https://example.test/products/123/search-results"

    secret_event = project_action_event(
        event_id=UUID("00000000-0000-0000-0000-000000000002"),
        tool="skyvern_type",
        selector="#search",
        typed_text="sk-test-secret-value",
        source_url="https://example.test/path?q=sk-test-secret-value",
        occurred_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        timing_ms={},
        outcome=ActionLogOutcome.SUCCESS,
        index=2,
    )
    assert secret_event.value is None
    assert secret_event.source_url == "https://example.test/path?q=__redacted__"
    assert "sk-test-secret-value" not in secret_event.model_dump_json()

    unquoted_secret_event = project_action_event(
        event_id=UUID("00000000-0000-0000-0000-000000000005"),
        tool="skyvern_click",
        selector="#sk-test-unquoted-secret",
        source_url="https://sk-test-host-secret.example/path",
        occurred_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        timing_ms={},
        outcome=ActionLogOutcome.SUCCESS,
        index=3,
    )
    assert unquoted_secret_event.selector == "__redacted__"
    assert unquoted_secret_event.source_url == "https://__redacted__/path"

    scheme_secret = "sk-abcdefghijklmnop"
    secret_scheme_event = project_action_event(
        event_id=UUID("00000000-0000-0000-0000-000000000006"),
        tool="skyvern_navigate",
        source_url=f"{scheme_secret}://example.test/path",
        occurred_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        timing_ms={},
        outcome=ActionLogOutcome.SUCCESS,
        index=4,
    )
    assert secret_scheme_event.source_url == "__redacted__"
    assert scheme_secret not in secret_scheme_event.model_dump_json()


def test_action_log_batch_and_error_contract_are_strict() -> None:
    event = project_action_event(
        event_id=UUID("00000000-0000-0000-0000-000000000003"),
        tool="skyvern_click",
        selector="#submit",
        occurred_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        timing_ms={},
        outcome=ActionLogOutcome.SUCCESS,
        index=3,
    )

    with pytest.raises(ValidationError):
        ActionLogBatchRequest(events=[event] * (ACTION_LOG_MAX_EVENTS_PER_BATCH + 1))

    with pytest.raises(ValidationError):
        project_action_event(
            event_id=UUID("00000000-0000-0000-0000-000000000004"),
            tool="skyvern_click",
            selector="#submit",
            occurred_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
            timing_ms={},
            outcome=ActionLogOutcome.ERROR,
            index=4,
        )

    valid = event.model_dump(mode="json")
    for invalid in (
        {**valid, "tool": "skyvern_not_instrumented"},
        {**valid, "outcome": "error", "error_code": "SECRET"},
        {**valid, "timing_ms": {"sk-abcdefghijklmnop": 1}},
    ):
        with pytest.raises(ValidationError):
            ActionLogBatchRequest.model_validate({"events": [invalid]})
