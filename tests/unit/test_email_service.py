from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from skyvern.services import email
from skyvern.services.email import gmail_client
from tests.unit.scoped_asyncio import ScopedAsyncio


@pytest.mark.asyncio
async def test_match_email_propagates_llm_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_llm(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr(email.inbox.app, "SECONDARY_LLM_API_HANDLER", fail_llm)

    with pytest.raises(RuntimeError, match="LLM unavailable"):
        await email.match_email(
            criteria="invoice",
            email=email.EmailMessage(id="msg-1", subject="Invoice"),
            organization_id="org-1",
        )


@pytest.mark.asyncio
async def test_match_email_excludes_non_dict_llm_output(monkeypatch: pytest.MonkeyPatch) -> None:
    async def malformed_llm(**_kwargs: Any) -> str:
        return "not an object"

    monkeypatch.setattr(email.inbox.app, "SECONDARY_LLM_API_HANDLER", malformed_llm)

    matches = await email.match_email(
        criteria="invoice",
        email=email.EmailMessage(id="msg-1", subject="Invoice"),
        organization_id="org-1",
    )

    assert matches is False


def _mock_response(status_code: int, *, json: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(status_code, json=json or {})


def _outlook_recipient(address: str) -> dict[str, dict[str, str]]:
    return {"emailAddress": {"address": address}}


def _outlook_otp_message(
    message_id: str,
    *,
    received_datetime: str = "2026-07-30T12:00:00Z",
    recipient: str | None = "user@example.com",
    subject: str = "Verification code",
    preview: str = "",
    body: str = "Your code is 123456",
    **overrides: Any,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "id": message_id,
        "parentFolderId": "inbox_id",
        "isDraft": False,
        "subject": subject,
        "bodyPreview": preview,
        "body": {"contentType": "html", "content": body},
        "receivedDateTime": received_datetime,
        "toRecipients": [_outlook_recipient(recipient)] if recipient else [],
        "ccRecipients": [],
        "bccRecipients": [],
        "internetMessageHeaders": [],
    }
    message.update(overrides)
    return message


def _outlook_otp_list_rows(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in message.items() if key != "body"} for message in messages]


def _outlook_otp_hydration(messages: list[dict[str, Any]], message_id: str) -> dict[str, Any]:
    return next(message for message in messages if message.get("id") == message_id)


def _patch_outlook_otp_graph(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[dict[str, Any]],
    *,
    mailbox_payload: dict[str, Any] | None = None,
) -> list[tuple[str, dict[str, Any] | None, tuple[str, ...] | None]]:
    calls: list[tuple[str, dict[str, Any] | None, tuple[str, ...] | None]] = []

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
        prefer: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        del client, access_token
        calls.append((url, params, prefer))
        if url.endswith("/me"):
            return mailbox_payload or {"mail": "mailbox@example.com"}
        if url.endswith("/mailFolders/junkemail"):
            return {"id": "junk_id"}
        if url.endswith("/mailFolders/deleteditems"):
            return {"id": "deleted_id"}
        if url.endswith("/mailFolders/sentitems"):
            return {"id": "sent_id"}
        if url.endswith("/me/messages"):
            return {"value": _outlook_otp_list_rows(messages)}
        if "/me/messages/" in url:
            message_id = url.rsplit("/", 1)[-1]
            return _outlook_otp_hydration(messages, message_id)
        raise AssertionError(f"Unexpected Graph request: {url}")

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)
    return calls


def test_gmail_build_folder_query_quotes_untrusted_values() -> None:
    query = email.gmail._build_folder_query("x OR label:SENT newer_than:10y", 'a" OR b', 3)

    assert query == 'from:"x OR label:SENT newer_than:10y" subject:"a OR b" newer_than:3d'
    assert email.gmail._quote_gmail_value('a" \\ b') == '"a  b"'
    assert email.gmail._quote_gmail_value(' " \\ ') is None
    assert "from:x OR label:SENT" not in query
    assert 'subject:a" OR b' not in query
    assert 'subject:"a OR b"' in query


def test_gmail_build_folder_query_clamps_newer_than_days() -> None:
    assert email.gmail._build_folder_query(None, None, 0) == "newer_than:1d"


@pytest.mark.asyncio
async def test_gmail_list_folder_messages_uses_escaped_query(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_query = None

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        nonlocal captured_query
        if url.endswith("/users/me/messages"):
            captured_query = (params or {}).get("q")
            return {"messages": [{"id": "msg_1"}]}
        return {
            "id": "msg_1",
            "threadId": "thread_1",
            "labelIds": ["INBOX"],
            "snippet": "snippet",
            "payload": {"headers": [{"name": "Subject", "value": "subject"}]},
        }

    monkeypatch.setattr(email.gmail, "get_json", fake_get_json)

    messages = await email.gmail.list_folder_messages(
        access_token="AT",
        label="INBOX",
        sender="x OR label:SENT newer_than:10y",
        subject='a" OR b',
        newer_than_days=10,
        include_body=False,
    )

    assert [message.id for message in messages] == ["msg_1"]
    assert captured_query == 'from:"x OR label:SENT newer_than:10y" subject:"a OR b" newer_than:10d'
    assert "from:x OR label:SENT" not in captured_query
    assert 'subject:a" OR b' not in captured_query
    assert messages[0].has_attachments is None
    assert messages[0].web_link == "https://mail.google.com/mail/u/0/#all/thread_1"


@pytest.mark.asyncio
async def test_gmail_list_folder_messages_sets_include_spam_trash(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if url.endswith("/users/me/messages"):
            captured[str((params or {}).get("labelIds"))] = str((params or {}).get("includeSpamTrash"))
        return {"messages": []}

    monkeypatch.setattr(email.gmail, "get_json", fake_get_json)

    await email.gmail.list_folder_messages(access_token="AT", label="TRASH", include_body=False)
    await email.gmail.list_folder_messages(access_token="AT", label="INBOX", include_body=False)

    assert captured == {"TRASH": "true", "INBOX": "false"}


def test_email_provider_clamp_max_results_boundaries() -> None:
    for provider in (email.gmail, email.outlook):
        assert provider._clamp_max_results(0) == 1
        assert provider._clamp_max_results(100) == 100
        assert provider._clamp_max_results(101) == 100


@pytest.mark.asyncio
async def test_gmail_get_json_retries_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"message": "rate limited"}}, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(gmail_client, "asyncio", ScopedAsyncio(sleep=fake_sleep))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        payload = await gmail_client.get_json(client, "https://gmail.example/messages", access_token="AT")

    assert payload == {"ok": True}
    assert calls == 2
    assert sleeps == [0.0]


@pytest.mark.asyncio
async def test_gmail_get_json_raises_after_retryable_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def fake_sleep(_seconds: float) -> None:
        return None

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"error": {"message": "server error"}})

    monkeypatch.setattr(gmail_client, "asyncio", ScopedAsyncio(sleep=fake_sleep))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(email.GmailAPIError) as exc_info:
            await gmail_client.get_json(client, "https://gmail.example/messages", access_token="AT")

    assert calls == 3
    assert exc_info.value.status == 500


@pytest.mark.asyncio
async def test_outlook_get_json_retries_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"code": "TooManyRequests"}}, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(email.outlook, "asyncio", ScopedAsyncio(sleep=fake_sleep))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        payload = await email.outlook._get_json(client, "https://graph.example/messages", access_token="AT")

    assert payload == {"ok": True}
    assert calls == 2
    assert sleeps == [0.0]


@pytest.mark.asyncio
async def test_outlook_get_json_raises_after_retryable_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def fake_sleep(_seconds: float) -> None:
        return None

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"error": {"code": "ServiceUnavailable", "message": "server error"}})

    monkeypatch.setattr(email.outlook, "asyncio", ScopedAsyncio(sleep=fake_sleep))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(email.OutlookAPIError) as exc_info:
            await email.outlook._get_json(client, "https://graph.example/messages", access_token="AT")

    assert calls == 3
    assert exc_info.value.status == 500
    assert exc_info.value.code == "ServiceUnavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "graph_code", "expected_code"),
    [
        (401, "InvalidAuthenticationToken", "reconnect_required"),
        (403, "ErrorAccessDenied", "reconnect_required"),
        (403, "ErrorQuotaExceeded", "ErrorQuotaExceeded"),
    ],
)
async def test_outlook_get_json_reconnect_required_mapping(
    status_code: int,
    graph_code: str,
    expected_code: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"code": graph_code, "message": "graph error"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(email.OutlookAPIError) as exc_info:
            await email.outlook._get_json(client, "https://graph.example/messages", access_token="AT")

    assert exc_info.value.code == expected_code


@pytest.mark.asyncio
async def test_outlook_otp_search_request_and_identifier_security(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_outlook_otp_graph(monkeypatch, [])
    cutoff = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    state: dict = {}

    assert (
        await email.outlook.search_recent_otp_messages(
            access_token="AT",
            totp_identifier="user@example.com",
            created_after=cutoff,
            state=state,
        )
        == []
    )
    _, params, prefer = next(call for call in calls if call[0].endswith("/me/messages"))
    assert params is not None
    assert params["$filter"] == "receivedDateTime ge 2026-07-30T11:59:59Z"
    assert params["$orderby"] == "receivedDateTime desc"
    assert {"bodyPreview", "toRecipients", "ccRecipients", "bccRecipients", "internetMessageHeaders"} <= set(
        params["$select"].split(",")
    )
    assert "body" not in params["$select"].split(",")
    assert prefer == ('IdType="ImmutableId"', 'outlook.body-content-type="html"')
    _, mailbox_params, _ = next(call for call in calls if call[0].endswith("/me"))
    assert mailbox_params == {"$select": "mail,userPrincipalName,proxyAddresses"}
    assert (
        await email.outlook.search_recent_otp_messages(
            access_token="AT", totp_identifier="user@example.com", state=state
        )
        == []
    )
    assert len([call for call in calls if call[0].endswith("/me")]) == 1
    assert len([call for call in calls if "/mailFolders/" in call[0]]) == 3
    calls.clear()
    assert await email.outlook.search_recent_otp_messages(access_token="AT", totp_identifier="a@b) OR (x") == []
    assert calls == []


def test_outlook_otp_recipient_security_uses_only_trusted_delivery_headers() -> None:
    assert email.outlook._DELIVERY_HEADER_NAMES == {
        "x-ms-exchange-organization-originalenveloperecipient",
        "x-ms-exchange-organization-originalenveloperecipients",
        "x-ms-exchange-organization-originalto",
    }
    message = _outlook_otp_message(
        "forged",
        recipient=None,
        internetMessageHeaders=[{"name": "Delivered-To", "value": "user@example.com"}],
    )

    assert email.outlook._message_recipient_addresses(message) == set()


def test_outlook_mailbox_identity_set_excludes_external_contact_addresses() -> None:
    assert email.outlook._mailbox_identity_set(
        {
            "mail": "primary@example.com",
            "userPrincipalName": "principal@example.com",
            "otherMails": ["external@example.net"],
            "proxyAddresses": ["SMTP:alias@example.com"],
        }
    ) == {"primary@example.com", "principal@example.com", "alias@example.com"}


@pytest.mark.asyncio
async def test_outlook_otp_search_default_cutoff_accepts_recent_message(monkeypatch: pytest.MonkeyPatch) -> None:
    received_datetime = datetime.now(timezone.utc) - timedelta(minutes=5)
    _patch_outlook_otp_graph(
        monkeypatch,
        [_outlook_otp_message("recent", received_datetime=received_datetime.isoformat().replace("+00:00", "Z"))],
    )

    candidates = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="user@example.com",
    )

    assert [candidate.message_id for candidate in candidates] == ["recent"]


@pytest.mark.asyncio
async def test_outlook_otp_search_retries_excluded_folder_lookup_without_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder_calls: list[str] = []
    message_calls = 0
    state: dict = {}
    messages = [
        _outlook_otp_message("valid"),
        _outlook_otp_message("deleted", parentFolderId="deleted_id"),
    ]

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
        prefer: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        nonlocal message_calls
        del client, access_token, params, prefer
        if url.endswith("/me"):
            return {"mail": "mailbox@example.com"}
        if url.endswith("/mailFolders/junkemail"):
            folder_calls.append("junkemail")
            if folder_calls.count("junkemail") == 1:
                raise ValueError("malformed JSON")
            return {"id": "junk_id"}
        if url.endswith("/mailFolders/deleteditems"):
            folder_calls.append("deleteditems")
            return {"id": "deleted_id"}
        if url.endswith("/mailFolders/sentitems"):
            folder_calls.append("sentitems")
            return {"id": "sent_id"}
        if url.endswith("/me/messages"):
            message_calls += 1
            return {"value": _outlook_otp_list_rows(messages)}
        if "/me/messages/" in url:
            return _outlook_otp_hydration(messages, url.rsplit("/", 1)[-1])
        raise AssertionError(f"Unexpected Graph request: {url}")

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)
    cutoff = datetime(2026, 7, 30, 11, 59, tzinfo=timezone.utc)

    first = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="user@example.com",
        created_after=cutoff,
        state=state,
    )

    assert first == []
    assert message_calls == 0
    assert email.outlook._OTP_EXCLUDED_FOLDER_IDS_STATE_KEY not in state

    second = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="user@example.com",
        created_after=cutoff,
        state=state,
    )

    assert [candidate.message_id for candidate in second] == ["valid"]
    assert message_calls == 1
    assert folder_calls == [
        "junkemail",
        "deleteditems",
        "sentitems",
        "junkemail",
        "deleteditems",
        "sentitems",
    ]
    assert state[email.outlook._OTP_EXCLUDED_FOLDER_IDS_STATE_KEY] == {"junk_id", "deleted_id", "sent_id"}


@pytest.mark.asyncio
async def test_outlook_otp_search_retries_mailbox_identity_lookup_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    me_calls = 0
    message_calls = 0
    state: dict = {}
    strong = _outlook_otp_message("strong")
    fallback = _outlook_otp_message("fallback", recipient=None)
    messages = [strong, fallback]

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
        prefer: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        nonlocal me_calls, message_calls
        del client, access_token, params, prefer
        if url.endswith("/me"):
            me_calls += 1
            if me_calls == 1:
                raise email.OutlookAPIError(status=503, code="ServiceUnavailable", message="temporarily unavailable")
            return {"mail": "user@example.com"}
        if url.endswith("/mailFolders/junkemail"):
            return {"id": "junk_id"}
        if url.endswith("/mailFolders/deleteditems"):
            return {"id": "deleted_id"}
        if url.endswith("/mailFolders/sentitems"):
            return {"id": "sent_id"}
        if url.endswith("/me/messages"):
            message_calls += 1
            if message_calls == 1:
                return {"value": _outlook_otp_list_rows(messages)}
            return {"value": _outlook_otp_list_rows([fallback])}
        if "/me/messages/" in url:
            return _outlook_otp_hydration(messages, url.rsplit("/", 1)[-1])
        raise AssertionError(f"Unexpected Graph request: {url}")

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)
    cutoff = datetime(2026, 7, 30, 11, 59, tzinfo=timezone.utc)

    first = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="user@example.com",
        created_after=cutoff,
        state=state,
    )

    assert [candidate.message_id for candidate in first] == ["strong"]
    assert email.outlook._OTP_MAILBOX_IDENTITIES_STATE_KEY not in state

    second = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="user@example.com",
        created_after=cutoff,
        state=state,
    )

    assert [candidate.message_id for candidate in second] == ["fallback"]
    assert me_calls == 2
    assert state[email.outlook._OTP_MAILBOX_IDENTITIES_STATE_KEY] == {"user@example.com"}


@pytest.mark.asyncio
async def test_outlook_otp_search_malformed_mailbox_metadata_uses_strict_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = [
        _outlook_otp_message("strong"),
        _outlook_otp_message("hidden", recipient=None),
    ]

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
        prefer: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        del client, access_token, params, prefer
        if url.endswith("/me"):
            raise ValueError("malformed JSON")
        if "/mailFolders/" in url:
            return {"id": url.rsplit("/", 1)[-1]}
        if url.endswith("/me/messages"):
            return {"value": _outlook_otp_list_rows(messages)}
        if "/me/messages/" in url:
            return _outlook_otp_hydration(messages, url.rsplit("/", 1)[-1])
        raise AssertionError(f"Unexpected Graph request: {url}")

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)

    candidates = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="user@example.com",
        created_after=datetime(2026, 7, 30, 11, 59, tzinfo=timezone.utc),
    )

    assert [candidate.message_id for candidate in candidates] == ["strong"]


@pytest.mark.asyncio
async def test_outlook_otp_search_alias_fallback_requires_no_observable_recipients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    different_alias = _outlook_otp_message(
        "different_alias",
        recipient="primary@example.com",
        body="",
    )
    different_alias.pop("body")
    hidden_recipient = _outlook_otp_message("hidden_recipient", recipient=None)
    hydration_calls: list[str] = []

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
        prefer: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        del client, access_token, params, prefer
        if url.endswith("/me"):
            return {
                "mail": "primary@example.com",
                "proxyAddresses": ["smtp:automation@example.com"],
            }
        if "/mailFolders/" in url:
            return {"id": url.rsplit("/", 1)[-1]}
        if url.endswith("/me/messages"):
            return {"value": _outlook_otp_list_rows([different_alias, hidden_recipient])}
        if "/me/messages/" in url:
            hydration_calls.append(url.rsplit("/", 1)[-1])
            return {"body": {"contentType": "html", "content": "Verification code 123456"}}
        raise AssertionError(f"Unexpected Graph request: {url}")

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)

    candidates = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="automation@example.com",
        created_after=datetime(2026, 7, 30, 11, 59, tzinfo=timezone.utc),
    )

    assert [candidate.message_id for candidate in candidates] == ["hidden_recipient"]
    assert hydration_calls == ["hidden_recipient"]


@pytest.mark.asyncio
async def test_outlook_otp_search_excludes_seen_messages_before_candidate_budget_and_hydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_outlook_otp_graph(
        monkeypatch,
        [
            _outlook_otp_message("seen", received_datetime="2026-07-30T12:02:00Z"),
            _outlook_otp_message("unseen", received_datetime="2026-07-30T12:01:00Z"),
        ],
    )

    candidates = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="user@example.com",
        created_after=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
        max_results=1,
        excluded_message_ids={"seen"},
    )

    assert [candidate.message_id for candidate in candidates] == ["unseen"]
    assert [call[0].rsplit("/", 1)[-1] for call in calls if "/me/messages/" in call[0]] == ["unseen"]


@pytest.mark.asyncio
async def test_outlook_otp_search_returns_strong_then_hidden_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = [
        _outlook_otp_message(
            "fallback",
            received_datetime="2026-07-30T12:02:00Z",
            recipient=None,
        ),
        _outlook_otp_message(
            "strong",
            received_datetime="2026-07-30T12:01:00Z",
        ),
    ]
    _patch_outlook_otp_graph(
        monkeypatch,
        messages,
        mailbox_payload={"mail": "user@example.com"},
    )

    candidates = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="user@example.com",
        created_after=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
    )

    assert [candidate.message_id for candidate in candidates] == ["strong", "fallback"]


@pytest.mark.asyncio
async def test_outlook_otp_search_distinguishes_empty_body_from_missing_hydration_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_only = _outlook_otp_message("subject_only", subject="Verification code 654321", body="")
    subject_only.pop("body")
    incomplete = _outlook_otp_message("incomplete", subject="Verification code", body="")
    incomplete.pop("body")
    hydration_calls: list[str] = []
    incomplete_hydrations = 0

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
        prefer: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        del client, access_token, params, prefer
        if url.endswith("/me"):
            return {"mail": "mailbox@example.com"}
        if "/mailFolders/" in url:
            return {"id": url.rsplit("/", 1)[-1]}
        if url.endswith("/me/messages"):
            return {"value": [subject_only, incomplete]}
        if "/me/messages/" in url:
            nonlocal incomplete_hydrations
            message_id = url.rsplit("/", 1)[-1]
            hydration_calls.append(message_id)
            if message_id == "subject_only":
                return {"body": {"contentType": "html", "content": ""}}
            incomplete_hydrations += 1
            if incomplete_hydrations == 1:
                return {}
            return {"body": {"contentType": "html", "content": "Your verification code is 123456"}}
        raise AssertionError(f"Unexpected Graph request: {url}")

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)

    first = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="user@example.com",
        created_after=datetime(2026, 7, 30, 11, 59, tzinfo=timezone.utc),
    )

    second = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="user@example.com",
        created_after=datetime(2026, 7, 30, 11, 59, tzinfo=timezone.utc),
    )

    assert [candidate.message_id for candidate in first] == ["subject_only"]
    assert "Verification code 654321" in first[0].content
    assert [candidate.message_id for candidate in second] == ["subject_only", "incomplete"]
    assert hydration_calls == ["subject_only", "incomplete", "subject_only", "incomplete"]


@pytest.mark.asyncio
async def test_outlook_otp_search_continues_after_non_auth_hydration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodyless = _outlook_otp_message("bodyless", subject="Verification code", preview="", body="")
    bodyless.pop("body")
    hydration_calls = 0

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
        prefer: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        nonlocal hydration_calls
        del client, access_token, params, prefer
        if url.endswith("/me"):
            return {"mail": "mailbox@example.com"}
        if "/mailFolders/" in url:
            return {"id": url.rsplit("/", 1)[-1]}
        if url.endswith("/me/messages"):
            return {"value": [bodyless]}
        if url.endswith("/me/messages/bodyless"):
            hydration_calls += 1
            if hydration_calls == 1:
                raise email.OutlookAPIError(status=404, code="ErrorItemNotFound", message="message not found")
            return {"body": {"contentType": "html", "content": "Your verification code is 123456"}}
        raise AssertionError(f"Unexpected Graph request: {url}")

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)

    first = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="user@example.com",
        created_after=datetime(2026, 7, 30, 11, 59, tzinfo=timezone.utc),
    )

    assert first == []

    second = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="user@example.com",
        created_after=datetime(2026, 7, 30, 11, 59, tzinfo=timezone.utc),
    )

    assert [candidate.message_id for candidate in second] == ["bodyless"]
    assert hydration_calls == 2


@pytest.mark.asyncio
async def test_outlook_otp_search_continues_after_malformed_hydration_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodyless = _outlook_otp_message("bodyless", subject="Verification code", preview="", body="")
    bodyless.pop("body")
    valid = _outlook_otp_message("valid")

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
        prefer: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        del client, access_token, params, prefer
        if url.endswith("/me"):
            return {"mail": "mailbox@example.com"}
        if "/mailFolders/" in url:
            return {"id": url.rsplit("/", 1)[-1]}
        if url.endswith("/me/messages"):
            return {"value": _outlook_otp_list_rows([bodyless, valid])}
        if url.endswith("/me/messages/bodyless"):
            raise ValueError("malformed JSON")
        if url.endswith("/me/messages/valid"):
            return valid
        raise AssertionError(f"Unexpected Graph request: {url}")

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)

    candidates = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="user@example.com",
        created_after=datetime(2026, 7, 30, 11, 59, tzinfo=timezone.utc),
    )

    assert [candidate.message_id for candidate in candidates] == ["valid"]


@pytest.mark.asyncio
async def test_outlook_otp_search_propagates_reconnect_required_hydration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodyless = _outlook_otp_message("bodyless", subject="Sign in", preview="", body="")
    bodyless.pop("body")

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
        prefer: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        del client, access_token, params, prefer
        if url.endswith("/me"):
            return {"mail": "mailbox@example.com"}
        if "/mailFolders/" in url:
            return {"id": url.rsplit("/", 1)[-1]}
        if url.endswith("/me/messages"):
            return {"value": [bodyless]}
        if url.endswith("/me/messages/bodyless"):
            raise email.OutlookAPIError(status=401, code="reconnect_required", message="token expired")
        raise AssertionError(f"Unexpected Graph request: {url}")

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)

    with pytest.raises(email.OutlookAPIError) as exc_info:
        await email.outlook.search_recent_otp_messages(
            access_token="AT",
            totp_identifier="user@example.com",
            created_after=datetime(2026, 7, 30, 11, 59, tzinfo=timezone.utc),
        )

    assert exc_info.value.code == "reconnect_required"


@pytest.mark.asyncio
async def test_outlook_otp_search_rejects_old_draft_and_junk_before_hydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = [
        _outlook_otp_message("old", received_datetime="2026-07-30T11:59:59Z", body=""),
        _outlook_otp_message("draft", isDraft=True, body=""),
        _outlook_otp_message("junk", parentFolderId="junk_id", body=""),
    ]
    for message in messages:
        message.pop("body")
    hydration_calls = 0

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
        prefer: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        nonlocal hydration_calls
        del client, access_token, params, prefer
        if url.endswith("/me"):
            return {"mail": "mailbox@example.com"}
        if url.endswith("/mailFolders/junkemail"):
            return {"id": "junk_id"}
        if url.endswith("/mailFolders/deleteditems"):
            return {"id": "deleted_id"}
        if url.endswith("/mailFolders/sentitems"):
            return {"id": "sent_id"}
        if url.endswith("/me/messages"):
            return {"value": messages}
        if "/me/messages/" in url:
            hydration_calls += 1
            return {"body": {"contentType": "html", "content": "Verification code 123456"}}
        raise AssertionError(f"Unexpected Graph request: {url}")

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)

    candidates = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="user@example.com",
        created_after=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
    )

    assert candidates == []
    assert hydration_calls == 0


@pytest.mark.asyncio
async def test_outlook_otp_search_filters_and_orders_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    href = "https://login.example/verify?token=a%2Bb&next=%2Fhome"
    messages = [
        _outlook_otp_message(
            "to",
            received_datetime="2026-07-30T12:04:00Z",
            recipient=" USER@EXAMPLE.COM ",
            preview="Use code 123456",
            body=f'<a href="{href}">verify</a>',
        ),
        _outlook_otp_message(
            "cc",
            received_datetime="2026-07-30T12:03:00Z",
            recipient=None,
            subject="Sign in",
            body="one time credential",
            ccRecipients=[_outlook_recipient("user@example.com")],
        ),
        _outlook_otp_message(
            "bcc",
            received_datetime="2026-07-30T12:02:00Z",
            recipient=None,
            subject="Sign in",
            body="one-time credential",
            bccRecipients=[_outlook_recipient("user@example.com")],
        ),
        _outlook_otp_message(
            "header",
            received_datetime="2026-07-30T12:01:00Z",
            recipient=None,
            internetMessageHeaders=[{"name": "Delivered-To", "value": "USER@example.com"}],
        ),
        _outlook_otp_message("old", received_datetime="2026-07-30T11:59:59Z"),
        _outlook_otp_message("mismatch", recipient="other@example.com"),
        _outlook_otp_message("no_keyword", subject="Weekly digest", body="Here is what happened this week."),
        _outlook_otp_message("junk", parentFolderId="junk_id"),
        _outlook_otp_message("deleted", parentFolderId="deleted_id"),
        _outlook_otp_message("sent", parentFolderId="sent_id"),
        _outlook_otp_message("draft", isDraft=True),
        _outlook_otp_message("missing_date", receivedDateTime=None),
        _outlook_otp_message("invalid_date", received_datetime="invalid"),
        _outlook_otp_message("empty", subject="", preview="", body=""),
    ]
    _patch_outlook_otp_graph(monkeypatch, messages)

    candidates = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="user@example.com",
        created_after=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
        max_results=20,
    )

    assert [candidate.message_id for candidate in candidates] == ["to", "cc", "bcc"]
    assert "Subject: Verification code" in candidates[0].content
    assert "Snippet: Use code 123456" in candidates[0].content
    assert f'<a href="{href}">verify</a>' in candidates[0].content
    _patch_outlook_otp_graph(
        monkeypatch,
        [_outlook_otp_message("fallback", recipient=None)],
        mailbox_payload={"mail": "user@example.com"},
    )
    cutoff = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    fallback = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="user@example.com",
        created_after=cutoff,
    )
    rejected = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="other@example.com",
        created_after=cutoff,
    )
    assert [candidate.message_id for candidate in fallback] == ["fallback"]
    assert rejected == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "expected", "requests"), [("valid", ["page_2"], 2), ("evil", [], 1), ("cap", [], 1)])
async def test_outlook_otp_search_pagination_guards(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected: list[str],
    requests: int,
) -> None:
    next_link = f"{email.outlook.GRAPH_API_BASE}/me/messages?$skip=1"
    message_calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
        prefer: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        del client, access_token, prefer
        if url.endswith("/me"):
            return {"mail": "mailbox@example.com"}
        if "/mailFolders/" in url:
            return {"id": url.rsplit("/", 1)[-1]}
        if url.endswith("/me/messages/page_2"):
            return _outlook_otp_message("page_2")
        message_calls.append((url, params))
        if len(message_calls) == 2:
            return {"value": _outlook_otp_list_rows([_outlook_otp_message("page_2")])}
        items = [_outlook_otp_message(f"raw_{i}", recipient="other@example.com") for i in range(101)]
        return {
            "value": _outlook_otp_list_rows(items) if case == "cap" else [],
            "@odata.nextLink": "https://evil.example/messages" if case == "evil" else next_link,
        }

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)
    candidates = await email.outlook.search_recent_otp_messages(
        access_token="AT",
        totp_identifier="user@example.com",
        created_after=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
    )
    assert [candidate.message_id for candidate in candidates] == expected
    assert len(message_calls) == requests
    if case == "valid":
        assert message_calls[1] == (next_link, None)


@pytest.mark.asyncio
async def test_outlook_list_folder_messages_omits_body_when_include_body_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_select = None
    attachment_fetch_count = 0

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        nonlocal captured_select, attachment_fetch_count
        if url.endswith("/attachments"):
            attachment_fetch_count += 1
            return {"value": [{"id": "att_1", "name": "receipt.pdf"}]}
        captured_select = (params or {}).get("$select")
        return {
            "value": [
                {
                    "id": "msg_1",
                    "conversationId": "thread_1",
                    "subject": "Receipt",
                    "from": {"emailAddress": {"address": "sender@example.com", "name": "Sender"}},
                    "toRecipients": [],
                    "ccRecipients": [],
                    "receivedDateTime": "2026-07-09T12:00:00Z",
                    "bodyPreview": "preview text",
                    "body": {"contentType": "html", "content": "<p>body</p>"},
                    "hasAttachments": True,
                    "isRead": False,
                    "webLink": "https://example.com/message",
                }
            ]
        }

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)

    messages = await email.outlook.list_folder_messages(
        access_token="AT",
        folder="inbox",
        include_body=False,
    )

    assert captured_select is not None
    assert "bodyPreview" in captured_select.split(",")
    assert "body" not in captured_select.split(",")
    assert attachment_fetch_count == 0
    assert len(messages) == 1
    assert messages[0].snippet == "preview text"
    assert messages[0].body_text == ""
    assert messages[0].body_html is None
    assert messages[0].attachments == []


def test_outlook_html_message_uses_full_body_for_prompt_matching() -> None:
    message = email.outlook._message_from_graph(
        {
            "id": "msg_1",
            "bodyPreview": "preview text",
            "body": {"contentType": "html", "content": "<p>full body with matching criteria</p>"},
        },
        [],
        include_body=True,
    )

    assert message is not None
    assert message.snippet == "preview text"
    assert message.body_text == ""
    assert message.body_html == "<p>full body with matching criteria</p>"


@pytest.mark.asyncio
async def test_outlook_list_folder_messages_orders_filter_by_received_datetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_params: dict[str, Any] | None = None

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        nonlocal captured_params
        captured_params = params
        return {"value": []}

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)

    messages = await email.outlook.list_folder_messages(
        access_token="AT",
        folder="inbox",
        sender="o'hara@example.com",
        include_body=False,
    )

    assert messages == []
    assert captured_params is not None
    order_by = captured_params.get("$orderby")
    filter_expression = captured_params.get("$filter")
    assert order_by == "receivedDateTime desc"
    assert isinstance(filter_expression, str)
    assert filter_expression.startswith("receivedDateTime ge 1900-01-01T00:00:00Z")
    assert "from/emailAddress/address eq 'o''hara@example.com'" in filter_expression
    assert not filter_expression.startswith("from/emailAddress/address")


@pytest.mark.asyncio
async def test_outlook_list_folder_messages_paginates_for_subject_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    next_link = f"{email.outlook.GRAPH_API_BASE}/me/mailFolders/inbox/messages?$skip=1"
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        calls.append((url, params))
        if url == next_link:
            return {
                "value": [
                    {
                        "id": "msg_match",
                        "conversationId": "thread_2",
                        "subject": "Invoice from June",
                        "from": {"emailAddress": {"address": "billing@example.com"}},
                        "toRecipients": [],
                        "ccRecipients": [],
                        "receivedDateTime": "2026-07-08T12:00:00Z",
                        "bodyPreview": "invoice",
                        "hasAttachments": False,
                        "isRead": True,
                    }
                ]
            }
        return {
            "value": [
                {
                    "id": "msg_skip",
                    "conversationId": "thread_1",
                    "subject": "Welcome",
                    "from": {"emailAddress": {"address": "billing@example.com"}},
                    "toRecipients": [],
                    "ccRecipients": [],
                    "receivedDateTime": "2026-07-09T12:00:00Z",
                    "bodyPreview": "welcome",
                    "hasAttachments": False,
                    "isRead": True,
                }
            ],
            "@odata.nextLink": next_link,
        }

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)

    messages = await email.outlook.list_folder_messages(
        access_token="AT",
        folder="inbox",
        subject="invoice",
        max_results=1,
        include_body=False,
    )

    assert [message.id for message in messages] == ["msg_match"]
    assert len(calls) == 2
    assert calls[0][1] is not None
    assert calls[1] == (next_link, None)


@pytest.mark.asyncio
async def test_outlook_list_folder_messages_rejects_untrusted_next_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del client, access_token, params
        calls.append(url)
        return {
            "value": [
                {
                    "id": "msg_1",
                    "subject": "Invoice",
                    "from": {"emailAddress": {"address": "billing@example.com"}},
                    "toRecipients": [],
                    "ccRecipients": [],
                    "hasAttachments": False,
                }
            ],
            "@odata.nextLink": "https://evil.example.com/messages?$skip=1",
        }

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)

    messages = await email.outlook.list_folder_messages(
        access_token="AT",
        folder="inbox",
        subject="invoice",
        max_results=2,
        include_body=False,
    )

    assert [message.id for message in messages] == ["msg_1"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_outlook_subject_pagination_stops_at_page_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del client, access_token, params
        calls.append(url)
        index = len(calls)
        subject = "Invoice match" if index == 1 else "Welcome"
        return {
            "value": [
                {
                    "id": f"msg_{index}",
                    "conversationId": f"thread_{index}",
                    "subject": subject,
                    "from": {"emailAddress": {"address": "billing@example.com"}},
                    "toRecipients": [],
                    "ccRecipients": [],
                    "receivedDateTime": "2026-07-09T12:00:00Z",
                    "bodyPreview": subject,
                    "hasAttachments": False,
                    "isRead": True,
                }
            ],
            "@odata.nextLink": f"{email.outlook.GRAPH_API_BASE}/me/mailFolders/inbox/messages?$skip={index}",
        }

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)

    messages = await email.outlook.list_folder_messages(
        access_token="AT",
        folder="inbox",
        subject="invoice",
        max_results=2,
        include_body=False,
    )

    assert [message.id for message in messages] == ["msg_1"]
    assert len(calls) == email.outlook._MAX_SUBJECT_FILTER_PAGES


@pytest.mark.asyncio
async def test_outlook_resolve_nested_folder_by_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del client, access_token
        calls.append((url, params))
        filter_expression = (params or {}).get("$filter")
        if url.endswith("/me/mailFolders") and filter_expression == "displayName eq 'A'":
            return {"value": [{"id": "folder_a", "displayName": "A"}]}
        if url.endswith("/me/mailFolders/folder_a/childFolders") and filter_expression == "displayName eq 'B'":
            return {"value": [{"id": "folder_b", "displayName": "B"}]}
        return {"value": []}

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)

    folder_id = await email.outlook._resolve_folder_id(httpx.AsyncClient(), "AT", "A/B")

    assert folder_id == "folder_b"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_outlook_resolve_nested_folder_by_bare_name_bfs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del client, access_token
        calls.append((url, params))
        if (params or {}).get("$filter") == "displayName eq 'B'":
            return {"value": []}
        if url.endswith("/me/mailFolders"):
            return {"value": [{"id": "folder_a", "displayName": "A"}]}
        if url.endswith("/me/mailFolders/folder_a/childFolders"):
            return {"value": [{"id": "folder_b", "displayName": "B"}]}
        return {"value": []}

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)

    folder_id = await email.outlook._resolve_folder_id(httpx.AsyncClient(), "AT", "B")

    assert folder_id == "folder_b"
    assert [call[0] for call in calls] == [
        f"{email.outlook.GRAPH_API_BASE}/me/mailFolders",
        f"{email.outlook.GRAPH_API_BASE}/me/mailFolders",
        f"{email.outlook.GRAPH_API_BASE}/me/mailFolders/folder_a/childFolders",
    ]


@pytest.mark.asyncio
async def test_outlook_resolve_missing_nested_folder_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del client, url, access_token, params
        return {"value": []}

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)

    with pytest.raises(email.OutlookAPIError) as exc_info:
        await email.outlook._resolve_folder_id(httpx.AsyncClient(), "AT", "A/B")

    assert exc_info.value.status == 404
    assert exc_info.value.code == "folder_not_found"


@pytest.mark.asyncio
async def test_outlook_attachments_follow_next_link(monkeypatch: pytest.MonkeyPatch) -> None:
    next_link = f"{email.outlook.GRAPH_API_BASE}/me/messages/msg_1/attachments?$skip=1"
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del client, access_token
        calls.append((url, params))
        if url == next_link:
            return {"value": [{"id": "att_2", "name": "b.pdf", "contentType": "application/pdf", "size": 2}]}
        return {
            "value": [{"id": "att_1", "name": "a.pdf", "contentType": "application/pdf", "size": 1}],
            "@odata.nextLink": next_link,
        }

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)

    attachments = await email.outlook._attachments(httpx.AsyncClient(), access_token="AT", message_id="msg_1")

    assert [attachment.name for attachment in attachments] == ["a.pdf", "b.pdf"]
    assert calls[0][1] == {"$select": "id,name,contentType,size"}
    assert calls[1] == (next_link, None)


@pytest.mark.asyncio
async def test_outlook_attachments_reject_untrusted_next_link(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_get_json(
        client: httpx.AsyncClient,
        url: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del client, access_token, params
        calls.append(url)
        return {
            "value": [{"id": "att_1", "name": "a.pdf", "contentType": "application/pdf", "size": 1}],
            "@odata.nextLink": "https://evil.example.com/attachments?$skip=1",
        }

    monkeypatch.setattr(email.outlook, "_get_json", fake_get_json)

    attachments = await email.outlook._attachments(httpx.AsyncClient(), access_token="AT", message_id="msg_1")

    assert [attachment.name for attachment in attachments] == ["a.pdf"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_outlook_list_folder_messages_raises_when_attachment_fetch_requires_reconnect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/attachments"):
            return httpx.Response(
                401,
                json={"error": {"code": "InvalidAuthenticationToken", "message": "token expired"}},
            )
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "msg_1",
                        "subject": "Invoice",
                        "from": {"emailAddress": {"address": "billing@example.com"}},
                        "toRecipients": [],
                        "ccRecipients": [],
                        "hasAttachments": True,
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(email.OutlookAPIError) as exc_info:
            await email.outlook.list_folder_messages(access_token="AT", include_body=True, client=client)

    assert exc_info.value.code == "reconnect_required"


@pytest.mark.asyncio
async def test_outlook_list_folder_messages_keeps_message_when_attachment_fetch_fails_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_calls = 0

    async def fake_sleep(_seconds: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attachment_calls
        if request.url.path.endswith("/attachments"):
            attachment_calls += 1
            return httpx.Response(
                500,
                json={"error": {"code": "ServiceUnavailable", "message": "server error"}},
            )
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "msg_1",
                        "subject": "Invoice",
                        "from": {"emailAddress": {"address": "billing@example.com"}},
                        "toRecipients": [],
                        "ccRecipients": [],
                        "hasAttachments": True,
                    }
                ]
            },
        )

    monkeypatch.setattr(email.outlook, "asyncio", ScopedAsyncio(sleep=fake_sleep))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        messages = await email.outlook.list_folder_messages(access_token="AT", include_body=True, client=client)

    assert attachment_calls == 3
    assert [message.id for message in messages] == ["msg_1"]
    assert messages[0].attachments == []
