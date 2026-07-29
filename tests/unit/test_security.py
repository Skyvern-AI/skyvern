import pytest
from freezegun import freeze_time

from skyvern.forge.sdk.core.security import (
    MAX_WEBHOOK_PAYLOAD_LOG_SIZE,
    create_access_token,
    generate_skyvern_webhook_signature,
    redact_credentialed_urls,
)

_SIGNED = "https://api.skyvern.com/v1/artifacts/art_1/content?expiry=1800000600&kid=k1&sig=SIGNED_SECRET"
_S3 = (
    "https://bucket.s3.amazonaws.com/org/art_2.png"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIA%2F20260716&X-Amz-Expires=900&X-Amz-Signature=S3_SECRET"
)
_AZURE = "https://acct.blob.core.windows.net/c/art_3.webm?sv=2021-08-06&se=2026-07-16T00%3A00%3A00Z&sig=AZURE_SECRET"
_GCS = "https://storage.googleapis.com/bucket/art_4.png?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Signature=GCS_SECRET"
_PLAIN = "https://consumer.example.com/callback?run_id=r_1&status=done"


@pytest.mark.skip(reason="Skipping test_generate_skyvern_signature")
@freeze_time("2023-11-30 00:00:00")
def test_generate_skyvern_signature() -> None:
    api_key = create_access_token("o_12345")
    payload = {"task_id": "t_12345", "float": 1.0}
    signed_data = generate_skyvern_webhook_signature(payload, api_key)
    assert signed_data.signature == "1fac4204e1abc7cb0bdf1a42eb17d27f6f1feba065d5726777d5eb77581298c1"


class TestRedactCredentialedUrls:
    def test_strips_skyvern_signed_content_url(self) -> None:
        assert redact_credentialed_urls(_SIGNED) == "https://api.skyvern.com/v1/artifacts/art_1/content"

    def test_strips_s3_presigned(self) -> None:
        out = redact_credentialed_urls(_S3)
        assert "S3_SECRET" not in out
        assert out == "https://bucket.s3.amazonaws.com/org/art_2.png"

    def test_strips_azure_sas(self) -> None:
        out = redact_credentialed_urls(_AZURE)
        assert "AZURE_SECRET" not in out
        assert out == "https://acct.blob.core.windows.net/c/art_3.webm"

    def test_strips_gcs_presigned(self) -> None:
        out = redact_credentialed_urls(_GCS)
        assert "GCS_SECRET" not in out
        assert out == "https://storage.googleapis.com/bucket/art_4.png"

    def test_preserves_non_credentialed_url(self) -> None:
        assert redact_credentialed_urls(_PLAIN) == _PLAIN

    def test_strips_signed_url_embedded_in_text(self) -> None:
        out = redact_credentialed_urls(f"failed to fetch {_SIGNED} after 3 retries")
        assert "SIGNED_SECRET" not in out
        assert out == "failed to fetch https://api.skyvern.com/v1/artifacts/art_1/content after 3 retries"

    def test_leaves_plain_text_untouched(self) -> None:
        assert redact_credentialed_urls("no urls here, just text") == "no urls here, just text"

    def test_strips_uppercase_scheme(self) -> None:
        url = "HTTPS://api.skyvern.com/v1/artifacts/art_5/content?expiry=1&kid=k&sig=UPPER_SECRET"
        assert "UPPER_SECRET" not in redact_credentialed_urls(url)

    def test_strips_presigned_url_with_apostrophe_in_path(self) -> None:
        url = "https://bucket.s3.amazonaws.com/o'brien.pdf?X-Amz-Signature=APOS_SECRET"
        out = redact_credentialed_urls(url)
        assert "APOS_SECRET" not in out
        assert out == "https://bucket.s3.amazonaws.com/o'brien.pdf"

    def test_strips_basic_auth_userinfo_on_credentialed_url(self) -> None:
        url = "https://user:PASSWORD@api.skyvern.com/v1/artifacts/art_6/content?sig=X"
        out = redact_credentialed_urls(url)
        assert "PASSWORD" not in out
        assert out == "https://api.skyvern.com/v1/artifacts/art_6/content"

    def test_fails_closed_on_unparseable_url(self) -> None:
        # An invalid IPv6 host makes urlsplit raise ValueError — must not leak the sig.
        url = "https://[bad_ipv6/path?sig=UNPARSEABLE_SECRET"
        out = redact_credentialed_urls(url)
        assert "UNPARSEABLE_SECRET" not in out


class TestWebhookPayloadForLog:
    def test_payload_for_log_redacts_urls_but_signed_payload_stays_raw(self) -> None:
        api_key = "test-api-key"
        payload = {
            "task_id": "t_1",
            "screenshot_url": _SIGNED,
            "recording_url": _S3,
            "webhook_callback_url": _PLAIN,
            "notes": f"artifact at {_SIGNED}",
        }
        signed = generate_skyvern_webhook_signature(payload, api_key)

        # log copy is scrubbed of every signature/presign secret, incl. the embedded one
        for secret in ("SIGNED_SECRET", "S3_SECRET"):
            assert secret not in signed.payload_for_log
        # non-credentialed callback URL is preserved in the log
        assert "run_id=r_1" in signed.payload_for_log
        # delivered payload + signature are untouched (raw, still carry the secrets)
        assert "SIGNED_SECRET" in signed.signed_payload
        assert "S3_SECRET" in signed.signed_payload
        assert signed.signature == generate_skyvern_webhook_signature(payload, api_key).signature

    def test_oversized_payload_is_redacted_and_truncated(self) -> None:
        api_key = "test-api-key"
        filler = "x" * MAX_WEBHOOK_PAYLOAD_LOG_SIZE
        payload = {"screenshot_url": _SIGNED, "filler": filler}
        signed = generate_skyvern_webhook_signature(payload, api_key)

        assert "SIGNED_SECRET" not in signed.payload_for_log
        assert "truncated" in signed.payload_for_log
        assert signed.payload_for_log.endswith(")")
