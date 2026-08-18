import base64
import os
import re
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
SENSITIVE_SEND_EMAIL_FIELDS: frozenset[str] = frozenset({"custom_smtp_password"})
ENCRYPTED_SECRET_PREFIX = "skyvern_enc:"
_METHOD = "aesgcm-v1"
_SENTINEL_PREFIX = f"{ENCRYPTED_SECRET_PREFIX}{_METHOD}:"
_NONCE_LEN = 12


def is_encrypted_secret(value: str | None) -> bool:
    # Match the whole encrypted-secret namespace, not just the current method, so no sentinel
    # (including an older method) is ever re-encrypted; decrypt still accepts only the current method.
    return isinstance(value, str) and value.startswith(ENCRYPTED_SECRET_PREFIX)


# Only a bare parameter reference may skip encryption. Arbitrary Jinja expressions
# (e.g. "{{ 7*7 }}") are treated as literals: encrypted at rest and never rendered,
# because rendering would corrupt a password that merely looks like a template.
_FULL_TEMPLATE_REFERENCE_RE = re.compile(r"\s*\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}\s*")


def is_full_template_reference(value: str | None) -> bool:
    """True when the whole value is a single parameter reference (e.g. "{{ my_secret_param }}").

    Used for secret fields where only a real parameter reference may skip encryption:
    a literal password that merely CONTAINS Jinja-looking characters must still be
    encrypted, and must never be rendered as a template.
    """
    return isinstance(value, str) and bool(_FULL_TEMPLATE_REFERENCE_RE.fullmatch(value))


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
    full_template_reference_only: bool = False,
) -> str | None:
    if not value or is_encrypted_secret(value) or not encryption_available():
        return value
    template_skips_encryption = (
        is_full_template_reference(value) if full_template_reference_only else _has_jinja_syntax(value)
    )
    if template_skips_encryption:
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

    async def encrypt_block_fields(
        block: Any, field_names: frozenset[str], *, full_template_reference_only: bool = False
    ) -> None:
        for field_name in field_names:
            value = getattr(block, field_name, None)
            setattr(
                block,
                field_name,
                await encrypt_secret_field_value(
                    value,
                    organization_id=organization_id,
                    field_name=field_name,
                    full_template_reference_only=full_template_reference_only,
                ),
            )

    async def encrypt_blocks(blocks: list[Any]) -> None:
        for block in blocks:
            block_type = getattr(block, "block_type", None)
            if block_type in (BlockType.FILE_UPLOAD, BlockType.FILE_DOWNLOAD):
                await encrypt_block_fields(block, SENSITIVE_DESTINATION_FIELDS)
            elif block_type == BlockType.SEND_EMAIL:
                # A literal password that merely contains Jinja-looking characters must still
                # be encrypted; only a full "{{ param }}" reference stays a template.
                await encrypt_block_fields(block, SENSITIVE_SEND_EMAIL_FIELDS, full_template_reference_only=True)
            elif block_type in (BlockType.FOR_LOOP, BlockType.WHILE_LOOP):
                await encrypt_blocks(getattr(block, "loop_blocks", []))

    await encrypt_blocks(definition.blocks)
