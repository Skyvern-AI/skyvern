import base64
import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.artifact import manager as artifact_manager
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
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


def test_large_multi_box_artifact_masking_has_bounded_cost() -> None:
    context = SkyvernContext(task_id="task")
    text = "<p>ordinary html</p>" * 300_000
    data = text.encode()
    with skyvern_context.scoped(context):
        skyvern_context.normalize_multi_field_totp_code("ABCDEF", 6)
        started = time.perf_counter()
        masked = redact_multi_field_totp_artifact_bytes(data)
        elapsed = time.perf_counter() - started
    assert masked == data
    assert elapsed < 0.05, f"Artifact masking blocked for {elapsed:.3f}s"


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("multi_box", [False, True])
def test_artifact_otp_floor_is_independent_of_credential_masking(monkeypatch, enabled: bool, multi_box: bool) -> None:
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
