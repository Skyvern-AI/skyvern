"""Unit tests for skyvern.forge.sdk.artifact.signing.

Tests cover:
- ArtifactHmacKeyring validation
- parse_keyring (JSON parsing + caching)
- sign_artifact_url (URL structure, query params, signature format)
- verify_artifact_signature (happy path, expired, tampered, unknown kid,
  old-key-still-valid during rotation)
"""

import json
import time

import pytest

from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.artifact.signing import (
    ARTIFACT_URL_EXPIRY_SECONDS,
    ARTIFACT_URL_EXPIRY_SECONDS_MAX,
    ARTIFACT_URL_EXPIRY_SECONDS_MIN,
    ARTIFACT_URL_ON_DEMAND_EXPIRY_SECONDS,
    SENSITIVE_ARTIFACT_TYPES,
    SENSITIVE_ARTIFACT_URL_EXPIRY_SECONDS,
    ArtifactHmacKeyring,
    HmacKeyEntry,
    _canonical_string,
    _hmac_b64,
    artifact_url_expiry_seconds_for_type,
    effective_artifact_url_expiry_seconds,
    parse_artifact_content_url,
    parse_keyring,
    sign_artifact_url,
    verify_artifact_signature,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SECRET_PLAIN = "supersecret"
_KID_V1 = "2026-01-15-v1"
_KID_V2 = "2026-03-12-v2"
_ARTIFACT_ID = "art_abc123"
_BASE_URL = "https://api.skyvern.com"
_CONTENT_PATH = f"/v1/artifacts/{_ARTIFACT_ID}/content"


def _make_keyring(
    current_kid: str = _KID_V1,
    extra_keys: dict | None = None,
    secret: str = _SECRET_PLAIN,
) -> ArtifactHmacKeyring:
    keys = {current_kid: HmacKeyEntry(secret=secret, created_at="2026-01-15")}
    if extra_keys:
        keys.update(extra_keys)
    return ArtifactHmacKeyring(current_kid=current_kid, keys=keys)


def _keyring_json(current_kid: str = _KID_V1, secret: str = _SECRET_PLAIN) -> str:
    return json.dumps(
        {
            "current_kid": current_kid,
            "keys": {
                current_kid: {"secret": secret, "created_at": "2026-01-15"},
            },
        }
    )


# ---------------------------------------------------------------------------
# ArtifactHmacKeyring — model validation
# ---------------------------------------------------------------------------


class TestArtifactHmacKeyring:
    def test_valid_keyring_parses(self) -> None:
        kr = _make_keyring()
        assert kr.current_kid == _KID_V1
        assert _KID_V1 in kr.keys

    def test_current_kid_missing_from_keys_raises(self) -> None:
        with pytest.raises(ValueError, match="current_kid"):
            ArtifactHmacKeyring(
                current_kid="nonexistent",
                keys={_KID_V1: HmacKeyEntry(secret=_SECRET_PLAIN, created_at="2026-01-15")},
            )

    def test_get_secret_bytes_plain(self) -> None:
        kr = _make_keyring()
        assert kr.get_secret_bytes(_KID_V1) == _SECRET_PLAIN.encode()

    def test_get_secret_bytes_unknown_kid_returns_none(self) -> None:
        kr = _make_keyring()
        assert kr.get_secret_bytes("unknown") is None

    def test_multiple_keys_both_accessible(self) -> None:
        kr = _make_keyring(
            current_kid=_KID_V2,
            extra_keys={_KID_V1: HmacKeyEntry(secret="oldsecret", created_at="2026-01-15")},
            secret="newsecret",
        )
        assert kr.get_secret_bytes(_KID_V1) == b"oldsecret"
        assert kr.get_secret_bytes(_KID_V2) == b"newsecret"


# ---------------------------------------------------------------------------
# parse_keyring
# ---------------------------------------------------------------------------


class TestParseKeyring:
    def test_parses_valid_json(self) -> None:
        kr = parse_keyring(_keyring_json())
        assert isinstance(kr, ArtifactHmacKeyring)
        assert kr.current_kid == _KID_V1

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(Exception):
            parse_keyring("not-json")

    def test_invalid_schema_raises(self) -> None:
        bad = json.dumps({"current_kid": "x", "keys": {}})
        with pytest.raises(ValueError):
            parse_keyring(bad)

    def test_same_json_returns_cached_instance(self) -> None:
        raw = _keyring_json()
        kr1 = parse_keyring(raw)
        kr2 = parse_keyring(raw)
        assert kr1 is kr2


# ---------------------------------------------------------------------------
# sign_artifact_url
# ---------------------------------------------------------------------------


class TestSignArtifactUrl:
    def test_returns_string_url(self) -> None:
        kr = _make_keyring()
        url = sign_artifact_url(_BASE_URL, _ARTIFACT_ID, kr)
        assert isinstance(url, str)

    def test_url_contains_correct_path(self) -> None:
        kr = _make_keyring()
        url = sign_artifact_url(_BASE_URL, _ARTIFACT_ID, kr)
        assert _CONTENT_PATH in url

    def test_url_contains_expiry_kid_sig(self) -> None:
        kr = _make_keyring()
        url = sign_artifact_url(_BASE_URL, _ARTIFACT_ID, kr)
        assert "expiry=" in url
        assert f"kid={_KID_V1}" in url
        assert "sig=" in url

    def test_expiry_is_roughly_12h_from_now(self) -> None:
        kr = _make_keyring()
        before = int(time.time())
        url = sign_artifact_url(_BASE_URL, _ARTIFACT_ID, kr)
        after = int(time.time())
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(url).query)
        expiry = int(qs["expiry"][0])
        assert before + ARTIFACT_URL_EXPIRY_SECONDS <= expiry <= after + ARTIFACT_URL_EXPIRY_SECONDS

    def test_url_omits_artifact_metadata(self) -> None:
        from urllib.parse import parse_qs, urlparse

        url = sign_artifact_url(
            _BASE_URL,
            _ARTIFACT_ID,
            _make_keyring(),
            artifact_name="sensitive-screenshot.png",
            artifact_type="screenshot_action",
        )

        assert set(parse_qs(urlparse(url).query)) == {"expiry", "kid", "sig"}

    def test_sig_is_url_safe_base64_no_padding(self) -> None:
        kr = _make_keyring()
        url = sign_artifact_url(_BASE_URL, _ARTIFACT_ID, kr)
        from urllib.parse import parse_qs, urlparse

        sig = parse_qs(urlparse(url).query)["sig"][0]
        assert "+" not in sig
        assert "/" not in sig
        assert "=" not in sig
        # 32 bytes → ceil(32*4/3) = 43 base64url chars (no padding)
        assert len(sig) == 43

    def test_unknown_current_kid_raises(self) -> None:
        kr = ArtifactHmacKeyring(
            current_kid=_KID_V1,
            keys={_KID_V1: HmacKeyEntry(secret=_SECRET_PLAIN, created_at="2026-01-15")},
        )
        # Manually corrupt current_kid after construction to force the error path
        object.__setattr__(kr, "current_kid", "ghost-kid")
        with pytest.raises(ValueError, match="No secret found"):
            sign_artifact_url(_BASE_URL, _ARTIFACT_ID, kr)

    def test_base_url_trailing_slash_stripped(self) -> None:
        kr = _make_keyring()
        url = sign_artifact_url(_BASE_URL + "/", _ARTIFACT_ID, kr)
        assert "//" not in url.split("://", 1)[1]

    def test_custom_expiry_seconds_used(self) -> None:
        """A non-default expiry_seconds is reflected in the URL's expiry timestamp."""
        from urllib.parse import parse_qs, urlparse

        kr = _make_keyring()
        custom_ttl = 24 * 60 * 60  # 24 hours
        before = int(time.time())
        url = sign_artifact_url(_BASE_URL, _ARTIFACT_ID, kr, expiry_seconds=custom_ttl)
        after = int(time.time())

        expiry = int(parse_qs(urlparse(url).query)["expiry"][0])
        assert before + custom_ttl <= expiry <= after + custom_ttl

    def test_custom_expiry_signature_verifies(self) -> None:
        """A URL signed with a custom expiry still verifies correctly."""
        from urllib.parse import parse_qs, urlparse

        kr = _make_keyring()
        url = sign_artifact_url(_BASE_URL, _ARTIFACT_ID, kr, expiry_seconds=3600)
        qs = parse_qs(urlparse(url).query)
        assert verify_artifact_signature(
            _ARTIFACT_ID,
            qs["expiry"][0],
            qs["kid"][0],
            qs["sig"][0],
            kr,
        )

    def test_none_expiry_seconds_uses_default(self) -> None:
        """expiry_seconds=None falls back to the global 12h default."""
        from urllib.parse import parse_qs, urlparse

        kr = _make_keyring()
        before = int(time.time())
        url = sign_artifact_url(_BASE_URL, _ARTIFACT_ID, kr, expiry_seconds=None)
        after = int(time.time())
        expiry = int(parse_qs(urlparse(url).query)["expiry"][0])
        assert before + ARTIFACT_URL_EXPIRY_SECONDS <= expiry <= after + ARTIFACT_URL_EXPIRY_SECONDS


# ---------------------------------------------------------------------------
# effective_artifact_url_expiry_seconds
# ---------------------------------------------------------------------------


class TestEffectiveArtifactUrlExpirySeconds:
    def test_none_returns_global_default(self) -> None:
        assert effective_artifact_url_expiry_seconds(None) == ARTIFACT_URL_EXPIRY_SECONDS

    def test_value_within_bounds_passes_through(self) -> None:
        # 4 hours
        assert effective_artifact_url_expiry_seconds(4 * 3600) == 4 * 3600

    def test_below_min_clamped_up(self) -> None:
        assert effective_artifact_url_expiry_seconds(1) == ARTIFACT_URL_EXPIRY_SECONDS_MIN

    def test_above_max_clamped_down(self) -> None:
        # 30 days
        assert effective_artifact_url_expiry_seconds(30 * 24 * 3600) == ARTIFACT_URL_EXPIRY_SECONDS_MAX

    def test_exactly_min_passes(self) -> None:
        assert effective_artifact_url_expiry_seconds(ARTIFACT_URL_EXPIRY_SECONDS_MIN) == ARTIFACT_URL_EXPIRY_SECONDS_MIN

    def test_exactly_max_passes(self) -> None:
        assert effective_artifact_url_expiry_seconds(ARTIFACT_URL_EXPIRY_SECONDS_MAX) == ARTIFACT_URL_EXPIRY_SECONDS_MAX


# ---------------------------------------------------------------------------
# artifact_url_expiry_seconds_for_type
# ---------------------------------------------------------------------------


class TestArtifactUrlExpirySecondsForType:
    def test_sensitive_ttl_is_substantially_shorter_than_default(self) -> None:
        assert SENSITIVE_ARTIFACT_URL_EXPIRY_SECONDS <= ARTIFACT_URL_EXPIRY_SECONDS / 4

    def test_sensitive_ttl_is_not_below_the_per_org_floor(self) -> None:
        """Webhook consumers cannot re-mint, so the cap must not dip under the sanctioned floor."""
        assert SENSITIVE_ARTIFACT_URL_EXPIRY_SECONDS >= ARTIFACT_URL_EXPIRY_SECONDS_MIN

    def test_screenshots_and_recordings_are_sensitive(self) -> None:
        assert ArtifactType.RECORDING in SENSITIVE_ARTIFACT_TYPES
        assert ArtifactType.SCREENSHOT_FINAL in SENSITIVE_ARTIFACT_TYPES
        assert ArtifactType.SESSION_REPLAY in SENSITIVE_ARTIFACT_TYPES

    @pytest.mark.parametrize("artifact_type", sorted(SENSITIVE_ARTIFACT_TYPES))
    def test_sensitive_type_is_capped(self, artifact_type: ArtifactType) -> None:
        assert (
            artifact_url_expiry_seconds_for_type(artifact_type, ARTIFACT_URL_EXPIRY_SECONDS)
            == SENSITIVE_ARTIFACT_URL_EXPIRY_SECONDS
        )

    def test_sensitive_type_with_no_expiry_gets_the_cap(self) -> None:
        assert (
            artifact_url_expiry_seconds_for_type(ArtifactType.RECORDING, None) == SENSITIVE_ARTIFACT_URL_EXPIRY_SECONDS
        )

    def test_never_lengthens_an_already_shorter_ttl(self) -> None:
        """The on-demand mint endpoint's 5-minute window must survive the cap."""
        assert (
            artifact_url_expiry_seconds_for_type(ArtifactType.RECORDING, ARTIFACT_URL_ON_DEMAND_EXPIRY_SECONDS)
            == ARTIFACT_URL_ON_DEMAND_EXPIRY_SECONDS
        )

    def test_download_keeps_the_callers_ttl(self) -> None:
        assert (
            artifact_url_expiry_seconds_for_type(ArtifactType.DOWNLOAD, ARTIFACT_URL_EXPIRY_SECONDS)
            == ARTIFACT_URL_EXPIRY_SECONDS
        )

    def test_non_sensitive_type_with_no_expiry_stays_none(self) -> None:
        assert artifact_url_expiry_seconds_for_type(ArtifactType.DOWNLOAD, None) is None

    def test_none_type_passes_through(self) -> None:
        assert artifact_url_expiry_seconds_for_type(None, ARTIFACT_URL_EXPIRY_SECONDS) == ARTIFACT_URL_EXPIRY_SECONDS


# ---------------------------------------------------------------------------
# verify_artifact_signature
# ---------------------------------------------------------------------------


class TestVerifyArtifactSignature:
    def _sign_and_extract(self, keyring: ArtifactHmacKeyring) -> tuple[str, str, str]:
        """Sign and return (expiry, kid, sig) extracted from the URL."""
        from urllib.parse import parse_qs, urlparse

        url = sign_artifact_url(_BASE_URL, _ARTIFACT_ID, keyring)
        qs = parse_qs(urlparse(url).query)
        return qs["expiry"][0], qs["kid"][0], qs["sig"][0]

    def test_valid_signature_returns_true(self) -> None:
        kr = _make_keyring()
        expiry, kid, sig = self._sign_and_extract(kr)
        assert verify_artifact_signature(_ARTIFACT_ID, expiry, kid, sig, kr)

    def test_wrong_artifact_id_returns_false(self) -> None:
        kr = _make_keyring()
        expiry, kid, sig = self._sign_and_extract(kr)
        assert not verify_artifact_signature("art_WRONG", expiry, kid, sig, kr)

    def test_tampered_sig_returns_false(self) -> None:
        kr = _make_keyring()
        expiry, kid, sig = self._sign_and_extract(kr)
        tampered = sig[:-1] + ("A" if sig[-1] != "A" else "B")
        assert not verify_artifact_signature(_ARTIFACT_ID, expiry, kid, tampered, kr)

    def test_expired_url_returns_false(self) -> None:
        kr = _make_keyring()
        past_expiry = str(int(time.time()) - 1)
        path = f"/v1/artifacts/{_ARTIFACT_ID}/content"
        canonical = _canonical_string("GET", path, int(past_expiry), _KID_V1)
        sig = _hmac_b64(kr.get_secret_bytes(_KID_V1), canonical)  # type: ignore[arg-type]
        assert not verify_artifact_signature(_ARTIFACT_ID, past_expiry, _KID_V1, sig, kr)

    def test_unknown_kid_returns_false(self) -> None:
        kr = _make_keyring()
        expiry, _, sig = self._sign_and_extract(kr)
        assert not verify_artifact_signature(_ARTIFACT_ID, expiry, "ghost-kid", sig, kr)

    def test_non_integer_expiry_returns_false(self) -> None:
        kr = _make_keyring()
        _, kid, sig = self._sign_and_extract(kr)
        assert not verify_artifact_signature(_ARTIFACT_ID, "not-a-number", kid, sig, kr)

    def test_old_kid_still_valid_during_rotation(self) -> None:
        """URLs signed with the old key remain valid while it is still in the keyring."""
        old_kr = _make_keyring(current_kid=_KID_V1, secret="oldsecret")
        expiry, kid, sig = self._sign_and_extract(old_kr)

        # Simulate rotation: new kid added and becomes current, old kid retained
        rotated_kr = ArtifactHmacKeyring(
            current_kid=_KID_V2,
            keys={
                _KID_V1: HmacKeyEntry(secret="oldsecret", created_at="2026-01-15"),
                _KID_V2: HmacKeyEntry(secret="newsecret", created_at="2026-03-12"),
            },
        )
        assert verify_artifact_signature(_ARTIFACT_ID, expiry, kid, sig, rotated_kr)

    def test_new_urls_use_new_kid_after_rotation(self) -> None:
        rotated_kr = ArtifactHmacKeyring(
            current_kid=_KID_V2,
            keys={
                _KID_V1: HmacKeyEntry(secret="oldsecret", created_at="2026-01-15"),
                _KID_V2: HmacKeyEntry(secret="newsecret", created_at="2026-03-12"),
            },
        )
        expiry, kid, sig = self._sign_and_extract(rotated_kr)
        assert kid == _KID_V2
        assert verify_artifact_signature(_ARTIFACT_ID, expiry, kid, sig, rotated_kr)

    def test_removed_old_key_invalidates_old_urls(self) -> None:
        """Once the old key is dropped from the keyring, old URLs are rejected."""
        old_kr = _make_keyring(current_kid=_KID_V1, secret="oldsecret")
        expiry, kid, sig = self._sign_and_extract(old_kr)

        # Old key removed after the rotation window
        new_kr = _make_keyring(current_kid=_KID_V2, secret="newsecret")
        object.__setattr__(new_kr, "current_kid", _KID_V2)
        new_kr = ArtifactHmacKeyring(
            current_kid=_KID_V2,
            keys={_KID_V2: HmacKeyEntry(secret="newsecret", created_at="2026-03-12")},
        )
        assert not verify_artifact_signature(_ARTIFACT_ID, expiry, kid, sig, new_kr)


class TestParseArtifactContentUrl:
    def test_parses_signed_first_party_url(self) -> None:
        keyring = _make_keyring()
        url = sign_artifact_url(_BASE_URL, _ARTIFACT_ID, keyring)

        parsed = parse_artifact_content_url(url, _BASE_URL)

        assert parsed is not None
        assert parsed.artifact_id == _ARTIFACT_ID
        assert parsed.kid == _KID_V1
        assert parsed.sig is not None and len(parsed.sig) == 43

    def test_parses_unsigned_first_party_url(self) -> None:
        parsed = parse_artifact_content_url(f"{_BASE_URL}{_CONTENT_PATH}", _BASE_URL)

        assert parsed is not None
        assert parsed.artifact_id == _ARTIFACT_ID
        assert (parsed.expiry, parsed.kid, parsed.sig) == (None, None, None)

    def test_tolerates_trailing_slash_on_base_url(self) -> None:
        parsed = parse_artifact_content_url(f"{_BASE_URL}{_CONTENT_PATH}?sig=abc", f"{_BASE_URL}/")

        assert parsed is not None and parsed.sig == "abc"

    def test_honors_base_url_path_prefix(self) -> None:
        parsed = parse_artifact_content_url(f"{_BASE_URL}/api{_CONTENT_PATH}", f"{_BASE_URL}/api")

        assert parsed is not None and parsed.artifact_id == _ARTIFACT_ID

    def test_rejects_foreign_origin(self) -> None:
        assert parse_artifact_content_url(f"https://evil.example.com{_CONTENT_PATH}", _BASE_URL) is None

    def test_rejects_other_first_party_paths(self) -> None:
        assert parse_artifact_content_url(f"{_BASE_URL}/v1/artifacts/{_ARTIFACT_ID}", _BASE_URL) is None
        assert parse_artifact_content_url(f"{_BASE_URL}/v1/runs/wr_1", _BASE_URL) is None

    def test_rejects_non_http_urls(self) -> None:
        assert parse_artifact_content_url("s3://bucket/key.pdf", _BASE_URL) is None

    def test_rejects_empty_base_url(self) -> None:
        assert parse_artifact_content_url(f"{_BASE_URL}{_CONTENT_PATH}", "") is None
