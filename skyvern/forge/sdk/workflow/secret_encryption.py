import base64
import json
import os
import re
from copy import deepcopy
from typing import Any, NoReturn

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from skyvern.config import settings
from skyvern.exceptions import SkyvernHTTPException
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
WORKFLOW_SECRET_MASK = "[REDACTED]"
ENCRYPTED_SECRET_PREFIX = "skyvern_enc:"
_METHOD = "aesgcm-v1"
_SENTINEL_PREFIX = f"{ENCRYPTED_SECRET_PREFIX}{_METHOD}:"
_NONCE_LEN = 12
_SENSITIVE_BLOCK_FIELDS = {
    BlockType.FILE_UPLOAD: SENSITIVE_DESTINATION_FIELDS,
    BlockType.FILE_DOWNLOAD: SENSITIVE_DESTINATION_FIELDS,
    BlockType.SEND_EMAIL: SENSITIVE_SEND_EMAIL_FIELDS,
}


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _set_value(item: Any, name: str, value: Any) -> None:
    if isinstance(item, dict):
        item[name] = value
    else:
        setattr(item, name, value)


def _blocks(definition: Any) -> list[Any]:
    blocks = _value(definition, "blocks", [])
    return blocks if isinstance(blocks, list) else []


def _loop_blocks(block: Any) -> list[Any]:
    blocks = _value(block, "loop_blocks", [])
    return blocks if isinstance(blocks, list) else []


def _all_blocks(definition: Any) -> list[Any]:
    blocks: list[Any] = []

    def collect(items: list[Any]) -> None:
        for block in items:
            blocks.append(block)
            if _value(block, "block_type") in (BlockType.FOR_LOOP, BlockType.WHILE_LOOP):
                collect(_loop_blocks(block))

    collect(_blocks(definition))
    return blocks


def _block_identity(block: Any) -> tuple[Any, Any]:
    return _value(block, "block_type"), _value(block, "label")


def _sensitive_fields(block_type: Any) -> frozenset[str]:
    try:
        normalized_type = BlockType(block_type)
    except (TypeError, ValueError):
        return frozenset()
    return _SENSITIVE_BLOCK_FIELDS.get(normalized_type, frozenset())


def _secret_binding_signature(block: Any, secret_field: str) -> tuple[BlockType, str] | None:
    try:
        block_type = BlockType(_value(block, "block_type"))
    except (TypeError, ValueError):
        return None

    binding_fields: tuple[str, ...]
    if secret_field == "custom_smtp_password" and block_type == BlockType.SEND_EMAIL:
        binding_fields = ("custom_smtp_host", "custom_smtp_port", "custom_smtp_username")
    elif secret_field in {"sftp_password", "sftp_private_key", "sftp_private_key_passphrase"}:
        binding_fields = ("sftp_host", "sftp_port", "sftp_username", "sftp_host_key")
    elif secret_field == "aws_secret_access_key":
        binding_fields = ("s3_bucket", "aws_access_key_id", "region_name", "endpoint_url")
    elif secret_field == "azure_storage_account_key":
        binding_fields = ("azure_storage_account_name",)
    else:
        return None

    if not any(_value(block, field) not in (None, "") for field in binding_fields):
        return None

    fields = binding_fields
    if block_type == BlockType.FILE_UPLOAD:
        fields = ("storage_type", *fields)
    elif block_type == BlockType.FILE_DOWNLOAD and secret_field not in {
        "sftp_password",
        "sftp_private_key",
        "sftp_private_key_passphrase",
    }:
        fields = ("download_target", *fields)
    values = {field: _value(block, field) for field in fields}
    if "sftp_port" in values and values["sftp_port"] is None:
        values["sftp_port"] = 22
    if "endpoint_url" in values:
        values["endpoint_url"] = values["endpoint_url"] or None
    return block_type, json.dumps(values, sort_keys=True, default=str)


def _secret_field_applies_to_block(block: Any, secret_field: str) -> bool:
    try:
        block_type = BlockType(_value(block, "block_type"))
    except (TypeError, ValueError):
        return True

    if block_type not in (BlockType.FILE_UPLOAD, BlockType.FILE_DOWNLOAD):
        return True
    if (
        block_type == BlockType.FILE_DOWNLOAD
        and secret_field in {"sftp_password", "sftp_private_key", "sftp_private_key_passphrase"}
        and _value(block, "sftp_source_path") not in (None, "")
    ):
        return True
    selector_field = "storage_type" if block_type == BlockType.FILE_UPLOAD else "download_target"
    selector = _value(block, selector_field)
    selector = getattr(selector, "value", selector)
    if selector in (None, ""):
        return True

    if secret_field == "aws_secret_access_key":
        return selector == "s3"
    if secret_field == "azure_storage_account_key":
        return selector == "azure"
    if secret_field in {"sftp_password", "sftp_private_key", "sftp_private_key_passphrase"}:
        return selector == "sftp"
    return True


def definition_has_inline_secret_fields(definition: Any) -> bool:
    return any(_sensitive_fields(_value(block, "block_type")) for block in _all_blocks(definition))


def _field_was_supplied(block: Any, field_name: str) -> bool:
    if isinstance(block, dict):
        return field_name in block
    fields_set = _value(block, "model_fields_set")
    if fields_set is None:
        fields_set = _value(block, "__fields_set__")
    return field_name in fields_set if fields_set is not None else hasattr(block, field_name)


def _raise_secret_preservation_error(block: Any, secret_field: str, reason: str) -> NoReturn:
    label = _value(block, "label", "<unknown>")
    raise SkyvernHTTPException(
        f"Cannot save '{secret_field}' for workflow block '{label}' because {reason}. "
        "Re-enter the secret and try again.",
        status_code=422,
        error_code="workflow_secret_preservation_failed",
    )


def mask_workflow_definition_secrets(definition: Any) -> bool:
    """Mask literal secrets in a workflow definition without changing parameter references."""
    changed = False

    def mask_blocks(blocks: list[Any]) -> None:
        nonlocal changed
        for block in blocks:
            block_type = _value(block, "block_type")
            for field_name in _sensitive_fields(block_type):
                value = _value(block, field_name)
                if (
                    isinstance(value, str)
                    and value
                    and value != WORKFLOW_SECRET_MASK
                    and not is_full_template_reference(value)
                ):
                    _set_value(block, field_name, WORKFLOW_SECRET_MASK)
                    changed = True
            if block_type in (BlockType.FOR_LOOP, BlockType.WHILE_LOOP):
                mask_blocks(_loop_blocks(block))

    mask_blocks(_blocks(definition))
    return changed


def mask_workflow_for_api(workflow: Any) -> Any:
    """Return a masked copy for API serialization; stored/runtime workflow data stays untouched."""
    definition = _value(workflow, "workflow_definition")
    if not any(
        isinstance(value := _value(block, field_name), str)
        and value
        and value != WORKFLOW_SECRET_MASK
        and not is_full_template_reference(value)
        for block in _all_blocks(definition)
        for field_name in _sensitive_fields(_value(block, "block_type"))
    ):
        return workflow

    visible = workflow.model_copy(deep=True) if hasattr(workflow, "model_copy") else deepcopy(workflow)
    mask_workflow_definition_secrets(_value(visible, "workflow_definition"))
    return visible


def preserve_workflow_definition_secrets(
    definition: Any, previous_definition: Any | None, source_definition: Any | None = None
) -> None:
    """Keep old secrets when an edit omits them or round-trips their API mask."""
    if previous_definition is None:
        for block in _all_blocks(definition):
            for field_name in _sensitive_fields(_value(block, "block_type")):
                if _value(block, field_name) == WORKFLOW_SECRET_MASK:
                    _raise_secret_preservation_error(block, field_name, "no stored block matches the masked value")
        return

    current_blocks = _all_blocks(definition)
    previous_blocks = _all_blocks(previous_definition)
    source_by_identity = (
        {_block_identity(block): block for block in _all_blocks(source_definition)}
        if source_definition is not None
        else {}
    )
    previous_by_identity = {_block_identity(block): block for block in previous_blocks}
    used_previous_secrets: set[tuple[int, str]] = set()
    omitted_secrets_to_check: list[tuple[Any, str]] = []
    for block in current_blocks:
        source = source_by_identity.get(_block_identity(block), block)
        for field_name in _sensitive_fields(_value(block, "block_type")):
            value = _value(block, field_name)
            omitted = value is None and not _field_was_supplied(source, field_name)
            if not _secret_field_applies_to_block(block, field_name):
                if value == WORKFLOW_SECRET_MASK:
                    previous_block = previous_by_identity.get(_block_identity(block))
                    if _value(previous_block, field_name) not in (None, ""):
                        used_previous_secrets.add((id(previous_block), field_name))
                    _set_value(block, field_name, None)
                if omitted or value == WORKFLOW_SECRET_MASK:
                    continue
            if value != WORKFLOW_SECRET_MASK and not omitted:
                previous_block = previous_by_identity.get(_block_identity(block))
                if _value(previous_block, field_name) not in (None, ""):
                    used_previous_secrets.add((id(previous_block), field_name))
                continue

            signature = _secret_binding_signature(block, field_name)
            same_label_block = previous_by_identity.get(_block_identity(block))
            same_label_secret = _value(same_label_block, field_name) if same_label_block is not None else None
            if same_label_secret not in (None, "") and signature != _secret_binding_signature(
                same_label_block, field_name
            ):
                _raise_secret_preservation_error(block, field_name, "its credential destination changed")

            candidates = [
                candidate
                for candidate in previous_blocks
                if _value(candidate, field_name) not in (None, "")
                and (
                    _secret_binding_signature(candidate, field_name) == signature
                    if signature is not None
                    else _block_identity(candidate) == _block_identity(block)
                    and _secret_binding_signature(candidate, field_name) is None
                )
            ]
            if len(candidates) == 1:
                candidate_key = (id(candidates[0]), field_name)
                if candidate_key in used_previous_secrets:
                    _raise_secret_preservation_error(
                        block, field_name, "another block already matches its stored value"
                    )
                _set_value(block, field_name, _value(candidates[0], field_name))
                used_previous_secrets.add(candidate_key)
            elif len(candidates) > 1:
                _raise_secret_preservation_error(block, field_name, "multiple stored blocks match its destination")
            elif value == WORKFLOW_SECRET_MASK:
                _raise_secret_preservation_error(block, field_name, "no stored block matches its destination")
            elif same_label_secret not in (None, ""):
                _raise_secret_preservation_error(block, field_name, "its omitted value may belong to a changed block")
            elif same_label_block is None:
                omitted_secrets_to_check.append((block, field_name))

    for block, field_name in omitted_secrets_to_check:
        if any(
            _value(candidate, field_name) not in (None, "")
            and _value(candidate, "block_type") == _value(block, "block_type")
            and _secret_field_applies_to_block(candidate, field_name)
            and (id(candidate), field_name) not in used_previous_secrets
            for candidate in previous_blocks
        ):
            _raise_secret_preservation_error(block, field_name, "its omitted value may belong to a changed block")


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
            fields = _sensitive_fields(block_type)
            if fields:
                await encrypt_block_fields(
                    block,
                    fields,
                    full_template_reference_only=block_type == BlockType.SEND_EMAIL,
                )
            elif block_type in (BlockType.FOR_LOOP, BlockType.WHILE_LOOP):
                await encrypt_blocks(getattr(block, "loop_blocks", []))

    await encrypt_blocks(definition.blocks)
