import base64
from typing import Iterator

import pytest

from skyvern.forge.sdk.encrypt import encryptor
from skyvern.forge.sdk.encrypt.aes_gcm import AESGCMEncryptor
from skyvern.forge.sdk.encrypt.base import DEFAULT_ENCRYPT_METHOD, EncryptMethod, TokenDecryptionError
from skyvern.forge.sdk.encrypt.bootstrap import PLACEHOLDER_SECRET_KEY, register_aes_encryptor

SECRET_KEY = "self-hosted-secret-key-000"
# "refresh-token" as the retired CBC writer stored it under SECRET_KEY with each kind of salt/IV a deployment
# could hold.
LEGACY_PUBLIC_DEFAULT_CIPHERTEXT = "TmO1Ph6RwR1dVoSl7iLZ9g=="
LEGACY_EXPLICIT_CIPHERTEXT = "wPXE7jBaF8hk9AZgVPP6rQ=="
# Written under the salt/IV derived from SECRET_KEY while ENCRYPTOR_AES_SALT / ENCRYPTOR_AES_IV were unset. Only the
# derivation reads it (no fallback does), so a derivation change fails here.
LEGACY_DERIVED_CIPHERTEXT = "HiAexXnV+9rKTiIGzNrrig=="


@pytest.fixture(autouse=True)
def isolated_encrypt_methods() -> Iterator[None]:
    original = dict(encryptor._methods)
    encryptor._methods = {}
    try:
        yield
    finally:
        encryptor._methods = original


@pytest.mark.parametrize("secret_key", ["", PLACEHOLDER_SECRET_KEY])
def test_register_aes_encryptor_rejects_unusable_secret_key(secret_key: str) -> None:
    with pytest.raises(RuntimeError, match="ENCRYPTOR_AES_SECRET_KEY"):
        register_aes_encryptor(secret_key=secret_key, salt="a-salt", iv="an-iv")

    assert EncryptMethod.AES not in encryptor._methods


@pytest.mark.asyncio
async def test_default_method_round_trips_and_legacy_cbc_is_read_only() -> None:
    register_aes_encryptor(secret_key=SECRET_KEY)

    assert DEFAULT_ENCRYPT_METHOD is EncryptMethod.AES_GCM
    ciphertext = await encryptor.encrypt("refresh-token", DEFAULT_ENCRYPT_METHOD)
    assert ciphertext != "refresh-token"
    assert await encryptor.decrypt(ciphertext, DEFAULT_ENCRYPT_METHOD) == "refresh-token"

    with pytest.raises(NotImplementedError, match="AES-GCM"):
        await encryptor.encrypt("refresh-token", EncryptMethod.AES)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("salt", "iv", "ciphertext"),
    [
        (None, None, LEGACY_PUBLIC_DEFAULT_CIPHERTEXT),
        ("legacy-salt", None, "HWfEfJWYbO0JqAYUam2rwQ=="),
        (None, "legacy-iv", "GdJIuflCrG26sExLEulc+g=="),
        ("explicit-salt", "explicit-iv", LEGACY_EXPLICIT_CIPHERTEXT),
    ],
)
async def test_register_aes_encryptor_decrypts_ciphertext_created_with_legacy_parameters(
    salt: str | None, iv: str | None, ciphertext: str
) -> None:
    register_aes_encryptor(secret_key=SECRET_KEY, salt=salt, iv=iv)

    assert await encryptor.decrypt(ciphertext, EncryptMethod.AES) == "refresh-token"


@pytest.mark.asyncio
async def test_rows_written_under_the_secret_key_derived_salt_and_iv_stay_readable() -> None:
    register_aes_encryptor(secret_key=SECRET_KEY)

    assert await encryptor.decrypt(LEGACY_DERIVED_CIPHERTEXT, EncryptMethod.AES) == "refresh-token"


@pytest.mark.asyncio
async def test_rows_written_under_other_explicit_parameters_are_not_readable() -> None:
    register_aes_encryptor(secret_key=SECRET_KEY)

    with pytest.raises(TokenDecryptionError):
        await encryptor.decrypt(LEGACY_EXPLICIT_CIPHERTEXT, EncryptMethod.AES)


@pytest.mark.asyncio
async def test_legacy_decrypt_is_scoped_to_the_secret_key() -> None:
    register_aes_encryptor(secret_key=f"{SECRET_KEY}-other")
    with pytest.raises(TokenDecryptionError):
        await encryptor.decrypt(LEGACY_PUBLIC_DEFAULT_CIPHERTEXT, EncryptMethod.AES)


@pytest.mark.asyncio
async def test_gcm_salt_is_derived_from_the_secret_key_when_unset() -> None:
    register_aes_encryptor(secret_key=SECRET_KEY)
    ciphertext = await encryptor.encrypt("refresh-token", EncryptMethod.AES_GCM)

    with pytest.raises(TokenDecryptionError):
        await AESGCMEncryptor(secret_key=SECRET_KEY, salt=None).decrypt(ciphertext)


@pytest.mark.asyncio
async def test_register_aes_encryptor_adds_gcm_with_a_fresh_nonce_per_encryption() -> None:
    register_aes_encryptor(secret_key=SECRET_KEY)

    first = await encryptor.encrypt("refresh-token", EncryptMethod.AES_GCM)
    second = await encryptor.encrypt("refresh-token", EncryptMethod.AES_GCM)

    assert first != second
    assert await encryptor.decrypt(first, EncryptMethod.AES_GCM) == "refresh-token"
    assert await encryptor.decrypt(second, EncryptMethod.AES_GCM) == "refresh-token"


@pytest.mark.asyncio
async def test_gcm_rejects_tampered_ciphertext_and_other_deployment_keys() -> None:
    register_aes_encryptor(secret_key=SECRET_KEY)
    ciphertext = await encryptor.encrypt("refresh-token", EncryptMethod.AES_GCM)
    raw = bytearray(base64.b64decode(ciphertext))
    raw[-1] ^= 1

    with pytest.raises(TokenDecryptionError):
        await encryptor.decrypt(base64.b64encode(bytes(raw)).decode(), EncryptMethod.AES_GCM)

    register_aes_encryptor(secret_key=f"{SECRET_KEY}-other")
    with pytest.raises(TokenDecryptionError):
        await encryptor.decrypt(ciphertext, EncryptMethod.AES_GCM)


@pytest.mark.asyncio
async def test_decrypt_unlabeled_reads_gcm_and_legacy_cbc_values() -> None:
    register_aes_encryptor(secret_key=SECRET_KEY)
    legacy = "59tEbedVMkasA7GzdJMm1w=="  # "ws://legacy" under the public default salt/IV
    current = await encryptor.encrypt("ws://current", EncryptMethod.AES_GCM)

    assert await encryptor.decrypt_unlabeled(legacy) == "ws://legacy"
    assert await encryptor.decrypt_unlabeled(current) == "ws://current"
