from types import SimpleNamespace

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.workflow.secret_encryption import (
    ENCRYPTED_SECRET_PREFIX,
    decrypt_secret_field_value,
    encrypt_secret_field_value,
    encrypt_workflow_definition_secrets,
    is_encrypted_secret,
)
from skyvern.schemas.workflows import BlockType


@pytest.fixture
def enabled_encryption(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENABLE_ENCRYPTION", True)
    monkeypatch.setattr(settings, "ENCRYPTOR_AES_SECRET_KEY", "unit-test-secret-key-please-0000")
    monkeypatch.setattr(settings, "ENCRYPTOR_AES_SALT", "unit-test-salt-000")


@pytest.fixture
def disabled_encryption(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENABLE_ENCRYPTION", False)
    monkeypatch.setattr(settings, "ENCRYPTOR_AES_SECRET_KEY", "unit-test-secret-key-please-0000")
    monkeypatch.setattr(settings, "ENCRYPTOR_AES_SALT", "unit-test-salt-000")


@pytest.mark.asyncio
async def test_encrypt_secret_field_value_preserves_non_encryptable_values(enabled_encryption: None) -> None:
    jinja_value = "{{ secret_parameter }}"
    encrypted_value = f"{ENCRYPTED_SECRET_PREFIX}aesgcm-v1:existing"

    assert (
        await encrypt_secret_field_value(jinja_value, organization_id="o_a", field_name="sftp_password") == jinja_value
    )
    assert (
        await encrypt_secret_field_value(encrypted_value, organization_id="o_a", field_name="sftp_password")
        == encrypted_value
    )


@pytest.mark.asyncio
async def test_encrypt_secret_field_value_is_disabled(disabled_encryption: None) -> None:
    plaintext = "disabled-test-value"

    assert await encrypt_secret_field_value(plaintext, organization_id="o_a", field_name="sftp_password") == plaintext


@pytest.mark.asyncio
async def test_encrypt_secret_field_value_encrypts_literal(enabled_encryption: None) -> None:
    plaintext = "literal-test-value"

    encrypted = await encrypt_secret_field_value(plaintext, organization_id="o_a", field_name="sftp_password")

    assert encrypted is not None
    assert is_encrypted_secret(encrypted)
    assert encrypted.startswith(f"{ENCRYPTED_SECRET_PREFIX}aesgcm-v1:")
    assert plaintext not in encrypted


@pytest.mark.asyncio
async def test_decrypt_secret_field_value_round_trips(enabled_encryption: None) -> None:
    plaintext = "round-trip-test-value"
    encrypted = await encrypt_secret_field_value(plaintext, organization_id="o_a", field_name="sftp_password")

    assert encrypted is not None
    assert await decrypt_secret_field_value(encrypted, organization_id="o_a", field_name="sftp_password") == plaintext


@pytest.mark.asyncio
async def test_encrypted_secret_cannot_move_to_another_field(enabled_encryption: None) -> None:
    plaintext = "field-binding-test-value"
    encrypted = await encrypt_secret_field_value(
        plaintext,
        organization_id="o_a",
        field_name="aws_secret_access_key",
    )
    assert encrypted is not None

    with pytest.raises(ValueError) as exc_info:
        await decrypt_secret_field_value(encrypted, organization_id="o_a", field_name="sftp_password")

    error = str(exc_info.value)
    assert plaintext not in error
    assert encrypted not in error


@pytest.mark.asyncio
async def test_encrypted_secret_cannot_move_to_another_organization(enabled_encryption: None) -> None:
    plaintext = "organization-binding-test-value"
    encrypted = await encrypt_secret_field_value(
        plaintext,
        organization_id="o_a",
        field_name="aws_secret_access_key",
    )
    assert encrypted is not None

    with pytest.raises(ValueError) as exc_info:
        await decrypt_secret_field_value(encrypted, organization_id="o_b", field_name="aws_secret_access_key")

    error = str(exc_info.value)
    assert plaintext not in error
    assert encrypted not in error


@pytest.mark.asyncio
async def test_encrypt_workflow_definition_secrets_recurses_and_binds_fields(enabled_encryption: None) -> None:
    upload = SimpleNamespace(
        block_type=BlockType.FILE_UPLOAD,
        aws_secret_access_key="upload-test-value",
        azure_storage_account_key=None,
        sftp_password="{{ sftp_password }}",
        sftp_private_key=None,
        sftp_private_key_passphrase=None,
    )
    nested_download = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        aws_secret_access_key=None,
        azure_storage_account_key=None,
        sftp_password="nested-test-value",
        sftp_private_key=None,
        sftp_private_key_passphrase=None,
    )
    loop = SimpleNamespace(block_type=BlockType.FOR_LOOP, loop_blocks=[nested_download])
    non_file = SimpleNamespace(block_type=object(), sftp_password="unchanged-test-value")
    definition = SimpleNamespace(blocks=[upload, loop, non_file])

    await encrypt_workflow_definition_secrets(definition, organization_id="o_x")

    assert is_encrypted_secret(upload.aws_secret_access_key)
    assert upload.sftp_password == "{{ sftp_password }}"
    assert is_encrypted_secret(nested_download.sftp_password)
    assert non_file.sftp_password == "unchanged-test-value"

    with pytest.raises(ValueError) as exc_info:
        await decrypt_secret_field_value(
            nested_download.sftp_password,
            organization_id="o_x",
            field_name="aws_secret_access_key",
        )

    error = str(exc_info.value)
    assert "nested-test-value" not in error
    assert nested_download.sftp_password not in error
