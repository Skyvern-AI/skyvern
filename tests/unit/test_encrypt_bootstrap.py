import base64
from typing import Iterator

import pytest

from skyvern.forge.sdk.encrypt import VERSIONED_CIPHERTEXT_PREFIX, encryptor
from skyvern.forge.sdk.encrypt.aes_gcm import AESGCMEncryptor
from skyvern.forge.sdk.encrypt.base import DEFAULT_ENCRYPT_METHOD, EncryptMethod, TokenDecryptionError
from skyvern.forge.sdk.encrypt.bootstrap import (
    _SALT_DERIVATION_LABEL,
    PLACEHOLDER_SECRET_KEY,
    _derive_from_secret_key,
    register_aes_encryptor,
)

SECRET_KEY = "self-hosted-secret-key-000"
V2_SECRET_KEY = "self-hosted-v2-secret-key-000"
V2_SALT = "self-hosted-v2-salt-000"
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


@pytest.fixture
def v2_fixture_encryptor() -> AESGCMEncryptor:
    return AESGCMEncryptor(secret_key=V2_SECRET_KEY, salt=V2_SALT, method=EncryptMethod.AES_GCM_V2)


@pytest.mark.parametrize(
    ("configuration", "message"),
    [
        ({"secret_key": ""}, "ENCRYPTOR_AES_SECRET_KEY"),
        ({"secret_key": PLACEHOLDER_SECRET_KEY}, "ENCRYPTOR_AES_SECRET_KEY"),
        ({"secret_key": SECRET_KEY, "v2_secret_key": V2_SECRET_KEY}, "configured together"),
        ({"secret_key": SECRET_KEY, "v2_salt": V2_SALT}, "configured together"),
        ({"secret_key": SECRET_KEY, "v2_secret_key": "", "v2_salt": ""}, "configured together"),
        ({"secret_key": SECRET_KEY, "v2_secret_key": PLACEHOLDER_SECRET_KEY, "v2_salt": V2_SALT}, "placeholder"),
        ({"secret_key": SECRET_KEY, "v2_secret_key": SECRET_KEY, "v2_salt": V2_SALT}, "must differ from v1"),
        (
            {"secret_key": SECRET_KEY, "salt": V2_SALT, "v2_secret_key": V2_SECRET_KEY, "v2_salt": V2_SALT},
            "must differ from v1",
        ),
        (
            {
                "secret_key": SECRET_KEY,
                "v2_secret_key": V2_SECRET_KEY,
                "v2_salt": _derive_from_secret_key(SECRET_KEY, _SALT_DERIVATION_LABEL),
            },
            "must differ from v1",
        ),
        ({"secret_key": SECRET_KEY, "write_version": "v2"}, "WRITE_VERSION=v1"),
    ],
)
def test_register_aes_encryptor_rejects_unusable_configuration(configuration: dict[str, str], message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        register_aes_encryptor(
            secret_key=configuration["secret_key"],
            salt=configuration.get("salt"),
            v2_secret_key=configuration.get("v2_secret_key"),
            v2_salt=configuration.get("v2_salt"),
            write_version=configuration.get("write_version", "v1"),
        )

    assert not encryptor._methods


@pytest.mark.asyncio
async def test_default_method_round_trips_and_other_versions_are_read_only(
    v2_fixture_encryptor: AESGCMEncryptor,
) -> None:
    register_aes_encryptor(secret_key=SECRET_KEY, v2_secret_key=V2_SECRET_KEY, v2_salt=V2_SALT)

    assert DEFAULT_ENCRYPT_METHOD is EncryptMethod.AES_GCM
    ciphertext = await encryptor.encrypt("refresh-token", DEFAULT_ENCRYPT_METHOD)
    v2_ciphertext = await v2_fixture_encryptor.encrypt("refresh-token-v2")
    assert ciphertext != "refresh-token"
    assert await encryptor.decrypt(ciphertext, DEFAULT_ENCRYPT_METHOD) == "refresh-token"
    assert await encryptor.decrypt(v2_ciphertext, EncryptMethod.AES_GCM_V2) == "refresh-token-v2"
    with pytest.raises(TokenDecryptionError):
        await encryptor.decrypt(v2_ciphertext, EncryptMethod.AES_GCM)

    with pytest.raises(NotImplementedError, match="AES-GCM"):
        await encryptor.encrypt("refresh-token", EncryptMethod.AES)
    with pytest.raises(NotImplementedError, match="AES v2 writes"):
        await encryptor.encrypt("refresh-token", EncryptMethod.AES_GCM_V2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "salt", "iv", "ciphertext", "readable"),
    [
        (SECRET_KEY, None, None, LEGACY_PUBLIC_DEFAULT_CIPHERTEXT, True),
        (SECRET_KEY, "legacy-salt", None, "HWfEfJWYbO0JqAYUam2rwQ==", True),
        (SECRET_KEY, None, "legacy-iv", "GdJIuflCrG26sExLEulc+g==", True),
        (SECRET_KEY, "explicit-salt", "explicit-iv", LEGACY_EXPLICIT_CIPHERTEXT, True),
        (SECRET_KEY, None, None, LEGACY_DERIVED_CIPHERTEXT, True),
        (SECRET_KEY, None, None, LEGACY_EXPLICIT_CIPHERTEXT, False),
        (f"{SECRET_KEY}-other", None, None, LEGACY_PUBLIC_DEFAULT_CIPHERTEXT, False),
    ],
)
async def test_legacy_parameters(key, salt, iv, ciphertext, readable):
    register_aes_encryptor(secret_key=key, salt=salt, iv=iv)
    if readable:
        assert await encryptor.decrypt(ciphertext, EncryptMethod.AES) == "refresh-token"
    else:
        with pytest.raises(TokenDecryptionError):
            await encryptor.decrypt(ciphertext, EncryptMethod.AES)


@pytest.mark.asyncio
async def test_register_aes_encryptor_adds_gcm_with_a_fresh_nonce_per_encryption() -> None:
    register_aes_encryptor(secret_key=SECRET_KEY)

    assert EncryptMethod.AES_GCM_V2 not in encryptor._methods
    first = await encryptor.encrypt("refresh-token", EncryptMethod.AES_GCM)
    second = await encryptor.encrypt("refresh-token", EncryptMethod.AES_GCM)

    with pytest.raises(TokenDecryptionError):
        await AESGCMEncryptor(secret_key=SECRET_KEY, salt=None).decrypt(first)
    assert first != second
    assert await encryptor.decrypt(first, EncryptMethod.AES_GCM) == "refresh-token"
    assert await encryptor.decrypt(second, EncryptMethod.AES_GCM) == "refresh-token"


@pytest.mark.asyncio
async def test_gcm_rejects_tampered_ciphertext_and_other_deployment_keys(
    v2_fixture_encryptor: AESGCMEncryptor,
) -> None:
    register_aes_encryptor(secret_key=SECRET_KEY, v2_secret_key=V2_SECRET_KEY, v2_salt=V2_SALT)
    ciphertext_by_method = {
        EncryptMethod.AES_GCM: await encryptor.encrypt("refresh-token", EncryptMethod.AES_GCM),
        EncryptMethod.AES_GCM_V2: await v2_fixture_encryptor.encrypt("refresh-token"),
    }

    for method, ciphertext in ciphertext_by_method.items():
        raw = bytearray(base64.b64decode(ciphertext))
        raw[-1] ^= 1
        with pytest.raises(TokenDecryptionError):
            await encryptor.decrypt(base64.b64encode(bytes(raw)).decode(), method)

    register_aes_encryptor(
        secret_key=f"{SECRET_KEY}-other",
        v2_secret_key=f"{V2_SECRET_KEY}-other",
        v2_salt=V2_SALT,
    )
    for method, ciphertext in ciphertext_by_method.items():
        with pytest.raises(TokenDecryptionError):
            await encryptor.decrypt(ciphertext, method)


@pytest.mark.asyncio
async def test_decrypt_unlabeled_reads_gcm_and_legacy_cbc_values(v2_fixture_encryptor: AESGCMEncryptor) -> None:
    register_aes_encryptor(secret_key=SECRET_KEY, v2_secret_key=V2_SECRET_KEY, v2_salt=V2_SALT)
    legacy = "59tEbedVMkasA7GzdJMm1w=="  # "ws://legacy" under the public default salt/IV
    current = await encryptor.encrypt("ws://current", EncryptMethod.AES_GCM)
    v2_ciphertext = await v2_fixture_encryptor.encrypt("ws://v2")
    versioned_v2 = f"{VERSIONED_CIPHERTEXT_PREFIX}{EncryptMethod.AES_GCM_V2.value}:{v2_ciphertext}"

    assert await encryptor.decrypt_unlabeled(legacy) == "ws://legacy"
    assert await encryptor.decrypt_unlabeled(current) == "ws://current"
    assert await encryptor.decrypt_unlabeled(versioned_v2) == "ws://v2"

    for invalid in (
        f"{VERSIONED_CIPHERTEXT_PREFIX}aes_gcm_v3:{current}",
        f"{VERSIONED_CIPHERTEXT_PREFIX}{EncryptMethod.AES_GCM_V2.value}:",
    ):
        with pytest.raises(TokenDecryptionError, match="Failed to decrypt token"):
            await encryptor.decrypt_unlabeled(invalid)

    register_aes_encryptor(secret_key=SECRET_KEY)
    with pytest.raises(TokenDecryptionError, match="Failed to decrypt token"):
        await encryptor.decrypt(v2_ciphertext, EncryptMethod.AES_GCM_V2)
    with pytest.raises(TokenDecryptionError, match="Failed to decrypt token"):
        await encryptor.decrypt_unlabeled(versioned_v2)
    assert await encryptor.decrypt_unlabeled(current) == "ws://current"
    assert await encryptor.decrypt_unlabeled(legacy) == "ws://legacy"


@pytest.mark.asyncio
@pytest.mark.parametrize("write_scoped", [False, True])
async def test_scoped_reader_prefers_shared_key_with_either_writer(monkeypatch, write_scoped):
    salt = "production-fixture-salt"
    shared = AESGCMEncryptor(secret_key=SECRET_KEY, salt=salt)
    scoped = AESGCMEncryptor(secret_key=V2_SECRET_KEY, salt=salt)
    register_aes_encryptor(secret_key=SECRET_KEY, salt=salt, scoped_secret_key=V2_SECRET_KEY, write_scoped=write_scoped)
    written = await encryptor.encrypt("new", DEFAULT_ENCRYPT_METHOD)
    writer, other = (scoped, shared) if write_scoped else (shared, scoped)
    assert await writer.decrypt(written) == "new"
    with pytest.raises(TokenDecryptionError):
        await other.decrypt(written)
    shared_value = await shared.encrypt("old")
    scoped_value = await scoped.encrypt("candidate")
    calls = []
    original = AESGCMEncryptor.decrypt

    async def recording_decrypt(reader, ciphertext):
        calls.append(reader)
        return await original(reader, ciphertext)

    monkeypatch.setattr(AESGCMEncryptor, "decrypt", recording_decrypt)
    pair = encryptor._methods[DEFAULT_ENCRYPT_METHOD]
    assert await encryptor.decrypt(shared_value, DEFAULT_ENCRYPT_METHOD) == "old"
    assert calls == [pair.shared]
    calls.clear()
    assert await encryptor.decrypt_unlabeled(scoped_value) == "candidate"
    assert calls == [pair.shared, pair.scoped]
    register_aes_encryptor(secret_key=SECRET_KEY, salt=salt)
    with pytest.raises(TokenDecryptionError):
        await encryptor.decrypt(scoped_value, DEFAULT_ENCRYPT_METHOD)
    with pytest.raises(RuntimeError, match="SCOPED_SECRET_KEY"):
        register_aes_encryptor(secret_key=SECRET_KEY, write_scoped=True)
