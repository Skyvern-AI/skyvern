import pytest

from skyvern.forge.sdk.encrypt.aes import AES

SECRET = "test-secret-key"
PRIMARY_SALT = "primary_salt_value_xxxxxxxxxxxxxxx"
PRIMARY_IV = "primary_iv_xxxxxxxxx"
PRIOR_SALT = "prior_salt_value_xxxxxxxxxxxxxxxxx"
PRIOR_IV = "prior_iv_value_xxxxx"


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
