import base64
import json
from types import SimpleNamespace

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.utils.secret_redaction import (
    REDACTED_SECRET_PLACEHOLDER,
    collect_redactable_secret_values,
    expand_secret_encodings,
    redact_har_bytes,
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
    assert redact_secrets_from_text("placeholder_prodtoken", values) == "placeholder_prodtoken"


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
    result = redact_secrets_from_text("placeholder_ab12_password pass", {"pass"})

    assert result == f"placeholder_ab12_password {REDACTED_SECRET_PLACEHOLDER}"


def test_redact_secrets_from_text_anchors_short_secret_variants() -> None:
    result = redact_secrets_from_text("password wordpress word word. =word&", {"word"})

    assert result == (
        "password wordpress "
        f"{REDACTED_SECRET_PLACEHOLDER} {REDACTED_SECRET_PLACEHOLDER}. ={REDACTED_SECRET_PLACEHOLDER}&"
    )


def test_redact_secrets_from_text_replaces_long_secret_inside_alphanumeric_run() -> None:
    result = redact_secrets_from_text("prefixlongword9suffix", {"longword9"})

    assert result == f"prefix{REDACTED_SECRET_PLACEHOLDER}suffix"


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


def test_redact_har_bytes_redacts_base64_response_content() -> None:
    secret = "hunter2"
    body = f"token={secret}"
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
                }
            ]
        }
    }

    result = json.loads(redact_har_bytes(json.dumps(har).encode(), {secret}))
    encoded_text = result["log"]["entries"][0]["response"]["content"]["text"]
    decoded_text = base64.b64decode(encoded_text).decode()

    assert decoded_text == f"token={REDACTED_SECRET_PLACEHOLDER}"
    assert secret not in decoded_text


def test_redact_har_bytes_redacts_raw_url_query_by_sensitive_name() -> None:
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "url": "https://example.test/callback?access_token=serverissued123&safe=ok",
                        "queryString": [],
                    },
                    "response": {},
                }
            ]
        }
    }

    result = json.loads(redact_har_bytes(json.dumps(har).encode(), set()))
    url = result["log"]["entries"][0]["request"]["url"]

    assert f"access_token={REDACTED_SECRET_PLACEHOLDER}" in url
    assert "serverissued123" not in url
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


def test_redact_har_bytes_leaves_json_post_data_to_value_matching() -> None:
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "postData": {
                            "mimeType": "application/json",
                            "text": '{"password":"notinvault"}',
                        }
                    },
                    "response": {},
                }
            ]
        }
    }

    result = json.loads(redact_har_bytes(json.dumps(har).encode(), set()))

    assert result["log"]["entries"][0]["request"]["postData"]["text"] == '{"password":"notinvault"}'


def test_redact_har_bytes_falls_back_to_plain_replacement_for_invalid_json() -> None:
    assert redact_har_bytes(b"{not-json hunter2", {"hunter2"}) == b"{not-json [REDACTED_SECRET]"


def test_get_secret_values_for_run_returns_empty_for_unknown_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    manager = WorkflowContextManager()

    assert manager.get_secret_values_for_run(None) == set()
    assert manager.get_secret_values_for_run("missing") == set()


def test_get_secret_values_for_run_respects_artifact_redaction_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", False)
    manager = WorkflowContextManager()
    manager.workflow_run_contexts["wr_1"] = SimpleNamespace(secrets={"password": "super-secret"})

    assert manager.get_secret_values_for_run("wr_1") == set()
    assert manager.get_secret_values_for_run("wr_1", respect_artifact_redaction_flag=False) == {"super-secret"}


def test_get_secret_values_for_run_returns_filtered_context_and_current_totp_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    manager = WorkflowContextManager()
    manager.workflow_run_contexts["wr_1"] = SimpleNamespace(
        secrets={
            "password": "super-secret",
            "short_numeric": "587",
            "placeholder_x1y2_password": "placeholder_x1y2_password",
            "sentinel": "OP_TOTP",
        }
    )

    with skyvern_context.scoped(SkyvernContext(totp_codes={"task_1": "654321", "task_2": None})):
        values = manager.get_secret_values_for_run("wr_1")

    assert values == {"super-secret", "654321"}


def test_get_secret_values_for_run_can_exclude_runtime_otp_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    manager = WorkflowContextManager()
    manager.workflow_run_contexts["wr_1"] = SimpleNamespace(
        secrets={
            "placeholder_ab_pw": "hunter2secret",
            "placeholder_cd_otp": "483920",
        },
        runtime_otp_values={"483920"},
    )

    assert manager.get_secret_values_for_run("wr_1") == {"hunter2secret", "483920"}
    assert manager.get_secret_values_for_run("wr_1", exclude_runtime_otp=True) == {"hunter2secret"}


def test_get_secret_values_for_run_collects_short_runtime_otp_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    manager = WorkflowContextManager()
    manager.workflow_run_contexts["wr_1"] = SimpleNamespace(
        secrets={
            "placeholder_ab_pw": "hunter2secret",
            "placeholder_cd_smtp_port": "587",
        },
        runtime_otp_values={"4821"},
    )

    assert manager.get_secret_values_for_run("wr_1") == {"hunter2secret", "4821"}
    assert manager.get_secret_values_for_run("wr_1", exclude_runtime_otp=True) == {"hunter2secret"}


def test_get_secret_values_for_run_skips_totp_cache_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    manager = WorkflowContextManager()
    manager.workflow_run_contexts["wr_1"] = SimpleNamespace(secrets={}, runtime_otp_values=set())

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
