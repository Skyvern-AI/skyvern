"""Registers the AES encrypt method from deployment settings.

Shared by the open-source forge app and the cloud bootstrap so both apply the same
fail-closed rules to the configured key material.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable

from skyvern.forge.sdk.encrypt import encryptor
from skyvern.forge.sdk.encrypt.aes import AES

PLACEHOLDER_SECRET_KEY = "fillmein"

# Changing either label re-derives the salt/IV and invalidates every ciphertext a
# deployment stored without explicit ENCRYPTOR_AES_SALT/ENCRYPTOR_AES_IV values.
_SALT_DERIVATION_LABEL = "skyvern.encryptor.aes.salt.v1"
_IV_DERIVATION_LABEL = "skyvern.encryptor.aes.iv.v1"


def _derive_from_secret_key(secret_key: str, label: str) -> str:
    return hmac.new(secret_key.encode("utf-8"), label.encode("utf-8"), hashlib.sha256).hexdigest()


def create_aes_encryptor(
    *,
    secret_key: str,
    salt: str | None = None,
    iv: str | None = None,
    fallback_decrypt_keys: Iterable[tuple[str | None, str | None]] | None = None,
) -> AES:
    """Create AES from deployment settings, deriving unset salt/IV from ``secret_key``.

    Passing ``salt``/``iv`` through to ``AES`` unset would silently select the public
    ``default_salt``/``default_iv`` in ``aes.py``, so derive deployment-private values
    from the operator's secret instead. A placeholder or empty secret key has no such
    fallback and fails closed: encrypting under a public default is worse than
    ENABLE_ENCRYPTION=false because operators believe the data is protected.
    """
    if not secret_key or secret_key == PLACEHOLDER_SECRET_KEY:
        raise RuntimeError(
            "ENABLE_ENCRYPTION=true requires ENCRYPTOR_AES_SECRET_KEY to be set to a real "
            "secret; empty values and the default placeholder 'fillmein' both fail closed."
        )
    fallback_parameters = list(fallback_decrypt_keys or ())
    # Legacy ciphertext has no version marker. Always retry the pre-SHA-normalization
    # parameters on decrypt, including explicit deployment salt/IV settings.
    fallback_parameters.insert(0, (salt, iv))
    return AES(
        secret_key=secret_key,
        salt=salt or _derive_from_secret_key(secret_key, _SALT_DERIVATION_LABEL),
        iv=iv or _derive_from_secret_key(secret_key, _IV_DERIVATION_LABEL),
        fallback_decrypt_keys=fallback_parameters,
    )


def register_aes_encryptor(
    *,
    secret_key: str,
    salt: str | None = None,
    iv: str | None = None,
    fallback_decrypt_keys: Iterable[tuple[str | None, str | None]] | None = None,
) -> None:
    encryptor.add_encrypt_method(
        create_aes_encryptor(
            secret_key=secret_key,
            salt=salt,
            iv=iv,
            fallback_decrypt_keys=fallback_decrypt_keys,
        )
    )
