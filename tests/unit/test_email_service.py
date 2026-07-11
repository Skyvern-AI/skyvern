from __future__ import annotations

from typing import Any

import httpx
import pytest

from skyvern.services import email
from skyvern.services.email import gmail_client


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


def _mock_response(status_code: int, *, json: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(status_code, json=json or {})


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

    monkeypatch.setattr(gmail_client.asyncio, "sleep", fake_sleep)

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

    monkeypatch.setattr(gmail_client.asyncio, "sleep", fake_sleep)

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

    monkeypatch.setattr(email.outlook.asyncio, "sleep", fake_sleep)

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

    monkeypatch.setattr(email.outlook.asyncio, "sleep", fake_sleep)

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

    monkeypatch.setattr(email.outlook.asyncio, "sleep", fake_sleep)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        messages = await email.outlook.list_folder_messages(access_token="AT", include_body=True, client=client)

    assert attachment_calls == 3
    assert [message.id for message in messages] == ["msg_1"]
    assert messages[0].attachments == []
