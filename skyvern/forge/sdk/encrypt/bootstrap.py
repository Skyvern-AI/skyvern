"""Registers the AES-GCM encrypt method and the decrypt-only legacy AES-CBC reader from deployment settings.

Shared by the open-source forge app and the cloud bootstrap so both apply the same
fail-closed rules to the configured key material.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable

from skyvern.forge.sdk.encrypt import encryptor
from skyvern.forge.sdk.encrypt.aes import AES
from skyvern.forge.sdk.encrypt.aes_gcm import AESGCMEncryptor, ScopedAESGCMEncryptor
from skyvern.forge.sdk.encrypt.base import BaseEncryptor, EncryptMethod

PLACEHOLDER_SECRET_KEY = "fillmein"

# Changing either label re-derives the salt/IV and invalidates every ciphertext a
# deployment stored without explicit ENCRYPTOR_AES_SALT/ENCRYPTOR_AES_IV values.
_SALT_DERIVATION_LABEL = "skyvern.encryptor.aes.salt.v1"
_IV_DERIVATION_LABEL = "skyvern.encryptor.aes.iv.v1"


def _derive_from_secret_key(secret_key: str, label: str) -> str:
    return hmac.new(secret_key.encode("utf-8"), label.encode("utf-8"), hashlib.sha256).hexdigest()


def _require_real_secret_key(secret_key: str) -> None:
    if not secret_key or secret_key == PLACEHOLDER_SECRET_KEY:
        raise RuntimeError(
            "ENABLE_ENCRYPTION=true requires ENCRYPTOR_AES_SECRET_KEY to be set to a real "
            "secret; empty values and the default placeholder 'fillmein' both fail closed."
        )


def create_aes_encryptor(
    *,
    secret_key: str,
    salt: str | None = None,
    iv: str | None = None,
    fallback_decrypt_keys: Iterable[tuple[str | None, str | None]] | None = None,
) -> AES:
    """Create the legacy AES-CBC reader from deployment settings, deriving unset salt/IV from ``secret_key``.

    Rows a deployment wrote while salt/IV were unset used the derived values, and rows
    older than the derivation used the public defaults in ``aes.py``, so both stay in the
    decrypt candidate set. A placeholder or empty secret key fails closed, as it does for
    the GCM writer: encrypting under a public default is worse than ENABLE_ENCRYPTION=false
    because operators believe the data is protected.
    """
    _require_real_secret_key(secret_key)
    fallback_parameters = list(fallback_decrypt_keys or ())
    if not (salt and iv):
        # Legacy ciphertext has no version marker, so retry the pre-derivation parameters on decrypt.
        fallback_parameters.insert(0, (salt, iv))
    return AES(
        secret_key=secret_key,
        salt=salt or _derive_from_secret_key(secret_key, _SALT_DERIVATION_LABEL),
        iv=iv or _derive_from_secret_key(secret_key, _IV_DERIVATION_LABEL),
        fallback_decrypt_keys=fallback_parameters,
    )


def create_aes_gcm_encryptor(
    *,
    secret_key: str,
    salt: str | None = None,
    method: EncryptMethod = EncryptMethod.AES_GCM,
) -> AESGCMEncryptor:
    _require_real_secret_key(secret_key)
    if method is EncryptMethod.AES_GCM_V2 and not salt:
        raise RuntimeError("ENCRYPTOR_AES_SALT_V2 is required when ENCRYPTOR_AES_SECRET_KEY_V2 is configured")
    return AESGCMEncryptor(
        secret_key=secret_key,
        salt=salt or _derive_from_secret_key(secret_key, _SALT_DERIVATION_LABEL),
        method=method,
    )


def validate_aes_v2_material(
    *,
    secret_key: str,
    salt: str | None,
    v2_secret_key: str | None,
    v2_salt: str | None,
) -> tuple[str, str] | None:
    if v2_secret_key is None and v2_salt is None:
        return None
    if not v2_secret_key or not v2_salt:
        raise RuntimeError("ENCRYPTOR_AES_SECRET_KEY_V2 and ENCRYPTOR_AES_SALT_V2 must be configured together")
    if v2_secret_key == PLACEHOLDER_SECRET_KEY:
        raise RuntimeError("ENCRYPTOR_AES_SECRET_KEY_V2 must not be a placeholder")
    if v2_secret_key == secret_key or v2_salt == (salt or _derive_from_secret_key(secret_key, _SALT_DERIVATION_LABEL)):
        raise RuntimeError("AES v2 key and salt must differ from v1")
    return v2_secret_key, v2_salt


def create_deployment_gcm_encryptor(
    secret_key: str, salt: str | None, scoped_secret_key: str | None, *, write_scoped: bool = False
) -> BaseEncryptor:
    if write_scoped and not scoped_secret_key:
        raise RuntimeError("ENCRYPTOR_AES_WRITE_SCOPED requires ENCRYPTOR_AES_SCOPED_SECRET_KEY")
    shared = create_aes_gcm_encryptor(secret_key=secret_key, salt=salt)
    gcm: BaseEncryptor = shared
    if scoped_secret_key is not None:
        if scoped_secret_key == secret_key:
            raise RuntimeError("ENCRYPTOR_AES_SCOPED_SECRET_KEY must differ from the primary key")
        scoped = create_aes_gcm_encryptor(secret_key=scoped_secret_key, salt=salt)
        gcm = ScopedAESGCMEncryptor(shared, scoped, write_scoped=write_scoped)

    return gcm


def register_aes_encryptor(
    *,
    secret_key: str,
    salt: str | None = None,
    iv: str | None = None,
    v2_secret_key: str | None = None,
    v2_salt: str | None = None,
    write_version: str = "v1",
    scoped_secret_key: str | None = None,
    write_scoped: bool = False,
    fallback_decrypt_keys: Iterable[tuple[str | None, str | None]] | None = None,
) -> None:
    _require_real_secret_key(secret_key)
    if write_version != "v1":
        raise RuntimeError("Only ENCRYPTOR_AES_WRITE_VERSION=v1 is supported")
    v2_material = validate_aes_v2_material(
        secret_key=secret_key, salt=salt, v2_secret_key=v2_secret_key, v2_salt=v2_salt
    )

    gcm = create_deployment_gcm_encryptor(secret_key, salt, scoped_secret_key, write_scoped=write_scoped)
    encryptor.add_encrypt_method(
        create_aes_encryptor(
            secret_key=secret_key,
            salt=salt,
            iv=iv,
            fallback_decrypt_keys=fallback_decrypt_keys,
        )
    )
    encryptor.add_encrypt_method(gcm)
    if v2_material is not None:
        v2_secret_key, v2_salt = v2_material
        encryptor.add_encrypt_method(
            create_aes_gcm_encryptor(
                secret_key=v2_secret_key,
                salt=v2_salt,
                method=EncryptMethod.AES_GCM_V2,
            )
        )
    else:
        encryptor._methods.pop(EncryptMethod.AES_GCM_V2, None)
