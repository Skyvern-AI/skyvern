import hashlib

import pytest

from skyvern.forge.sdk.encrypt.aes import AES

SECRET = "test-secret-key"
PRIMARY_SALT = "primary_salt_value_xxxxxxxxxxxxxxx"
PRIMARY_IV = "primary_iv_xxxxxxxxx"
PRIOR_SALT = "prior_salt_value_xxxxxxxxxxxxxxxxx"
PRIOR_IV = "prior_iv_value_xxxxx"
LEGACY_PRIMARY_CIPHERTEXT = "rvmea7ou1gzyata3OwKEQg=="


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
    legacy = AES(secret_key=SECRET, salt=PRIOR_SALT, iv=PRIOR_IV)
    ciphertext = await legacy.encrypt("hello world")

    rotated = AES(
        secret_key=SECRET,
        salt=PRIMARY_SALT,
        iv=PRIMARY_IV,
        fallback_decrypt_keys=[(PRIOR_SALT, PRIOR_IV)],
    )
    assert await rotated.decrypt(ciphertext) == "hello world"


@pytest.mark.asyncio
async def test_decrypt_uses_primary_first_when_round_tripping() -> None:
    aes = AES(
        secret_key=SECRET,
        salt=PRIMARY_SALT,
        iv=PRIMARY_IV,
        fallback_decrypt_keys=[(PRIOR_SALT, PRIOR_IV)],
    )
    ciphertext = await aes.encrypt("primary path")
    assert await aes.decrypt(ciphertext) == "primary path"


@pytest.mark.asyncio
async def test_decrypt_raises_after_exhausting_all_keys() -> None:
    legacy = AES(secret_key=SECRET, salt=PRIOR_SALT, iv=PRIOR_IV)
    ciphertext = await legacy.encrypt("unreachable")

    mismatched = AES(
        secret_key=SECRET,
        salt=PRIMARY_SALT,
        iv=PRIMARY_IV,
        fallback_decrypt_keys=[("another_salt_xxxxxxxxxxxx", "another_iv_xxxxxxxx")],
    )
    with pytest.raises(Exception, match="Failed to decrypt token"):
        await mismatched.decrypt(ciphertext)


@pytest.mark.asyncio
async def test_decrypt_without_fallbacks_still_works() -> None:
    aes = AES(secret_key=SECRET, salt=PRIMARY_SALT, iv=PRIMARY_IV)
    ciphertext = await aes.encrypt("no fallbacks")
    assert await aes.decrypt(ciphertext) == "no fallbacks"


@pytest.mark.asyncio
async def test_decrypt_tries_multiple_fallbacks_in_order() -> None:
    legacy = AES(secret_key=SECRET, salt=PRIOR_SALT, iv=PRIOR_IV)
    ciphertext = await legacy.encrypt("third match")

    aes = AES(
        secret_key=SECRET,
        salt=PRIMARY_SALT,
        iv=PRIMARY_IV,
        fallback_decrypt_keys=[
            ("never_used_salt_xxxxxxxxx", "never_used_iv_xxxxx"),
            (PRIOR_SALT, PRIOR_IV),
        ],
    )
    assert await aes.decrypt(ciphertext) == "third match"


@pytest.mark.asyncio
async def test_decrypt_reads_sha256_normalized_ciphertext() -> None:
    # Open-source releases normalized salt/IV with sha256 for a window. A deployment
    # upgrading off one of those has stored ciphertext that only those parameters open.
    writer = AES(secret_key=SECRET, salt=PRIMARY_SALT, iv=PRIMARY_IV)
    writer.salt = hashlib.sha256(PRIMARY_SALT.encode("utf-8")).digest()
    writer.iv = hashlib.sha256(PRIMARY_IV.encode("utf-8")).digest()[:16]
    ciphertext = await writer.encrypt("sha normalized")

    assert await AES(secret_key=SECRET, salt=PRIMARY_SALT, iv=PRIMARY_IV).decrypt(ciphertext) == "sha normalized"


@pytest.mark.asyncio
async def test_encrypt_output_still_opens_with_md5_parameters_alone() -> None:
    # Guards the rollback path: a release that knows only md5 normalization has to be
    # able to read anything this code writes, so encrypt must not move off md5 yet.
    ciphertext = await AES(secret_key=SECRET, salt=PRIMARY_SALT, iv=PRIMARY_IV).encrypt("md5 normalized")

    # Derive the reader's parameters here rather than through AES, so this still
    # fails if the encrypt path moves off md5.
    md5_only = AES(secret_key=SECRET, salt=PRIMARY_SALT, iv=PRIMARY_IV)
    md5_only.salt = hashlib.md5(PRIMARY_SALT.encode("utf-8"), usedforsecurity=False).digest()
    md5_only.iv = hashlib.md5(PRIMARY_IV.encode("utf-8"), usedforsecurity=False).digest()
    md5_only._fallback_decrypt_params = []
    assert await md5_only.decrypt(ciphertext) == "md5 normalized"
