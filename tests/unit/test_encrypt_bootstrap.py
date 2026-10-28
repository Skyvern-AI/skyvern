import base64
from typing import Iterator

import pytest

from skyvern.forge.sdk.encrypt import encryptor
from skyvern.forge.sdk.encrypt.aes import AES, default_iv, default_salt
from skyvern.forge.sdk.encrypt.base import EncryptMethod, TokenDecryptionError
from skyvern.forge.sdk.encrypt.bootstrap import PLACEHOLDER_SECRET_KEY, register_aes_encryptor

SECRET_KEY = "self-hosted-secret-key-000"


@pytest.fixture(autouse=True)
def isolated_encrypt_methods() -> Iterator[None]:
    original = dict(encryptor._methods)
    encryptor._methods = {}
    try:
        yield
    finally:
        encryptor._methods = original


def registered_aes() -> AES:
    method = encryptor._methods[EncryptMethod.AES]
    assert isinstance(method, AES)
    return method


@pytest.mark.parametrize("secret_key", ["", PLACEHOLDER_SECRET_KEY])
def test_register_aes_encryptor_rejects_unusable_secret_key(secret_key: str) -> None:
    with pytest.raises(RuntimeError, match="ENCRYPTOR_AES_SECRET_KEY"):
        register_aes_encryptor(secret_key=secret_key, salt="a-salt", iv="an-iv")

    assert EncryptMethod.AES not in encryptor._methods


@pytest.mark.asyncio
async def test_register_aes_encryptor_round_trips_without_salt_or_iv() -> None:
    register_aes_encryptor(secret_key=SECRET_KEY)

    ciphertext = await encryptor.encrypt("refresh-token", EncryptMethod.AES)
    assert ciphertext != "refresh-token"
    assert await encryptor.decrypt(ciphertext, EncryptMethod.AES) == "refresh-token"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("salt", "iv"),
    [(None, None), ("legacy-salt", None), (None, "legacy-iv")],
)
async def test_register_aes_encryptor_decrypts_ciphertext_created_with_legacy_parameters(
    salt: str | None, iv: str | None
) -> None:
    ciphertext = await AES(secret_key=SECRET_KEY, salt=salt, iv=iv).encrypt("refresh-token")

    register_aes_encryptor(secret_key=SECRET_KEY, salt=salt, iv=iv)

    assert await encryptor.decrypt(ciphertext, EncryptMethod.AES) == "refresh-token"


def test_derived_salt_and_iv_are_not_the_public_defaults() -> None:
    register_aes_encryptor(secret_key=SECRET_KEY)
    derived = registered_aes()

    assert derived.salt != default_salt
    assert derived.iv != default_iv


@pytest.mark.asyncio
async def test_explicit_salt_and_iv_take_precedence_over_derivation() -> None:
    register_aes_encryptor(secret_key=SECRET_KEY, salt="explicit-salt", iv="explicit-iv")
    explicit_ciphertext = await encryptor.encrypt("refresh-token", EncryptMethod.AES)

    register_aes_encryptor(secret_key=SECRET_KEY)
    derived_ciphertext = await encryptor.encrypt("refresh-token", EncryptMethod.AES)

    assert explicit_ciphertext != derived_ciphertext


@pytest.mark.asyncio
async def test_derivation_is_scoped_to_the_secret_key() -> None:
    register_aes_encryptor(secret_key=SECRET_KEY)
    ciphertext = await encryptor.encrypt("refresh-token", EncryptMethod.AES)

    register_aes_encryptor(secret_key=f"{SECRET_KEY}-other")
    with pytest.raises(Exception, match="Failed to decrypt token"):
        await encryptor.decrypt(ciphertext, EncryptMethod.AES)


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
    legacy = await encryptor.encrypt("ws://legacy", EncryptMethod.AES)
    current = await encryptor.encrypt("ws://current", EncryptMethod.AES_GCM)

    assert await encryptor.decrypt_unlabeled(legacy) == "ws://legacy"
    assert await encryptor.decrypt_unlabeled(current) == "ws://current"
