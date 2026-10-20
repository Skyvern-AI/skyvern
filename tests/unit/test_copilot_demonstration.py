from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, ClassVar, Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.exceptions import SkyvernHTTPException
from skyvern.forge import app
from skyvern.forge.sdk.cache.base import NoopLock
from skyvern.forge.sdk.copilot import demonstration_recorder as recorder_module
from skyvern.forge.sdk.copilot import demonstration_replay as replay_module
from skyvern.forge.sdk.copilot.demonstration_recorder import (
    DemonstrationClaim,
    DemonstrationRecord,
    DemonstrationRecorder,
    DemonstrationRejection,
    active_demonstration,
    append_demonstration_control,
    claim_demonstration_recording,
    decode_demonstration_record,
    demonstration_browser_key,
    demonstration_controls_key,
    demonstration_event_key,
    demonstration_record_key,
    encode_demonstration_record,
    load_demonstration_record,
    validate_demonstration_for_replay,
)
from skyvern.forge.sdk.copilot.demonstration_replay import replay_demonstration
from skyvern.forge.sdk.copilot.demonstration_tap import TapEvent
from skyvern.forge.sdk.copilot.mcp_adapter import SkyvernOverlayMCPServer
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.secret_scrub import register_secret_scrub_value
from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays, get_skyvern_mcp_alias_map
from skyvern.forge.sdk.schemas.credentials import Credential, CredentialType, CredentialVaultType
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotDemonstrationControlRequest
from tests.unit.copilot_test_helpers import make_copilot_ctx


class _SharedCache:
    is_shared = True

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.expirations: dict[str, Any] = {}
        self.lock_names: list[str] = []

    async def get(self, key: str) -> Any:
        return self.store.get(key)

    async def set(self, key: str, value: Any, ex: Any = None) -> None:
        self.store[key] = value
        self.expirations[key] = ex

    def get_lock(self, lock_name: str, blocking_timeout: int = 5, timeout: int = 10) -> NoopLock:
        self.lock_names.append(lock_name)
        return NoopLock(lock_name, blocking_timeout, timeout)


def _signal_refresh_after(recorder: DemonstrationRecorder, started: asyncio.Event) -> asyncio.Event:
    refreshed = asyncio.Event()
    refresh_owner = recorder._refresh_owner

    async def refresh_and_signal() -> None:
        await refresh_owner()
        if started.is_set():
            refreshed.set()

    recorder._refresh_owner = refresh_and_signal  # type: ignore[method-assign]
    return refreshed


class _Stream:
    def __init__(self, *, disconnected: bool = False) -> None:
        self.frames: list[Any] = []
        self.disconnected = disconnected

    async def send(self, data: Any) -> bool:
        self.frames.append(data)
        return True

    async def is_disconnected(self) -> bool:
        return self.disconnected

    async def close(self) -> None:
        return None


class _NoDispatchClient:
    async def call_tool(self, name: str, args: dict[str, Any], raise_on_error: bool = False) -> Any:
        raise AssertionError(f"recorded call dispatched to MCP: {name}")


class _FakeTap:
    events: ClassVar[list[TapEvent]] = []
    flush_events: ClassVar[list[TapEvent]] = []

    def __init__(self, context: Any, *, max_events: int = recorder_module.DEMONSTRATION_MAX_EVENTS) -> None:
        events = list(type(self).events)
        self.max_events = max_events
        self.event_limit_reached = len(events) > max_events
        self.pending = events[:max_events]

    async def start(self, pages: Any) -> None:
        return None

    def drain_events(self) -> list[TapEvent]:
        events, self.pending = self.pending, []
        return events

    async def flush(self) -> None:
        self.pending.extend(type(self).flush_events)
        if len(self.pending) > self.max_events:
            self.event_limit_reached = True
            self.pending = self.pending[: self.max_events]

    async def stop(self) -> None:
        return None

    async def set_intercept_file_chooser_dialog(self, enabled: bool) -> None:
        return None

    async def frame_path(self, event: TapEvent) -> list[dict[str, str]] | None:
        return [] if event.is_main_frame else [{"name": event.frame_name}] if event.frame_name else None

    async def call_on_file_input(self, event: TapEvent, declaration: str, *args: Any) -> Any:
        if "selectorFor" in declaration:
            return {"selector": "#resume", "label": "Resume"}
        type(self).attached_files.append(args[:2])
        return 1

    attached_files: ClassVar[list[tuple[Any, ...]]] = []

    def begin_control_navigation(self) -> None:
        return None

    def end_control_navigation(self) -> None:
        return None


def _credential() -> Credential:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Credential(
        credential_id="cred-1",
        organization_id="org-1",
        name="Saved login",
        vault_type=CredentialVaultType.SKYVERN,
        item_id="item-1",
        credential_type=CredentialType.PASSWORD,
        username="user@example.test",
        card_last4=None,
        card_brand=None,
        created_at=now,
        modified_at=now,
    )


@pytest.mark.asyncio
async def test_replay_contract_never_touches_a_browser_and_consumes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _SharedCache()
    monkeypatch.setattr(app, "CACHE", cache)
    credential = _credential()
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            credentials=SimpleNamespace(get_credentials_by_ids=AsyncMock(return_value=[credential])),
        ),
    )
    token = "sk-proj-" + "a" * 48
    assert redact_raw_secrets_for_prompt(token) == "[REDACTED_SECRET]"

    async def safety_handler(*, prompt: str, prompt_name: str) -> dict[str, Any]:
        if "swordfish" in prompt:
            return {"version": "1", "state": "detected", "citations": ["swordfish"]}
        return {"version": "1", "state": "clean", "citations": []}

    monkeypatch.setattr(
        replay_module,
        "resolve_raw_secret_safety_handler",
        AsyncMock(return_value=safety_handler),
    )
    events = [
        {
            "kind": "click",
            "selector": "#company",
            "source_url": "https://example.test/form",
            "frame": {"is_main": True, "name": ""},
            "packet": {
                "role_name": {"role": "button", "accessible_name": "Company"},
                "selector_candidates": [{"selector": "#company", "source": "id", "match_count": 1}],
                "selector_match_count": 1,
            },
        },
        {
            "kind": "gap",
            "reason": "file_chooser",
            "source_url": f"https://example.test/form?token={token}",
            "frame": {"is_main": True, "name": ""},
        },
        {
            "kind": "type",
            "selector": "#name",
            "value": "Acme Corp",
            "source_url": "https://example.test/form",
            "frame": {"is_main": True, "name": ""},
            "packet": {"selector_match_count": 1},
        },
        {
            "kind": "type",
            "selector": "#reference",
            "value": token,
            "source_url": "https://example.test/form",
            "frame": {"is_main": True, "name": ""},
            "packet": {"selector_match_count": 1},
        },
        {
            "kind": "type",
            "selector": "#unlabelled",
            "value": "swordfish",
            "label": recorder_module._event_label({"kind": "type", "selector": "#unlabelled", "value": "swordfish"}),
            "source_url": "https://example.test/form",
            "frame": {"is_main": True, "name": ""},
            "packet": {"selector_match_count": 1},
        },
        {
            "kind": "select",
            "selector": "#tier",
            "value": "Premium",
            "source_url": "https://example.test/form",
            "frame": {"is_main": True, "name": ""},
        },
        {
            "kind": "key",
            "selector": "#tier",
            "key": "Enter",
            "source_url": "https://example.test/form",
            "frame": {"is_main": True, "name": ""},
        },
        {
            "kind": "navigate",
            "url": f"https://example.test/done?token={token}",
            "source_url": "https://example.test/form",
            "frame": {"is_main": True, "name": ""},
        },
        {
            "kind": "secret",
            "selector": "#password",
            "field": "password",
            "source_url": "https://example.test/done",
            "frame": {"is_main": True, "name": ""},
        },
        {
            "kind": "secret",
            "selector": "#nearby-heading",
            "field": "other",
            "field_label": 'Reference "code"',
            "source_url": "https://example.test/done",
            "frame": {"is_main": True, "name": ""},
        },
        {
            "kind": "credential_fill",
            "selector": "#password",
            "credential_id": "cred-1",
            "credential_name": "Saved login",
            "field": "password",
            "source_url": "https://example.test/done",
            "frame": {"is_main": True, "name": ""},
            "result": {"ok": True, "data": {"field": "password"}},
            "scout_fact": {
                "tool_name": "fill_credential_field",
                "selector": "#password",
                "source_url": "https://example.test/done",
                "credential_id": "cred-1",
                "credential_field": "password",
            },
        },
        {
            "kind": "upload",
            "selector": "#resume",
            "file_id": "file-1",
            "filename": "resume.pdf",
            "size_bytes": 4,
            "field_label": "Resume",
            "source_url": "https://example.test/done",
            "frame": {"is_main": True, "name": ""},
        },
        {
            "kind": "upload",
            "selector": "#cover",
            "field_label": "Cover letter",
            "source_url": "https://example.test/done",
            "frame": {"is_main": True, "name": ""},
        },
    ]
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord(
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            status="stopped",
            event_count=len(events),
            owner=None,
            owner_seen_at=None,
            final_url="https://example.test/done",
            stop_reason="disconnected",
            insertion={"after_label": "Saved step", "before_label": None},
        )
    )
    for seq, event in enumerate(events, 1):
        cache.store[demonstration_event_key("org-1", "demo-1", seq)] = json.dumps({"seq": seq, **event})

    ctx = make_copilot_ctx(
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        turn_id="turn-1",
    )
    ctx.demonstration_id = "demo-1"
    ctx.demonstration_insertion = {"after_label": "Ignored request step", "before_label": None}
    ctx.request_policy = RequestPolicy()
    aliases = get_skyvern_mcp_alias_map()
    overlays = _build_skyvern_mcp_overlays()
    names = ("click", "type_text", "select_option", "press_key", "navigate_browser")
    server = SkyvernOverlayMCPServer(
        transport=MagicMock(),
        overlays={name: overlays[name] for name in names},
        alias_map={name: aliases[name] for name in names},
        allowlist=frozenset(aliases[name] for name in names),
        context_provider=lambda: ctx,
    )
    server._client = _NoDispatchClient()
    ctx.discovery_mcp_server = server

    cache.store[demonstration_controls_key("org-1", "demo-1")] = "[]"
    history = await replay_demonstration(ctx, server)

    calls = [item for item in history if item.get("type") == "function_call"]
    assert [item["name"] for item in calls] == [
        "click",
        "type_text",
        "type_text",
        "type_text",
        "select_option",
        "press_key",
        "navigate_browser",
        "type_text",
        "type_text",
        "fill_credential_field",
        "upload_attached_file",
    ]
    assert json.loads(calls[-1]["arguments"]) == {"file_id": "file-1", "selector": "#resume"}
    serialized = json.dumps(history)
    assert "Acme Corp" in serialized
    assert token not in serialized
    assert "swordfish" not in serialized
    assert "<value not recorded>" in serialized
    assert any("file chooser" in str(item.get("content")) for item in history if item.get("role") == "user")
    assert any("disconnected" in str(item.get("content")) for item in history if item.get("role") == "user")
    assert any("#cover" in str(item.get("content")) and "no file" in str(item.get("content")) for item in history)
    gap_index = next(index for index, item in enumerate(history) if "file chooser" in str(item.get("content")))
    first_type_index = next(index for index, item in enumerate(history) if item.get("name") == "type_text")
    assert gap_index < first_type_index
    navigate_index = next(index for index, item in enumerate(history) if item.get("name") == "navigate_browser")
    assert "?token=" not in json.dumps(history[navigate_index : navigate_index + 2])
    secret_call = calls[7]
    assert json.loads(secret_call["arguments"])["text"] == "<value not recorded>"
    outputs = {
        item["call_id"]: json.loads(item["output"]).get("data")
        for item in history
        if item.get("type") == "function_call_output"
    }
    assert outputs[secret_call["call_id"]]["sensitive_field"] == "password"
    assert outputs[calls[8]["call_id"]] == {"value_recorded": False, "field_label": "Reference code"}
    assert json.loads(calls[4]["arguments"])["value"] == "Premium"
    assert ctx.pending_screenshots == []
    assert credential in ctx.request_policy.resolved_credentials
    assert all(item.get("actor") == "user" for item in ctx.scout_trajectory)
    assert ctx.scout_trajectory[0]["selector_candidates"][0]["selector"] == "#company"
    assert ctx.scout_trajectory[0]["result_url"] == "https://example.test/form"
    assert ctx.scout_trajectory[0]["observed_effects"]["url_changed"] is False
    assert "swordfish" not in json.dumps(ctx.demonstration_items)
    assert any("#unlabelled" in label for label in ctx.demonstration_items)
    assert "Saved step" in serialized
    assert "Ignored request step" not in serialized
    record = decode_demonstration_record(cache.store[demonstration_record_key("org-1", "demo-1")])
    assert record is not None and record.status == "consumed"
    payload_keys = [demonstration_event_key("org-1", "demo-1", seq) for seq in range(1, record.event_count + 1)]
    payload_keys.append(demonstration_record_key("org-1", "demo-1"))
    payload_keys.append(demonstration_controls_key("org-1", "demo-1"))
    assert {cache.expirations[key] for key in payload_keys} == {recorder_module.DEMONSTRATION_CANCELLED_TTL}
    with pytest.raises(DemonstrationRejection) as exc_info:
        await replay_demonstration(ctx, server)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("safety_handler", [None, AsyncMock(side_effect=RuntimeError("screen failed"))])
async def test_replay_values_fail_closed_when_secret_screen_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, safety_handler: Any
) -> None:
    cache = _SharedCache()
    monkeypatch.setattr(app, "CACHE", cache)
    monkeypatch.setattr(
        replay_module,
        "resolve_raw_secret_safety_handler",
        AsyncMock(return_value=safety_handler),
    )
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord(
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            status="stopped",
            event_count=1,
            owner=None,
            owner_seen_at=None,
            final_url="https://example.test/form",
        )
    )
    cache.store[demonstration_event_key("org-1", "demo-1", 1)] = json.dumps(
        {
            "seq": 1,
            "kind": "type",
            "selector": "#plain",
            "value": "ordinary words",
            "source_url": "https://example.test/form",
            "frame": {"is_main": True, "name": ""},
        }
    )
    ctx = make_copilot_ctx(organization_id="org-1", workflow_permanent_id="wfp-1")
    ctx.demonstration_id = "demo-1"
    server = SimpleNamespace(call_tool=AsyncMock(return_value={"ok": True}))

    history = await replay_demonstration(ctx, server)  # type: ignore[arg-type]

    assert "ordinary words" not in json.dumps(history)
    assert "<value not recorded>" in json.dumps(history)
    outage_facts = [item for item in history if item.get("role") == "user" and "unavailable" in item.get("content", "")]
    assert len(outage_facts) == 1


@pytest.mark.asyncio
async def test_replay_reports_nested_frame_chain_as_a_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _SharedCache()
    monkeypatch.setattr(app, "CACHE", cache)
    monkeypatch.setattr(replay_module, "resolve_raw_secret_safety_handler", AsyncMock(return_value=None))
    events = [
        {
            "kind": "click",
            "selector": "#inside",
            "source_url": "https://example.test/frame",
            "frame": {
                "is_main": False,
                "path": [{"name": "outer"}, {"owner_selector": "#inner"}],
            },
        },
        {
            "kind": "click",
            "selector": "#outside",
            "source_url": "https://example.test/",
            "frame": {"is_main": True, "path": []},
        },
    ]
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord(
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            status="stopped",
            event_count=len(events),
            owner=None,
            owner_seen_at=None,
            final_url="https://example.test/",
        )
    )
    for seq, event in enumerate(events, 1):
        cache.store[demonstration_event_key("org-1", "demo-1", seq)] = json.dumps({"seq": seq, **event})
    ctx = make_copilot_ctx(organization_id="org-1", workflow_permanent_id="wfp-1")
    ctx.demonstration_id = "demo-1"
    call_tool = AsyncMock(return_value={"ok": True})

    history = await replay_demonstration(ctx, SimpleNamespace(call_tool=call_tool))  # type: ignore[arg-type]

    call_tool.assert_awaited_once_with("click", {"selector": "#outside"})
    assert any("nested frame" in str(item.get("content")) for item in history)


@pytest.mark.asyncio
async def test_recorder_persists_tap_actions_controls_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _SharedCache()
    demonstration_id = "demo-1"
    owner = "owner-1"
    cache.store[demonstration_record_key("org-1", demonstration_id)] = encode_demonstration_record(
        DemonstrationRecord(
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            status="recording",
            event_count=0,
            owner=owner,
            owner_seen_at=datetime.now(UTC),
            final_url=None,
        )
    )
    cache.store[demonstration_controls_key("org-1", demonstration_id)] = json.dumps(
        [
            {"seq": 1, "action": "navigate", "url": "http://127.0.0.1/private"},
            {"seq": 2, "action": "navigate", "url": "https://example.test/next"},
            {"seq": 3, "action": "upload_file", "file_id": "file-1"},
        ]
    )
    await cache.set(demonstration_browser_key("org-1", "pbs-1"), demonstration_id, ex=timedelta(minutes=1))
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            uploaded_files=SimpleNamespace(
                get_uploaded_file=AsyncMock(
                    return_value=SimpleNamespace(storage_uri="s3://bucket/org-1/resume.pdf", filename="resume.pdf")
                )
            )
        ),
    )
    monkeypatch.setattr(app, "STORAGE", SimpleNamespace(download_managed_file=AsyncMock(return_value=b"%PDF")))
    page = SimpleNamespace(url="https://example.test/start", goto=AsyncMock(), is_closed=lambda: False)
    click = TapEvent(
        page=page,
        session_key="tap-1",
        frame_id="main",
        is_main_frame=True,
        frame_name="",
        payload={
            "kind": "click",
            "ts": 1,
            "url": page.url,
            "selector": "#continue",
            "packet": {"role_name": {"role": "button", "accessible_name": "Continue"}},
        },
    )
    password = TapEvent(
        page=page,
        session_key="tap-1",
        frame_id="main",
        is_main_frame=True,
        frame_name="",
        payload={
            "kind": "secret",
            "ts": 2,
            "url": "https://example.test/start?token=secret#password",
            "selector": "#password",
            "field": "password",
        },
    )
    password_focus = TapEvent(
        page=page,
        session_key="tap-1",
        frame_id="main",
        is_main_frame=True,
        frame_name="",
        payload={
            "kind": "secret_focus",
            "ts": 1.5,
            "url": "https://example.test/start?token=secret#password",
            "selector": "#password",
            "field": "password",
        },
    )
    file_click = TapEvent(
        page=page,
        session_key="tap-1",
        frame_id="main",
        is_main_frame=True,
        frame_name="",
        payload={"kind": "file_click", "ts": 3, "url": page.url, "selector": "#upload"},
    )
    file_chooser = TapEvent(
        page=page,
        session_key="tap-1",
        frame_id="main",
        is_main_frame=True,
        frame_name="",
        payload={"kind": "file_chooser", "ts": 3.1, "backend_node_id": 7},
    )
    unanswered_chooser = TapEvent(
        page=page,
        session_key="tap-1",
        frame_id="main",
        is_main_frame=True,
        frame_name="",
        payload={"kind": "file_chooser", "ts": 3.2, "backend_node_id": 8},
    )

    browser_context = SimpleNamespace(pages=[page])
    browser_state = SimpleNamespace(
        browser_context=browser_context,
        get_working_page=AsyncMock(return_value=page),
    )
    _FakeTap.events = [click, password_focus, password, file_click, file_chooser, unanswered_chooser]
    _FakeTap.flush_events = []
    _FakeTap.attached_files = []
    monkeypatch.setattr(recorder_module, "PageTap", _FakeTap)
    monkeypatch.setattr(recorder_module, "resolve_persistent_browser_state", AsyncMock(return_value=browser_state))

    def validate(url: str) -> str:
        if "127.0.0.1" in url:
            raise SkyvernHTTPException(message="private", status_code=400)
        return url

    monkeypatch.setattr(recorder_module, "validate_fetch_url", validate)
    stream = _Stream()
    stream.is_disconnected = AsyncMock(side_effect=[False, True])  # type: ignore[method-assign]
    recorder = DemonstrationRecorder(
        cache=cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        claim=DemonstrationClaim(demonstration_id, owner, False, 0),
        stream=stream,
    )

    await recorder.run()

    assert stream.frames[0]["type"] == "demonstration_started"
    assert stream.frames[0]["can_fill_credentials"] is True
    first = json.loads(cache.store[demonstration_event_key("org-1", demonstration_id, 1)])
    second = json.loads(cache.store[demonstration_event_key("org-1", demonstration_id, 2)])
    third = json.loads(cache.store[demonstration_event_key("org-1", demonstration_id, 3)])
    fourth = json.loads(cache.store[demonstration_event_key("org-1", demonstration_id, 4)])
    assert [first["kind"], second["kind"], third["kind"], fourth["kind"]] == [
        "click",
        "secret",
        "navigate",
        "upload",
    ]
    assert {key: fourth[key] for key in ("selector", "file_id", "filename", "size_bytes")} == {
        "selector": "#resume",
        "file_id": "file-1",
        "filename": "resume.pdf",
        "size_bytes": 4,
    }
    assert _FakeTap.attached_files == [("resume.pdf", "application/pdf")]
    assert {"type": "demonstration_file_requested", "label": "Resume"} in stream.frames
    unanswered = json.loads(cache.store[demonstration_event_key("org-1", demonstration_id, 5)])
    assert unanswered["kind"] == "upload" and "file_id" not in unanswered
    assert page.goto.await_args.args == ("https://example.test/next",)
    assert any(frame.get("type") == "demonstration_notice" and frame.get("level") == "error" for frame in stream.frames)
    action_frames = [frame for frame in stream.frames if frame.get("type") == "demonstration_action"]
    assert [(frame["seq"], frame["tool_name"]) for frame in action_frames] == [
        (1, "click"),
        (2, "type_text"),
        (3, "navigate_browser"),
        (4, "upload_attached_file"),
    ]
    assert action_frames[1]["label"] == second["label"] == "Fill password (value not recorded)"
    sensitive_frame = next(frame for frame in stream.frames if frame.get("type") == "demonstration_sensitive_field")
    assert sensitive_frame == {
        "type": "demonstration_sensitive_field",
        "field": "password",
        "selector": "#password",
        "url": "https://example.test/start",
    }
    stopped = decode_demonstration_record(cache.store[demonstration_record_key("org-1", demonstration_id)])
    assert stopped is not None and stopped.status == "stopped" and stopped.event_count == 5
    assert stopped.stop_reason == "disconnected"
    assert stream.frames[-1] == {
        "type": "demonstration_stopped",
        "demonstration_id": "demo-1",
        "reason": "disconnected",
        "action_count": 5,
    }
    assert cache.expirations[demonstration_browser_key("org-1", "pbs-1")] == recorder_module.DEMONSTRATION_TTL


@pytest.mark.asyncio
async def test_live_owner_blocks_a_second_recording_on_the_browser() -> None:
    cache = _SharedCache()
    cache.store[demonstration_browser_key("org-1", "pbs-1")] = "demo-live"
    cache.store[demonstration_record_key("org-1", "demo-live")] = encode_demonstration_record(
        DemonstrationRecord(
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            status="recording",
            event_count=0,
            owner="owner-live",
            owner_seen_at=datetime.now(UTC),
            final_url=None,
        )
    )

    with pytest.raises(DemonstrationRejection) as exc_info:
        await claim_demonstration_recording(
            cache,  # type: ignore[arg-type]
            organization_id="org-1",
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            demonstration_id=None,
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_long_control_keeps_the_owner_live(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _SharedCache()
    demonstration_id = "demo-long-control"
    owner = "owner-1"
    cache.store[demonstration_browser_key("org-1", "pbs-1")] = demonstration_id
    cache.store[demonstration_record_key("org-1", demonstration_id)] = encode_demonstration_record(
        DemonstrationRecord(
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            status="recording",
            event_count=0,
            owner=owner,
            owner_seen_at=datetime.now(UTC) - timedelta(seconds=recorder_module.DEMONSTRATION_OWNER_LIVE_SECONDS + 1),
            final_url=None,
        )
    )
    cache.store[demonstration_controls_key("org-1", demonstration_id)] = json.dumps(
        [{"seq": 1, "action": "navigate", "url": "https://example.test/slow"}]
    )
    navigation_started = asyncio.Event()
    finish_navigation = asyncio.Event()

    async def slow_goto(url: str) -> None:
        assert url == "https://example.test/slow"
        navigation_started.set()
        await finish_navigation.wait()

    monkeypatch.setattr(recorder_module, "DEMONSTRATION_OWNER_REFRESH_SECONDS", 0.01)
    monkeypatch.setattr(recorder_module, "validate_fetch_url", lambda url: url)
    recorder = DemonstrationRecorder(
        cache=cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        claim=DemonstrationClaim(demonstration_id, owner, False, 0),
        stream=_Stream(),
    )
    recorder.page = SimpleNamespace(url="https://example.test/start", goto=slow_goto)
    refreshed = _signal_refresh_after(recorder, navigation_started)

    control = asyncio.create_task(recorder._run_controls())
    await asyncio.wait_for(refreshed.wait(), 5)
    try:
        with pytest.raises(DemonstrationRejection) as exc_info:
            await claim_demonstration_recording(
                cache,  # type: ignore[arg-type]
                organization_id="org-1",
                workflow_permanent_id="wfp-1",
                browser_session_id="pbs-1",
                demonstration_id=None,
            )
        assert exc_info.value.status_code == 409
    finally:
        finish_navigation.set()
        await control


@pytest.mark.asyncio
async def test_stop_flush_keeps_the_owner_live(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _SharedCache()
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord(
            "wfp-1",
            "pbs-1",
            "recording",
            0,
            "owner-1",
            datetime.now(UTC) - timedelta(seconds=recorder_module.DEMONSTRATION_OWNER_LIVE_SECONDS + 1),
            None,
        )
    )
    flush_started = asyncio.Event()
    finish_flush = asyncio.Event()

    class SlowTap:
        event_limit_reached = False

        async def flush(self) -> None:
            flush_started.set()
            await finish_flush.wait()

        def drain_events(self) -> list[TapEvent]:
            return []

        async def stop(self) -> None:
            return None

        async def set_intercept_file_chooser_dialog(self, enabled: bool) -> None:
            return None

    monkeypatch.setattr(recorder_module, "DEMONSTRATION_OWNER_REFRESH_SECONDS", 0.01)
    recorder = DemonstrationRecorder(
        cache=cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        claim=DemonstrationClaim("demo-1", "owner-1", False, 0),
        stream=_Stream(),
    )
    recorder.tap = SlowTap()  # type: ignore[assignment]
    recorder.page = SimpleNamespace(url="https://example.test/final", is_closed=lambda: False)

    refreshed = _signal_refresh_after(recorder, flush_started)
    stopping = asyncio.create_task(recorder._stop("stopped"))
    await asyncio.wait_for(refreshed.wait(), 5)
    try:
        with pytest.raises(DemonstrationRejection) as exc_info:
            await validate_demonstration_for_replay(
                cache,  # type: ignore[arg-type]
                organization_id="org-1",
                workflow_permanent_id="wfp-1",
                demonstration_id="demo-1",
            )
        assert exc_info.value.status_code == 409
    finally:
        finish_flush.set()
        await stopping

    record = await load_demonstration_record(cache, "org-1", "demo-1")  # type: ignore[arg-type]
    assert record is not None and record.status == "stopped"


@pytest.mark.asyncio
async def test_record_claim_never_locks_the_browser_index_key_and_keeps_orgs_isolated() -> None:
    cache = _SharedCache()
    claim = await claim_demonstration_recording(
        cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        demonstration_id=None,
        insertion={"after_label": "First", "before_label": None},
    )

    browser_key = demonstration_browser_key("org-1", "pbs-1")
    assert browser_key not in cache.lock_names
    assert f"{browser_key}:lock" in cache.lock_names
    record = await load_demonstration_record(cache, "org-1", claim.demonstration_id)  # type: ignore[arg-type]
    assert record is not None and record.insertion == {"after_label": "First", "before_label": None}
    record.status = "stopped"
    record.owner = None
    record.owner_seen_at = None
    record.stop_reason = "disconnected"
    record.final_url = "https://example.test/old-final"
    cache.store[demonstration_record_key("org-1", claim.demonstration_id)] = encode_demonstration_record(record)
    await claim_demonstration_recording(
        cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        demonstration_id=claim.demonstration_id,
        insertion={"after_label": "Overwritten", "before_label": "Last"},
    )
    resumed = await load_demonstration_record(cache, "org-1", claim.demonstration_id)  # type: ignore[arg-type]
    assert resumed is not None and resumed.insertion == {"after_label": "First", "before_label": None}
    assert resumed.stop_reason is None
    assert resumed.final_url is None
    assert await load_demonstration_record(cache, "org-2", claim.demonstration_id) is None  # type: ignore[arg-type]
    with pytest.raises(DemonstrationRejection) as exc_info:
        await validate_demonstration_for_replay(
            cache,  # type: ignore[arg-type]
            organization_id="org-2",
            workflow_permanent_id="wfp-1",
            demonstration_id=claim.demonstration_id,
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(("stop_reason", "expected_reason"), [(None, "stopped"), ("browser_gone", "browser_gone")])
async def test_stop_control_flushes_focused_typing_before_recorder_commits_stopped(
    monkeypatch: pytest.MonkeyPatch, stop_reason: Literal["browser_gone"] | None, expected_reason: str
) -> None:
    cache = _SharedCache()
    record = DemonstrationRecord(
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        status="recording",
        event_count=0,
        owner="owner-1",
        owner_seen_at=datetime.now(UTC),
        final_url=None,
    )
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(record)
    cache.store[demonstration_controls_key("org-1", "demo-1")] = "[]"

    await append_demonstration_control(
        cache,  # type: ignore[arg-type]
        organization_id="org-1",
        request=WorkflowCopilotDemonstrationControlRequest(
            demonstration_id="demo-1", action="stop", stop_reason=stop_reason
        ),
    )

    stopping = await load_demonstration_record(cache, "org-1", "demo-1")  # type: ignore[arg-type]
    assert stopping is not None and stopping.status == "stopping" and stopping.stop_reason == expected_reason
    page = SimpleNamespace(url="https://example.test/form", is_closed=lambda: False)
    typed = TapEvent(
        page=page,
        session_key="tap-1",
        frame_id="main",
        is_main_frame=True,
        frame_name="",
        payload={
            "kind": "type",
            "ts": 1,
            "url": page.url,
            "selector": "#final",
            "value": "last field value",
        },
    )
    recorder = DemonstrationRecorder(
        cache=cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        claim=DemonstrationClaim("demo-1", "owner-1", False, 0),
        stream=_Stream(),
    )
    _FakeTap.events = []
    _FakeTap.flush_events = [typed]
    recorder.tap = _FakeTap(SimpleNamespace())  # type: ignore[assignment]
    recorder.page = page

    await recorder._stop("disconnected")

    stopped = await load_demonstration_record(cache, "org-1", "demo-1")  # type: ignore[arg-type]
    assert stopped is not None and stopped.status == "stopped" and stopped.stop_reason == expected_reason
    event_key = demonstration_event_key("org-1", "demo-1", 1)
    assert json.loads(cache.store[event_key])["value"] == "last field value"
    assert cache.expirations[event_key] == recorder_module.DEMONSTRATION_TTL
    if stop_reason is not None:
        monkeypatch.setattr(app, "CACHE", cache)
        monkeypatch.setattr(replay_module, "resolve_raw_secret_safety_handler", AsyncMock(return_value=None))
        ctx = make_copilot_ctx(organization_id="org-1", workflow_permanent_id="wfp-1")
        ctx.demonstration_id = "demo-1"
        history = await replay_demonstration(ctx, SimpleNamespace(call_tool=AsyncMock(return_value={"ok": True})))  # type: ignore[arg-type]
        assert any("browser gone" in str(item.get("content")) for item in history)


@pytest.mark.asyncio
async def test_discard_after_resume_commits_cancelled_even_when_the_stream_drops() -> None:
    cache = _SharedCache()
    cache.store[demonstration_browser_key("org-1", "pbs-1")] = "demo-1"
    cache.store[demonstration_controls_key("org-1", "demo-1")] = "[]"
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord(
            "wfp-1",
            "pbs-1",
            "stopped",
            2,
            None,
            None,
            "https://example.test/old-final",
            stop_reason="disconnected",
        )
    )
    for seq in (1, 2):
        await cache.set(
            demonstration_event_key("org-1", "demo-1", seq),
            json.dumps({"kind": "type", "value": f"typed {seq}"}),
            ex=recorder_module.DEMONSTRATION_TTL,
        )
    claim = await claim_demonstration_recording(
        cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        demonstration_id="demo-1",
    )
    await append_demonstration_control(
        cache,  # type: ignore[arg-type]
        organization_id="org-1",
        request=WorkflowCopilotDemonstrationControlRequest(demonstration_id="demo-1", action="cancel"),
    )
    recorder = DemonstrationRecorder(
        cache=cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        claim=claim,
        stream=_Stream(),
    )

    await recorder._stop("disconnected")

    record = await load_demonstration_record(cache, "org-1", "demo-1")  # type: ignore[arg-type]
    assert record is not None and record.status == "cancelled" and record.stop_reason == "cancelled"
    assert {
        cache.expirations[key]
        for key in [
            *(demonstration_event_key("org-1", "demo-1", seq) for seq in (1, 2)),
            demonstration_controls_key("org-1", "demo-1"),
            demonstration_record_key("org-1", "demo-1"),
        ]
    } == {recorder_module.DEMONSTRATION_CANCELLED_TTL}
    active = await active_demonstration(
        cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
    )
    assert active is None


@pytest.mark.asyncio
async def test_client_upload_control_cannot_name_a_file() -> None:
    cache = _SharedCache()
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord(
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            status="recording",
            event_count=0,
            owner="owner-1",
            owner_seen_at=datetime.now(UTC),
            final_url=None,
        )
    )
    cache.store[demonstration_controls_key("org-1", "demo-1")] = "[]"
    crafted = WorkflowCopilotDemonstrationControlRequest.model_validate(
        {"demonstration_id": "demo-1", "action": "upload_file", "file_id": "file-from-elsewhere"}
    )

    await append_demonstration_control(cache, organization_id="org-1", request=crafted)  # type: ignore[arg-type]
    await append_demonstration_control(
        cache,  # type: ignore[arg-type]
        organization_id="org-1",
        request=WorkflowCopilotDemonstrationControlRequest(demonstration_id="demo-1", action="upload_file"),
        file_id="file-stored-for-demo",
    )

    controls = json.loads(cache.store[demonstration_controls_key("org-1", "demo-1")])
    assert [control.get("file_id") for control in controls] == [None, "file-stored-for-demo"]


@pytest.mark.asyncio
async def test_recorder_runs_controls_queued_before_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _SharedCache()
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord("wfp-1", "pbs-1", "recording", 0, "owner-1", datetime.now(UTC), None)
    )
    cache.store[demonstration_controls_key("org-1", "demo-1")] = json.dumps(
        [{"seq": 1, "action": "navigate", "url": "https://example.test/next"}]
    )
    await append_demonstration_control(
        cache,  # type: ignore[arg-type]
        organization_id="org-1",
        request=WorkflowCopilotDemonstrationControlRequest(demonstration_id="demo-1", action="stop"),
    )
    page = SimpleNamespace(url="https://example.test/start", goto=AsyncMock(), is_closed=lambda: False)
    browser_state = SimpleNamespace(
        browser_context=SimpleNamespace(pages=[page]),
        get_working_page=AsyncMock(return_value=page),
    )
    _FakeTap.events = []
    _FakeTap.flush_events = []
    monkeypatch.setattr(recorder_module, "PageTap", _FakeTap)
    monkeypatch.setattr(recorder_module, "validate_fetch_url", lambda url: url)
    monkeypatch.setattr(recorder_module, "resolve_persistent_browser_state", AsyncMock(return_value=browser_state))
    recorder = DemonstrationRecorder(
        cache=cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        claim=DemonstrationClaim("demo-1", "owner-1", False, 0),
        stream=_Stream(),
    )

    await recorder.run()

    page.goto.assert_awaited_once_with("https://example.test/next")
    record = await load_demonstration_record(cache, "org-1", "demo-1")  # type: ignore[arg-type]
    assert record is not None and record.status == "stopped" and record.event_count == 1
    event = json.loads(cache.store[demonstration_event_key("org-1", "demo-1", 1)])
    assert event["kind"] == "navigate"


@pytest.mark.asyncio
async def test_replay_failure_restores_stopped_record(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _SharedCache()
    monkeypatch.setattr(app, "CACHE", cache)
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord(
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            status="stopped",
            event_count=1,
            owner=None,
            owner_seen_at=None,
            final_url=None,
        )
    )
    cache.store[demonstration_event_key("org-1", "demo-1", 1)] = json.dumps(
        {
            "seq": 1,
            "kind": "click",
            "selector": "#submit",
            "source_url": "https://example.test/form",
            "frame": {"is_main": True, "name": ""},
        }
    )
    monkeypatch.setattr(replay_module, "resolve_raw_secret_safety_handler", AsyncMock(return_value=None))
    ctx = make_copilot_ctx(organization_id="org-1", workflow_permanent_id="wfp-1")
    ctx.demonstration_id = "demo-1"
    call_tool = AsyncMock(side_effect=RuntimeError("replay failed"))

    with pytest.raises(RuntimeError, match="replay failed"):
        await replay_demonstration(ctx, SimpleNamespace(call_tool=call_tool))  # type: ignore[arg-type]

    record = await load_demonstration_record(cache, "org-1", "demo-1")  # type: ignore[arg-type]
    assert record is not None and record.status == "stopped"
    call_tool.assert_awaited_once_with("click", {"selector": "#submit"})


@pytest.mark.asyncio
async def test_expired_replay_cannot_finalize_a_newer_lease() -> None:
    cache = _SharedCache()
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord("wfp-1", "pbs-1", "stopped", 0, None, None, None)
    )
    ctx = make_copilot_ctx(organization_id="org-1", workflow_permanent_id="wfp-1")

    first = await replay_module._claim_replay(cache, ctx, "demo-1")  # type: ignore[arg-type]
    assert first.replay_token is not None
    stale = await load_demonstration_record(cache, "org-1", "demo-1")  # type: ignore[arg-type]
    assert stale is not None
    stale.replay_started_at = datetime.now(UTC) - recorder_module.DEMONSTRATION_REPLAY_LEASE - timedelta(seconds=1)
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(stale)

    second = await replay_module._claim_replay(cache, ctx, "demo-1")  # type: ignore[arg-type]
    assert second.replay_token is not None and second.replay_token != first.replay_token
    await replay_module._finish_replay(
        cache,  # type: ignore[arg-type]
        ctx,
        "demo-1",
        first.replay_token,
        consumed=True,
    )

    record = await load_demonstration_record(cache, "org-1", "demo-1")  # type: ignore[arg-type]
    assert record is not None and record.status == "replaying" and record.replay_token == second.replay_token

    await replay_module._finish_replay(
        cache,  # type: ignore[arg-type]
        ctx,
        "demo-1",
        second.replay_token,
        consumed=True,
    )
    record = await load_demonstration_record(cache, "org-1", "demo-1")  # type: ignore[arg-type]
    assert record is not None and record.status == "consumed" and record.replay_token is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disconnected", "page_closed", "max_seconds", "event_count", "typed_value", "expected_reason"),
    [
        (True, False, recorder_module.DEMONSTRATION_MAX_SECONDS, 0, None, "disconnected"),
        (False, True, recorder_module.DEMONSTRATION_MAX_SECONDS, 0, None, "browser_gone"),
        (False, False, 0, 0, None, "time_limit"),
        (False, False, recorder_module.DEMONSTRATION_MAX_SECONDS, 2, None, "event_limit"),
        (False, False, recorder_module.DEMONSTRATION_MAX_SECONDS, 0, "long notes", "event_limit"),
    ],
)
async def test_recorder_stops_for_terminal_reason(
    monkeypatch: pytest.MonkeyPatch,
    disconnected: bool,
    page_closed: bool,
    max_seconds: int,
    event_count: int,
    typed_value: str | None,
    expected_reason: str,
) -> None:
    cache = _SharedCache()
    demonstration_id = f"demo-{expected_reason}"
    owner = "owner-1"
    cache.store[demonstration_record_key("org-1", demonstration_id)] = encode_demonstration_record(
        DemonstrationRecord(
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            status="recording",
            event_count=0,
            owner=owner,
            owner_seen_at=datetime.now(UTC),
            final_url=None,
            recording_started_at=datetime.now(UTC),
        )
    )
    cache.store[demonstration_controls_key("org-1", demonstration_id)] = "[]"
    page = SimpleNamespace(url="https://example.test/start", is_closed=lambda: page_closed)

    browser_state = SimpleNamespace(
        browser_context=SimpleNamespace(pages=[page]),
        get_working_page=AsyncMock(return_value=page),
    )
    monkeypatch.setattr(recorder_module, "DEMONSTRATION_MAX_SECONDS", max_seconds)
    monkeypatch.setattr(recorder_module, "DEMONSTRATION_MAX_EVENTS", 1 if typed_value is None else 10)
    _FakeTap.events = [
        TapEvent(
            page,
            "tap-1",
            "main",
            True,
            "",
            {"kind": "click", "ts": seq, "url": page.url, "selector": f"#action-{seq}"},
        )
        for seq in range(event_count)
    ]
    if typed_value is not None:
        monkeypatch.setattr(recorder_module, "DEMONSTRATION_MAX_VALUE_CHARS", len(typed_value))
        _FakeTap.events = [
            TapEvent(
                page,
                "tap-1",
                "main",
                True,
                "",
                {"kind": "type", "ts": seq, "url": page.url, "selector": "#notes", "value": typed_value},
            )
            for seq in range(2)
        ]
    _FakeTap.flush_events = []
    monkeypatch.setattr(recorder_module, "PageTap", _FakeTap)
    monkeypatch.setattr(recorder_module, "resolve_persistent_browser_state", AsyncMock(return_value=browser_state))
    stream = _Stream(disconnected=disconnected)
    recorder = DemonstrationRecorder(
        cache=cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        claim=DemonstrationClaim(demonstration_id, owner, False, 0),
        stream=stream,
    )

    await recorder.run()

    record = await load_demonstration_record(cache, "org-1", demonstration_id)  # type: ignore[arg-type]
    assert record is not None and record.status == "stopped" and record.stop_reason == expected_reason
    assert stream.frames[-1]["reason"] == expected_reason
    if typed_value is not None:
        assert record.event_count == 1
        stored = json.loads(cache.store[demonstration_event_key("org-1", demonstration_id, 1)])
        assert stored["value"] == typed_value
        with pytest.raises(DemonstrationRejection, match="event limit"):
            await claim_demonstration_recording(
                cache,  # type: ignore[arg-type]
                organization_id="org-1",
                workflow_permanent_id="wfp-1",
                browser_session_id="pbs-1",
                demonstration_id=demonstration_id,
            )
    if expected_reason == "event_limit":
        monkeypatch.setattr(app, "CACHE", cache)
        monkeypatch.setattr(replay_module, "resolve_raw_secret_safety_handler", AsyncMock(return_value=None))
        ctx = make_copilot_ctx(organization_id="org-1", workflow_permanent_id="wfp-1")
        ctx.demonstration_id = demonstration_id
        history = await replay_demonstration(ctx, SimpleNamespace(call_tool=AsyncMock(return_value={"ok": True})))  # type: ignore[arg-type]
        assert any("event limit" in str(item.get("content")) for item in history)


@pytest.mark.asyncio
async def test_resumed_recording_clears_prior_stop_reason_before_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _SharedCache()
    monkeypatch.setattr(app, "CACHE", cache)
    monkeypatch.setattr(replay_module, "resolve_raw_secret_safety_handler", AsyncMock(return_value=None))
    cache.store[demonstration_browser_key("org-1", "pbs-1")] = "demo-1"
    cache.store[demonstration_controls_key("org-1", "demo-1")] = "[]"
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord(
            "wfp-1",
            "pbs-1",
            "recording",
            0,
            "owner-1",
            datetime.now(UTC),
            None,
        )
    )
    page = SimpleNamespace(url="https://example.test/start", is_closed=lambda: False)
    browser_state = SimpleNamespace(
        browser_context=SimpleNamespace(pages=[page]),
        get_working_page=AsyncMock(return_value=page),
    )
    _FakeTap.events = []
    _FakeTap.flush_events = []
    monkeypatch.setattr(recorder_module, "PageTap", _FakeTap)
    monkeypatch.setattr(recorder_module, "resolve_persistent_browser_state", AsyncMock(return_value=browser_state))
    first_recorder = DemonstrationRecorder(
        cache=cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        claim=DemonstrationClaim("demo-1", "owner-1", False, 0),
        stream=_Stream(disconnected=True),
    )
    await first_recorder.run()
    disconnected = await load_demonstration_record(cache, "org-1", "demo-1")  # type: ignore[arg-type]
    assert disconnected is not None and disconnected.stop_reason == "disconnected"

    claim = await claim_demonstration_recording(
        cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        demonstration_id="demo-1",
    )
    await append_demonstration_control(
        cache,  # type: ignore[arg-type]
        organization_id="org-1",
        request=WorkflowCopilotDemonstrationControlRequest(demonstration_id="demo-1", action="stop"),
    )
    recorder = DemonstrationRecorder(
        cache=cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        claim=claim,
        stream=_Stream(),
    )
    await recorder._stop("stopped")
    ctx = make_copilot_ctx(organization_id="org-1", workflow_permanent_id="wfp-1")
    ctx.demonstration_id = "demo-1"

    history = await replay_demonstration(ctx, SimpleNamespace(call_tool=AsyncMock()))  # type: ignore[arg-type]

    assert not any("ended because" in str(item.get("content")) for item in history)


@pytest.mark.asyncio
async def test_recorder_switches_to_remaining_page_when_popup_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _SharedCache()
    cache.store[demonstration_record_key("org-1", "demo-popup")] = encode_demonstration_record(
        DemonstrationRecord(
            "wfp-1", "pbs-1", "recording", 0, "owner-1", datetime.now(UTC), None, recording_started_at=datetime.now(UTC)
        )
    )
    cache.store[demonstration_controls_key("org-1", "demo-popup")] = "[]"
    popup = SimpleNamespace(url="https://auth.example.test", is_closed=lambda: True)
    opener = SimpleNamespace(url="https://example.test/after-auth", is_closed=lambda: False)
    browser_state = SimpleNamespace(
        browser_context=SimpleNamespace(pages=[opener]),
        get_working_page=AsyncMock(return_value=popup),
    )
    _FakeTap.events = []
    _FakeTap.flush_events = []
    monkeypatch.setattr(recorder_module, "PageTap", _FakeTap)
    monkeypatch.setattr(recorder_module, "DEMONSTRATION_MAX_SECONDS", 0)
    monkeypatch.setattr(recorder_module, "resolve_persistent_browser_state", AsyncMock(return_value=browser_state))
    recorder = DemonstrationRecorder(
        cache=cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        claim=DemonstrationClaim("demo-popup", "owner-1", False, 0),
        stream=_Stream(),
    )

    await recorder.run()

    record = await load_demonstration_record(cache, "org-1", "demo-popup")  # type: ignore[arg-type]
    assert record is not None and record.stop_reason == "time_limit"
    assert record.final_url == opener.url


@pytest.mark.asyncio
async def test_credential_fill_control_never_persists_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sentinel = "credential-secret-sentinel"
    cache = _SharedCache()
    demonstration_id = "demo-credential"
    owner = "owner-1"
    cache.store[demonstration_record_key("org-1", demonstration_id)] = encode_demonstration_record(
        DemonstrationRecord(
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            status="recording",
            event_count=0,
            owner=owner,
            owner_seen_at=datetime.now(UTC),
            final_url=None,
        )
    )
    credential = _credential()
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(credentials=SimpleNamespace(get_credentials_by_ids=AsyncMock(return_value=[credential]))),
    )
    page = SimpleNamespace(url="https://example.test/login")

    async def fill(ctx: Any, selector: str, credential_id: str, field: str) -> dict[str, Any]:
        policy = ctx.request_policy
        assert policy.credential_ask_login_page_urls == ["https://example.test/login"]
        assert credential in policy.resolved_credentials
        register_secret_scrub_value(ctx, sentinel)
        ctx.scout_trajectory.append(
            {
                "tool_name": "fill_credential_field",
                "selector": selector,
                "credential_id": credential_id,
                "credential_field": field,
            }
        )
        await recorder._record_tap_event(
            TapEvent(
                page=page,
                session_key="tap-1",
                frame_id="main",
                is_main_frame=True,
                frame_name="",
                payload={"kind": "secret", "selector": selector, "field": field, "url": page.url},
            )
        )
        return {"ok": True, "data": {"readback": sentinel}}

    monkeypatch.setattr(recorder_module, "fill_credential_field", fill)
    stream = _Stream()
    recorder = DemonstrationRecorder(
        cache=cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        claim=DemonstrationClaim(demonstration_id, owner, False, 0),
        stream=stream,
    )
    recorder.page = page

    await recorder._fill_credential(
        {
            "action": "fill_credential",
            "credential_id": credential.credential_id,
            "selector": "#password",
            "field": "password",
        }
    )

    event = cache.store[demonstration_event_key("org-1", demonstration_id, 1)]
    assert sentinel not in event
    assert sentinel not in json.dumps(stream.frames)
    assert sentinel not in caplog.text
    stored = json.loads(event)
    assert stored["result"]["data"]["readback"] == "[REDACTED_SECRET]"
    assert stored["scout_fact"]["actor"] == "user"

    await recorder._record_tap_event(
        TapEvent(
            page=page,
            session_key="tap-1",
            frame_id="main",
            is_main_frame=True,
            frame_name="",
            payload={"kind": "secret", "selector": "#password", "field": "password", "url": page.url},
        )
    )
    corrected = await load_demonstration_record(cache, "org-1", demonstration_id)  # type: ignore[arg-type]
    assert corrected is not None and corrected.event_count == 2
    assert json.loads(cache.store[demonstration_event_key("org-1", demonstration_id, 2)])["kind"] == "secret"


@pytest.mark.asyncio
async def test_credential_fill_control_refuses_a_credential_bound_to_another_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _SharedCache()
    cache.store[demonstration_record_key("org-1", "demo-origin")] = encode_demonstration_record(
        DemonstrationRecord("wfp-1", "pbs-1", "recording", 0, "owner-1", datetime.now(UTC), None)
    )
    credential = _credential().model_copy(update={"tested_url": "https://site-a.example/login"})
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(credentials=SimpleNamespace(get_credentials_by_ids=AsyncMock(return_value=[credential]))),
    )

    async def fill(ctx: Any, selector: str, credential_id: str, field: str) -> dict[str, Any]:
        assert ctx.request_policy.live_page_admitted_urls[credential_id] == credential.tested_url
        return {"ok": False, "error": "origin mismatch"}

    monkeypatch.setattr(recorder_module, "fill_credential_field", fill)
    stream = _Stream()
    recorder = DemonstrationRecorder(
        cache=cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        claim=DemonstrationClaim("demo-origin", "owner-1", False, 0),
        stream=stream,
    )
    recorder.page = SimpleNamespace(url="https://site-b.example/login", is_closed=lambda: False)

    await recorder._fill_credential(
        {"credential_id": credential.credential_id, "selector": "#password", "field": "password"}
    )

    record = await load_demonstration_record(cache, "org-1", "demo-origin")  # type: ignore[arg-type]
    assert record is not None and record.event_count == 0
    assert any(frame.get("type") == "demonstration_notice" and frame.get("level") == "error" for frame in stream.frames)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "replay_started_at", "owner", "stop_reason"),
    [
        (
            "replaying",
            datetime.now(UTC) - recorder_module.DEMONSTRATION_REPLAY_LEASE - timedelta(seconds=1),
            None,
            None,
        ),
        ("stopping", None, None, "disconnected"),
        ("recording", None, "dead-owner", "disconnected"),
    ],
)
async def test_abandoned_replay_or_stop_is_retryable_and_active(
    status: str, replay_started_at: datetime | None, owner: str | None, stop_reason: str | None
) -> None:
    cache = _SharedCache()
    cache.store[demonstration_browser_key("org-1", "pbs-1")] = "demo-1"
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord(
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            status=status,  # type: ignore[arg-type]
            event_count=0,
            owner=owner,
            owner_seen_at=datetime.now(UTC) - timedelta(minutes=1) if owner else None,
            final_url=None,
            replay_started_at=replay_started_at,
            stop_reason="stopped" if status == "stopping" else None,
        )
    )

    record = await validate_demonstration_for_replay(
        cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        demonstration_id="demo-1",
    )
    active = await active_demonstration(
        cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
    )

    assert record.status == "stopped" and record.replay_started_at is None and record.stop_reason == stop_reason
    assert active is not None and active[1].status == "stopped"


@pytest.mark.asyncio
async def test_active_route_returns_live_labels_and_credential_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, CopilotConfig
    from skyvern.forge.sdk.routes import workflow_copilot as route_module

    cache = _SharedCache()
    cache.store[demonstration_browser_key("org-1", "pbs-1")] = "demo-1"
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord(
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            status="stopping",
            event_count=2,
            owner="owner-1",
            owner_seen_at=datetime.now(UTC),
            final_url=None,
        )
    )
    cache.store[demonstration_event_key("org-1", "demo-1", 1)] = json.dumps(
        {
            "seq": 1,
            "kind": "type",
            "selector": "#name",
            "value": "their text",
            "label": 'Type "their text"',
            "packet": {"role_name": {"accessible_name": "Full name"}},
        }
    )
    cache.store[demonstration_event_key("org-1", "demo-1", 2)] = json.dumps(
        {"seq": 2, "kind": "secret", "field": "password"}
    )
    monkeypatch.setattr(route_module, "_demonstration_cache", AsyncMock(return_value=cache))
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "get_copilot_config_for_request",
        AsyncMock(return_value=CopilotConfig(block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER)),
    )

    result = await route_module.workflow_copilot_demonstration_active(
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        organization=SimpleNamespace(organization_id="org-1"),  # type: ignore[arg-type]
    )

    assert result is not None
    assert result.status == "stopping"
    assert result.action_labels == ['Type into "Full name"']
    assert result.can_fill_credentials is True


@pytest.mark.asyncio
async def test_recorder_persists_each_capture_gap_kind() -> None:
    cache = _SharedCache()
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord(
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            status="recording",
            event_count=0,
            owner="owner-1",
            owner_seen_at=datetime.now(UTC),
            final_url=None,
        )
    )
    page = SimpleNamespace(url="https://example.test", is_closed=lambda: False)
    recorder = DemonstrationRecorder(
        cache=cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        claim=DemonstrationClaim("demo-1", "owner-1", False, 0),
        stream=_Stream(),
    )
    _FakeTap.events = []
    _FakeTap.flush_events = []
    recorder.tap = _FakeTap(SimpleNamespace())  # type: ignore[assignment]
    recorder.tap.frame_path = AsyncMock(
        side_effect=[
            [{"name": "outer"}, {"owner_selector": "#inner"}],
            [{"name": "child"}],
        ]
    )
    events = [
        TapEvent(page, "tap", "nested", False, "", {"kind": "click", "ts": 1, "selector": "#inside"}),
        TapEvent(
            page,
            "tap",
            "child",
            False,
            "child",
            {"kind": "navigate", "ts": 2, "url": "https://example.test/frame"},
        ),
        TapEvent(
            page,
            "tap",
            "main",
            True,
            "",
            {"kind": "unsupported_input", "ts": 3, "url": page.url, "selector": "#range", "value": "7"},
        ),
    ]

    for event in events:
        await recorder._record_tap_event(event)

    stored = [json.loads(cache.store[demonstration_event_key("org-1", "demo-1", seq)]) for seq in range(1, 4)]
    assert [event["reason"] for event in stored] == [
        "nested_frame",
        "frame_navigation",
        "unsupported_event:unsupported_input",
    ]


@pytest.mark.asyncio
async def test_replay_retains_unreadable_stored_event_as_a_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _SharedCache()
    monkeypatch.setattr(app, "CACHE", cache)
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord(
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            status="stopped",
            event_count=1,
            owner=None,
            owner_seen_at=None,
            final_url=None,
        )
    )
    cache.store[demonstration_event_key("org-1", "demo-1", 1)] = "not-json"
    ctx = make_copilot_ctx(organization_id="org-1", workflow_permanent_id="wfp-1")
    ctx.demonstration_id = "demo-1"

    history = await replay_demonstration(ctx, SimpleNamespace(call_tool=AsyncMock()))  # type: ignore[arg-type]

    assert any("recorded event unavailable" in str(item.get("content")) for item in history)
    assert ctx.demonstration_items == ["Step not recorded"]


@pytest.mark.asyncio
async def test_deleted_demonstrated_credential_replays_a_failed_result(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _SharedCache()
    monkeypatch.setattr(app, "CACHE", cache)
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(credentials=SimpleNamespace(get_credentials_by_ids=AsyncMock(return_value=[]))),
    )
    event = {
        "seq": 1,
        "kind": "credential_fill",
        "selector": "#password",
        "credential_id": "cred-deleted",
        "field": "password",
        "source_url": "https://example.test/login",
        "frame": {"is_main": True, "name": ""},
        "result": {"ok": True},
    }
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord("wfp-1", "pbs-1", "stopped", 1, None, None, None)
    )
    cache.store[demonstration_event_key("org-1", "demo-1", 1)] = json.dumps(event)
    ctx = make_copilot_ctx(organization_id="org-1", workflow_permanent_id="wfp-1")
    ctx.demonstration_id = "demo-1"

    history = await replay_demonstration(ctx, SimpleNamespace(call_tool=AsyncMock()))  # type: ignore[arg-type]

    result = next(item for item in history if item.get("type") == "function_call_output")
    assert json.loads(result["output"])["ok"] is False
    assert ctx.scout_trajectory == []


@pytest.mark.asyncio
async def test_late_recorder_stop_cannot_take_over_a_replaying_record() -> None:
    cache = _SharedCache()
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord(
            "wfp-1",
            "pbs-1",
            "replaying",
            1,
            "owner-1",
            datetime.now(UTC),
            None,
            stop_reason="stopped",
            replay_started_at=datetime.now(UTC),
        )
    )
    recorder = DemonstrationRecorder(
        cache=cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        claim=DemonstrationClaim("demo-1", "owner-1", False, 0),
        stream=_Stream(),
    )

    await recorder._stop("disconnected")

    record = await load_demonstration_record(cache, "org-1", "demo-1")  # type: ignore[arg-type]
    assert record is not None and record.status == "replaying" and record.stop_reason == "stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["discard_stopped", "discard_abandoned", "superseded"])
async def test_every_discard_path_expires_the_record_events_and_controls(path: str) -> None:
    cache = _SharedCache()
    stale = datetime.now(UTC) - timedelta(seconds=recorder_module.DEMONSTRATION_OWNER_LIVE_SECONDS + 1)
    record = DemonstrationRecord(
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        status="stopping" if path == "discard_abandoned" else "stopped",
        event_count=2,
        owner="dead-owner" if path == "discard_abandoned" else None,
        owner_seen_at=stale if path == "discard_abandoned" else None,
        final_url=None,
        stop_reason="cancelled" if path == "discard_abandoned" else "stopped",
    )
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(record)
    cache.store[demonstration_browser_key("org-1", "pbs-1")] = "demo-1"
    cache.store[demonstration_controls_key("org-1", "demo-1")] = "[]"
    for seq in (1, 2):
        cache.store[demonstration_event_key("org-1", "demo-1", seq)] = json.dumps({"kind": "type", "value": "v"})

    if path == "discard_stopped":
        await append_demonstration_control(
            cache,  # type: ignore[arg-type]
            organization_id="org-1",
            request=WorkflowCopilotDemonstrationControlRequest(demonstration_id="demo-1", action="cancel"),
        )
    elif path == "discard_abandoned":
        active = await active_demonstration(
            cache,  # type: ignore[arg-type]
            organization_id="org-1",
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
        )
        assert active is None
    else:
        await claim_demonstration_recording(
            cache,  # type: ignore[arg-type]
            organization_id="org-1",
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            demonstration_id=None,
        )

    keys = [
        demonstration_record_key("org-1", "demo-1"),
        demonstration_controls_key("org-1", "demo-1"),
        *(demonstration_event_key("org-1", "demo-1", seq) for seq in (1, 2)),
    ]
    assert {cache.expirations.get(key) for key in keys} == {recorder_module.DEMONSTRATION_CANCELLED_TTL}


@pytest.mark.asyncio
async def test_resume_after_the_wall_clock_time_limit_is_refused() -> None:
    cache = _SharedCache()
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord(
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            status="stopped",
            event_count=1,
            owner=None,
            owner_seen_at=None,
            final_url=None,
            stop_reason="disconnected",
            recording_started_at=datetime.now(UTC) - timedelta(seconds=recorder_module.DEMONSTRATION_MAX_SECONDS + 1),
        )
    )

    with pytest.raises(DemonstrationRejection, match="time limit"):
        await claim_demonstration_recording(
            cache,  # type: ignore[arg-type]
            organization_id="org-1",
            workflow_permanent_id="wfp-1",
            browser_session_id="pbs-1",
            demonstration_id="demo-1",
        )


@pytest.mark.asyncio
async def test_sensitive_select_is_reported_and_replays_as_a_withheld_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _SharedCache()
    monkeypatch.setattr(app, "CACHE", cache)
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(
        DemonstrationRecord("wfp-1", "pbs-1", "recording", 0, "owner-1", datetime.now(UTC), None)
    )
    page = SimpleNamespace(url="https://example.test/form")
    stream = _Stream()
    recorder = DemonstrationRecorder(
        cache=cache,  # type: ignore[arg-type]
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        browser_session_id="pbs-1",
        claim=DemonstrationClaim("demo-1", "owner-1", False, 0),
        stream=stream,
    )

    await recorder._record_tap_event(
        TapEvent(
            page,
            "tap-1",
            "main",
            True,
            "",
            {"kind": "secret_select", "selector": "#plan", "field": "other", "url": page.url},
        )
    )

    assert [frame["label"] for frame in stream.frames if frame["type"] == "demonstration_action"] == [
        "Select a sensitive option"
    ]
    record = await load_demonstration_record(cache, "org-1", "demo-1")  # type: ignore[arg-type]
    assert record is not None
    record.status = "stopped"
    record.owner = None
    cache.store[demonstration_record_key("org-1", "demo-1")] = encode_demonstration_record(record)
    monkeypatch.setattr(replay_module, "resolve_raw_secret_safety_handler", AsyncMock(return_value=None))
    ctx = make_copilot_ctx(organization_id="org-1", workflow_permanent_id="wfp-1")
    ctx.demonstration_id = "demo-1"

    history = await replay_demonstration(ctx, SimpleNamespace(call_tool=AsyncMock()))  # type: ignore[arg-type]

    calls = [item for item in history if item.get("type") == "function_call"]
    assert [(item["name"], json.loads(item["arguments"])) for item in calls] == [
        ("select_option", {"selector": "#plan", "value": "<value not recorded>"})
    ]
    assert ctx.demonstration_items == ["Select a sensitive option"]
