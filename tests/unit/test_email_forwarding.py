import asyncio
import inspect
import json
import threading
import time
from collections.abc import AsyncIterator, Generator
from email import _header_value_parser
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request

from skyvern.forge import request_logging
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.log_redaction import REDACTED
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import (
    CredentialModel,
    OrganizationEmailConfigModel,
    OrganizationForwardingAddressModel,
    OrganizationInboundEmailModel,
    TOTPCodeModel,
)
from skyvern.forge.sdk.db.repositories import email_forwarding as email_forwarding_repository
from skyvern.forge.sdk.routes import email_forwarding as email_routes
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.email_forwarding import (
    EmailConfigCreate,
    ForwardingAddressCreate,
    ForwardingAddressRotate,
    ForwardingAddressStatus,
    ForwardingMatchMode,
    InboundEmailCategory,
)
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.forge.sdk.services import org_auth_service
from skyvern.services import email_forwarding, otp_service

FIXTURES = Path(__file__).parent / "fixtures" / "email_forwarding"


@pytest.fixture
def agent_db(sqlite_engine: AsyncEngine) -> Generator[AgentDB, None, None]:
    yield AgentDB(database_string="sqlite+aiosqlite:///:memory:", debug_enabled=True, db_engine=sqlite_engine)


def _request(body: bytes, content_type: str = "message/rfc822", content_length: int | None = None) -> Request:
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    length = len(body) if content_length is None else content_length
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/email/inbound/emc_test",
            "headers": [
                (b"content-type", content_type.encode()),
                (b"content-length", str(length).encode()),
            ],
            "query_string": b"",
        },
        receive,
    )


def _asgi_app(agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch, *, with_request_logging: bool = False) -> FastAPI:
    fastapi_app = FastAPI()
    if with_request_logging:
        fastapi_app.middleware("http")(request_logging.log_raw_request_middleware)
    fastapi_app.include_router(base_router, prefix="/v1")
    fastapi_app.dependency_overrides[org_auth_service.get_current_org_for_credential_routes] = lambda: SimpleNamespace(
        organization_id="o_test",
        organization_name="Test",
        slug="test",
    )
    monkeypatch.setattr(email_routes.app, "DATABASE", agent_db)
    monkeypatch.setattr(email_forwarding.app, "DATABASE", agent_db)
    return fastapi_app


def _provider_message(
    recipient: str,
    external_message_id: str,
    *,
    sender: str = "security@bank.example",
    text: str = "Verification code 123456",
) -> email_forwarding.NormalizedInboundEmail:
    return email_forwarding.NormalizedInboundEmail(
        recipients=[recipient],
        sender=sender,
        subject="Sign-in code",
        text=text,
        html="",
        external_message_id=external_message_id,
    )


async def _capped_inbound_count(agent_db: AgentDB, email_config_id: str) -> int:
    """Count the rows the daily-cap gate counts: capped categories for one config."""
    async with agent_db.Session() as session:
        count = await session.scalar(
            select(func.count(OrganizationInboundEmailModel.inbound_email_id))
            .join(
                OrganizationForwardingAddressModel,
                OrganizationForwardingAddressModel.forwarding_address_id
                == OrganizationInboundEmailModel.forwarding_address_id,
            )
            .where(
                OrganizationForwardingAddressModel.email_config_id == email_config_id,
                OrganizationInboundEmailModel.category.in_(
                    (InboundEmailCategory.OTP, InboundEmailCategory.PROVIDER_CONFIRMATION)
                ),
            )
        )
        return int(count or 0)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("UNIQUE constraint failed: organization_forwarding_addresses.email_address", True),
        ("NOT NULL constraint failed: organization_forwarding_addresses.email_address", False),
    ],
)
def test_sqlite_forwarding_address_unique_violation_detection(message: str, expected: bool) -> None:
    error = IntegrityError("INSERT", {}, Exception(message))

    assert email_forwarding_repository._is_forwarding_address_unique_violation(error) is expected


@pytest.mark.parametrize("fixture_name", ["gmail_auto_forward.eml", "manual_forward_html.eml", "outlook_forward.eml"])
def test_fixture_mime_extraction_prefers_plain_and_drops_attachments(fixture_name: str) -> None:
    parsed = email_forwarding.parse_raw_email((FIXTURES / fixture_name).read_bytes())
    body, links = email_forwarding.extract_email_body(parsed)

    assert parsed.sender
    assert body
    assert "999999" not in body
    if fixture_name == "manual_forward_html.eml":
        assert body == "Your login code is 731204. Verify account"
        assert links == [{"url": "https://app.example/verify?t=abc", "text": "Verify account"}]


def test_link_extraction_is_bounded_and_deduplicated() -> None:
    repeated = '<a href="https://example.com/dup">Dup</a>' * 40
    unique = "".join(f'<a href="https://example.com/u{index}">U{index}</a>' for index in range(80))
    message = email_forwarding.NormalizedInboundEmail(
        recipients=["forward@inbound.example"],
        sender="security@bank.example",
        subject="Codes",
        text="",
        html=f"<html><body>{repeated}{unique}</body></html>",
        external_message_id="links-1",
    )
    _, links = email_forwarding.extract_email_body(message)
    assert len(links) == email_forwarding._MAX_LINKS
    assert links[0] == {"url": "https://example.com/dup", "text": "Dup"}
    assert len({link["url"] for link in links}) == len(links)


def test_link_fields_and_serialized_total_are_bounded() -> None:
    overlong_url = "https://example.com/" + ("x" * email_forwarding._MAX_LINK_URL_BYTES)
    long_anchor = "é" * email_forwarding._MAX_LINK_TEXT_BYTES
    anchors = f'<a href="{overlong_url}">Skip</a><a href="https://example.com/text">{long_anchor}</a>' + "".join(
        f'<a href="https://example.com/{index}">{"x" * email_forwarding._MAX_LINK_TEXT_BYTES}</a>'
        for index in range(email_forwarding._MAX_LINKS)
    )
    message = email_forwarding.NormalizedInboundEmail(
        recipients=["forward@inbound.example"],
        sender="security@bank.example",
        subject="Links",
        text="",
        html=anchors,
        external_message_id="links-sized",
    )

    _, links = email_forwarding.extract_email_body(message)
    bounded = email_forwarding._bound_links_json(links)
    serialized = json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    assert all(link["url"] != overlong_url for link in links)
    assert len(links[0]["text"].encode("utf-8")) <= email_forwarding._MAX_LINK_TEXT_BYTES
    assert all(len(link["url"].encode("utf-8")) <= email_forwarding._MAX_LINK_URL_BYTES for link in links)
    assert len(serialized) <= email_forwarding._MAX_LINKS_JSON_BYTES


def test_html_parser_cpu_budget_is_checked_between_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    fed_lengths: list[int] = []
    original_feed = email_forwarding._HTMLTextExtractor.feed
    cpu_times = iter([0.0, email_forwarding._MAX_MIME_PARSE_CPU_SECONDS + 0.1])

    def record_feed(parser: email_forwarding._HTMLTextExtractor, data: str) -> None:
        fed_lengths.append(len(data))
        original_feed(parser, data)

    monkeypatch.setattr(email_forwarding._HTMLTextExtractor, "feed", record_feed)
    monkeypatch.setattr(email_forwarding.time, "thread_time", lambda: next(cpu_times))
    message = email_forwarding.NormalizedInboundEmail(
        recipients=["forward@inbound.example"],
        sender="security@bank.example",
        subject="HTML",
        text="",
        html="x" * (email_forwarding._HTML_PARSE_CHUNK_SIZE * 2),
        external_message_id="html-capped",
    )

    with pytest.raises(email_forwarding.InboundEmailLimitExceeded, match="HTML CPU"):
        email_forwarding.extract_email_body(message)

    assert fed_lengths == [email_forwarding._HTML_PARSE_CHUNK_SIZE]


@pytest.mark.parametrize(
    ("subject", "text", "message_id"),
    [
        ("Code", "Verification code 112233", "postmark-1"),
        ("\ud800", "Code 123456 \ud800", "\ud800"),
    ],
)
def test_provider_json_shapes_and_schema_validation(subject: str, text: str, message_id: str) -> None:
    postmark = email_forwarding.normalize_provider_payload(
        {
            "From": "Security <security@example.com>",
            "OriginalRecipient": "forward@inbound.example",
            "Subject": subject,
            "TextBody": text,
            "MessageID": message_id,
            "Headers": [{"Name": f"X-{message_id}", "Value": message_id}],
        }
    )
    sendgrid = email_forwarding.normalize_provider_payload(
        {
            "from": "security@example.com",
            "to": "employee@customer.example",
            "envelope": '{"to":["forward@inbound.example"]}',
            "text": "OTP 445566",
            "message_id": "sendgrid-1",
        }
    )
    assert postmark.recipients == ["forward@inbound.example"]
    assert postmark.subject == subject.replace("\ud800", "\ufffd")
    assert postmark.text == text.replace("\ud800", "\ufffd")
    assert postmark.external_message_id == message_id.replace("\ud800", "\ufffd")
    for value in (postmark.subject, postmark.text, postmark.external_message_id):
        assert value is not None
        assert value.encode("utf-8")
    assert sendgrid.recipients == ["forward@inbound.example"]
    # `to` is freely composed by the sender; it must never surface as a delivery stamp.
    assert sendgrid.delivered_recipients == []
    assert EmailConfigCreate().daily_inbound_cap == 500
    with pytest.raises(ValidationError):
        ForwardingAddressCreate(email_config_id="emc_test", email_address="not-an-email")
    max_length_email = f"{'a' * 64}@{'b' * 63}.{'c' * 63}.{'d' * 61}"
    assert len(max_length_email.encode()) == 254
    assert (
        ForwardingAddressCreate(email_config_id="emc_test", email_address=max_length_email).email_address
        == max_length_email
    )
    overlong_email = "a" * 250 + "@x.co"
    with pytest.raises(ValidationError):
        ForwardingAddressCreate(email_config_id="emc_test", email_address=overlong_email)
    with pytest.raises(ValidationError):
        ForwardingAddressRotate(email_address=overlong_email)
    with pytest.raises(ValidationError):
        ForwardingAddressCreate(
            email_config_id="emc_test",
            email_address="forward@inbound.example",
            credential_id="cred_test",
            match_mode=ForwardingMatchMode.ORIGINAL_RECIPIENT,
        )
    normalized_allowlist = ForwardingAddressCreate(
        email_config_id="emc_test",
        email_address="forward@inbound.example",
        sender_allowlist=["Security@Bank.Example", "@TRUSTED.Example"],
    ).sender_allowlist
    assert normalized_allowlist == ["security@bank.example", "trusted.example"]
    for invalid_entry in ("bank.example\x00", "bad domain.example", "a" * 254):
        with pytest.raises(ValidationError):
            ForwardingAddressCreate(
                email_config_id="emc_test",
                email_address="forward@inbound.example",
                sender_allowlist=[invalid_entry],
            )


def test_sender_allowlist_distinguishes_exact_mailboxes_from_domains() -> None:
    assert email_forwarding._sender_allowed(
        "User@Corp.Example",
        None,
        ["user@corp.example"],
    )
    assert email_forwarding._sender_allowed(
        "forwarder@example.net",
        "user@corp.example",
        ["user@corp.example"],
    )
    assert not email_forwarding._sender_allowed(
        "other@corp.example",
        None,
        ["user@corp.example"],
    )
    assert email_forwarding._sender_allowed(
        "other@sub.corp.example.com",
        None,
        ["corp.example.com"],
    )


@pytest.mark.asyncio
async def test_receiver_auth_dedupe_and_raw_storage(agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(email_routes.app, "DATABASE", agent_db)
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    await agent_db.email_forwarding.create_address(
        "o_test", config.email_config_id, "forward@inbound.example", "test", "dedicated", None, None
    )
    raw = (FIXTURES / "gmail_auto_forward.eml").read_bytes()

    response = await email_routes.receive_inbound_email(config.email_config_id, _request(raw), "wrong")
    assert response.status_code == 200
    async with agent_db.Session() as session:
        assert await session.scalar(select(func.count(OrganizationInboundEmailModel.inbound_email_id))) == 0
        assert await session.scalar(select(func.count(TOTPCodeModel.totp_code_id))) == 0
    oversized = await email_routes.receive_inbound_email(
        config.email_config_id,
        _request(raw, content_length=email_forwarding.MAX_INBOUND_EMAIL_BYTES + 1),
        "secret",
    )
    assert oversized.status_code == 200
    async with agent_db.Session() as session:
        assert await session.scalar(select(func.count(OrganizationInboundEmailModel.inbound_email_id))) == 0
        assert await session.scalar(select(func.count(TOTPCodeModel.totp_code_id))) == 0
    unknown_raw = raw.replace(b"forward@inbound.example", b"unknown@inbound.example")
    unknown_response = await email_routes.receive_inbound_email(config.email_config_id, _request(unknown_raw), "secret")
    assert unknown_response.status_code == 200
    async with agent_db.Session() as session:
        assert await session.scalar(select(func.count(OrganizationInboundEmailModel.inbound_email_id))) == 0
        assert await session.scalar(select(func.count(TOTPCodeModel.totp_code_id))) == 0

    response = await email_routes.receive_inbound_email(config.email_config_id, _request(raw), "secret")
    assert response.status_code == 200
    raw_codes = await agent_db.otp.get_raw_otp_codes("o_test", "forward@inbound.example")
    assert len(raw_codes) == 1
    assert raw_codes[0].totp_identifier == "forward@inbound.example"
    assert raw_codes[0].source == "email_forwarding"
    assert raw_codes[0].content == "Subject: Your verification code\nYour one-time verification code is 482913."
    assert await agent_db.otp.get_otp_codes("o_test", "forward@inbound.example") == []
    addresses = await agent_db.email_forwarding.list_addresses("o_test")
    assert addresses[0].status == ForwardingAddressStatus.ACTIVE

    await email_routes.receive_inbound_email(config.email_config_id, _request(raw), "secret")
    async with agent_db.Session() as session:
        inbound_count = await session.scalar(select(func.count(OrganizationInboundEmailModel.inbound_email_id)))
        totp_count = await session.scalar(select(func.count(TOTPCodeModel.totp_code_id)))
    assert (inbound_count, totp_count) == (1, 1)
    assert await agent_db.otp.get_raw_otp_codes("o_test", "forward@inbound.example") == raw_codes
    assert await agent_db.otp.get_otp_codes("o_test", "forward@inbound.example") == []

    second = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("second-secret"), 500, True
    )
    await agent_db.email_forwarding.create_address(
        "o_test", second.email_config_id, "other@inbound.example", "test", "dedicated", None, None
    )
    second_raw = raw.replace(b"forward@inbound.example", b"other@inbound.example")
    await email_routes.receive_inbound_email(second.email_config_id, _request(second_raw), "second-secret")
    second_codes = await agent_db.otp.get_raw_otp_codes("o_test", "other@inbound.example")
    assert len(second_codes) == 1
    assert second_codes[0].totp_identifier == "other@inbound.example"
    assert second_codes[0].source == "email_forwarding"
    assert second_codes[0].content == raw_codes[0].content
    assert await agent_db.otp.get_otp_codes("o_test", "other@inbound.example") == []

    claim = _provider_message(
        "forward@inbound.example", "claim-number", text="Your code request concerns claim number 7654321"
    )
    assert await email_forwarding.ingest_inbound_email(config, claim) == "otp"
    claim_rows = await agent_db.otp.get_raw_otp_codes("o_test", "forward@inbound.example")
    assert len(claim_rows) == 2
    claim_row = next(row for row in claim_rows if row.totp_code_id != raw_codes[0].totp_code_id)
    assert claim_row.totp_identifier == "forward@inbound.example"
    assert claim_row.source == "email_forwarding"
    assert claim_row.content == "Subject: Sign-in code\nYour code request concerns claim number 7654321"
    assert await agent_db.otp.get_otp_codes("o_test", "forward@inbound.example") == []


@pytest.mark.asyncio
async def test_original_recipient_and_confirmation_diversion(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(email_forwarding.app, "DATABASE", agent_db)
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    address = await agent_db.email_forwarding.create_address(
        "o_test",
        config.email_config_id,
        "forward@inbound.example",
        "test",
        ForwardingMatchMode.ORIGINAL_RECIPIENT,
        None,
        ["service.example"],
    )
    outlook = email_forwarding.parse_raw_email((FIXTURES / "outlook_forward.eml").read_bytes())
    assert await email_forwarding.ingest_inbound_email(config, outlook) == "otp"
    raw_codes = await agent_db.otp.get_raw_otp_codes("o_test", "employee@customer.example")
    assert len(raw_codes) == 1
    assert raw_codes[0].totp_identifier == "employee@customer.example"
    assert raw_codes[0].source == "email_forwarding"
    assert raw_codes[0].content == "Subject: Security code\nUse security code 667788 to finish signing in."
    assert await agent_db.otp.get_otp_codes("o_test", "employee@customer.example") == []

    confirmation = email_forwarding.parse_raw_email((FIXTURES / "google_confirmation.eml").read_bytes())
    outcome = await email_forwarding.ingest_inbound_email(config, confirmation)
    assert outcome == "provider_confirmation"
    inbox = await agent_db.email_forwarding.list_inbound_emails(address.forwarding_address_id, "o_test", 10, 0)
    assert inbox[0].category == InboundEmailCategory.PROVIDER_CONFIRMATION
    assert inbox[0].totp_code_id is None
    confirmation_codes = await agent_db.otp.get_raw_otp_codes("o_test", address.email_address)
    assert confirmation_codes == []
    listed = await agent_db.email_forwarding.list_addresses("o_test")
    assert listed[0].has_pending_confirmation is True

    after_confirmation = _provider_message(address.email_address, "after-confirmation", sender="alerts@service.example")
    assert await email_forwarding.ingest_inbound_email(config, after_confirmation) == "otp"
    listed = await agent_db.email_forwarding.list_addresses("o_test")
    assert listed[0].has_pending_confirmation is False


@pytest.mark.asyncio
async def test_forwarded_magic_links_are_pinned_to_task_site(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(otp_service.app, "DATABASE", SimpleNamespace(otp=agent_db.otp))
    await agent_db.otp.create_otp_code(
        organization_id="o_test",
        totp_identifier="match@example.com",
        content="https://login.example.com/verify?t=ok",
        code="https://login.example.com/verify?t=ok",
        otp_type=OTPType.MAGIC_LINK,
        source="email_forwarding",
    )
    await agent_db.otp.create_otp_code(
        organization_id="o_test",
        totp_identifier="mismatch@example.com",
        content="https://attacker.test/verify?t=bad",
        code="https://attacker.test/verify?t=bad",
        otp_type=OTPType.MAGIC_LINK,
        source="email_forwarding",
    )
    matching = await otp_service._get_otp_value_from_db(
        "o_test", "match@example.com", expected_otp_type=OTPType.MAGIC_LINK, task_url="https://app.example.com/login"
    )
    rejected = await otp_service._get_otp_value_from_db(
        "o_test", "mismatch@example.com", expected_otp_type=OTPType.MAGIC_LINK, task_url="https://app.example.com/login"
    )
    assert matching and matching.value.endswith("t=ok")
    assert rejected is None
    rejected_without_target = await otp_service._get_otp_value_from_db(
        "o_test", "match@example.com", expected_otp_type=OTPType.MAGIC_LINK, task_url=None
    )
    assert rejected_without_target is None


@pytest.mark.asyncio
@pytest.mark.parametrize("parsed", [False, True])
@pytest.mark.parametrize(
    "link", [r"https://attacker.test\@login.example.com/verify", "https://login.example.com:bad/verify", "https://["]
)
async def test_forwarded_magic_links_reject_browser_parser_differentials(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch, parsed: bool, link: str
) -> None:
    monkeypatch.setattr(otp_service.app, "DATABASE", SimpleNamespace(otp=agent_db.otp))
    if parsed:
        await agent_db.otp.create_otp_code(
            organization_id="o_test",
            totp_identifier="forward@example.test",
            content=link,
            code=link,
            otp_type=OTPType.MAGIC_LINK,
            source="email_forwarding",
        )
    else:
        await agent_db.otp.create_raw_otp_code(
            organization_id="o_test",
            totp_identifier="forward@example.test",
            content=link,
            source="email_forwarding",
        )
    monkeypatch.setattr(
        otp_service,
        "parse_otp_login",
        AsyncMock(return_value=otp_service.OTPValue(value=link, type=OTPType.MAGIC_LINK)),
    )
    assert (
        await otp_service._get_otp_value_from_db(
            "o_test",
            "forward@example.test",
            expected_otp_type=OTPType.MAGIC_LINK,
            task_url="https://login.example.com/login",
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("padding", [0, 70_000])
async def test_html_only_magic_link_survives_ingestion_and_resolution(
    agent_db: AgentDB,
    monkeypatch: pytest.MonkeyPatch,
    padding: int,
) -> None:
    monkeypatch.setattr(email_forwarding.app, "DATABASE", agent_db)
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    address = await agent_db.email_forwarding.create_address(
        "o_test", config.email_config_id, "forward@inbound.example", "test", "dedicated", None, None
    )
    link = "https://login.example.com/verify?token=abc&code=123456"
    message = EmailMessage()
    message["From"] = "security@example.test"
    message["To"] = address.email_address
    message["Subject"] = "Sign-in code"
    message["Message-ID"] = "<html-link@example.test>"
    message.set_content(f'<p>{"x" * padding}</p><a href="{link.replace("&", "&amp;")}">Sign in</a>', subtype="html")
    response = await email_routes.receive_inbound_email(config.email_config_id, _request(message.as_bytes()), "secret")
    assert response.status_code == 200
    assert await agent_db.otp.get_otp_codes("o_test", address.email_address) == []
    raw = await agent_db.otp.get_raw_otp_codes("o_test", address.email_address)
    assert len(raw) == 1
    assert raw[0].totp_identifier == address.email_address
    assert raw[0].source == "email_forwarding"
    assert any(line == link for line in raw[0].content.splitlines())
    assert len(raw[0].content) <= email_forwarding.settings.TOTP_RAW_CONTENT_MAX_LENGTH
    inbox = await agent_db.email_forwarding.list_inbound_emails(address.forwarding_address_id, "o_test", 10, 0)
    assert inbox[0].links == [{"url": link, "text": "Sign in"}]
    assert link not in inbox[0].body_text

    async def select_visible_link(content: str, organization_id: str, enforced_otp_type: OTPType):
        value = otp_service._verbatim_otp_value(content, enforced_otp_type, link)
        return otp_service.OTPValue(value=value, type=OTPType.MAGIC_LINK) if value else None

    monkeypatch.setattr(otp_service, "parse_otp_login", select_visible_link)
    result = await otp_service._get_otp_value_from_db(
        "o_test",
        address.email_address,
        expected_otp_type=OTPType.MAGIC_LINK,
        task_url="https://app.example.com/login",
    )
    assert result is not None and result.value == link


@pytest.mark.asyncio
async def test_forwarded_magic_links_fail_closed_for_hostless_urls(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(otp_service.app, "DATABASE", SimpleNamespace(otp=agent_db.otp))
    hostless_link = "https:///attacker.test/verify?t=bad"
    await agent_db.otp.create_otp_code(
        organization_id="o_test",
        totp_identifier="hostless@example.com",
        content=hostless_link,
        code=hostless_link,
        otp_type=OTPType.MAGIC_LINK,
        source="email_forwarding",
    )

    result = await otp_service._get_otp_value_from_db(
        "o_test",
        "hostless@example.com",
        expected_otp_type=OTPType.MAGIC_LINK,
        task_url="about:blank",
    )

    assert result is None


@pytest.mark.asyncio
async def test_agent_magic_link_poll_receives_task_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_poll(**kwargs: object) -> otp_service.OTPValue:
        captured.update(kwargs)
        return otp_service.OTPValue(value="https://app.example.com/verify?t=ok", type=OTPType.MAGIC_LINK)

    monkeypatch.setattr("skyvern.forge.agent.poll_otp_value", fake_poll)
    context = MagicMock()
    monkeypatch.setattr("skyvern.forge.agent.skyvern_context.ensure_context", lambda: context)
    task = SimpleNamespace(
        organization_id="o_test",
        task_id="tsk_test",
        workflow_run_id="wr_test",
        totp_verification_url=None,
        totp_identifier="user@example.com",
        url="https://app.example.com/login",
    )
    step = SimpleNamespace(step_id="stp_test", order=0)
    browser_state = SimpleNamespace(new_page=AsyncMock(return_value=MagicMock()))

    actions = await ForgeAgent.handle_potential_magic_link(
        MagicMock(),
        task,
        step,
        MagicMock(),
        browser_state,
        {"should_verify_by_magic_link": True},
    )

    assert captured["task_url"] == task.url
    assert actions[0].url == "https://app.example.com/verify?t=ok"


def test_raw_mime_recipient_precedence_repeated_headers_and_resent_to() -> None:
    raw = (
        b"From: alerts@example.com\r\n"
        b"Resent-To: forward-one@inbound.example\r\n"
        b"Resent-To: forward-two@inbound.example\r\n"
        b"To: employee@customer.example\r\n"
        b"Subject: Sign-in code\r\n"
        b"Message-ID: <repeated@example.com>\r\n"
        b"\r\nVerification code 123456"
    )
    parsed = email_forwarding.parse_raw_email(raw)

    assert parsed.recipients == ["forward-one@inbound.example", "forward-two@inbound.example"]
    # Only MTA delivery stamps are identifier-eligible; the composed To header is not.
    assert parsed.delivered_recipients == [
        "forward-one@inbound.example",
        "forward-two@inbound.example",
    ]
    assert parsed.external_message_id_requires_namespace is True

    to_only = email_forwarding.parse_raw_email(
        b"From: alerts@example.com\r\n"
        b"To: employee@customer.example\r\n"
        b"Subject: Sign-in code\r\n"
        b"\r\nVerification code 123456"
    )
    assert to_only.recipients == ["employee@customer.example"]
    assert to_only.original_recipient is None
    assert to_only.delivered_recipients == []


def test_attached_message_rfc822_subtree_is_not_parsed() -> None:
    raw = (
        b"From: alerts@example.com\r\n"
        b"To: forward@inbound.example\r\n"
        b"Subject: Account notice\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="outer"\r\n\r\n'
        b"--outer\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nNo current code.\r\n"
        b"--outer\r\nContent-Type: message/rfc822\r\n"
        b'Content-Disposition: attachment; filename="old.eml"\r\n\r\n'
        b"From: old@example.com\r\nTo: user@example.com\r\nSubject: Old code\r\n\r\n"
        b"Verification code 999999\r\n"
        b"--outer--\r\n"
    )
    parsed = email_forwarding.parse_raw_email(raw)
    body, _ = email_forwarding.extract_email_body(parsed)

    assert body == "No current code."
    assert "999999" not in body
    assert "Verification code" not in body


def test_deep_mime_is_rejected_during_parsing() -> None:
    with pytest.raises(email_forwarding.InboundEmailLimitExceeded, match="depth"):
        email_forwarding.parse_raw_email((FIXTURES / "deeply_nested.eml").read_bytes())


def test_mime_part_limit_rejects_during_parsing() -> None:
    message = EmailMessage()
    message["From"] = "alerts@example.com"
    message["To"] = "forward@inbound.example"
    message.make_mixed()
    for index in range(201):
        part = EmailMessage()
        part.set_content(f"part-{index}")
        message.attach(part)

    with pytest.raises(email_forwarding.InboundEmailLimitExceeded, match="parts"):
        email_forwarding.parse_raw_email(message.as_bytes())


@pytest.mark.parametrize(
    ("charset", "payload", "expected"),
    [
        ("iso-8859-1", b"Verification code 123456=00 for caf=E9", "Verification code 123456 for café"),
        ("punycode", b"a" * (1024 * 1024 - 1) + b"=FF", "a" * (1024 * 1024 - 1) + "\ufffd"),
        ("utf-7", b"Code 123456 +2AA-", "Code 123456 �"),
    ],
    ids=["latin1-nul", "large-punycode", "utf7-unpaired-surrogate"],
)
def test_quoted_printable_non_utf8_and_nul_text_is_sanitized(charset: str, payload: bytes, expected: str) -> None:
    raw = (
        b"From: alerts@example.com\r\n"
        b"To: forward@inbound.example\r\n"
        b"Subject: Security code\r\n"
        + f"Content-Type: text/plain; charset={charset}\r\n".encode()
        + b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        + payload
    )
    started = time.process_time()
    parsed = email_forwarding.parse_raw_email(raw)
    assert time.process_time() - started < 1.0
    assert parsed.text == expected
    body, _ = email_forwarding.extract_email_body(parsed)

    assert body == expected
    assert "\x00" not in body


@pytest.mark.asyncio
async def test_all_persisted_provider_text_is_postgres_sanitized(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(email_forwarding.app, "DATABASE", agent_db)
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    await agent_db.email_forwarding.create_address(
        "o_test", config.email_config_id, "forward@inbound.example", "test", "dedicated", None, None
    )
    message = email_forwarding.NormalizedInboundEmail(
        recipients=["forward\x00@inbound.example"],
        sender="security\x00@example.com",
        subject="Security\x00 code",
        text="Verification code 123456\x00",
        html='<a href="https://example.com/verify\x00?t=1">Verify\x00</a>',
        external_message_id="provider\x00-id",
        original_recipient="employee\x00@customer.example",
        verdicts={"spf\x00Verdict": "pass\x00"},
    )

    assert await email_forwarding.ingest_inbound_email(config, message) == "otp"
    inbox = await agent_db.email_forwarding.list_inbound_emails(
        (await agent_db.email_forwarding.list_addresses("o_test"))[0].forwarding_address_id,
        "o_test",
        10,
        0,
    )
    persisted = inbox[0]
    assert "\x00" not in persisted.from_address
    assert "\x00" not in persisted.subject
    assert "\x00" not in persisted.body_text
    assert "\x00" not in str(persisted.links)
    assert "\x00" not in str(persisted.verdicts)
    assert "\x00" not in (persisted.external_message_id or "")
    assert "\x00" not in (persisted.original_recipient or "")


@pytest.mark.asyncio
async def test_provider_verdict_shapes_gate_spam_and_fail_closed(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(email_forwarding.app, "DATABASE", agent_db)
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    address = await agent_db.email_forwarding.create_address(
        "o_test", config.email_config_id, "forward@inbound.example", "test", "dedicated", None, None
    )

    def _message(external_id: str, verdicts: object) -> email_forwarding.NormalizedInboundEmail:
        return email_forwarding.normalize_provider_payload(
            {
                "From": "security@bank.example",
                "recipient": address.email_address,
                "Subject": "Sign-in code",
                "TextBody": "Verification code 123456",
                "message_id": external_id,
                "verdicts": verdicts,
            }
        )

    # SES-native nested statuses gate exactly like flat strings.
    nested_spam = _message("nested-spam", {"spamVerdict": {"status": "FAIL"}})
    assert await email_forwarding.ingest_inbound_email(config, nested_spam) == "dropped_spam"

    nested_virus = _message("nested-virus", {"virusVerdict": {"status": "FAIL"}})
    assert await email_forwarding.ingest_inbound_email(config, nested_virus) == "dropped_spam"

    flat_spam = _message("flat-spam", {"spamVerdict": "FAIL"})
    assert await email_forwarding.ingest_inbound_email(config, flat_spam) == "dropped_spam"

    # An unreadable verdict shape fails closed instead of silently passing the gate.
    unreadable_spam = _message("unreadable-spam", {"spamVerdict": ["FAIL"]})
    assert await email_forwarding.ingest_inbound_email(config, unreadable_spam) == "dropped_spam"

    unreadable_virus = _message("unreadable-virus", {"virusVerdict": {"outcome": "FAIL"}})
    assert await email_forwarding.ingest_inbound_email(config, unreadable_virus) == "dropped_spam"

    unreadable_container = _message("unreadable-container", '{"spamVerdict": "PASS"}')
    assert await email_forwarding.ingest_inbound_email(config, unreadable_container) == "dropped_spam"

    # Passing or absent verdicts admit the message.
    nested_pass = _message("nested-pass", {"spamVerdict": {"status": "PASS"}, "virusVerdict": None})
    assert await email_forwarding.ingest_inbound_email(config, nested_pass) == "otp"


@pytest.mark.asyncio
async def test_repository_sanitizes_sender_allowlist_before_persistence(agent_db: AgentDB) -> None:
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    address = await agent_db.email_forwarding.create_address(
        "o_test",
        config.email_config_id,
        "forward@inbound.example",
        "test",
        "dedicated",
        None,
        ["bank.example\x00"],
    )

    assert address.sender_allowlist == ["bank.example"]


@pytest.mark.asyncio
async def test_provider_rfc_message_id_is_namespaced_per_config(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(email_forwarding.app, "DATABASE", agent_db)
    configs = [
        await agent_db.email_forwarding.create_config(
            "o_test", email_forwarding.hash_webhook_secret(f"secret-{index}"), 500, True
        )
        for index in range(2)
    ]
    addresses = ["first@inbound.example", "second@inbound.example"]
    for config, address in zip(configs, addresses, strict=True):
        await agent_db.email_forwarding.create_address(
            "o_test", config.email_config_id, address, "test", "dedicated", None, None
        )
        message = email_forwarding.normalize_provider_payload(
            {
                "from": "security@example.com",
                "envelope": {"to": [address]},
                "text": "Verification code 123456",
                "headers": [{"Name": "Message-ID", "Value": "<same-rfc-id@example.com>"}],
            }
        )
        assert message.external_message_id_requires_namespace is True
        assert await email_forwarding.ingest_inbound_email(config, message) == "otp"

    async with agent_db.Session() as session:
        external_ids = (
            await session.scalars(
                select(OrganizationInboundEmailModel.external_message_id).order_by(
                    OrganizationInboundEmailModel.external_message_id
                )
            )
        ).all()
    assert external_ids == sorted(f"{config.email_config_id}:rfc822:<same-rfc-id@example.com>" for config in configs)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_header", ["X-Skyvern-External-Message-ID", "X-SES-Message-ID"])
async def test_raw_mime_transport_id_is_namespaced_per_config(
    transport_header: str, agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(email_forwarding.app, "DATABASE", agent_db)
    configs = [
        await agent_db.email_forwarding.create_config(
            "o_test", email_forwarding.hash_webhook_secret(f"secret-{index}"), 500, True
        )
        for index in range(2)
    ]
    for index, config in enumerate(configs):
        address = f"raw-{index}@inbound.example"
        await agent_db.email_forwarding.create_address(
            "o_test", config.email_config_id, address, "test", "dedicated", None, None
        )
        raw_message = EmailMessage()
        raw_message["From"] = "security@example.com"
        raw_message["To"] = address
        raw_message["Subject"] = "Code"
        raw_message[transport_header] = "sender-controlled-id"
        raw_message.set_content("Verification code 123456")
        message = email_forwarding.parse_raw_email(raw_message.as_bytes())
        assert message.external_message_id_requires_namespace is True
        assert await email_forwarding.ingest_inbound_email(config, message) == "otp"

    async with agent_db.Session() as session:
        external_ids = (
            await session.scalars(
                select(OrganizationInboundEmailModel.external_message_id).order_by(
                    OrganizationInboundEmailModel.external_message_id
                )
            )
        ).all()
    assert external_ids == sorted(f"{config.email_config_id}:rfc822:sender-controlled-id" for config in configs)


@pytest.mark.asyncio
async def test_cap_drops_without_rows_or_lockout_extension_and_revocation_is_permanent(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(email_forwarding.app, "DATABASE", agent_db)
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 1, True
    )
    address = await agent_db.email_forwarding.create_address(
        "o_test",
        config.email_config_id,
        "forward@inbound.example",
        "test",
        "dedicated",
        None,
        ["bank.example"],
    )

    disallowed = _provider_message(address.email_address, "dropped-allowlist", sender="alerts@other.example")
    assert await email_forwarding.ingest_inbound_email(config, disallowed) == "dropped_other"
    assert await _capped_inbound_count(agent_db, config.email_config_id) == 0

    accepted = _provider_message(address.email_address, "accepted")
    assert await email_forwarding.ingest_inbound_email(config, accepted) == "otp"
    assert await email_forwarding.ingest_inbound_email(config, accepted) == "deduped"
    after_accepted = await agent_db.email_forwarding.get_address(address.forwarding_address_id, "o_test")
    assert after_accepted is not None

    for message_id in ("over-cap-one", "over-cap-two"):
        over_cap = _provider_message(address.email_address, message_id)
        assert await email_forwarding.ingest_inbound_email(config, over_cap) == "dropped-cap"

    async with agent_db.Session() as session:
        inbound_count = await session.scalar(select(func.count(OrganizationInboundEmailModel.inbound_email_id)))
    after_over_cap = await agent_db.email_forwarding.get_address(address.forwarding_address_id, "o_test")
    assert inbound_count == 2
    assert await _capped_inbound_count(agent_db, config.email_config_id) == 1
    assert after_over_cap is not None
    assert after_over_cap.last_received_at == after_accepted.last_received_at
    assert after_over_cap.status == ForwardingAddressStatus.ACTIVE

    revoked = await agent_db.email_forwarding.set_address_status(
        address.forwarding_address_id,
        "o_test",
        ForwardingAddressStatus.REVOKED,
        (
            ForwardingAddressStatus.PENDING,
            ForwardingAddressStatus.ACTIVE,
            ForwardingAddressStatus.DISABLED,
        ),
    )
    assert revoked is not None
    after_revoke = _provider_message(address.email_address, "after-revoke")
    assert await email_forwarding.ingest_inbound_email(config, after_revoke) == "dropped-unregistered"
    current = await agent_db.email_forwarding.get_address(address.forwarding_address_id, "o_test")
    assert current is not None
    assert current.status == ForwardingAddressStatus.REVOKED
    assert (
        await agent_db.email_forwarding.set_address_status(
            address.forwarding_address_id,
            "o_test",
            ForwardingAddressStatus.ACTIVE,
            (
                ForwardingAddressStatus.PENDING,
                ForwardingAddressStatus.ACTIVE,
                ForwardingAddressStatus.DISABLED,
            ),
        )
        is None
    )
    assert (
        await agent_db.email_forwarding.rotate_address(
            address.forwarding_address_id,
            "o_test",
            "replacement@inbound.example",
        )
        is None
    )


@pytest.mark.asyncio
async def test_drop_diagnostics_are_capped_separately_from_accepted_rows(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(email_forwarding.app, "DATABASE", agent_db)
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 1, True
    )
    address = await agent_db.email_forwarding.create_address(
        "o_test",
        config.email_config_id,
        "forward@inbound.example",
        "test",
        "dedicated",
        None,
        ["bank.example"],
    )

    for index in range(50):
        disallowed = _provider_message(
            address.email_address,
            f"dropped-{index}",
            sender="alerts@other.example",
        )
        assert await email_forwarding.ingest_inbound_email(config, disallowed) == "dropped_other"

    log_mock = MagicMock()
    monkeypatch.setattr(email_forwarding, "LOG", log_mock)
    capped_drop = _provider_message(address.email_address, "dropped-at-cap", sender="alerts@other.example")
    assert await email_forwarding.ingest_inbound_email(config, capped_drop) == "dropped_other"
    assert all(call.args[0] != "Processed inbound email" for call in log_mock.info.call_args_list)

    duplicate_drop = _provider_message(
        address.email_address,
        "dropped-0",
        sender="alerts@other.example",
    )
    assert await email_forwarding.ingest_inbound_email(config, duplicate_drop) == "deduped"

    accepted = _provider_message(address.email_address, "accepted-after-drops")
    assert await email_forwarding.ingest_inbound_email(config, accepted) == "otp"

    async with agent_db.Session() as session:
        drop_count = await session.scalar(
            select(func.count(OrganizationInboundEmailModel.inbound_email_id)).where(
                OrganizationInboundEmailModel.category.in_(
                    (InboundEmailCategory.DROPPED_SPAM, InboundEmailCategory.DROPPED_OTHER)
                )
            )
        )
        accepted_count = await session.scalar(
            select(func.count(OrganizationInboundEmailModel.inbound_email_id)).where(
                OrganizationInboundEmailModel.category == InboundEmailCategory.OTP
            )
        )
    assert drop_count == 50
    assert accepted_count == 1
    assert await _capped_inbound_count(agent_db, config.email_config_id) == 1


@pytest.mark.asyncio
async def test_unexpected_record_outcome_never_logs_processed(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(email_forwarding.app, "DATABASE", agent_db)
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    address = await agent_db.email_forwarding.create_address(
        "o_test", config.email_config_id, "forward@inbound.example", "test", "dedicated", None, None
    )
    monkeypatch.setattr(
        agent_db.email_forwarding,
        "record_inbound",
        AsyncMock(return_value=("unhandled", None, None)),
    )
    log_mock = MagicMock()
    monkeypatch.setattr(email_forwarding, "LOG", log_mock)

    with pytest.raises(RuntimeError, match="Unexpected inbound email record status"):
        await email_forwarding.ingest_inbound_email(
            config,
            _provider_message(address.email_address, "unexpected-outcome"),
        )

    assert all(call.args[0] != "Processed inbound email" for call in log_mock.info.call_args_list)


@pytest.mark.asyncio
async def test_address_create_conflicts_then_allows_revoked_reregistration(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    transport = httpx.ASGITransport(app=_asgi_app(agent_db, monkeypatch))
    payload = {
        "email_config_id": config.email_config_id,
        "email_address": "forward@inbound.example",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/v1/email-forwarding/addresses", json=payload)
        assert created.status_code == 201

        duplicate = await client.post("/v1/email-forwarding/addresses", json=payload)
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"] == "Forwarding address is already registered"

        revoked = await client.delete(f"/v1/email-forwarding/addresses/{created.json()['forwarding_address_id']}")
        assert revoked.status_code == 204

        reregistered = await client.post("/v1/email-forwarding/addresses", json=payload)
        assert reregistered.status_code == 201
        assert reregistered.json()["forwarding_address_id"] != created.json()["forwarding_address_id"]

    # The revoked row must never shadow the live one: lookup routes to the
    # re-registered (pending) address, and a revoked-only address is unregistered.
    found = await agent_db.email_forwarding.find_address(config.email_config_id, ["forward@inbound.example"])
    assert found is not None
    assert found.forwarding_address_id == reregistered.json()["forwarding_address_id"]
    assert found.status == ForwardingAddressStatus.PENDING

    second_live = await agent_db.email_forwarding.create_address(
        "o_test", config.email_config_id, "second@inbound.example", "test", "dedicated", None, None
    )
    preferred = await agent_db.email_forwarding.find_address(
        config.email_config_id,
        [second_live.email_address, reregistered.json()["email_address"]],
    )
    assert preferred is not None
    assert preferred.forwarding_address_id == second_live.forwarding_address_id

    revoked_only = await agent_db.email_forwarding.create_address(
        "o_test", config.email_config_id, "revoked-only@inbound.example", "test", "dedicated", None, None
    )
    await agent_db.email_forwarding.set_address_status(
        revoked_only.forwarding_address_id,
        "o_test",
        ForwardingAddressStatus.REVOKED,
        allowed_current_statuses=(ForwardingAddressStatus.PENDING, ForwardingAddressStatus.ACTIVE),
    )
    assert (
        await agent_db.email_forwarding.find_address(config.email_config_id, ["revoked-only@inbound.example"])
    ) is None


@pytest.mark.asyncio
async def test_address_create_rejects_existing_credential_totp_binding(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    async with agent_db.Session() as session:
        session.add(
            CredentialModel(
                credential_id="cred_existing_binding",
                organization_id="o_test",
                name="Existing login",
                credential_type="password",
                totp_type="sms",
                totp_identifier="+15555550123",
            )
        )
        await session.commit()

    transport = httpx.ASGITransport(app=_asgi_app(agent_db, monkeypatch))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/email-forwarding/addresses",
            json={
                "email_config_id": config.email_config_id,
                "email_address": "forward@inbound.example",
                "credential_id": "cred_existing_binding",
            },
        )
    assert response.status_code == 409
    assert response.json()["detail"] == "Credential already has a different TOTP binding"
    async with agent_db.Session() as session:
        credential = await session.get(CredentialModel, "cred_existing_binding")
        assert credential is not None
        assert credential.totp_type == "sms"
        assert credential.totp_identifier == "+15555550123"


@pytest.mark.asyncio
async def test_address_create_adopts_equivalent_email_credential_binding(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    async with agent_db.Session() as session:
        session.add(
            CredentialModel(
                credential_id="cred_equivalent_binding",
                organization_id="o_test",
                name="Equivalent email",
                credential_type="password",
                totp_type="email",
                totp_identifier=" Forward@Inbound.Example ",
            )
        )
        await session.commit()

    transport = httpx.ASGITransport(app=_asgi_app(agent_db, monkeypatch))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/email-forwarding/addresses",
            json={
                "email_config_id": config.email_config_id,
                "email_address": "forward@inbound.example",
                "credential_id": "cred_equivalent_binding",
            },
        )

    assert response.status_code == 201
    async with agent_db.Session() as session:
        credential = await session.get(CredentialModel, "cred_equivalent_binding")
        assert credential is not None
        assert credential.totp_type == "email"
        assert credential.totp_identifier == "forward@inbound.example"


@pytest.mark.asyncio
async def test_address_rotation_only_updates_matching_email_credential_binding(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    async with agent_db.Session() as session:
        session.add_all(
            [
                CredentialModel(
                    credential_id="cred_matching_email",
                    organization_id="o_test",
                    name="Matching email",
                    credential_type="password",
                    totp_type="none",
                ),
                CredentialModel(
                    credential_id="cred_repointed_sms",
                    organization_id="o_test",
                    name="Repointed SMS",
                    credential_type="password",
                    totp_type="none",
                ),
            ]
        )
        await session.commit()

    matching_address = await agent_db.email_forwarding.create_address(
        "o_test",
        config.email_config_id,
        "matching-old@inbound.example",
        "test",
        "dedicated",
        "cred_matching_email",
        None,
    )
    repointed_address = await agent_db.email_forwarding.create_address(
        "o_test",
        config.email_config_id,
        "repointed-old@inbound.example",
        "test",
        "dedicated",
        "cred_repointed_sms",
        None,
    )
    async with agent_db.Session() as session:
        # The credential-update path stores identifiers unchanged; the rotation guard
        # must match case/whitespace variants of the old address (normalized compare).
        matching_credential = await session.get(CredentialModel, "cred_matching_email")
        assert matching_credential is not None
        matching_credential.totp_identifier = " Matching-Old@Inbound.Example "
        repointed_credential = await session.get(CredentialModel, "cred_repointed_sms")
        assert repointed_credential is not None
        repointed_credential.totp_type = "sms"
        repointed_credential.totp_identifier = "+15555550123"
        await session.commit()
        await session.refresh(repointed_credential)
        repointed_state = (
            repointed_credential.name,
            repointed_credential.totp_type,
            repointed_credential.totp_identifier,
            repointed_credential.modified_at,
        )

    log_mock = MagicMock()
    monkeypatch.setattr(email_forwarding_repository, "LOG", log_mock)
    matching_rotation = await agent_db.email_forwarding.rotate_address(
        matching_address.forwarding_address_id,
        "o_test",
        "matching-new@inbound.example",
    )
    repointed_rotation = await agent_db.email_forwarding.rotate_address(
        repointed_address.forwarding_address_id,
        "o_test",
        "repointed-new@inbound.example",
    )

    assert matching_rotation is not None
    assert repointed_rotation is not None
    repointed_old, repointed_replacement = repointed_rotation
    assert repointed_old.status == ForwardingAddressStatus.REVOKED
    assert repointed_replacement.email_address == "repointed-new@inbound.example"
    assert repointed_replacement.status == ForwardingAddressStatus.PENDING
    async with agent_db.Session() as session:
        matching_credential = await session.get(CredentialModel, "cred_matching_email")
        repointed_credential = await session.get(CredentialModel, "cred_repointed_sms")
        assert matching_credential is not None
        assert repointed_credential is not None
        assert matching_credential.totp_type == "email"
        assert matching_credential.totp_identifier == "matching-new@inbound.example"
        assert (
            repointed_credential.name,
            repointed_credential.totp_type,
            repointed_credential.totp_identifier,
            repointed_credential.modified_at,
        ) == repointed_state
    log_mock.warning.assert_called_once_with(
        "Skipping credential TOTP binding update during forwarding address rotation",
        organization_id="o_test",
        forwarding_address_id=repointed_address.forwarding_address_id,
        credential_id="cred_repointed_sms",
        credential_totp_type="sms",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_config", [False, True])
async def test_revocation_releases_only_matching_credential_binding(agent_db: AgentDB, delete_config: bool) -> None:
    repo = agent_db.email_forwarding
    config = await repo.create_config("o_test", email_forwarding.hash_webhook_secret("secret"), 500, True)
    async with agent_db.Session() as session:
        session.add_all(
            [
                CredentialModel(
                    credential_id=credential_id,
                    organization_id="o_test",
                    name="Login",
                    credential_type="password",
                    totp_type="none",
                )
                for credential_id in ("cred_bound", "cred_repointed")
            ]
        )
        await session.commit()
    bound = await repo.create_address(
        "o_test", config.email_config_id, "bound@inbound.example", "test", "dedicated", "cred_bound", None
    )
    repointed = await repo.create_address(
        "o_test", config.email_config_id, "old@inbound.example", "test", "dedicated", "cred_repointed", None
    )
    async with agent_db.Session() as session:
        credential = await session.get(CredentialModel, "cred_bound")
        assert credential is not None
        credential.totp_identifier = " Bound@Inbound.Example "
        credential = await session.get(CredentialModel, "cred_repointed")
        assert credential is not None
        credential.totp_type = "sms"
        credential.totp_identifier = "+15555550123"
        await session.commit()

    if delete_config:
        assert not await repo.delete_config(config.email_config_id, "o_other", authorized_credential_ids=set())
        assert await repo.delete_config(
            config.email_config_id,
            "o_test",
            authorized_credential_ids={"cred_bound", "cred_repointed"},
        )
    else:
        for address in (bound, repointed):
            assert await repo.set_address_status(
                address.forwarding_address_id,
                "o_test",
                ForwardingAddressStatus.REVOKED,
                (ForwardingAddressStatus.PENDING,),
            )

    async with agent_db.Session() as session:
        credential = await session.get(CredentialModel, "cred_bound")
        assert credential is not None
        assert credential.totp_type == "none"
        assert credential.totp_identifier is None
        credential = await session.get(CredentialModel, "cred_repointed")
        assert credential is not None
        assert (credential.totp_type, credential.totp_identifier) == ("sms", "+15555550123")
    replacement_config = await repo.create_config("o_test", email_forwarding.hash_webhook_secret("new"), 500, True)
    replacement = await repo.create_address(
        "o_test", replacement_config.email_config_id, "new@inbound.example", "test", "dedicated", "cred_bound", None
    )
    assert replacement.credential_id == "cred_bound"


@pytest.mark.asyncio
async def test_shared_config_reader_preserves_deployment_mode(agent_db: AgentDB) -> None:
    async with agent_db.Session() as session:
        config = OrganizationEmailConfigModel(
            email_config_id="emc_custom", organization_id="o_test", mode="custom", webhook_secret_hash=None
        )
        session.add(config)
        await session.commit()
    result = await agent_db.email_forwarding.get_config("emc_custom", "o_test")
    assert result is not None
    assert result.mode == "custom"


@pytest.mark.asyncio
async def test_config_secret_rotation_replaces_secret_and_returns_it_once(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = httpx.ASGITransport(app=_asgi_app(agent_db, monkeypatch))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/v1/email-forwarding/configs", json={})
        assert created.status_code == 201
        config_id = created.json()["email_config_id"]
        original_secret = created.json()["receiver_url"].partition("secret=")[2]

        rotated = await client.post(f"/v1/email-forwarding/configs/{config_id}/rotate-secret")
        assert rotated.status_code == 200
        rotated_secret = rotated.json()["receiver_url"].partition("secret=")[2]
        assert rotated_secret
        assert rotated_secret != original_secret

        listed = await client.get("/v1/email-forwarding/configs")
        assert listed.status_code == 200
        assert "receiver_url" not in listed.json()[0]

        missing = await client.post("/v1/email-forwarding/configs/emc_missing/rotate-secret")
        assert missing.status_code == 404

    config = await agent_db.email_forwarding.get_config(config_id, "o_test")
    assert config is not None
    assert not email_forwarding.webhook_secret_matches(config, original_secret)
    assert email_forwarding.webhook_secret_matches(config, rotated_secret)


@pytest.mark.asyncio
async def test_asgi_receiver_silently_handles_malformed_multipart_and_bounds_streaming_body(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    await agent_db.email_forwarding.create_address(
        "o_test", config.email_config_id, "forward@inbound.example", "test", "dedicated", None, None
    )
    monkeypatch.setattr(request_logging, "LOG", MagicMock())
    monkeypatch.setattr(request_logging.settings, "LOG_RAW_API_REQUESTS", True)
    fastapi_app = _asgi_app(agent_db, monkeypatch, with_request_logging=True)
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        malformed = await client.post(
            f"/v1/email/inbound/{config.email_config_id}?secret=secret",
            content=b"--missing-boundary",
            headers={"content-type": "multipart/form-data"},
        )
        assert malformed.status_code == 200

        large_text = "Verification code 123456\n" + ("x" * (1024 * 1024 + 1))
        valid_large_form = await client.post(
            f"/v1/email/inbound/{config.email_config_id}?secret=secret",
            files={
                "from": (None, "security@example.com"),
                "to": (None, "employee@customer.example"),
                "envelope": (None, '{"to":["forward@inbound.example"]}'),
                "text": (None, large_text),
                "message_id": (None, "large-sendgrid-form"),
            },
        )
        assert valid_large_form.status_code == 200

        monkeypatch.setattr(email_routes, "MAX_INBOUND_EMAIL_BYTES", 32)

        async def falsely_small_chunks() -> AsyncIterator[bytes]:
            yield b"x" * 20
            yield b"y" * 20
            raise AssertionError("receiver read past the configured cap")

        oversized = await client.post(
            f"/v1/email/inbound/{config.email_config_id}?secret=secret",
            content=falsely_small_chunks(),
            headers={"content-type": "message/rfc822", "content-length": "1"},
        )
        assert oversized.status_code == 200

        monkeypatch.setattr(email_routes, "INBOUND_BODY_READ_TIMEOUT_SECONDS", 0.01)
        chunks_read = 0

        async def slow_chunks() -> AsyncIterator[bytes]:
            nonlocal chunks_read
            chunks_read += 1
            yield b"x"
            await asyncio.sleep(1)
            chunks_read += 1
            yield b"y"

        timed_out = await client.post(
            f"/v1/email/inbound/{config.email_config_id}?secret=secret",
            content=slow_chunks(),
            headers={"content-type": "message/rfc822", "content-length": "1"},
        )
        assert timed_out.status_code == 200
        assert chunks_read == 1

        invalid_binding = await client.post(
            "/v1/email-forwarding/addresses",
            json={
                "email_config_id": config.email_config_id,
                "email_address": "forward@inbound.example",
                "credential_id": "cred_test",
                "match_mode": "original_recipient",
            },
        )
        assert invalid_binding.status_code == 422

        invalid_allowlist = await client.post(
            "/v1/email-forwarding/addresses",
            json={
                "email_config_id": config.email_config_id,
                "email_address": "another@inbound.example",
                "sender_allowlist": ["bank.example\u0000"],
            },
        )
        assert invalid_allowlist.status_code == 422

    async with agent_db.Session() as session:
        inbound_count = await session.scalar(select(func.count(OrganizationInboundEmailModel.inbound_email_id)))
    assert inbound_count == 1


@pytest.mark.asyncio
async def test_provider_json_subject_is_bounded_in_storage_and_inbox_response(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    address = await agent_db.email_forwarding.create_address(
        "o_test", config.email_config_id, "forward@inbound.example", "test", "dedicated", None, None
    )
    payload = (
        b'{"recipient":"forward@inbound.example","from":"security@example.test",'
        b'"TextBody":"Verification code 123456","MessageID":"provider-long-subject",'
        b'"Subject":"' + b"S" * 9_500_000 + b'"}'
    )
    assert len(payload) < email_forwarding.MAX_INBOUND_EMAIL_BYTES

    transport = httpx.ASGITransport(app=_asgi_app(agent_db, monkeypatch))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        received = await client.post(
            f"/v1/email/inbound/{config.email_config_id}?secret=secret",
            content=payload,
            headers={"content-type": "application/json"},
        )
        assert received.status_code == 200

        stored = await agent_db.email_forwarding.list_inbound_emails(address.forwarding_address_id, "o_test", 100, 0)
        assert len(stored) == 1
        assert stored[0].subject == "S" * email_forwarding._MAX_SUBJECT_BYTES

        inbox = await client.get(f"/v1/email-forwarding/addresses/{address.forwarding_address_id}/emails?limit=100")
        assert inbox.status_code == 200
        assert inbox.json()[0]["subject"] == stored[0].subject
        assert len(inbox.content) <= email_forwarding._MAX_SUBJECT_BYTES + 2 * 1024


@pytest.mark.asyncio
async def test_provider_json_verdicts_are_bounded_in_storage_and_inbox_response(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    address = await agent_db.email_forwarding.create_address(
        "o_test", config.email_config_id, "forward@inbound.example", "test", "dedicated", None, None
    )
    payload = (
        b'{"recipient":"forward@inbound.example","from":"security@example.test",'
        b'"TextBody":"Verification code 123456","MessageID":"provider-long-verdict",'
        b'"verdicts":{"SpamVerdict":"' + b"A" * 9_000_000 + b'","DMARCVerdict":{"status":"PASS"},"other":"ignored"}}'
    )
    assert len(payload) < email_forwarding.MAX_INBOUND_EMAIL_BYTES

    transport = httpx.ASGITransport(app=_asgi_app(agent_db, monkeypatch))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        received = await client.post(
            f"/v1/email/inbound/{config.email_config_id}?secret=secret",
            content=payload,
            headers={"content-type": "application/json"},
        )
        assert received.status_code == 200

        stored = await agent_db.email_forwarding.list_inbound_emails(address.forwarding_address_id, "o_test", 100, 0)
        assert len(stored) == 1
        assert stored[0].verdicts == {
            "spamverdict": "A" * email_forwarding._MAX_VERDICT_VALUE_BYTES,
            "dmarcverdict": "PASS",
        }
        assert "other" not in stored[0].verdicts
        assert all(
            len(value.encode("utf-8")) <= email_forwarding._MAX_VERDICT_VALUE_BYTES
            for value in stored[0].verdicts.values()
        )

        inbox = await client.get(f"/v1/email-forwarding/addresses/{address.forwarding_address_id}/emails?limit=100")
        assert inbox.status_code == 200
        assert inbox.json()[0]["verdicts"] == stored[0].verdicts
        assert len(inbox.content) <= 2 * 1024


@pytest.mark.asyncio
async def test_receiver_drops_html_extraction_errors_but_surfaces_database_errors(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    await agent_db.email_forwarding.create_address(
        "o_test", config.email_config_id, "forward@inbound.example", "test", "dedicated", None, None
    )
    malformed_html = (
        b"From: security@provider.test\r\n"
        b"To: forward@inbound.example\r\n"
        b"Subject: Verification code\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="part"\r\n\r\n'
        b"--part\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Your code is 482913\r\n"
        b"--part\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>&#" + (b"9" * 5000) + b";</p>\r\n--part--\r\n"
    )
    transport = httpx.ASGITransport(app=_asgi_app(agent_db, monkeypatch), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with monkeypatch.context() as extraction_failure:
            extraction_failure.setattr(
                email_forwarding, "extract_email_body", MagicMock(side_effect=ValueError("malformed HTML"))
            )
            malformed = await client.post(
                f"/v1/email/inbound/{config.email_config_id}?secret=secret",
                content=malformed_html,
                headers={"content-type": "message/rfc822"},
            )
        assert malformed.status_code == 200
        async with agent_db.Session() as session:
            assert await session.scalar(select(func.count(OrganizationInboundEmailModel.inbound_email_id))) == 0
            assert await session.scalar(select(func.count(TOTPCodeModel.totp_code_id))) == 0

        monkeypatch.setattr(
            agent_db.email_forwarding, "record_inbound", AsyncMock(side_effect=RuntimeError("database unavailable"))
        )
        database_error = await client.post(
            f"/v1/email/inbound/{config.email_config_id}?secret=secret",
            content=(
                b"From: security@provider.test\r\n"
                b"To: forward@inbound.example\r\n"
                b"Subject: Verification code\r\n\r\nYour code is 482913"
            ),
            headers={"content-type": "message/rfc822"},
        )
        assert database_error.status_code == 500


@pytest.mark.asyncio
async def test_receiver_and_management_logs_redact_body_and_receiver_secret(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_mock = MagicMock()
    monkeypatch.setattr(request_logging, "LOG", log_mock)
    monkeypatch.setattr(request_logging.settings, "LOG_RAW_API_REQUESTS", True)
    monkeypatch.setattr(request_logging.settings, "LOG_RAW_API_REQUESTS_SUCCESSFUL_READS", False)
    fastapi_app = _asgi_app(agent_db, monkeypatch, with_request_logging=True)
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/v1/email-forwarding/configs", json={})
        assert created.status_code == 201
        receiver_url = created.json()["receiver_url"]
        receiver_secret = receiver_url.partition("secret=")[2]
        received = await client.post(
            receiver_url,
            json={
                "to": "forward@inbound.example",
                "TextBody": "Verification code 987654",
                "MessageID": "provider-secret-message",
            },
        )
        assert received.status_code == 200

    assert log_mock.info.call_count == 2
    for call in log_mock.info.call_args_list:
        assert call.args[0] == "api.raw_request"
        assert call.kwargs["body"] == REDACTED
        assert call.kwargs["response_body"] == REDACTED
    logged = repr(log_mock.info.call_args_list)
    assert receiver_secret
    assert receiver_secret not in logged
    assert "987654" not in logged
    assert "provider-secret-message" not in logged


def test_all_management_and_inbox_routes_use_credential_auth_dependency() -> None:
    handlers = (
        email_routes.create_email_config,
        email_routes.rotate_email_config_secret,
        email_routes.list_email_configs,
        email_routes.delete_email_config,
        email_routes.create_forwarding_address,
        email_routes.list_forwarding_addresses,
        email_routes.list_forwarded_emails,
        email_routes.rotate_forwarding_address,
        email_routes.disable_forwarding_address,
        email_routes.enable_forwarding_address,
        email_routes.revoke_forwarding_address,
    )
    for handler in handlers:
        dependencies = {
            getattr(parameter.default, "dependency", None)
            for parameter in inspect.signature(handler).parameters.values()
        }
        assert email_routes.get_current_org_for_email_forwarding in dependencies
        assert org_auth_service.get_current_org not in dependencies


@pytest.mark.parametrize(
    "headers",
    [
        b"To: " + b'"' * 12000,
        b"To: " + b'"' * 6000 + b"\r\n " + b'"' * 6000,
        (b"X-Extra: " + b"a" * 3000 + b"\r\n") * 24,
    ],
    ids=["large-address", "folded-address", "total-headers"],
)
def test_raw_mime_rejects_oversized_headers(headers: bytes) -> None:
    with pytest.raises(ValueError, match="header"):
        email_forwarding.parse_raw_email(headers + b"\r\n\r\nVerification code 123456")


def test_raw_mime_retains_decoded_subject() -> None:
    parsed = email_forwarding.parse_raw_email(
        b"From: security@bank.example\r\nTo: forward@inbound.example\r\n"
        b"Subject: =?utf-8?b?U2lnbi1pbiBjb2Rl?=\r\n\r\nVerification code 123456"
    )
    assert parsed.subject == "Sign-in code"
    utf8_header = email_forwarding.parse_raw_email(
        "From: Sécurité <security@bank.example>\r\nTo: forward@inbound.example\r\n\r\nCode 123456".encode()
    )
    assert utf8_header.sender == "security@bank.example"


@pytest.mark.parametrize("limit", ["depth", "parts", "header", "lines"])
def test_pathological_mime_is_rejected_within_cpu_budget(limit: str) -> None:
    if limit == "depth":
        raw = (
            b"".join(
                f'Content-Type: multipart/mixed; boundary="b{index}"\r\n\r\n--b{index}\r\n'.encode()
                for index in range(900)
            )
            + b"\r\n"
            + b"xxx\r\n" * 200_000
        )
    elif limit == "parts":
        raw = (
            b'Content-Type: multipart/mixed; boundary="outer"\r\n\r\n'
            + b"--outer\r\nContent-Type: text/plain\r\n\r\nCode 123456\r\n" * 201
            + b"--outer--\r\n"
        )
    elif limit == "lines":
        raw = b'Content-Type: multipart/mixed; boundary="outer"\r\n\r\n--outer\r\n' + b"X:\n" * 350_000
    else:
        raw = (
            b'Content-Type: multipart/mixed; boundary="outer"\r\n\r\n--outer\r\n'
            + (b"X-Extra: " + b"a" * 3000 + b"\r\n") * 24
            + b"\r\nCode 123456\r\n--outer--\r\n"
        )
    assert len(raw) < 2 * 1024 * 1024
    with pytest.raises(email_forwarding.InboundEmailLimitExceeded, match=limit):
        started = time.process_time()
        try:
            email_forwarding.parse_raw_email(raw)
        finally:
            elapsed = time.process_time() - started
    assert elapsed < 1.0


def test_raw_mime_cpu_budget_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(email_forwarding, "_MAX_MIME_PARSE_CPU_SECONDS", 0)
    raw = b"From: alerts@example.com\r\nTo: forward@inbound.example\r\n\r\n" + b"body text\r\n" * 10_000
    with pytest.raises(email_forwarding.InboundEmailLimitExceeded, match="cpu_budget"):
        email_forwarding.parse_raw_email(raw)


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Código 482913".encode(), "Código 482913"),
        (b"=?utf-8?b?A?= 482913", "=?utf-8?b?A?= 482913"),
        ("Café =?utf-8?b?NDgyOTEz?=".encode(), "Café 482913"),
        (b"Raw \\uD800 =?utf-8?q?code?=", "Raw \\uD800 code"),
    ],
    ids=["raw-utf8", "malformed", "mixed", "literal-escape"],
)
def test_raw_mime_subject_decoding_preserves_body(subject: bytes, expected: str) -> None:
    parsed = email_forwarding.parse_raw_email(
        b"From: alerts@example.com\r\nTo: forward@inbound.example\r\nSubject: "
        + subject
        + b"\r\n\r\nVerification code 482913"
    )
    assert parsed.subject == expected
    assert parsed.text == "Verification code 482913"


@pytest.mark.asyncio
async def test_oversized_original_recipient_falls_back_to_forwarding_address(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(email_forwarding.app, "DATABASE", agent_db)
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    address = await agent_db.email_forwarding.create_address(
        "o_test", config.email_config_id, "forward@inbound.example", "test", "original_recipient", None, None
    )
    message = email_forwarding.parse_raw_email(
        b"From: alerts@example.com\r\nTo: forward@inbound.example\r\nX-Original-To: "
        + b"a" * 3000
        + b"@example.com\r\n\r\nVerification code 123456"
    )
    assert await email_forwarding.ingest_inbound_email(config, message) == "otp"
    raw_codes = await agent_db.otp.get_raw_otp_codes("o_test", address.email_address)
    assert len(raw_codes) == 1
    assert raw_codes[0].totp_identifier == address.email_address
    assert raw_codes[0].source == "email_forwarding"
    assert raw_codes[0].content == "Subject: \nVerification code 123456"
    assert await agent_db.otp.get_otp_codes("o_test", address.email_address) == []


def test_raw_mime_uses_compat32_address_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_structured_address_parser(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Structured address parsing must not run")

    monkeypatch.setattr(_header_value_parser, "get_address_list", reject_structured_address_parser)
    parsed = email_forwarding.parse_raw_email(
        b"From: alerts@example.com\r\nTo: employee@example.com\r\n"
        b"Delivered-To: forward@inbound.example\r\nX-Original-To: employee@example.com\r\n"
        b"\r\nVerification code 123456"
    )
    assert parsed.sender == "alerts@example.com"
    assert parsed.recipients == ["forward@inbound.example"]
    assert parsed.original_recipient == "forward@inbound.example"
    assert parsed.delivered_recipients == ["forward@inbound.example", "employee@example.com"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["mime", "html"])
async def test_receiver_keeps_event_loop_responsive_during_parsing(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    address = await agent_db.email_forwarding.create_address(
        "o_test", config.email_config_id, "forward@inbound.example", "test", "dedicated", None, None
    )
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    loop_responsive: list[bool] = []
    target = email_routes if phase == "mime" else email_forwarding
    name = "normalize_inbound_payload" if phase == "mime" else "extract_email_body"
    original = getattr(target, name)

    def gated_parse(*args: Any, **kwargs: Any) -> Any:
        loop.call_soon_threadsafe(entered.set)
        loop_responsive.append(release.wait(timeout=2))
        return original(*args, **kwargs)

    async def release_from_event_loop() -> None:
        await entered.wait()
        release.set()

    monkeypatch.setattr(target, name, gated_parse)
    heartbeat = asyncio.create_task(release_from_event_loop())
    try:
        transport = httpx.ASGITransport(app=_asgi_app(agent_db, monkeypatch))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/v1/email/inbound/{config.email_config_id}?secret=secret",
                content=b"From: security@bank.example\r\nTo: forward@inbound.example\r\n"
                b"Content-Type: text/html\r\n\r\n<p>Verification code 123456</p>",
                headers={"content-type": "message/rfc822"},
            )
        assert response.status_code == 200
        assert loop_responsive == [True], "Email parsing blocked the request event loop"
        inbox = await agent_db.email_forwarding.list_inbound_emails(address.forwarding_address_id, "o_test", 10, 0)
        assert len(inbox) == 1
    finally:
        release.set()
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


@pytest.mark.asyncio
async def test_mailgun_forward_preserves_author_delivery_and_dedup(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    address = await agent_db.email_forwarding.create_address(
        "o_test",
        config.email_config_id,
        "forward@inbound.example",
        "test",
        "original_recipient",
        None,
        ["security@bank.example"],
    )
    payload = json.loads((FIXTURES / "mailgun_forward.json").read_text())
    transport = httpx.ASGITransport(app=_asgi_app(agent_db, monkeypatch))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(2):
            response = await client.post(f"/v1/email/inbound/{config.email_config_id}?secret=secret", data=payload)
            assert response.status_code == 200
    inbox = await agent_db.email_forwarding.list_inbound_emails(address.forwarding_address_id, "o_test", 10, 0)
    assert len(inbox) == 1
    assert inbox[0].category == InboundEmailCategory.OTP
    assert inbox[0].from_address == "security@bank.example"
    assert inbox[0].original_recipient == "employee@account.example"
    assert inbox[0].external_message_id == f"{config.email_config_id}:rfc822:<mailgun-forward-1@bank.example>"
    raw_codes = await agent_db.otp.get_raw_otp_codes("o_test", "employee@account.example")
    assert len(raw_codes) == 1
    assert raw_codes[0].totp_identifier == "employee@account.example"
    assert raw_codes[0].source == "email_forwarding"
    assert raw_codes[0].content == "Subject: Sign-in code\nVerification code 731204"
    assert await agent_db.otp.get_otp_codes("o_test", "employee@account.example") == []


@pytest.mark.asyncio
async def test_provider_x_original_to_selects_original_recipient_identifier(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    address = await agent_db.email_forwarding.create_address(
        "o_test",
        config.email_config_id,
        "forward@inbound.example",
        "test",
        ForwardingMatchMode.ORIGINAL_RECIPIENT,
        None,
        ["security@bank.example"],
    )
    payload = {
        "recipient": address.email_address,
        "from": "security@bank.example",
        "headers": [{"Name": "X-Original-To", "Value": "employee@account.example"}],
        "TextBody": "Verification code 731204",
        "MessageID": "provider-x-original-to",
    }
    transport = httpx.ASGITransport(app=_asgi_app(agent_db, monkeypatch))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/v1/email/inbound/{config.email_config_id}?secret=secret",
            json=payload,
        )
    assert response.status_code == 200
    inbox = await agent_db.email_forwarding.list_inbound_emails(address.forwarding_address_id, "o_test", 10, 0)
    assert inbox[0].original_recipient == "employee@account.example"
    raw_codes = await agent_db.otp.get_raw_otp_codes("o_test", "employee@account.example")
    assert len(raw_codes) == 1
    assert raw_codes[0].totp_identifier == "employee@account.example"


@pytest.mark.asyncio
async def test_receiver_drops_excessive_recipients_before_lookup(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    lookup = AsyncMock(side_effect=AssertionError("Excessive recipients reached the database"))
    monkeypatch.setattr(agent_db.email_forwarding, "find_address", lookup)
    recipients = [f"recipient-{index}@inbound.example" for index in range(101)]
    transport = httpx.ASGITransport(app=_asgi_app(agent_db, monkeypatch))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/v1/email/inbound/{config.email_config_id}?secret=secret",
            json={"from": "security@bank.example", "to": ",".join(recipients), "text": "Code 123456"},
        )
    assert response.status_code == 200
    # The SES path can override normalized recipients with its envelope, so ingestion
    # must enforce the same bound even when normalization has already completed.
    message = _provider_message(recipients[0], "too-many-recipients")
    message.recipients = recipients
    assert await email_forwarding.ingest_inbound_email(config, message) == "dropped-invalid"
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_message_ids_are_bounded_without_losing_deduplication(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(email_forwarding.app, "DATABASE", agent_db)
    config = await agent_db.email_forwarding.create_config(
        "o_test", email_forwarding.hash_webhook_secret("secret"), 500, True
    )
    address = await agent_db.email_forwarding.create_address(
        "o_test", config.email_config_id, "forward@inbound.example", "test", "dedicated", None, None
    )
    for suffix in ("one", "two"):
        message = _provider_message(address.email_address, "é" * 3000 + suffix)
        message.external_message_id_requires_namespace = True
        assert await email_forwarding.ingest_inbound_email(config, message) == "otp"
        assert await email_forwarding.ingest_inbound_email(config, message) == "deduped"
    inbox = await agent_db.email_forwarding.list_inbound_emails(address.forwarding_address_id, "o_test", 10, 0)
    ids = [row.external_message_id for row in inbox]
    assert len(ids) == len(set(ids)) == 2
    assert all(value and len(value.encode()) <= 512 for value in ids)
