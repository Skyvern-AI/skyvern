from typing import Any

import pytest
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from skyvern.forge.sdk.encrypt import Encryptor
from skyvern.forge.sdk.encrypt import aes as aes_module
from skyvern.forge.sdk.encrypt.aes import AES
from skyvern.forge.sdk.encrypt.aes_gcm import AESGCMEncryptor
from skyvern.forge.sdk.encrypt.base import EncryptMethod, TokenDecryptionError

SECRET = "test-secret-key"
PRIMARY_SALT = "primary_salt_value_xxxxxxxxxxxxxxx"
PRIMARY_IV = "primary_iv_xxxxxxxxx"
PRIOR_SALT = "prior_salt_value_xxxxxxxxxxxxxxxxx"
PRIOR_IV = "prior_iv_value_xxxxx"
# Written by the CBC writer before it was retired, pinned so a derivation change that strands stored rows fails here.
LEGACY_PRIMARY_CIPHERTEXT = "rvmea7ou1gzyata3OwKEQg=="
LEGACY_PRIOR_CIPHERTEXT = "siwQyNbc6raO5BfK8ed/aw=="
# "no fallbacks" under the public default salt/IV (no ENCRYPTOR_AES_SALT / ENCRYPTOR_AES_IV).
LEGACY_PUBLIC_DEFAULT_CIPHERTEXT = "0EgVwps4hm/yPyLpOKypHg=="


@pytest.mark.asyncio
async def test_cbc_encrypt_is_refused_directly_and_through_the_dispatcher() -> None:
    aes = AES(secret_key=SECRET, salt=PRIMARY_SALT, iv=PRIMARY_IV)
    with pytest.raises(NotImplementedError, match="AES-GCM"):
        await aes.encrypt("new value")

    encryptor = Encryptor()
    encryptor.add_encrypt_method(aes)
    with pytest.raises(NotImplementedError, match="AES-GCM"):
        await encryptor.encrypt("new value", EncryptMethod.AES)
    assert await encryptor.decrypt(LEGACY_PRIMARY_CIPHERTEXT, EncryptMethod.AES) == "legacy primary"


@pytest.mark.asyncio
async def test_decrypts_ciphertext_created_with_legacy_primary_normalization() -> None:
    aes = AES(
        secret_key=SECRET,
        salt=PRIMARY_SALT,
        iv=PRIMARY_IV,
        fallback_decrypt_keys=[(PRIMARY_SALT, PRIMARY_IV)],
    )
    assert await aes.decrypt(LEGACY_PRIMARY_CIPHERTEXT) == "legacy primary"


@pytest.mark.asyncio
async def test_decrypt_with_legacy_fallback_after_rotation() -> None:
    rotated = AES(
        secret_key=SECRET,
        salt=PRIMARY_SALT,
        iv=PRIMARY_IV,
        fallback_decrypt_keys=[(PRIOR_SALT, PRIOR_IV)],
    )
    assert await rotated.decrypt(LEGACY_PRIOR_CIPHERTEXT) == "prior path"


@pytest.mark.asyncio
async def test_decrypt_raises_after_exhausting_all_keys() -> None:
    mismatched = AES(
        secret_key=SECRET,
        salt=PRIMARY_SALT,
        iv=PRIMARY_IV,
        fallback_decrypt_keys=[("another_salt_xxxxxxxxxxxx", "another_iv_xxxxxxxx")],
    )
    # Typed so callers that poll can tell a terminal key mismatch from a transient failure.
    with pytest.raises(TokenDecryptionError):
        await mismatched.decrypt(LEGACY_PRIOR_CIPHERTEXT)


@pytest.mark.asyncio
async def test_decrypt_reads_rows_written_under_the_public_defaults() -> None:
    assert await AES(secret_key=SECRET).decrypt(LEGACY_PUBLIC_DEFAULT_CIPHERTEXT) == "no fallbacks"


@pytest.mark.asyncio
async def test_decrypt_tries_multiple_fallbacks_in_order() -> None:
    aes = AES(
        secret_key=SECRET,
        salt=PRIMARY_SALT,
        iv=PRIMARY_IV,
        fallback_decrypt_keys=[
            ("never_used_salt_xxxxxxxxx", "never_used_iv_xxxxx"),
            (PRIOR_SALT, PRIOR_IV),
        ],
    )
    assert await aes.decrypt(LEGACY_PRIOR_CIPHERTEXT) == "prior path"


@pytest.mark.asyncio
async def test_derives_each_salt_key_once_and_still_reads_existing_ciphertext(monkeypatch: pytest.MonkeyPatch) -> None:
    derived_salts: list[bytes] = []

    def counting_pbkdf2(**kwargs: Any) -> PBKDF2HMAC:
        derived_salts.append(kwargs["salt"])
        return PBKDF2HMAC(**kwargs)

    monkeypatch.setattr(aes_module, "PBKDF2HMAC", counting_pbkdf2)
    aes = AES(
        secret_key=SECRET,
        salt=PRIMARY_SALT,
        iv=PRIMARY_IV,
        fallback_decrypt_keys=[(PRIOR_SALT, PRIOR_IV)],
    )

    for _ in range(3):
        assert await aes.decrypt(LEGACY_PRIMARY_CIPHERTEXT) == "legacy primary"
        assert await aes.decrypt(LEGACY_PRIOR_CIPHERTEXT) == "prior path"

    assert len(derived_salts) == 2, "PBKDF2 must run once per salt per process, not on every decrypt"


@pytest.mark.asyncio
@pytest.mark.parametrize("ciphertext", ["not base64 at all!", "rvmea7ou1gzyata3OwKEQg==", "AAAA"])
async def test_decrypt_failures_hide_the_underlying_cause(ciphertext: str) -> None:
    for encryptor in (
        AES(secret_key="other-key", salt=PRIMARY_SALT, iv=PRIMARY_IV),
        AESGCMEncryptor(secret_key=SECRET, salt=None),
    ):
        with pytest.raises(TokenDecryptionError, match="^Failed to decrypt token$") as excinfo:
            await encryptor.decrypt(ciphertext)
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__
