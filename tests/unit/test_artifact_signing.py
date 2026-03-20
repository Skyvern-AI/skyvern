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

from skyvern.forge.sdk.artifact.signing import (
    ARTIFACT_URL_EXPIRY_SECONDS,
    ArtifactHmacKeyring,
    HmacKeyEntry,
    _canonical_string,
    _hmac_b64,
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
