import base64
import os
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from skyvern.config import settings
from skyvern.schemas.workflows import BlockType, _has_jinja_syntax

SENSITIVE_DESTINATION_FIELDS: frozenset[str] = frozenset(
    {
        "aws_secret_access_key",
        "azure_storage_account_key",
        "sftp_password",
        "sftp_private_key",
        "sftp_private_key_passphrase",
    }
)
ENCRYPTED_SECRET_PREFIX = "skyvern_enc:"
_METHOD = "aesgcm-v1"
_SENTINEL_PREFIX = f"{ENCRYPTED_SECRET_PREFIX}{_METHOD}:"
_NONCE_LEN = 12


def is_encrypted_secret(value: str | None) -> bool:
    # Match the whole encrypted-secret namespace, not just the current method, so no sentinel
    # (including an older method) is ever re-encrypted; decrypt still accepts only the current method.
    return isinstance(value, str) and value.startswith(ENCRYPTED_SECRET_PREFIX)


def encryption_available() -> bool:
    return (
        bool(settings.ENABLE_ENCRYPTION)
        and bool(settings.ENCRYPTOR_AES_SECRET_KEY)
        and settings.ENCRYPTOR_AES_SECRET_KEY != "fillmein"
    )


def _derive_key() -> bytes:
    salt = settings.ENCRYPTOR_AES_SALT.encode() if settings.ENCRYPTOR_AES_SALT else None
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"skyvern-file-destination-secret-aesgcm-v1",
    )
    return hkdf.derive(settings.ENCRYPTOR_AES_SECRET_KEY.encode())


# AAD binds each ciphertext to its org + field so a value moved to another field or org fails to authenticate.
def _binding_aad(organization_id: str | None, field_name: str) -> bytes:
    return b"\x00".join((b"skyvern-file-destination-secret-v1", (organization_id or "").encode(), field_name.encode()))


async def encrypt_secret_field_value(
    value: str | None,
    *,
    organization_id: str | None,
    field_name: str,
) -> str | None:
    if not value or _has_jinja_syntax(value) or is_encrypted_secret(value) or not encryption_available():
        return value
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(_derive_key()).encrypt(nonce, value.encode(), _binding_aad(organization_id, field_name))
    return _SENTINEL_PREFIX + base64.b64encode(nonce + ciphertext).decode()


async def decrypt_secret_field_value(value: str, *, organization_id: str | None, field_name: str) -> str:
    if not is_encrypted_secret(value):
        raise ValueError("Value is not an encrypted secret")
    try:
        raw = base64.b64decode(value[len(_SENTINEL_PREFIX) :], validate=True)
        nonce, ciphertext = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
        if len(nonce) != _NONCE_LEN or not ciphertext:
            raise ValueError
        plaintext = AESGCM(_derive_key()).decrypt(nonce, ciphertext, _binding_aad(organization_id, field_name))
    except Exception:
        # Generic on purpose: never reveal the ciphertext/plaintext or which check failed.
        raise ValueError("Failed to decrypt or authenticate the encrypted secret") from None
    return plaintext.decode()


async def encrypt_workflow_definition_secrets(definition: Any, organization_id: str | None) -> None:
    if not encryption_available():
        return

    async def encrypt_blocks(blocks: list[Any]) -> None:
        for block in blocks:
            block_type = getattr(block, "block_type", None)
            if block_type in (BlockType.FILE_UPLOAD, BlockType.FILE_DOWNLOAD):
                for field_name in SENSITIVE_DESTINATION_FIELDS:
                    value = getattr(block, field_name, None)
                    setattr(
                        block,
                        field_name,
                        await encrypt_secret_field_value(
                            value,
                            organization_id=organization_id,
                            field_name=field_name,
                        ),
                    )
            elif block_type in (BlockType.FOR_LOOP, BlockType.WHILE_LOOP):
                await encrypt_blocks(getattr(block, "loop_blocks", []))

    await encrypt_blocks(definition.blocks)
