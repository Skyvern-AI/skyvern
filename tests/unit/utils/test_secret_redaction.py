import base64
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.artifact import manager as artifact_manager
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.utils import secret_redaction as secret_redaction_module
from skyvern.utils.secret_redaction import (
    REDACTED_SECRET_PLACEHOLDER,
    collect_redactable_secret_values,
    expand_secret_encodings,
    redact_har_bytes,
    redact_multi_field_totp_artifact_bytes,
    redact_secrets_from_bytes,
    redact_secrets_from_text,
)


def test_collect_redactable_secret_values_filters_noise() -> None:
    values = collect_redactable_secret_values(
        {
            "short_numeric": "587",
            "otp": "123456",
            "minimum": "abcd",
            "too_short": "abc",
            "non_str": 123456,
            "placeholder_x1y2_password": "placeholder_x1y2_password",
            "totp_sentinel": "BW_TOTP",
        }
    )

    assert values == {"123456", "abcd"}


def test_collect_redactable_secret_values_skips_short_sensitive_keyed_values() -> None:
    assert collect_redactable_secret_values({"placeholder_ab12_card_cvv": "123"}) == set()
    assert collect_redactable_secret_values({"placeholder_ab12": "587"}) == set()


def test_collect_redactable_secret_values_collects_short_known_otp_values() -> None:
    values = collect_redactable_secret_values(
        {"placeholder_ab12": "587"},
        otp_values=["4821", "123", "BW_TOTP", "placeholder_ab12"],
    )

    assert values == {"4821"}


def test_collect_redactable_secret_values_keeps_real_placeholder_prefixed_secret_values() -> None:
    values = collect_redactable_secret_values({"placeholder_ab12": "placeholder_prodtoken"})

    assert values == {"placeholder_prodtoken"}
    # Only the ids the run registered are exempt, so a secret that merely looks like one is scrubbed.
    assert redact_secrets_from_text("placeholder_prodtoken", values) == REDACTED_SECRET_PLACEHOLDER
    assert (
        redact_secrets_from_text("placeholder_prodtoken", values, placeholder_ids={"placeholder_ab12"})
        == REDACTED_SECRET_PLACEHOLDER
    )


def test_collect_redactable_secret_values_skips_values_equal_to_placeholder_keys() -> None:
    values = collect_redactable_secret_values(
        {
            "placeholder_ab12": "ordinary-secret",
            "placeholder_cd34": "placeholder_ab12",
        }
    )

    assert values == {"ordinary-secret"}


def test_redact_secrets_from_text_replaces_encoded_variants() -> None:
    secret = 'p a"s<&'
    text = "\n".join(expand_secret_encodings(secret))

    result = redact_secrets_from_text(text, {secret})

    assert secret not in result
    assert result.splitlines() == [REDACTED_SECRET_PLACEHOLDER] * len(expand_secret_encodings(secret))


def test_redact_secrets_from_text_replaces_longest_secret_first() -> None:
    assert redact_secrets_from_text("hunter21", {"hunter2", "hunter21"}) == REDACTED_SECRET_PLACEHOLDER


def test_redact_secrets_from_text_preserves_placeholder_tokens() -> None:
    result = redact_secrets_from_text(
        "placeholder_ab12_password pass", {"pass"}, placeholder_ids={"placeholder_ab12_password"}
    )

    assert result == f"placeholder_ab12_password {REDACTED_SECRET_PLACEHOLDER}"


def test_redact_secrets_from_text_redacts_a_secret_behind_an_unregistered_placeholder_prefix() -> None:
    """SKY-17864: the exemption used to key on the ``placeholder_`` shape, so a template that
    rendered a secret straight after the literal prefix was never scrubbed.
    """
    leak = "Use placeholder_hunter2x9 now"
    expected = f"Use placeholder_{REDACTED_SECRET_PLACEHOLDER} now"

    assert redact_secrets_from_text(leak, {"hunter2x9"}) == expected
    # Still scrubbed when the run has registered ids — just not this one.
    assert redact_secrets_from_text(leak, {"hunter2x9"}, placeholder_ids={"placeholder_ab12_username"}) == expected


def test_redact_secrets_from_text_keeps_a_registered_id_whose_suffix_matches_a_secret() -> None:
    """What the exemption is for: a credential whose value is literally "password" must not rewrite
    the field name out of a token the run still has to resolve.
    """
    text = "type placeholder_ab12_password into the form"

    assert redact_secrets_from_text(text, {"password"}, placeholder_ids={"placeholder_ab12_password"}) == text
    # Longest id first, or the shorter id matches and leaves "_password" exposed to the scrub.
    assert (
        redact_secrets_from_text(text, {"password"}, placeholder_ids={"placeholder_ab12", "placeholder_ab12_password"})
        == text
    )
    assert redact_secrets_from_text(text, {"password"}) == (
        f"type placeholder_ab12_{REDACTED_SECRET_PLACEHOLDER} into the form"
    )


def test_redact_secrets_from_bytes_forwards_the_placeholder_exemption() -> None:
    data = b"placeholder_ab12_password then placeholder_hunter2x9"

    assert (
        redact_secrets_from_bytes(data, {"password", "hunter2x9"}, placeholder_ids={"placeholder_ab12_password"})
        == f"placeholder_ab12_password then placeholder_{REDACTED_SECRET_PLACEHOLDER}".encode()
    )


def test_redact_secrets_from_text_anchors_short_secret_variants() -> None:
    result = redact_secrets_from_text("password wordpress word word. =word&", {"word"})

    assert result == (
        "password wordpress "
        f"{REDACTED_SECRET_PLACEHOLDER} {REDACTED_SECRET_PLACEHOLDER}. ={REDACTED_SECRET_PLACEHOLDER}&"
    )


def test_redact_secrets_from_text_can_match_embedded_short_secret_variants() -> None:
    assert redact_secrets_from_text("AUTH&lt;ab", {"<ab"}) == "AUTH&lt;ab"
    assert redact_secrets_from_text("AUTH&lt;ab", {"<ab"}, match_inside_words=True) == (
        f"AUTH{REDACTED_SECRET_PLACEHOLDER}"
    )
    assert redact_secrets_from_text("ERR1234", {"1234"}, match_inside_words=True) == (
        f"ERR{REDACTED_SECRET_PLACEHOLDER}"
    )
    assert redact_secrets_from_text("placeholder_1234", {"1234"}, match_inside_words=True) == (
        f"placeholder_{REDACTED_SECRET_PLACEHOLDER}"
    )


def test_redact_secrets_from_text_replaces_long_secret_inside_alphanumeric_run() -> None:
    result = redact_secrets_from_text("prefixlongword9suffix", {"longword9"})

    assert result == f"prefix{REDACTED_SECRET_PLACEHOLDER}suffix"


def test_redact_secrets_from_text_boundary_all_lengths_toggles_long_secret_matching() -> None:
    text = "prefixlongword9suffix"

    assert redact_secrets_from_text(text, {"longword9"}) == f"prefix{REDACTED_SECRET_PLACEHOLDER}suffix"
    assert redact_secrets_from_text(text, {"longword9"}, boundary_all_lengths=False) == (
        f"prefix{REDACTED_SECRET_PLACEHOLDER}suffix"
    )
    assert redact_secrets_from_text(text, {"longword9"}, boundary_all_lengths=True) == text
    assert redact_secrets_from_text("code: longword9 ok", {"longword9"}, boundary_all_lengths=True) == (
        f"code: {REDACTED_SECRET_PLACEHOLDER} ok"
    )


def test_redact_secrets_from_bytes_replaces_invalid_utf8_and_redacts() -> None:
    data = b"\xffhunter2"

    result = redact_secrets_from_bytes(data, {"hunter2"})

    assert b"hunter2" not in result
    assert REDACTED_SECRET_PLACEHOLDER.encode() in result


def test_redact_har_bytes_redacts_structured_fields_and_embedded_secret_variants() -> None:
    secret = "pa ss/word"
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "headers": [
                            {"name": "Authorization", "value": f"Bearer {secret}"},
                            {"name": "X-Trace", "value": "safe"},
                        ],
                        "queryString": [{"name": "password", "value": secret}],
                        "cookies": [{"name": "sid", "value": secret}],
                        "postData": {
                            "text": f"raw={secret}&encoded=pa%20ss%2Fword",
                            "params": [{"name": "cvv", "value": "123"}],
                        },
                    },
                    "response": {
                        "headers": [{"name": "Set-Cookie", "value": f"sid={secret}"}],
                        "cookies": [{"name": "rsid", "value": secret}],
                    },
                }
            ]
        }
    }

    result = json.loads(redact_har_bytes(json.dumps(har).encode(), {secret}))
    request = result["log"]["entries"][0]["request"]
    response = result["log"]["entries"][0]["response"]
    serialized = json.dumps(result)

    assert request["headers"][0]["value"] == REDACTED_SECRET_PLACEHOLDER
    assert request["headers"][1]["value"] == "safe"
    assert request["queryString"][0]["value"] == REDACTED_SECRET_PLACEHOLDER
    assert request["cookies"][0]["value"] == REDACTED_SECRET_PLACEHOLDER
    assert request["postData"]["params"][0]["value"] == REDACTED_SECRET_PLACEHOLDER
    assert response["headers"][0]["value"] == REDACTED_SECRET_PLACEHOLDER
    assert response["cookies"][0]["value"] == REDACTED_SECRET_PLACEHOLDER
    assert secret not in serialized
    assert "pa%20ss%2Fword" not in serialized


def test_redact_har_bytes_preserves_original_bytes_when_nothing_is_redacted() -> None:
    har_data = b'{"log":{"entries":[]}}'

    assert redact_har_bytes(har_data, set()) == har_data


def test_redact_har_bytes_redacts_base64_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_redaction_module, "_MAX_BASE64_POST_DATA_CHARS", 64)
    secret = "hunter2"
    body = f"token={secret}"
    large_json = base64.b64encode(json.dumps({"password": "big-secret-1", "pad": "x" * 80}).encode()).decode()
    large_binary = base64.b64encode(bytes(range(256))).decode()
    padded_json = base64.b64encode((" " * 4000 + json.dumps({"password": "ws-prefix-secret"})).encode()).decode()
    har = {
        "log": {
            "entries": [
                {
                    "request": {},
                    "response": {
                        "content": {
                            "encoding": "base64",
                            "text": base64.b64encode(body.encode()).decode(),
                        }
                    },
                },
                {
                    "request": {"postData": {"mimeType": "text/plain", "encoding": "base64", "text": large_json}},
                    "response": {},
                },
                {
                    "request": {"postData": {"mimeType": "image/png", "encoding": "base64", "text": large_binary}},
                    "response": {},
                },
                {
                    "request": {"postData": {"mimeType": "text/plain", "encoding": "base64", "text": padded_json}},
                    "response": {},
                },
            ]
        }
    }

    once = redact_har_bytes(json.dumps(har).encode(), {secret})
    result = json.loads(once)
    encoded_text = result["log"]["entries"][0]["response"]["content"]["text"]
    decoded_text = base64.b64decode(encoded_text).decode()

    assert decoded_text == f"token={REDACTED_SECRET_PLACEHOLDER}"
    assert secret not in decoded_text
    for index, mime_type in ((1, "text/plain"), (2, "image/png"), (3, "text/plain")):
        assert result["log"]["entries"][index]["request"]["postData"] == {
            "mimeType": mime_type,
            "text": REDACTED_SECRET_PLACEHOLDER,
        }
    assert redact_har_bytes(once, {secret}) == once


@pytest.mark.parametrize(
    ("url", "secret_values", "leaked"),
    [
        ("https://example.test/callback?access_token=serverissued123&safe=ok", set(), "serverissued123"),
        # An unclosed IPv6 bracket makes urlsplit raise, so the query takes the pattern scrub.
        ("https://[example.test/callback?access_token=serverissued123&safe=ok", set(), "serverissued123"),
        # The name pattern stops at the space; the registered value's second word must still go.
        (
            "https://[example.test/callback?access_token=firstword suffixword&safe=ok",
            {"firstword suffixword"},
            "suffixword",
        ),
    ],
)
def test_redact_har_bytes_redacts_raw_url_query_by_sensitive_name(
    url: str, secret_values: set[str], leaked: str
) -> None:
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "url": url,
                        "queryString": [],
                    },
                    "response": {},
                }
            ]
        }
    }

    result = json.loads(redact_har_bytes(json.dumps(har).encode(), secret_values))
    url = result["log"]["entries"][0]["request"]["url"]

    assert f"access_token={REDACTED_SECRET_PLACEHOLDER}" in url
    assert leaked not in url
    assert "safe=ok" in url


def test_redact_har_bytes_redacts_urlencoded_post_data_text_by_sensitive_name() -> None:
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "postData": {
                            "mimeType": "application/x-www-form-urlencoded; charset=utf-8",
                            "text": "username=alice&password=notinvault",
                        }
                    },
                    "response": {},
                }
            ]
        }
    }

    result = json.loads(redact_har_bytes(json.dumps(har).encode(), set()))
    text = result["log"]["entries"][0]["request"]["postData"]["text"]

    assert text == f"username=alice&password={REDACTED_SECRET_PLACEHOLDER}"
    assert "notinvault" not in text


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _jwt(subject: str) -> str:
    header = _b64url('{"alg":"HS256","typ":"JWT"}')
    return f"{header}.{_b64url(json.dumps({'sub': subject, 'exp': 1700000000}))}.{_b64url('sig-' + subject)}"


_HEX40 = "3f9a1c0b7d2e4f6a8b9c0d1e2f3a4b5c6d7e8f90"
_HEX40_OTHER = "9e8d7c6b5a4f" * 3 + "3e2d"
_HEX32_UPPER = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
_DASHED_UUID = "123e4567-e89b-12d3-a456-426614174000"


def _har_entry(
    url: str = "https://example.test/",
    *,
    headers: tuple[tuple[str, str], ...] = (),
    query: tuple[tuple[str, str], ...] = (),
    post: dict[str, Any] | None = None,
    response_headers: tuple[tuple[str, str], ...] = (),
    redirect: str = "",
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "method": "POST" if post else "GET",
        "url": url,
        "headers": [{"name": name, "value": value} for name, value in headers],
        "queryString": [{"name": name, "value": value} for name, value in query],
    }
    if post is not None:
        request["postData"] = post
    return {
        "request": request,
        "response": {
            "status": 302 if redirect else 200,
            "redirectURL": redirect,
            "headers": [{"name": name, "value": value} for name, value in response_headers],
        },
    }


@pytest.mark.parametrize(
    ("entry", "gone", "kept"),
    [
        (_har_entry(headers=(("X-Session-Assertion", _jwt("hdr")),)), [_jwt("hdr")], ["X-Session-Assertion"]),
        (_har_entry(response_headers=(("X-Upstream-Credential", "Bearer a1b2c3d4e5f6g7"),)), ["a1b2c3d4e5f6g7"], []),
        (
            _har_entry(headers=(("X-Forwarded-Credential", "Basic " + base64.b64encode(b"svc:pw-91").decode()),)),
            [base64.b64encode(b"svc:pw-91").decode()],
            [],
        ),
        (
            _har_entry(headers=(("X-Original-Auth", 'Digest username="digest-user-77", realm="example"'),)),
            ["digest-user-77"],
            [],
        ),
        (
            _har_entry(
                headers=(
                    ("X-Plan", "Basic Information"),
                    ("X-Status", "Token expired"),
                    ("X-Note", "token 2024-11-20"),
                    ("X-Help", "send the token abc-12345 again"),
                    ("X-Prose-1", "Bearer of bad news"),
                    ("X-Prose-2", "Token unrecognized"),
                    ("X-Prose-3", "Bearer authentication required"),
                    ("X-Prose-4", "Token verification failed"),
                    ("X-Short", "Bearer abcdefghijklmno"),
                    ("X-Prose-5", "Token internationalization"),
                ),
                response_headers=(("WWW-Authenticate", 'Bearer realm="example"'),),
            ),
            [],
            [
                "Basic Information",
                "Token expired",
                "token 2024-11-20",
                "token abc-12345",
                "Bearer realm=",
                "Bearer of bad news",
                "Token unrecognized",
                "Bearer authentication required",
                "Token verification failed",
                "Bearer abcdefghijklmno",
                "Token internationalization",
            ],
        ),
        (
            _har_entry(headers=(("X-Custom", "Bearer abcdefghijklmnopqrstuvwxyz"),)),
            ["abcdefghijklmnopqrstuvwxyz"],
            [],
        ),
        (
            _har_entry(f"https://example.test/r?ref={_HEX40}&page=2", query=(("ref", _HEX40), ("page", "2"))),
            [_HEX40],
            ["page=2", "https://example.test/r?"],
        ),
        (
            _har_entry(
                f"https://example.test/items/{_DASHED_UUID}/abc123/{'0f1e2d3c' * 4}.js"
                f"?n={'1234' * 8}&w={'abcdef' * 6}&author=alice&credential_id=cred42",
                query=(("author", "alice"), ("credential_id", "cred42")),
            ),
            [f"{'0f1e2d3c' * 4}"],
            [
                _DASHED_UUID,
                "/abc123/",
                "/REDACTED_SECRET_1.js?",
                f"n={'1234' * 8}",
                f"w={'abcdef' * 6}",
                "author=alice",
                "credential_id=cred42",
                '"value": "alice"',
                '"value": "cred42"',
            ],
        ),
        (_har_entry(f"https://example.test/v/{_jwt('path')}/view"), [_jwt("path")], ["https://example.test/v/"]),
        (_har_entry(f"https://example.test/files/{_HEX40}/view"), [_HEX40], ["https://example.test/files/"]),
        (
            _har_entry(f"https://example.test/app;jsessionid={_HEX32_UPPER};session=opaque-sess-1/home"),
            [_HEX32_UPPER, "opaque-sess-1"],
            ["/home"],
        ),
        (
            _har_entry(redirect=f"https://example.test/cb#id_token={_jwt('frag')}&state=st-123"),
            [_jwt("frag")],
            ["state=st-123"],
        ),
        (
            _har_entry(response_headers=(("Location", "/cb#access_token=opaque-frag-tok&state=st-9"),)),
            ["opaque-frag-tok"],
            ["state=st-9"],
        ),
        (
            _har_entry(headers=(("Referer", "https://example.test/prev?token=opaque-ref-tok"),)),
            ["opaque-ref-tok"],
            ["https://example.test/prev?token="],
        ),
        (_har_entry(response_headers=(("Content-Location", f"/doc/{_jwt('cl')}"),)), [_jwt("cl")], ["/doc/"]),
        (
            _har_entry(
                "https://example.test/cb?accessToken=opaque-camel-tok&x=1",
                query=(("accessToken", "opaque-camel-tok"), ("x", "1")),
            ),
            ["opaque-camel-tok"],
            ["x=1"],
        ),
        (
            _har_entry(
                post={
                    "mimeType": "application/json",
                    "text": json.dumps(
                        {
                            "user": {
                                "accessToken": "opaque-json-tok",
                                "pin": 482913,
                                "password": {"current": "deep-secret-1", "shown": True},
                            },
                            "note": "plain-note-value",
                            "ref": _jwt("body"),
                        }
                    ),
                }
            ),
            ["opaque-json-tok", "482913", "deep-secret-1", _jwt("body")],
            ["plain-note-value", "shown"],
        ),
        (
            _har_entry(post={"mimeType": "text/plain;charset=UTF-8", "text": '[{"session_id": "beacon-sess-1"}]'}),
            ["beacon-sess-1"],
            [],
        ),
        (_har_entry(post={"text": '{"api-key": "nomime-key-1"}'}), ["nomime-key-1"], []),
        (
            _har_entry(
                post={"mimeType": "application/json", "text": '{"password": "broken-secret-1", "pin": 7788, "n": "kept'}
            ),
            ["broken-secret-1", "7788", "kept"],
            ['"text": "[REDACTED_SECRET]"'],
        ),
        (
            _har_entry(
                post={
                    "mimeType": "application/x-www-form-urlencoded",
                    "text": f"ref={_jwt('form')}&q=plain-q",
                    "params": [{"name": "blob", "value": _HEX40}],
                }
            ),
            [_jwt("form"), _HEX40],
            ["q=plain-q"],
        ),
        (
            _har_entry(
                post={"mimeType": "application/json", "text": json.dumps({"next": f"/cb%3Fjwt%3D{_jwt('pb')}"})}
            ),
            [_jwt("pb")],
            [],
        ),
        (_har_entry(headers=(("X-Return", f"/cb%3Fjwt%3D{_jwt('ph')}"),)), [_jwt("ph")], []),
        (
            _har_entry(f"https://example.test/go/x%3D{_jwt('pp')}/{_jwt('pd').replace('.', '%252E')}/end"),
            [_jwt("pp"), _jwt("pd").replace(".", "%252E")],
            ["https://example.test/go/", "/end"],
        ),
        (_har_entry(f"https://example.test/?next=%2Fcb%253Fjwt%253D{_jwt('pq')}"), [_jwt("pq")], []),
        (_har_entry(f"https://example.test/?v=jwt_{_jwt('pv')}"), [_jwt("pv")], []),
        (
            _har_entry(f"https://example.test/dl/{_jwt('ext')}.pdf"),
            [_jwt("ext")],
            ["/dl/REDACTED_SECRET_1.pdf"],
        ),
        (
            _har_entry(
                post={
                    "mimeType": "application/x-www-form-urlencoded; charset=UTF-8",
                    "text": json.dumps({"password": "jq-secret-1", "t": _jwt("jq")}),
                    "params": [{"name": json.dumps({"password": "jq-secret-1"}), "value": ""}],
                }
            ),
            ["jq-secret-1", _jwt("jq")],
            [],
        ),
        (
            _har_entry(redirect="https://example.test/app#/cb/access_token=opaque-route-tok&state=s1"),
            ["opaque-route-tok"],
            ["#/cb/access_token=", "state=s1"],
        ),
        (
            _har_entry(
                post={
                    "mimeType": "application/json",
                    "encoding": "base64",
                    "text": base64.b64encode(b'{"password": "b64-secret-1", "note": "b64-note"}').decode(),
                }
            ),
            ["b64-secret-1"],
            ["b64-note"],
        ),
        (
            _har_entry(
                post={
                    "mimeType": "application/json",
                    "text": '{"password": {"current": "nest-secret-1", "n": 4455, "list": ["arr-secret-1", [6677]]}, '
                    '"note": "after-nest", "count": 12,',
                }
            ),
            ["nest-secret-1", "4455", "arr-secret-1", "6677", "current", "after-nest"],
            ['"text": "[REDACTED_SECRET]"'],
        ),
        (
            _har_entry(
                "https://example.test/cb?access_token=opaque-h2-tok&page=1",
                headers=(
                    (":authority", "example.test"),
                    (":method", "GET"),
                    (":path", "/cb?access_token=opaque-h2-tok&page=1"),
                    (":scheme", "https"),
                ),
                query=(("access_token", "opaque-h2-tok"), ("page", "1")),
            ),
            ["opaque-h2-tok"],
            ['"value": "/cb?access_token=[REDACTED_SECRET]&page=1"', '"value": "example.test"'],
        ),
        (
            _har_entry(
                "https://alice:userinfo-pass-1@example.test/a",
                headers=(("Referer", "https://bob@example.test/b"),),
                response_headers=(("Content-Location", "https://Ab3dEf6hIj9kLm2nOp5qRs8t@example.test/c"),),
            ),
            ["userinfo-pass-1", "alice", "bob@", "Ab3dEf6hIj9kLm2nOp5qRs8t"],
            [
                "https://REDACTED_SECRET@example.test/a",
                "https://REDACTED_SECRET@example.test/b",
                "https://REDACTED_SECRET@example.test/c",
            ],
        ),
        (
            _har_entry(
                post={
                    "mimeType": "application/x-www-form-urlencoded",
                    "params": [
                        {"name": "user[password]", "value": "brk-secret-1"},
                        {"name": "a[b][token]", "value": "brk-secret-2"},
                        {"name": "ctl00$Password", "value": "dollar-secret-1"},
                        {"name": "j_password", "value": "jpw-secret-1"},
                        {"name": "pass", "value": "pass-secret-1"},
                        {"name": "client_secret", "value": "cs-secret-1"},
                        {"name": "SAMLResponse", "value": "saml-secret-1"},
                        {"name": "user[name]", "value": "kept-user-name"},
                        {"name": "passenger", "value": "kept-passenger"},
                    ],
                }
            ),
            ["brk-secret-1", "brk-secret-2", "dollar-secret-1", "jpw-secret-1", "pass-secret-1", "cs-secret-1"]
            + ["saml-secret-1"],
            ["kept-user-name", "kept-passenger"],
        ),
        (
            _har_entry(
                headers=(
                    ("Token", "hdr-opaque-1"),
                    ("X-Access-Token", "hdr-opaque-2"),
                    ("Api-Key", "hdr-opaque-3"),
                    ("X-Auth-Token", "hdr-opaque-4"),
                    ("X-Requested-With", "XMLHttpRequest"),
                ),
            ),
            ["hdr-opaque-1", "hdr-opaque-2", "hdr-opaque-3", "hdr-opaque-4"],
            ["XMLHttpRequest"],
        ),
        (
            _har_entry(f"https://example.test/a/{_HEX40}/b/{_HEX40_OTHER}/c/{_HEX40}"),
            [_HEX40, _HEX40_OTHER],
            ["/a/REDACTED_SECRET_1/b/REDACTED_SECRET_2/c/REDACTED_SECRET_1"],
        ),
        (
            _har_entry(
                f"https://example.test/?v={_jwt('pqd').replace('.', '%252E')}",
                headers=(("X-T", _jwt("phd").replace(".", "%2E")), ("X-H", "%30" + _HEX40[1:])),
                post={"mimeType": "application/json", "text": json.dumps({"t": _jwt("pjd").replace(".", "%2E")})},
            ),
            [
                _jwt("pqd").replace(".", "%252E"),
                _jwt("phd").replace(".", "%2E"),
                _jwt("pjd").replace(".", "%2E"),
                "%30" + _HEX40[1:],
            ],
            [],
        ),
        (
            _har_entry(
                post={
                    "mimeType": "application/x-www-form-urlencoded",
                    "params": [
                        {"name": name, "value": f"twin-{index}-secret"}
                        for index, name in enumerate(
                            [
                                "user[password_confirmation]",
                                "user[current_password]",
                                "new_password",
                                "confirm_password",
                                "user_password",
                                "form1:password",
                                "login.password",
                                "passwordConfirm",
                                "password2",
                            ]
                        )
                    ]
                    + [
                        {"name": name, "value": f"kept-{name}"}
                        for name in ("password_hint", "token_type", "next_token", "passenger")
                    ],
                }
            ),
            [f"twin-{index}-secret" for index in range(9)],
            ["kept-password_hint", "kept-token_type", "kept-next_token", "kept-passenger"],
        ),
        (
            _har_entry(post={"mimeType": "application/json", "encoding": "base64", "text": '{"password":"leak-me"}'}),
            ["leak-me"],
            ['"text": "[REDACTED_SECRET]"'],
        ),
        (
            _har_entry(
                post={
                    "mimeType": "multipart/form-data; boundary=b",
                    "text": '--b\r\nContent-Disposition: form-data; name="password"\r\n\r\nmulti-secret-1\r\n--b--\r\n',
                }
            ),
            ["multi-secret-1"],
            ["multipart/form-data; boundary=b"],
        ),
        (
            _har_entry(
                post={"mimeType": "text/plain", "text": f"note with {_jwt('prose').replace('.', '%2E')} inside"}
            ),
            ["note with"],
            ['"mimeType": "text/plain"'],
        ),
        (
            _har_entry(
                post={"mimeType": "application/x-www-form-urlencoded", "text": "user=alice&password=raw space secret"}
            ),
            ["raw space secret", "user=alice"],
            ['"mimeType": "application/x-www-form-urlencoded"'],
        ),
        (
            _har_entry(
                post={"mimeType": "application/x-www-form-urlencoded", "params": [{"name": _jwt("blank"), "value": ""}]}
            ),
            [_jwt("blank")],
            [],
        ),
        (
            _har_entry(
                headers=(
                    ("X-C1", "Bearer ab%3Bcd1234567"),
                    ("X-C2", "Bearer %22abcdef123456%22"),
                    ("X-C3", "Bearer %7Bghijkl123456%7D"),
                    ("X-C4", "%2" + _jwt("glued-2")),
                    ("X-C5", "x%E" + _jwt("glued-e")),
                ),
                post={"mimeType": "application/json", "text": json.dumps({"a": "%2" + _jwt("glued-json")})},
            ),
            ["ab%3Bcd1234567", "abcdef123456", "ghijkl123456", _jwt("glued-2"), _jwt("glued-e"), _jwt("glued-json")],
            [],
        ),
        (
            _har_entry(post={"mimeType": "application/x-www-form-urlencoded", "text": f"{_jwt('bare-body')}&x=1"}),
            [_jwt("bare-body")],
            ["x=1"],
        ),
        (
            _har_entry(post={"mimeType": "application/json", "text": {"password": "leak-me-dict"}}),
            ["leak-me-dict"],
            ['"text": "[REDACTED_SECRET]"'],
        ),
        (
            _har_entry(
                f"https://[example.test/?token{'1' * 40}x=kept-fallback&password{'1' * 40}=gone-fallback",
                post={
                    "mimeType": "application/x-www-form-urlencoded",
                    "params": [
                        {"name": "password" + "1" * 40, "value": "digit-twin-secret"},
                        {"name": "token" + "1" * 40 + "x", "value": "kept-digit-run"},
                    ],
                },
            ),
            ["digit-twin-secret", "gone-fallback"],
            ["kept-digit-run", "kept-fallback"],
        ),
        (
            _har_entry(post={"mimeType": "application/json", "encoding": "base64", "text": "[REDACTED_SECRET]"}),
            ['"encoding"'],
            ['"text": "[REDACTED_SECRET]"'],
        ),
        (
            _har_entry(
                post={"mimeType": "application/x-www-form-urlencoded", "text": "user=alice&password=nl-secret-1\r\n"}
            ),
            ["nl-secret-1"],
            ["user=alice&password=[REDACTED_SECRET]"],
        ),
        (
            _har_entry(
                post={
                    "mimeType": "application/x-www-form-urlencoded",
                    "text": "user=alice&state=a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4\n",
                }
            ),
            ["a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"],
            ["user=alice&state=[REDACTED_SECRET]\\n"],
        ),
        (
            _har_entry(
                post={
                    "mimeType": "application/x-www-form-urlencoded",
                    "encoding": "base64",
                    "text": base64.b64encode(b"state=a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4\r\n").decode(),
                }
            ),
            ["a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"],
            ["state=[REDACTED_SECRET]\r\n"],
        ),
        (
            _har_entry(
                post={
                    "mimeType": "application/x-www-form-urlencoded",
                    "text": "user=alice&state=a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4 ",
                }
            ),
            ["a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"],
            ['"text": "[REDACTED_SECRET]"'],
        ),
    ],
    ids=[
        "jwt-custom-request-header",
        "bearer-custom-response-header",
        "basic-custom-header",
        "digest-custom-header",
        "scheme-words-in-prose-survive",
        "all-letters-bearer",
        "hex-query-value",
        "asset-hash-uuid-slug-and-names-survive",
        "jwt-path-segment",
        "hex-path-segment",
        "matrix-params",
        "redirect-url-fragment",
        "location-header-fragment",
        "referer-header-query",
        "content-location-path",
        "normalized-query-name",
        "json-body",
        "text-plain-json-body",
        "json-body-without-mime-type",
        "unparseable-json-body",
        "urlencoded-and-param-values",
        "percent-encoded-jwt-in-json-body",
        "percent-encoded-jwt-in-header",
        "percent-encoded-jwt-in-path",
        "double-encoded-jwt-in-query",
        "prefixed-jwt-in-query",
        "path-segment-keeps-extension",
        "json-body-under-form-mime-type",
        "fragment-route-without-question-mark",
        "base64-json-body",
        "unparseable-json-nested-under-sensitive-key",
        "http2-path-pseudo-header",
        "userinfo-password",
        "structured-and-common-form-names",
        "field-names-as-header-names",
        "distinct-path-tokens-stay-distinct",
        "percent-encoded-dots-jwt",
        "name-twins",
        "malformed-base64-post-data",
        "unparsed-multipart-withheld",
        "unparsed-prose-withheld",
        "urlencoded-with-raw-whitespace-withheld",
        "blank-valued-jwt-param",
        "raw-and-decoded-shapes",
        "bare-token-urlencoded-body-is-idempotent",
        "non-string-body-withheld",
        "long-digit-run-names",
        "withheld-base64-body-drops-its-encoding",
        "urlencoded-body-with-trailing-newline",
        "hex-value-before-trailing-newline",
        "base64-hex-value-before-trailing-crlf",
        "urlencoded-body-with-trailing-space-withheld",
    ],
)
def test_redact_har_bytes_scrubs_credentials_by_name_and_shape(
    entry: dict[str, Any], gone: list[str], kept: list[str]
) -> None:
    raw = json.dumps({"log": {"entries": [entry]}}).encode()

    once = redact_har_bytes(raw, set())

    text = once.decode()
    stored_post = json.loads(once)["log"]["entries"][0]["request"].get("postData", {})
    if stored_post.get("encoding") == "base64":
        text += base64.b64decode(stored_post["text"]).decode()
    for token in gone:
        assert token not in text, token
    for fragment in kept:
        assert fragment in text, fragment
    assert redact_har_bytes(once, set()) == once
    post = entry["request"].get("postData", {})
    body = post.get("text", "")
    if isinstance(body, str) and body.startswith(("{", "[")) and body.endswith(("}", "]")) and "encoding" not in post:
        json.loads(json.loads(once)["log"]["entries"][0]["request"]["postData"]["text"])


def test_redact_har_bytes_scrubs_sensitive_names_in_a_truncated_har() -> None:
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "url": "https://example.test/login?password=urlpass123&accessToken=trunc-camel-1&page=2",
                        "headers": [
                            {"name": "Accept", "value": "text/html"},
                            {"name": "Authorization", "value": "Bearer header-token-123"},
                            {"name": "X-Session-Assertion", "value": _jwt("truncated")},
                            {"name": "X-Encoded-Assertion", "value": _jwt("trunc-pct").replace(".", "%2E")},
                            {"name": "X-Glued-Assertion", "value": "%2" + _jwt("trunc-glued")},
                            {"name": "X-Upstream-Credential", "value": "Bearer trunc-bearer-9z"},
                            {"name": "X-API_KEY", "value": "trunc-apikey-1"},
                            {"name": "X-Trace-Sig", "value": _HEX40},
                        ],
                        "cookies": [{"name": "sid", "value": "cookie-value-123", "httpOnly": True}],
                        "postData": {
                            "mimeType": "application/x-www-form-urlencoded",
                            "text": f"username=alice&password=formpass123&blob={_HEX32_UPPER}",
                            "params": [{"name": "cvv", "value": "987"}],
                        },
                    },
                    "response": {
                        "content": {"text": "welcome vault-secret-123 password=firstword suffixword ref=7482913"}
                    },
                },
                {"request": {"url": "https://example.test/next"}},
            ]
        }
    }
    truncated = json.dumps(har).encode()[:-40]
    with pytest.raises(json.JSONDecodeError):
        json.loads(truncated)

    result = redact_har_bytes(truncated, {"vault-secret-123", "firstword suffixword", "482913"})

    for leaked in (
        b"header-token-123",
        _jwt("truncated").encode(),
        _jwt("trunc-pct").split(".")[1].encode(),
        _jwt("trunc-glued").encode(),
        b"trunc-bearer-9z",
        b"trunc-apikey-1",
        b"trunc-camel-1",
        _HEX40.encode(),
        _HEX32_UPPER.encode(),
        b"cookie-value-123",
        b"formpass123",
        b"urlpass123",
        b"987",
        b"vault-secret",
        b"suffixword",
        b"482913",
    ):
        assert leaked not in result
    assert REDACTED_SECRET_PLACEHOLDER.encode() in result
    assert b"text/html" in result
    assert b"username=alice" in result
    assert b"page=2" in result


def test_get_secret_values_for_run_returns_empty_for_unknown_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    manager = WorkflowContextManager()

    assert manager.get_secret_values_for_run(None) == set()
    assert manager.get_secret_values_for_run("missing") == set()


def test_mask_secrets_enabled_for_run_truth_table(
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    manager = workflow_context_manager_factory(workflow_run_id="wr_enabled", mask_secrets=True)
    manager.workflow_run_contexts["wr_disabled"] = workflow_context_manager_factory(
        workflow_run_id="wr_disabled", mask_secrets=False
    ).workflow_run_contexts["wr_disabled"]

    assert manager.mask_secrets_enabled_for_run("wr_enabled") is True
    assert manager.mask_secrets_enabled_for_run("wr_disabled") is False
    assert manager.mask_secrets_enabled_for_run("missing") is False
    assert manager.mask_secrets_enabled_for_run(None) is False

    # A removed run keeps its setting for the scrub, until a retry's new context for the same run replaces it.
    manager.remove_workflow_run_context("wr_enabled")
    assert manager.mask_secrets_enabled_for_run("wr_enabled") is True
    manager.workflow_run_contexts["wr_enabled"] = manager.workflow_run_contexts["wr_disabled"]
    assert manager.mask_secrets_enabled_for_run("wr_enabled") is False


def test_secret_redaction_enabled_for_run_requires_env_flag_and_workflow_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    manager = workflow_context_manager_factory(workflow_run_id="wr_enabled", mask_secrets=True)
    manager.workflow_run_contexts["wr_disabled"] = workflow_context_manager_factory(
        workflow_run_id="wr_disabled", mask_secrets=False
    ).workflow_run_contexts["wr_disabled"]

    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    assert manager.secret_redaction_enabled_for_run("wr_enabled") is True
    assert manager.secret_redaction_enabled_for_run("wr_disabled") is False
    assert manager.secret_redaction_enabled_for_run("missing") is False
    assert manager.secret_redaction_enabled_for_run(None) is False

    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", False)
    assert manager.secret_redaction_enabled_for_run("wr_enabled") is False


@pytest.mark.parametrize(("enabled", "expected"), [(True, True), (False, False)])
def test_artifact_redaction_enabled_for_bare_task_uses_global_flag(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    expected: bool,
) -> None:
    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", enabled)
    manager = WorkflowContextManager()

    assert manager.artifact_redaction_enabled(None) is expected


def test_artifact_redaction_enabled_for_workflow_run_delegates_to_per_run_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = WorkflowContextManager()
    delegated_run_ids: list[str | None] = []

    def per_run_gate(workflow_run_id: str | None) -> bool:
        delegated_run_ids.append(workflow_run_id)
        return True

    monkeypatch.setattr(manager, "secret_redaction_enabled_for_run", per_run_gate)

    assert manager.artifact_redaction_enabled("wr_1") is True
    assert delegated_run_ids == ["wr_1"]


def test_get_secret_values_for_run_respects_workflow_opt_out(
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    manager = workflow_context_manager_factory(
        workflow_run_id="wr_1",
        mask_secrets=False,
        secrets={"password": "super-secret"},
    )

    assert manager.get_secret_values_for_run("wr_1") == set()
    assert manager.get_secret_values_for_run("wr_1", respect_artifact_redaction_flag=False) == {"super-secret"}


def test_registered_placeholder_ids_for_run_names_only_placeholder_keys(
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    manager = workflow_context_manager_factory(
        workflow_run_id="wr_1",
        secrets={"placeholder_ab12_password": "super-secret", "MASTER_PASSWORD": "other-secret"},
    )

    # Unlike the secret-value set, this is not gated on the masking opt-in: it is what redaction
    # exempts, so it has to be readable wherever redaction runs.
    assert manager.registered_placeholder_ids_for_run("wr_1") == frozenset({"placeholder_ab12_password"})
    assert manager.registered_placeholder_ids_for_run("wr_unknown") == frozenset()
    assert manager.registered_placeholder_ids_for_run(None) == frozenset()


@pytest.mark.parametrize("mask_secrets", [True, False], ids=["mask-secrets", "no-mask-secrets"])
@pytest.mark.parametrize("context_removed", [False, True], ids=["live-context", "context-removed"])
def test_artifact_redaction_exempts_the_runs_registered_placeholder_ids(
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
    mask_secrets: bool,
    context_removed: bool,
) -> None:
    """The prompt artifact is the record of what crossed the model boundary, so it keeps the run's
    own token — but a secret rendered behind an unregistered prefix is scrubbed (SKY-17864). Work the
    run started can still write one after the run's context is removed, and it keeps the same scrub."""
    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    manager = workflow_context_manager_factory(
        workflow_run_id="wr_artifact",
        mask_secrets=mask_secrets,
        secrets={"placeholder_ab12_password": "password"},
    )
    monkeypatch.setattr(artifact_manager.app, "WORKFLOW_CONTEXT_MANAGER", manager)
    monkeypatch.setattr(artifact_manager, "_RUNS_WARNED_MISSING_CONTEXT", {})
    if context_removed:
        assert manager.remove_workflow_run_context_if_current(
            "wr_artifact", manager.workflow_run_contexts["wr_artifact"]
        )
        assert manager.has_workflow_run_context("wr_artifact") is False

    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_artifact")):
        redacted = artifact_manager._maybe_redact_artifact_data(
            ArtifactType.LLM_PROMPT, b"type placeholder_ab12_password, never placeholder_password"
        )

    assert redacted == f"type placeholder_ab12_password, never placeholder_{REDACTED_SECRET_PLACEHOLDER}".encode()
    assert artifact_manager._RUNS_WARNED_MISSING_CONTEXT == {}


def test_get_secret_values_for_run_returns_empty_when_global_flag_disabled(
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", False)
    manager = workflow_context_manager_factory(
        workflow_run_id="wr_1",
        secrets={"password": "super-secret"},
    )

    assert manager.get_secret_values_for_run("wr_1") == set()
    assert manager.get_secret_values_for_run("wr_1", respect_artifact_redaction_flag=False) == {"super-secret"}


def test_get_secret_values_for_run_returns_filtered_context_and_current_totp_values(
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    manager = workflow_context_manager_factory(
        workflow_run_id="wr_1",
        secrets={
            "password": "super-secret",
            "short_numeric": "587",
            "placeholder_x1y2_password": "placeholder_x1y2_password",
            "sentinel": "OP_TOTP",
        },
    )

    with skyvern_context.scoped(SkyvernContext(totp_codes={"task_1": "654321", "task_2": None})):
        values = manager.get_secret_values_for_run("wr_1")

    assert values == {"super-secret", "654321"}


def test_get_secret_values_for_run_can_exclude_runtime_otp_values(
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    manager = workflow_context_manager_factory(
        workflow_run_id="wr_1",
        secrets={
            "placeholder_ab_pw": "hunter2secret",
            "placeholder_cd_otp": "483920",
        },
        runtime_otp_values={"483920"},
    )

    assert manager.get_secret_values_for_run("wr_1") == {"hunter2secret", "483920"}
    assert manager.get_secret_values_for_run("wr_1", exclude_runtime_otp=True) == {"hunter2secret"}


def test_get_secret_values_for_run_collects_short_runtime_otp_values(
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    manager = workflow_context_manager_factory(
        workflow_run_id="wr_1",
        secrets={
            "placeholder_ab_pw": "hunter2secret",
            "placeholder_cd_smtp_port": "587",
        },
        runtime_otp_values={"4821"},
    )

    assert manager.get_secret_values_for_run("wr_1") == {"hunter2secret", "4821"}
    assert manager.get_secret_values_for_run("wr_1", exclude_runtime_otp=True) == {"hunter2secret"}


def test_get_secret_values_for_run_skips_totp_cache_metadata(
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    manager = workflow_context_manager_factory(workflow_run_id="wr_1")

    with skyvern_context.scoped(
        SkyvernContext(
            totp_codes={
                "task_1": "654321",
                "task_1_valid_from": "1720000000",
                "task_1_valid_until": "1720000030",
            }
        )
    ):
        values = manager.get_secret_values_for_run("wr_1")

    assert values == {"654321"}


@pytest.mark.parametrize("state", ["candidate", "ledger", "none"])
def test_multi_box_artifact_retention_masks_representations_only_with_state(monkeypatch, state: str) -> None:
    context = SkyvernContext(task_id="task")
    with skyvern_context.scoped(context):
        if state == "candidate":
            assert skyvern_context.normalize_multi_field_totp_code("ABC-DEF", 6) == "ABCDEF"
        elif state == "ledger":
            context.multi_field_totp_rejections["previous-task"] = skyvern_context.MultiFieldTotpRejection(
                hashlib.sha256(b"ABCDEF").hexdigest(), datetime.now(UTC), None, "Rejected"
            )
        monkeypatch.setattr(
            artifact_manager,
            "app",
            SimpleNamespace(
                WORKFLOW_CONTEXT_MANAGER=SimpleNamespace(
                    artifact_redaction_enabled=Mock(return_value=True),
                    get_secret_values_for_run=Mock(return_value={"other-secret"} if state == "none" else set()),
                )
            ),
        )
        for artifact_type, text in [
            (ArtifactType.HTML_SCRAPE, "<p>ABC DEF</p><label>ＡＢＣＤＥＦ</label> paﬀword"),
            (ArtifactType.LLM_RESPONSE, '{"reasoning": "ABC / — DEF rejected"}'),
        ]:
            original = text.encode()
            expected = (
                original
                if state == "none"
                else text.replace("ABC DEF", REDACTED_SECRET_PLACEHOLDER)
                .replace("ＡＢＣＤＥＦ", REDACTED_SECRET_PLACEHOLDER)
                .replace("ABC / — DEF", REDACTED_SECRET_PLACEHOLDER)
                .encode()
            )
            assert artifact_manager._maybe_redact_artifact_data(artifact_type, original) == expected
            assert redact_secrets_from_bytes(original, set()) == expected
        sample = b"other-secret ABC DEF placeholder_abcdef"
        assert artifact_manager._maybe_redact_artifact_data(ArtifactType.HTML_ACTION, sample) == (
            b"[REDACTED_SECRET] ABC DEF placeholder_abcdef"
            if state == "none"
            else b"other-secret [REDACTED_SECRET] placeholder_abcdef"
        )


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("multi_box", [False, True])
def test_artifact_otp_floor_is_independent_of_credential_masking(monkeypatch, enabled: bool, multi_box: bool) -> None:
    monkeypatch.setattr(artifact_manager.settings, "ENABLE_ARTIFACT_VALUE_SCRUB_ALL_RUNS", False)
    context = SkyvernContext(task_id="task")
    manager = SimpleNamespace(
        artifact_redaction_enabled=Mock(return_value=enabled),
        get_secret_values_for_run=Mock(return_value={"password-secret"}),
    )
    monkeypatch.setattr(artifact_manager, "app", SimpleNamespace(WORKFLOW_CONTEXT_MANAGER=manager))
    with skyvern_context.scoped(context):
        if multi_box:
            skyvern_context.normalize_multi_field_totp_code("ABC-DEF", 6)
        data = b"<p>ABC DEF</p><p>password-secret</p>"
        expected = data.replace(b"ABC DEF", b"[REDACTED_SECRET]") if multi_box else data
        if enabled:
            expected = expected.replace(b"password-secret", b"[REDACTED_SECRET]")
        assert artifact_manager._maybe_redact_artifact_data(ArtifactType.HTML_ACTION, data) == expected
        for artifact_type in (ArtifactType.VISIBLE_ELEMENTS_ID_CSS_MAP, ArtifactType.BROWSER_SESSION_ACTION_LOG):
            assert artifact_manager._maybe_redact_artifact_data(artifact_type, data) == (
                data.replace(b"ABC DEF", b"[REDACTED_SECRET]") if multi_box else data
            )


@pytest.mark.parametrize("spelling", ["ＡＢＣＤＥＦ", "A\nB\nC\nD\nE\nF"])
@pytest.mark.parametrize("artifact_type", [ArtifactType.HAR, ArtifactType.LLM_RESPONSE])
def test_json_artifact_masks_decoded_code_spellings(monkeypatch, spelling: str, artifact_type: ArtifactType) -> None:
    monkeypatch.setattr(artifact_manager.settings, "ENABLE_ARTIFACT_VALUE_SCRUB_ALL_RUNS", False)
    context = SkyvernContext(task_id="task")
    monkeypatch.setattr(
        artifact_manager,
        "app",
        SimpleNamespace(
            WORKFLOW_CONTEXT_MANAGER=SimpleNamespace(
                artifact_redaction_enabled=Mock(return_value=False),
                runtime_secret_values_for_artifacts=Mock(return_value=set()),
            )
        ),
    )
    payload = (
        {"log": {"entries": [{"response": {"content": {"text": spelling}}}]}}
        if artifact_type == ArtifactType.HAR
        else {"reasoning": spelling}
    )
    with skyvern_context.scoped(context):
        skyvern_context.normalize_multi_field_totp_code("ABC-DEF", 6)
        data = json.dumps(payload).encode()
        retained = artifact_manager._maybe_redact_artifact_data(artifact_type, data)
        parsed = json.loads(retained)
        value = (
            parsed["log"]["entries"][0]["response"]["content"]["text"]
            if artifact_type == ArtifactType.HAR
            else parsed["reasoning"]
        )
        assert value == REDACTED_SECRET_PLACEHOLDER
        if artifact_type == ArtifactType.HAR:
            assert json.loads(redact_har_bytes(data, set())) == parsed


@pytest.mark.parametrize("spelling", ["ABCDEF", "ABC DEF", "ＡＢＣＤＥＦ"])
@pytest.mark.parametrize("entry", ["direct", "artifact"])
def test_har_base64_content_keeps_representation_masking(monkeypatch, spelling: str, entry: str) -> None:
    context = SkyvernContext(task_id="task")
    with skyvern_context.scoped(context):
        skyvern_context.normalize_multi_field_totp_code("ABC-DEF", 6)
        encoded = base64.b64encode(spelling.encode()).decode()
        data = json.dumps(
            {"log": {"entries": [{"response": {"content": {"text": encoded, "encoding": "base64"}}}]}}
        ).encode()
        if entry == "artifact":
            monkeypatch.setattr(
                artifact_manager.app.WORKFLOW_CONTEXT_MANAGER, "artifact_redaction_enabled", lambda _: False
            )
            monkeypatch.setattr(
                artifact_manager.app.WORKFLOW_CONTEXT_MANAGER, "runtime_secret_values_for_artifacts", lambda: set()
            )
            data = artifact_manager._maybe_redact_artifact_data(ArtifactType.HAR, data)
        else:
            data = redact_har_bytes(data, set())
        content = json.loads(data)["log"]["entries"][0]["response"]["content"]
    assert content["encoding"] == "base64"
    assert base64.b64decode(content["text"], validate=True).decode() == REDACTED_SECRET_PLACEHOLDER


def test_artifact_masking_combines_task_widths_and_reuses_fingerprints() -> None:
    context = SkyvernContext(task_id="first")
    with skyvern_context.scoped(context):
        skyvern_context.normalize_multi_field_totp_code("ABC-DEF", 6, task_id="first")
        skyvern_context.normalize_multi_field_totp_code("WXYZ", 4, task_id="second")
        context.multi_field_totp_rejections["third"] = skyvern_context.MultiFieldTotpRejection(
            hashlib.sha256(b"SECOND").hexdigest(), datetime.now(UTC), None, "Rejected"
        )
        text = b"ABC DEF; W/X/Y/Z; SEC - OND"
        assert (
            redact_multi_field_totp_artifact_bytes(text) == b"[REDACTED_SECRET]; [REDACTED_SECRET]; [REDACTED_SECRET]"
        )
        cached = context.multi_field_totp_artifact_fingerprints["first"]
        assert redact_multi_field_totp_artifact_bytes(b"unrelated text") == b"unrelated text"
        assert context.multi_field_totp_artifact_fingerprints["first"] is cached


@pytest.mark.parametrize("body", [b"ABCDEF", b"\xff\xff"])
def test_har_base64_body_skips_generic_encoded_variant_replacement(body: bytes) -> None:
    encoded = base64.b64encode(body).decode()
    har = {"log": {"entries": [{"response": {"content": {"text": encoded, "encoding": "base64"}}}]}}
    data = json.dumps(har, indent=2).encode()
    # A registered literal that coincides with encoded bytes is absent from the decoded body.
    with skyvern_context.scoped(SkyvernContext()):
        retained = redact_har_bytes(data, {encoded})
    assert retained == data
    content = json.loads(retained)["log"]["entries"][0]["response"]["content"]
    assert content["encoding"] == "base64"
    assert base64.b64decode(content["text"], validate=True) == body


def test_har_invalid_base64_cannot_bypass_literal_redaction() -> None:
    data = json.dumps(
        {"log": {"entries": [{"response": {"content": {"encoding": "base64", "text": "ABCDEF"}}}]}}
    ).encode()
    with skyvern_context.scoped(SkyvernContext()):
        retained = redact_har_bytes(data, {"ABCDEF"})
    content = json.loads(retained)["log"]["entries"][0]["response"]["content"]
    assert content["encoding"] == "base64"
    assert base64.b64decode(content["text"], validate=True).decode() == REDACTED_SECRET_PLACEHOLDER
