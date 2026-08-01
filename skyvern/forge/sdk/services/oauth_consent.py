"""Binds an OAuth consent challenge to the caller that started it.

The provider echoes ``state`` back to whichever browser completes consent, so ``state``
alone only proves the challenge is live — not that the caller redeeming it is the one who
asked for it. Storing the derived nonce instead of the raw ``state`` makes that binding a
property of the lookup: the callback re-derives the nonce from its own caller identity, and
a challenge started by someone else simply resolves to a row that does not exist.
"""

from __future__ import annotations

import hashlib
import secrets

from skyvern.config import settings

# Callers with no resolvable user (raw API key, no UI session) share this binding; they
# already hold org-wide credentials, so there is no narrower identity to bind them to.
_UNIDENTIFIED_INITIATOR = "\x00unidentified"
_CONSENT_NONCE_KDF_ITERATIONS = 120_000


def generate_consent_state() -> str:
    """Mint the unguessable ``state`` handed to the OAuth provider."""
    return secrets.token_urlsafe(32)


def consent_nonce(state: str, initiator_id: str | None) -> str:
    """Derive the consent nonce stored against ``state`` for the caller that started the flow.

    An empty, unknown, or foreign ``state`` derives a nonce that was never stored, so every
    such callback fails the lookup instead of falling through to the token exchange.
    """
    material = f"{initiator_id or _UNIDENTIFIED_INITIATOR}\x00{state}".encode()
    return hashlib.pbkdf2_hmac(
        "sha256",
        settings.SECRET_KEY.encode(),
        material,
        _CONSENT_NONCE_KDF_ITERATIONS,
    ).hex()
