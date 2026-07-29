"""Shared expected-value helpers for the keyed ``diagnostic_fingerprint`` contract.

``expected_fingerprint`` re-implements the keyed HMAC independently (it does NOT call the production
helper) so the tests catch any drift in the algorithm while still asserting concrete expected values.
Pair it with the ``fingerprint_secret_key`` fixture (in ``conftest.py``), which pins ``SECRET_KEY`` to
``FINGERPRINT_TEST_SECRET_KEY`` so production and these helpers agree.
"""

from __future__ import annotations

import hashlib
import hmac

# Must stay byte-identical to skyvern.forge.sdk.core.hashing._FP_DOMAIN. The keyed contract tests assert
# full production output against expected_fingerprint(), so any divergence here surfaces as a failure.
_FP_DOMAIN = b"skyvern.download_suffix.diagnostic_fingerprint.v1"

FINGERPRINT_TEST_SECRET_KEY = "sky-unit-test-fingerprint-secret-v1"


def expected_fingerprint(value: str | None, *, key: str = FINGERPRINT_TEST_SECRET_KEY) -> str:
    if value is None:
        return "none"
    if not value:
        return "empty:0"
    digest = hmac.new(
        key.encode("utf-8"),
        _FP_DOMAIN + b"\x00" + value.encode("utf-8", "surrogatepass"),
        hashlib.sha256,
    )
    return f"{digest.hexdigest()[:12]}:{len(value)}"


def bare_sha256_fingerprint(value: str) -> str:
    """The OLD insecure format (unsalted sha256). Used only to prove production no longer emits it."""
    return f"{hashlib.sha256(value.encode()).hexdigest()[:12]}:{len(value)}"
