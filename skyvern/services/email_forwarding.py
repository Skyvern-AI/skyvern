from __future__ import annotations

import asyncio
import codecs
import hashlib
import hmac
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from email import policy
from email.feedparser import FeedParser
from email.header import Header, decode_header
from email.message import Message
from email.utils import getaddresses
from html.parser import HTMLParser
from typing import Any

import structlog
import tldextract

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.schemas.email_forwarding import (
    EmailConfigInternal,
    ForwardingAddress,
    ForwardingAddressStatus,
    ForwardingMatchMode,
    InboundEmailCategory,
)
from skyvern.forge.sdk.utils.sanitization import sanitize_postgres_text
from skyvern.utils.email_validation import SAFE_EMAIL_ADDRESS_PATTERN, normalize_email_address

LOG = structlog.get_logger()
MAX_INBOUND_EMAIL_BYTES = 10 * 1024 * 1024
INBOX_BODY_MAX_LENGTH = 16 * 1024
_MAX_SUBJECT_BYTES = 1024
_MAX_LINKS = 50
_MAX_LINK_URL_BYTES = 2 * 1024
_MAX_LINK_TEXT_BYTES = 1024
_MAX_LINKS_JSON_BYTES = 16 * 1024
_HTML_PARSE_CHUNK_SIZE = 4 * 1024
_MAX_MIME_DEPTH = 10
_MAX_MIME_PARTS = 200
_MAX_MIME_LINES = 250_000
_MAX_MIME_PARSE_CPU_SECONDS = 2.0
_MAX_HEADER_BYTES = 64 * 1024
_MAX_HEADER_VALUE_BYTES = 8 * 1024
_MAX_TOTAL_HEADER_BYTES = 256 * 1024
_MAX_EMAIL_ADDRESS_BYTES = 254
_MAX_RECIPIENTS = 100
_MAX_EXTERNAL_MESSAGE_ID_BYTES = 512
_MAX_VERDICT_VALUE_BYTES = 32
_VERDICT_KEYS = {"spamverdict", "virusverdict", "spfverdict", "dkimverdict", "dmarcverdict"}
_PROVIDER_CONFIRMATION_SENDERS = {"forwarding-noreply@google.com", "mail-noreply@google.com"}
_URL_PATTERN = re.compile(r'https?://[^\s"\'<>]+', re.IGNORECASE)
_ENCODED_WORD_PATTERN = re.compile(r"=\?[^?\s]+\?[bq]\?[^?\s]*\?=", re.IGNORECASE)
_PSL_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)


def _utf8_prefix(value: str, max_bytes: int) -> str:
    return value.encode("utf-8", errors="replace")[:max_bytes].decode("utf-8", errors="ignore")


def _bound_links_json(links: list[dict[str, str]]) -> list[dict[str, str]]:
    bounded: list[dict[str, str]] = []
    total_bytes = 2  # []
    for link in links:
        serialized_bytes = len(json.dumps(link, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        next_total = total_bytes + serialized_bytes + (1 if bounded else 0)
        if next_total > _MAX_LINKS_JSON_BYTES:
            continue
        bounded.append(link)
        total_bytes = next_total
    return bounded


class InboundEmailLimitExceeded(ValueError):
    def __init__(self, limit: str) -> None:
        LOG.warning("Inbound email parsing limit exceeded", limit=limit)
        super().__init__(f"Inbound email {limit} limit exceeded")


class _LimitedFeedParser(FeedParser):
    _msgstack: list[Message]

    def __init__(self) -> None:
        super().__init__(policy=policy.compat32)
        self._part_count = 0
        self._total_header_bytes = 0

    # These private stdlib hooks have been stable since Python 3.3.
    def _new_message(self) -> None:
        if len(self._msgstack) > _MAX_MIME_DEPTH:
            raise InboundEmailLimitExceeded("depth")
        if self._part_count >= _MAX_MIME_PARTS:
            raise InboundEmailLimitExceeded("parts")
        self._part_count += 1
        super()._new_message()  # type: ignore[misc]

    def _parse_headers(self, lines: list[str]) -> None:
        block_size = 0
        field_size = 0
        for line in lines:
            size = len(line)
            block_size += size
            self._total_header_bytes += size
            field_size = field_size + size if line.startswith((" ", "\t")) else size
            if (
                field_size > _MAX_HEADER_VALUE_BYTES
                or block_size > _MAX_HEADER_BYTES
                or self._total_header_bytes > _MAX_TOTAL_HEADER_BYTES
            ):
                raise InboundEmailLimitExceeded("header")
        super()._parse_headers(lines)  # type: ignore[misc]


@dataclass
class NormalizedInboundEmail:
    recipients: list[str]
    sender: str
    subject: str
    text: str
    html: str
    external_message_id: str | None
    external_message_id_requires_namespace: bool = False
    return_path: str | None = None
    original_recipient: str | None = None
    delivered_recipients: list[str] = field(default_factory=list)
    verdicts: dict[str, str] | None = None


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self._ignored_depth = 0
        self._link_url: str | None = None
        self._link_text: list[str] = []
        self._link_text_bytes = 0
        self._seen_link_urls: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in {"br", "p", "div", "li"}:
            self.text_parts.append("\n")
        if tag == "a":
            self.text_parts.append(" ")
            href = dict(attrs).get("href")
            # Bounded during collection: a crafted message with hundreds of thousands
            # of anchors must not materialize them all before a cap is applied.
            if (
                href
                and href.casefold().startswith(("http://", "https://"))
                and len(href.encode("utf-8", errors="replace")) <= _MAX_LINK_URL_BYTES
                and href not in self._seen_link_urls
                and len(self.links) < _MAX_LINKS
            ):
                self._link_url = href
                self._link_text = []
                self._link_text_bytes = 0

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if tag == "a" and self._link_url:
            self._seen_link_urls.add(self._link_url)
            self.links.append({"url": self._link_url, "text": "".join(self._link_text).strip()})
            self._link_url = None
            self._link_text = []
            self._link_text_bytes = 0

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        self.text_parts.append(data)
        if self._link_url and self._link_text_bytes < _MAX_LINK_TEXT_BYTES:
            text = _utf8_prefix(data, _MAX_LINK_TEXT_BYTES - self._link_text_bytes)
            self._link_text.append(text)
            self._link_text_bytes += len(text.encode("utf-8"))


def hash_webhook_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def webhook_secret_matches(config: EmailConfigInternal, secret: str | None) -> bool:
    if not secret or not config.webhook_secret_hash:
        return False
    return hmac.compare_digest(config.webhook_secret_hash, hash_webhook_secret(secret))


def _addresses(*values: str | None) -> list[str]:
    parsed = []
    sanitized_values = [sanitize_postgres_text(value) for value in values if value]
    sizes = [len(value.encode()) for value in sanitized_values]
    if (
        len(sizes) > _MAX_RECIPIENTS
        or sum(sizes) > _MAX_HEADER_BYTES
        or any(size > _MAX_HEADER_VALUE_BYTES for size in sizes)
    ):
        raise ValueError("Inbound email address header limit exceeded")
    for _, address in getaddresses(sanitized_values):
        normalized = normalize_email_address(address)
        if SAFE_EMAIL_ADDRESS_PATTERN.fullmatch(normalized) and len(normalized.encode()) <= _MAX_EMAIL_ADDRESS_BYTES:
            parsed.append(normalized)
            if len(parsed) > _MAX_RECIPIENTS:
                raise ValueError("Inbound email recipient limit exceeded")
    return list(dict.fromkeys(parsed))


def _first_address(*values: str | None) -> str | None:
    addresses = _addresses(*values)
    return addresses[0] if addresses else None


def _decode_email_text(payload: bytes, charset: str) -> str:
    try:
        codec = codecs.lookup(charset)
        # These codecs can exceed the CPU budget within a single decode call or always fail.
        if codec.name not in {"punycode", "idna", "undefined"}:
            text = payload.decode(codec.name, errors="replace")
            # Some codecs emit surrogates; combine valid pairs and replace unpaired ones.
            return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    except (LookupError, UnicodeError):
        pass
    return payload.decode("utf-8", errors="replace")


def _message_text(message: Message) -> tuple[str, str]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    part_count = 0
    traversal_limit: str | None = None

    def visit(part: Message, depth: int) -> None:
        nonlocal part_count, traversal_limit
        if traversal_limit is not None:
            return
        if depth > _MAX_MIME_DEPTH:
            traversal_limit = "depth"
            return
        if part_count >= _MAX_MIME_PARTS:
            traversal_limit = "part_count"
            return
        part_count += 1
        if part.get_filename() or part.get_content_disposition() == "attachment":
            return
        if part.is_multipart():
            payload = part.get_payload()
            if isinstance(payload, list):
                for child in payload:
                    if isinstance(child, Message):
                        visit(child, depth + 1)
                        if traversal_limit is not None:
                            break
            return
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            return
        payload = part.get_payload(decode=True)
        payload_bytes = payload if isinstance(payload, bytes) else b""
        charset = part.get_content_charset() or "utf-8"
        content = _decode_email_text(payload_bytes, charset)
        (plain_parts if content_type == "text/plain" else html_parts).append(content)

    visit(message, 0)
    if traversal_limit is not None:
        LOG.warning(
            "Stopped MIME traversal at safety limit",
            limit=traversal_limit,
            max_depth=_MAX_MIME_DEPTH,
            max_parts=_MAX_MIME_PARTS,
            parts_visited=part_count,
        )
    return "\n".join(plain_parts), "\n".join(html_parts)


def _message_header_values(message: Message, *names: str) -> list[str]:
    values: list[str] = []
    for name in names:
        values.extend(str(value) for value in message.get_all(name, []))
    return values


def _validate_raw_headers(raw_mime: bytes) -> None:
    total_size = 0
    field_size = 0
    # Continuation lines share their field's limit even in this bounded prefix.
    for line in raw_mime[: _MAX_HEADER_BYTES + 1].splitlines(keepends=True):
        if line in (b"\r\n", b"\n", b"\r"):
            return
        total_size += len(line)
        field_size = field_size + len(line) if line.startswith((b" ", b"\t")) else len(line)
        if total_size > _MAX_HEADER_BYTES or field_size > _MAX_HEADER_VALUE_BYTES:
            raise InboundEmailLimitExceeded("header")


def _decoded_subject(value: str | Header) -> str:
    recovered = str(value)
    parts: list[str] = []
    try:
        if isinstance(value, Header):
            recovered = "".join(
                _decode_email_text(part, charset or "utf-8") if isinstance(part, bytes) else part
                for part, charset in decode_header(value)
            )
        end = 0
        for match in _ENCODED_WORD_PATTERN.finditer(recovered):
            unencoded = recovered[end : match.start()]
            if not end or unencoded.strip(" \t\r\n"):
                parts.append(unencoded)
            for part, charset in decode_header(match.group()):
                parts.append(_decode_email_text(part, charset or "utf-8") if isinstance(part, bytes) else part)
            end = match.end()
        parts.append(recovered[end:])
    except Exception:
        return sanitize_postgres_text(recovered)
    return sanitize_postgres_text("".join(parts))


def parse_raw_email(raw_mime: bytes) -> NormalizedInboundEmail:
    started = time.thread_time()
    if raw_mime.count(b"\n") + raw_mime.count(b"\r") - raw_mime.count(b"\r\n") > _MAX_MIME_LINES:
        raise InboundEmailLimitExceeded("lines")
    _validate_raw_headers(raw_mime)
    parser = _LimitedFeedParser()
    decoded = raw_mime.decode("ascii", "surrogateescape")
    for offset in range(0, len(decoded), 64 * 1024):
        parser.feed(decoded[offset : offset + 64 * 1024])
        if time.thread_time() - started > _MAX_MIME_PARSE_CPU_SECONDS:
            raise InboundEmailLimitExceeded("cpu_budget")
    message = parser.close()
    if time.thread_time() - started > _MAX_MIME_PARSE_CPU_SECONDS:
        raise InboundEmailLimitExceeded("cpu_budget")
    text, html = _message_text(message)
    routing_header_groups = [
        _addresses(*_message_header_values(message, name))
        for name in ("Delivered-To", "X-Forwarded-To", "Resent-To", "To")
    ]
    recipients = next((group for group in routing_header_groups if group), [])
    original_header_groups = [
        _addresses(*_message_header_values(message, name))
        for name in ("Delivered-To", "X-Original-To", "X-Forwarded-To", "Resent-To")
    ]
    original_headers = [recipient for group in original_header_groups for recipient in group]
    transport_id = message.get("X-Skyvern-External-Message-ID") or message.get("X-SES-Message-ID")
    rfc_message_id = message.get("Message-ID")
    external_id = transport_id or rfc_message_id
    return NormalizedInboundEmail(
        recipients=recipients,
        sender=_first_address(*_message_header_values(message, "From")) or "",
        subject=_decoded_subject(message.get("Subject", "")),
        text=sanitize_postgres_text(text),
        html=sanitize_postgres_text(html),
        external_message_id=sanitize_postgres_text(str(external_id).strip()) if external_id else None,
        external_message_id_requires_namespace=external_id is not None,
        return_path=_first_address(*_message_header_values(message, "Return-Path")),
        original_recipient=_first_address(*original_headers),
        delivered_recipients=_addresses(*original_headers),
    )


def _field(data: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = data.get(name)
        if isinstance(value, str):
            return value
    return ""


def _provider_headers(data: Mapping[str, Any]) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    headers = data.get("Headers") or data.get("headers")
    if not headers:
        headers = data.get("message-headers")
        if isinstance(headers, str):
            headers = json.loads(headers)
    if isinstance(headers, str):
        headers = [line.split(":", 1) for line in headers.splitlines() if ":" in line]
    total_size = 0
    for header in headers if isinstance(headers, list) else []:
        if isinstance(header, dict):
            name, value = header.get("Name"), header.get("Value")
        elif isinstance(header, (list, tuple)) and len(header) == 2:
            name, value = header
        else:
            continue
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        name = sanitize_postgres_text(name)
        value = sanitize_postgres_text(value)
        size = len(name.encode()) + len(value.encode())
        total_size += size
        if size > _MAX_HEADER_VALUE_BYTES or total_size > _MAX_HEADER_BYTES:
            raise ValueError("Inbound email provider header limit exceeded")
        values.setdefault(name.strip().casefold(), []).append(value.strip())
    return values


def _verdict_status(value: Any) -> str | None:
    """Flatten one provider verdict to its status string.

    SES receipt notifications nest verdicts as ``{"status": "FAIL"}`` while flat
    providers send plain strings. ``None`` means the provider sent no verdict.
    Any other shape is unreadable, so it fails closed as ``"fail"`` instead of
    silently passing the spam/virus gate.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        status = value.get("status")
        if isinstance(status, str):
            return status
    return "fail"


def normalize_provider_payload(data: Mapping[str, Any]) -> NormalizedInboundEmail:
    headers = _provider_headers(data)
    envelope: Mapping[str, Any] = {}
    envelope_value = data.get("envelope")
    if isinstance(envelope_value, str):
        try:
            parsed_envelope = json.loads(envelope_value)
            if isinstance(parsed_envelope, dict):
                envelope = parsed_envelope
        except json.JSONDecodeError:
            pass
    elif isinstance(envelope_value, dict):
        envelope = envelope_value
    envelope_to = envelope.get("to")
    envelope_recipient = (
        ",".join(str(value) for value in envelope_to) if isinstance(envelope_to, list) else str(envelope_to or "")
    )
    original_envelope = _field(data, "OriginalRecipient", "original_recipient", "original-recipient")
    smtp_recipients = _addresses(original_envelope, _field(data, "recipient"), envelope_recipient)
    routing_recipients = _addresses(
        _field(data, "Delivered-To", "delivered-to", "delivered_to"),
        *headers.get("delivered-to", []),
        *headers.get("x-forwarded-to", []),
        *headers.get("resent-to", []),
    )
    delivered_recipients = _addresses(
        _field(data, "Delivered-To", "delivered-to", "delivered_to"),
        *headers.get("delivered-to", []),
        *headers.get("x-original-to", []),
        *headers.get("x-forwarded-to", []),
        *headers.get("resent-to", []),
    )
    to_recipients = _addresses(
        _field(data, "To", "to"),
        *headers.get("to", []),
    )
    recipients = smtp_recipients or routing_recipients or to_recipients
    sender_value = (
        _field(data, "From", "from", "FromFull")
        or next(iter(headers.get("from", [])), "")
        or _field(data, "sender")
        or str(envelope.get("from") or "")
    )
    original = next(iter([*smtp_recipients, *delivered_recipients]), None)
    transport_id = _field(data, "MessageID", "message_id", "event_id")
    rfc_message_id = next(iter(headers.get("message-id", [])), "") or _field(
        data, "Message-Id", "Message-ID", "message-id"
    )
    external_id = transport_id or rfc_message_id
    raw_verdicts = data.get("verdicts")
    verdicts: dict[str, str] | None = None
    if isinstance(raw_verdicts, Mapping):
        verdicts = {}
        for key, value in raw_verdicts.items():
            normalized_key = str(key).casefold()
            if normalized_key not in _VERDICT_KEYS:
                continue
            status = _verdict_status(value)
            if status is not None:
                verdicts[normalized_key] = _utf8_prefix(status, _MAX_VERDICT_VALUE_BYTES)
    elif raw_verdicts is not None:
        # A present but unreadable verdict container fails closed, exactly like an
        # unreadable per-key value: the provider asserted verdicts we cannot read.
        verdicts = {"spamverdict": "fail", "virusverdict": "fail"}
    return NormalizedInboundEmail(
        recipients=recipients,
        sender=(_addresses(sender_value) or [""])[0],
        subject=sanitize_postgres_text(_field(data, "Subject", "subject")),
        text=sanitize_postgres_text(_field(data, "TextBody", "body-plain", "text")),
        html=sanitize_postgres_text(_field(data, "HtmlBody", "body-html", "html")),
        external_message_id=sanitize_postgres_text(external_id.strip()) or None,
        external_message_id_requires_namespace=not transport_id and bool(rfc_message_id),
        return_path=_first_address(
            _field(data, "ReturnPath", "Return-Path", "sender") or str(envelope.get("from") or "")
        ),
        original_recipient=original,
        delivered_recipients=delivered_recipients,
        verdicts=verdicts,
    )


def normalize_inbound_payload(
    content_type: str, body: bytes, fields: Mapping[str, Any] | None = None
) -> NormalizedInboundEmail:
    media_type = content_type.partition(";")[0].strip().casefold()
    if media_type == "message/rfc822" or not media_type:
        return parse_raw_email(body)
    if fields is not None:
        return normalize_provider_payload(fields)
    if media_type == "application/json":
        decoded = json.loads(body)
        if not isinstance(decoded, dict):
            raise ValueError("Inbound JSON payload must be an object")
        return normalize_provider_payload(decoded)
    raise ValueError(f"Unsupported inbound content type: {media_type}")


def extract_email_body(message: NormalizedInboundEmail) -> tuple[str, list[dict[str, str]]]:
    html_parser = _HTMLTextExtractor()
    if message.html:
        started = time.thread_time()
        for offset in range(0, len(message.html), _HTML_PARSE_CHUNK_SIZE):
            html_parser.feed(message.html[offset : offset + _HTML_PARSE_CHUNK_SIZE])
            if time.thread_time() - started > _MAX_MIME_PARSE_CPU_SECONDS:
                raise InboundEmailLimitExceeded("HTML CPU")
        html_parser.close()
        if time.thread_time() - started > _MAX_MIME_PARSE_CPU_SECONDS:
            raise InboundEmailLimitExceeded("HTML CPU")
    body = message.text if message.text.strip() else "".join(html_parser.text_parts)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    # The parser already deduplicates and stops at the cap; scan the body the same
    # way instead of materializing every URL candidate first.
    deduped = {link["url"]: link for link in html_parser.links}
    for match in _URL_PATTERN.finditer(body):
        if len(deduped) >= _MAX_LINKS:
            break
        if match.end() - match.start() > _MAX_LINK_URL_BYTES:
            continue
        url = match.group(0).rstrip(".,;:!?")
        if len(url.encode("utf-8", errors="replace")) > _MAX_LINK_URL_BYTES:
            continue
        deduped.setdefault(url, {"url": url, "text": ""})
    return body, list(deduped.values())


def _registrable_domain(value: str) -> str:
    host = value.rsplit("@", 1)[-1].strip().casefold().rstrip(".")
    extracted = _PSL_EXTRACT(host)
    return extracted.top_domain_under_public_suffix or host


def _sender_allowed(sender: str, return_path: str | None, allowlist: list[str] | None) -> bool:
    """Anti-noise filter over sender-asserted identity; NOT an authentication boundary.

    ``sender`` (From) and ``return_path`` are whatever the forwarding hop asserted, and
    this receiver does not verify DKIM/SPF itself: forwarding rewrites the envelope, so
    the original author survives only in these headers. An absent allowlist accepts
    every sender, and a sender who can already reach the secret receiver address can
    satisfy an exact entry by asserting it. The secret URL, the registered-address
    lookup, and run-time identifier matching are the authentication layers.
    """
    if not allowlist:
        return True
    sender_addresses = {normalize_email_address(value) for value in (sender, return_path) if value}
    sender_domains = {_registrable_domain(value) for value in sender_addresses}
    for entry in allowlist:
        normalized_entry = normalize_email_address(entry)
        if "@" in normalized_entry:
            if normalized_entry in sender_addresses:
                return True
        elif _registrable_domain(normalized_entry) in sender_domains:
            return True
    return False


def _original_recipient(message: NormalizedInboundEmail, address: ForwardingAddress) -> str | None:
    """Pick the mailbox the message was originally delivered to, if it survives.

    Only the provider envelope recipient and MTA-stamped delivery headers
    (``delivered_recipients``) are eligible; freely composed headers such as ``To``
    never become an OTP identifier. For mail sent directly to the forwarding address
    these stamps are still sender-asserted: the secret address is the routing
    boundary, and the identifier must still match a registered credential before any
    waiting run consumes the code.
    """
    candidates = [message.original_recipient, *message.delivered_recipients]
    return next((candidate for candidate in candidates if candidate and candidate != address.email_address), None)


def _otp_parser_content(subject: str, body_text: str, links: list[dict[str, str]]) -> str:
    """Preserve complete link targets within the parser's content limit."""
    visible = f"Subject: {subject}\n{body_text}"
    limit = max(0, settings.TOTP_RAW_CONTENT_MAX_LENGTH)
    prefix = "\nLinks:\n"
    remaining = limit - min(len(visible), limit // 2) - len(prefix)
    targets: list[str] = []
    for link in links:
        target = f"{link.get('text', '')[:120]}\n{link['url']}\n"
        if len(target) <= remaining:
            targets.append(target)
            remaining -= len(target)
    suffix = prefix + "".join(targets) if targets else ""
    return sanitize_postgres_text(visible[: limit - len(suffix)] + suffix)


async def ingest_inbound_email(
    config: EmailConfigInternal,
    message: NormalizedInboundEmail,
) -> str:
    try:
        recipients = _addresses(*message.recipients)
    except ValueError:
        LOG.info("Dropped inbound email with excessive recipients", email_config_id=config.email_config_id)
        return "dropped-invalid"
    address = await app.DATABASE.email_forwarding.find_address(config.email_config_id, recipients)
    if address is None or address.status not in {ForwardingAddressStatus.PENDING, ForwardingAddressStatus.ACTIVE}:
        return "dropped-unregistered"

    try:
        body_text, links = await asyncio.to_thread(extract_email_body, message)
    except Exception:  # noqa: BLE001 - extraction parses untrusted email input.
        LOG.warning(
            "Dropped malformed inbound email during body extraction",
            email_config_id=config.email_config_id,
            exc_info=True,
        )
        return "dropped-invalid"
    body_text = sanitize_postgres_text(body_text)
    links = _bound_links_json(
        [
            {
                "url": sanitize_postgres_text(link.get("url", "")),
                "text": sanitize_postgres_text(link.get("text", "")),
            }
            for link in links
        ]
    )
    original_recipient = _original_recipient(message, address)
    original_recipient = sanitize_postgres_text(original_recipient) if original_recipient else None
    sender = sanitize_postgres_text(message.sender)
    return_path = sanitize_postgres_text(message.return_path) if message.return_path else None
    subject = _utf8_prefix(sanitize_postgres_text(message.subject), _MAX_SUBJECT_BYTES)
    verdicts = {}
    for key, value in (message.verdicts or {}).items():
        normalized_key = sanitize_postgres_text(str(key)).casefold()
        if normalized_key not in _VERDICT_KEYS:
            continue
        verdicts[normalized_key] = _utf8_prefix(sanitize_postgres_text(str(value)), _MAX_VERDICT_VALUE_BYTES)
    normalized_verdicts = {key.casefold(): value.casefold() for key, value in verdicts.items()}

    category = (
        InboundEmailCategory.PROVIDER_CONFIRMATION
        if sender.casefold() in _PROVIDER_CONFIRMATION_SENDERS
        else InboundEmailCategory.OTP
    )
    if normalized_verdicts.get("virusverdict") == "fail" or (
        config.drop_flagged_spam and normalized_verdicts.get("spamverdict") == "fail"
    ):
        category = InboundEmailCategory.DROPPED_SPAM
    elif category != InboundEmailCategory.PROVIDER_CONFIRMATION and not _sender_allowed(
        sender, return_path, address.sender_allowlist
    ):
        category = InboundEmailCategory.DROPPED_OTHER

    identifier = address.email_address
    if address.match_mode == ForwardingMatchMode.ORIGINAL_RECIPIENT and original_recipient:
        identifier = original_recipient
    identifier = sanitize_postgres_text(identifier)
    parser_content = _otp_parser_content(subject, body_text, links)
    writes_otp = category == InboundEmailCategory.OTP
    external_message_id = sanitize_postgres_text(message.external_message_id) if message.external_message_id else None
    if external_message_id and message.external_message_id_requires_namespace:
        # Raw-MIME headers and RFC Message-ID fallbacks are sender-controlled. Namespace them by config
        # so they can only dedupe deliveries within the authenticated receiver that accepted the message.
        external_message_id = sanitize_postgres_text(f"{config.email_config_id}:rfc822:{external_message_id}")
    if external_message_id and len(external_message_id.encode()) > _MAX_EXTERNAL_MESSAGE_ID_BYTES:
        # PostgreSQL btree entries have a byte limit. Preserve distinct IDs and
        # receiver-scoped deduplication without persisting an unbounded header.
        external_message_id = f"sha256:{hashlib.sha256(external_message_id.encode()).hexdigest()}"
    record_status, _, _ = await app.DATABASE.email_forwarding.record_inbound(
        organization_id=config.organization_id,
        email_config_id=config.email_config_id,
        daily_inbound_cap=config.daily_inbound_cap,
        forwarding_address_id=address.forwarding_address_id,
        external_message_id=external_message_id,
        from_address=sender,
        original_recipient=original_recipient,
        subject=subject,
        body_text=body_text[:INBOX_BODY_MAX_LENGTH],
        links=links,
        category=category,
        verdicts=verdicts or None,
        totp_identifier=identifier if writes_otp else None,
        parser_content=parser_content if writes_otp else None,
    )
    if record_status == "deduped":
        return "deduped"
    if record_status == "over-cap":
        LOG.info(
            "Dropped inbound email over cap",
            organization_id=config.organization_id,
            email_config_id=config.email_config_id,
            forwarding_address_id=address.forwarding_address_id,
        )
        return "dropped-cap"
    if record_status == "inactive":
        return "dropped-unregistered"
    if record_status == "drop-cap":
        LOG.info(
            "Dropped inbound email after diagnostic cap",
            organization_id=config.organization_id,
            email_config_id=config.email_config_id,
            forwarding_address_id=address.forwarding_address_id,
            outcome=category.value,
        )
        return category.value
    if record_status != "recorded":
        raise RuntimeError(f"Unexpected inbound email record status: {record_status}")
    LOG.info(
        "Processed inbound email",
        organization_id=config.organization_id,
        email_config_id=config.email_config_id,
        forwarding_address_id=address.forwarding_address_id,
        # Sender-controlled and may embed a live secret; log only a bounded digest.
        external_message_id_sha=hashlib.sha256(external_message_id.encode()).hexdigest()[:12]
        if external_message_id
        else None,
        outcome=category.value,
    )
    return category.value
