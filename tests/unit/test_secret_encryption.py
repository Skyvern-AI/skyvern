from copy import deepcopy
from types import SimpleNamespace

import pytest

from skyvern.config import settings
from skyvern.exceptions import SkyvernHTTPException
from skyvern.forge.sdk.workflow.secret_encryption import (
    ENCRYPTED_SECRET_PREFIX,
    WORKFLOW_SECRET_MASK,
    decrypt_secret_field_value,
    encrypt_secret_field_value,
    encrypt_workflow_definition_secrets,
    is_encrypted_secret,
    mask_workflow_definition_secrets,
    mask_workflow_for_api,
    preserve_workflow_definition_secrets,
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


@pytest.mark.asyncio
async def test_workflow_secret_mask_round_trip_preserves_and_replaces_runtime_value(
    enabled_encryption: None,
) -> None:
    from skyvern.forge.sdk.workflow.models.block import _resolve_sensitive_block_secret

    plaintext = "synthetic-private-key-for-round-trip"
    stored_block = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        label="download",
        sftp_host="sftp.example.invalid",
        sftp_username="synthetic-user",
        sftp_private_key=plaintext,
        sftp_password=None,
        sftp_private_key_passphrase=None,
        aws_secret_access_key=None,
        azure_storage_account_key=None,
    )
    stored_definition = SimpleNamespace(
        blocks=[SimpleNamespace(block_type=BlockType.FOR_LOOP, label="loop", loop_blocks=[stored_block])]
    )
    await encrypt_workflow_definition_secrets(stored_definition, organization_id="o_test")
    stored_value = stored_block.sftp_private_key
    assert is_encrypted_secret(stored_value)

    # Reads are masked on a copy; saving that representation retains the stored ciphertext.
    read_definition = deepcopy(stored_definition)
    mask_workflow_definition_secrets(read_definition)
    assert read_definition.blocks[0].loop_blocks[0].sftp_private_key == WORKFLOW_SECRET_MASK
    assert stored_block.sftp_private_key == stored_value

    unchanged_save = deepcopy(read_definition)
    preserve_workflow_definition_secrets(unchanged_save, stored_definition)
    assert unchanged_save.blocks[0].loop_blocks[0].sftp_private_key == stored_value
    context = SimpleNamespace(organization_id="o_test", get_original_secret_value_or_none=lambda _: None)
    assert (
        await _resolve_sensitive_block_secret(
            context, unchanged_save.blocks[0].loop_blocks[0].sftp_private_key, "sftp_private_key"
        )
        == plaintext
    )

    replacement = "synthetic-replacement-private-key"
    changed_save = deepcopy(read_definition)
    changed_save.blocks[0].loop_blocks[0].sftp_private_key = replacement
    preserve_workflow_definition_secrets(changed_save, stored_definition)
    await encrypt_workflow_definition_secrets(changed_save, organization_id="o_test")
    assert is_encrypted_secret(changed_save.blocks[0].loop_blocks[0].sftp_private_key)
    assert (
        await _resolve_sensitive_block_secret(
            context, changed_save.blocks[0].loop_blocks[0].sftp_private_key, "sftp_private_key"
        )
        == replacement
    )


def test_workflow_secret_mask_survives_block_rename_and_loop_move() -> None:
    secret = "synthetic-renamed-private-key"
    previous_block = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        label="download",
        sftp_host="sftp.example.invalid",
        sftp_username="synthetic-user",
        sftp_private_key=secret,
        sftp_password=None,
        sftp_private_key_passphrase=None,
        aws_secret_access_key=None,
        azure_storage_account_key=None,
    )
    previous_definition = SimpleNamespace(
        blocks=[SimpleNamespace(block_type=BlockType.FOR_LOOP, label="loop", loop_blocks=[previous_block])]
    )
    current_definition = deepcopy(previous_definition)
    current_block = current_definition.blocks[0].loop_blocks.pop()
    current_block.label = "renamed-download"
    current_definition.blocks.append(current_block)
    mask_workflow_definition_secrets(current_definition)

    preserve_workflow_definition_secrets(current_definition, previous_definition)

    assert current_definition.blocks[1].sftp_private_key == secret


def test_mask_workflow_definition_secrets_reports_only_real_changes() -> None:
    definition = {
        "blocks": [
            {
                "block_type": BlockType.FILE_DOWNLOAD,
                "sftp_private_key": "synthetic-mask-change-key",
            }
        ]
    }

    assert mask_workflow_definition_secrets(definition)
    assert definition["blocks"][0]["sftp_private_key"] == WORKFLOW_SECRET_MASK
    assert not mask_workflow_definition_secrets(definition)


def test_unmatched_masked_workflow_secret_rejects_save() -> None:
    previous = SimpleNamespace(
        blocks=[
            SimpleNamespace(
                block_type=BlockType.FILE_DOWNLOAD,
                label="download",
                navigation_goal="Download the configured file",
                download_url="https://old.example.invalid/file",
                sftp_host="old-sftp.example.invalid",
                sftp_username="synthetic-user",
                sftp_private_key="synthetic-unmatched-private-key",
                sftp_password=None,
                sftp_private_key_passphrase=None,
                aws_secret_access_key=None,
                azure_storage_account_key=None,
            )
        ]
    )
    current = SimpleNamespace(
        blocks=[
            SimpleNamespace(
                block_type=BlockType.FILE_DOWNLOAD,
                label="renamed_download",
                navigation_goal="Download the updated file",
                download_url="https://new.example.invalid/file",
                sftp_host="new-sftp.example.invalid",
                sftp_username="synthetic-user",
                sftp_private_key=WORKFLOW_SECRET_MASK,
                sftp_password=None,
                sftp_private_key_passphrase=None,
                aws_secret_access_key=None,
                azure_storage_account_key=None,
            )
        ]
    )

    with pytest.raises(SkyvernHTTPException) as exc_info:
        preserve_workflow_definition_secrets(current, previous)

    assert exc_info.value.status_code == 422
    assert exc_info.value.error_code == "workflow_secret_preservation_failed"


def test_repeated_secret_destinations_reject_ambiguous_matching() -> None:
    def download(label: str, secret: str) -> SimpleNamespace:
        return SimpleNamespace(
            block_type=BlockType.FILE_DOWNLOAD,
            label=label,
            navigation_goal="Download the configured file",
            sftp_host="sftp.example.invalid",
            sftp_username="synthetic-user",
            sftp_private_key=secret,
            sftp_password=None,
            sftp_private_key_passphrase=None,
            aws_secret_access_key=None,
            azure_storage_account_key=None,
        )

    previous = SimpleNamespace(
        blocks=[download("first", "synthetic-first-key"), download("second", "synthetic-second-key")]
    )
    current = SimpleNamespace(
        blocks=[download("renamed_first", WORKFLOW_SECRET_MASK), download("renamed_second", WORKFLOW_SECRET_MASK)]
    )

    with pytest.raises(SkyvernHTTPException) as exc_info:
        preserve_workflow_definition_secrets(current, previous)

    assert exc_info.value.status_code == 422
    assert [block.sftp_private_key for block in current.blocks] == [WORKFLOW_SECRET_MASK, WORKFLOW_SECRET_MASK]


def test_masked_secret_does_not_match_when_destination_fields_are_missing() -> None:
    previous_block = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        label="download",
        sftp_private_key="synthetic-unbound-private-key",
    )
    current_block = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        label="renamed_download",
        sftp_private_key=WORKFLOW_SECRET_MASK,
    )

    with pytest.raises(SkyvernHTTPException) as exc_info:
        preserve_workflow_definition_secrets(
            SimpleNamespace(blocks=[current_block]),
            SimpleNamespace(blocks=[previous_block]),
        )

    assert exc_info.value.error_code == "workflow_secret_preservation_failed"
    assert current_block.sftp_private_key == WORKFLOW_SECRET_MASK


def test_same_label_masked_secret_rejects_signature_mismatch_even_with_alternative() -> None:
    def download(label: str, navigation_goal: str, secret: str) -> SimpleNamespace:
        return SimpleNamespace(
            block_type=BlockType.FILE_DOWNLOAD,
            label=label,
            navigation_goal=navigation_goal,
            download_url=f"https://{label}.example.invalid/file",
            sftp_host=f"sftp-{label}.example.invalid",
            sftp_username=label,
            sftp_private_key=secret,
            sftp_password=None,
            sftp_private_key_passphrase=None,
            aws_secret_access_key=None,
            azure_storage_account_key=None,
        )

    previous = SimpleNamespace(
        blocks=[
            download("upload", "Download original A", "synthetic-key-a"),
            download("download", "Download original B", "synthetic-key-b"),
        ]
    )
    current_block = deepcopy(previous.blocks[1])
    current_block.label = "upload"
    current_block.sftp_private_key = WORKFLOW_SECRET_MASK
    current = SimpleNamespace(blocks=[current_block])

    with pytest.raises(SkyvernHTTPException) as exc_info:
        preserve_workflow_definition_secrets(current, previous)

    assert exc_info.value.error_code == "workflow_secret_preservation_failed"
    assert current_block.sftp_private_key == WORKFLOW_SECRET_MASK
    assert "upload" in (exc_info.value.message or "")
    assert "Re-enter the secret" in (exc_info.value.message or "")


def test_swapped_labels_reject_masked_secrets_across_signature_changes() -> None:
    def download(label: str, url: str, secret: str) -> SimpleNamespace:
        return SimpleNamespace(
            block_type=BlockType.FILE_DOWNLOAD,
            label=label,
            download_url=url,
            sftp_host=f"sftp-{label}.example.invalid",
            sftp_username=label,
            sftp_private_key=secret,
            sftp_password=None,
            sftp_private_key_passphrase=None,
            aws_secret_access_key=None,
            azure_storage_account_key=None,
        )

    previous = SimpleNamespace(
        blocks=[
            download("first", "https://a.example.invalid/file", "synthetic-swap-key-a"),
            download("second", "https://b.example.invalid/file", "synthetic-swap-key-b"),
        ]
    )
    current_first = deepcopy(previous.blocks[0])
    current_second = deepcopy(previous.blocks[1])
    current_first.label, current_second.label = current_second.label, current_first.label
    current_first.sftp_private_key = WORKFLOW_SECRET_MASK
    current_second.sftp_private_key = WORKFLOW_SECRET_MASK
    current = SimpleNamespace(blocks=[current_first, current_second])

    with pytest.raises(SkyvernHTTPException) as exc_info:
        preserve_workflow_definition_secrets(current, previous)

    assert exc_info.value.error_code == "workflow_secret_preservation_failed"
    assert current_first.sftp_private_key == WORKFLOW_SECRET_MASK
    assert current_second.sftp_private_key == WORKFLOW_SECRET_MASK


def test_same_label_destination_change_requires_secret_reentry_when_sibling_is_deleted() -> None:
    def upload(label: str, bucket_name: str, secret: str) -> SimpleNamespace:
        return SimpleNamespace(
            block_type=BlockType.FILE_UPLOAD,
            label=label,
            s3_bucket=bucket_name,
            aws_secret_access_key=secret,
            azure_storage_account_key=None,
            sftp_password=None,
            sftp_private_key=None,
            sftp_private_key_passphrase=None,
        )

    previous = SimpleNamespace(
        blocks=[
            upload("first", "bucket-a", "synthetic-delete-sibling-key-a"),
            upload("second", "bucket-b", "synthetic-delete-sibling-key-b"),
        ]
    )
    edited_first = upload("first", "bucket-a-renamed", WORKFLOW_SECRET_MASK)
    current = SimpleNamespace(blocks=[edited_first])

    with pytest.raises(SkyvernHTTPException) as exc_info:
        preserve_workflow_definition_secrets(current, previous)

    assert exc_info.value.error_code == "workflow_secret_preservation_failed"
    assert edited_first.aws_secret_access_key == WORKFLOW_SECRET_MASK


def test_same_label_secret_reuse_without_destination_match_fails_closed() -> None:
    def download(label: str, url: str, host: str, username: str, secret: str) -> SimpleNamespace:
        return SimpleNamespace(
            block_type=BlockType.FILE_DOWNLOAD,
            label=label,
            url=url,
            sftp_host=host,
            sftp_username=username,
            sftp_private_key=secret,
            sftp_password=None,
            sftp_private_key_passphrase=None,
            aws_secret_access_key=None,
            azure_storage_account_key=None,
        )

    previous = SimpleNamespace(
        blocks=[
            download(
                "download",
                "https://old-a.example.invalid/file",
                "sftp-a.example.invalid",
                "user-a",
                "synthetic-old-a-key",
            ),
            download(
                "other",
                "https://old-b.example.invalid/file",
                "sftp-b.example.invalid",
                "user-b",
                "synthetic-old-b-key",
            ),
        ]
    )
    current_block = download(
        "download",
        "https://new.example.invalid/file",
        "sftp-new.example.invalid",
        "user-new",
        WORKFLOW_SECRET_MASK,
    )
    current = SimpleNamespace(blocks=[current_block])

    with pytest.raises(SkyvernHTTPException) as exc_info:
        preserve_workflow_definition_secrets(current, previous)

    assert exc_info.value.error_code == "workflow_secret_preservation_failed"
    assert current_block.sftp_private_key == WORKFLOW_SECRET_MASK
    assert "download" in (exc_info.value.message or "")
    assert "Re-enter the secret" in (exc_info.value.message or "")


def test_omitted_secret_is_preserved_when_renamed_block_changes_navigation_goal() -> None:
    secret = "synthetic-omitted-renamed-key"
    previous = SimpleNamespace(
        blocks=[
            SimpleNamespace(
                block_type=BlockType.FILE_DOWNLOAD,
                label="download",
                navigation_goal="Download old instructions",
                download_url="https://files.example.invalid/archive",
                sftp_host="sftp.example.invalid",
                sftp_username="synthetic-user",
                sftp_private_key=secret,
                sftp_password=None,
                sftp_private_key_passphrase=None,
                aws_secret_access_key=None,
                azure_storage_account_key=None,
            )
        ]
    )
    current_block = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        label="renamed_download",
        navigation_goal="Download revised instructions",
        download_url="https://files.example.invalid/archive",
        sftp_host="sftp.example.invalid",
        sftp_port=22,
        sftp_username="synthetic-user",
        sftp_private_key=None,
        sftp_password=None,
        sftp_private_key_passphrase=None,
        aws_secret_access_key=None,
        azure_storage_account_key=None,
    )
    current = SimpleNamespace(blocks=[current_block])
    source_block = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        label="renamed_download",
        model_fields_set={"block_type", "label", "navigation_goal", "download_url"},
    )
    source = SimpleNamespace(blocks=[source_block])

    preserve_workflow_definition_secrets(current, previous, source)

    assert current_block.sftp_private_key == secret


def test_omitted_secret_is_preserved_when_source_url_and_path_change() -> None:
    previous_block = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        label="download",
        download_target="sftp",
        navigation_goal="Download the configured file",
        url="https://old.example.invalid/file",
        sftp_source_path="/old/path",
        sftp_host="sftp.example.invalid",
        sftp_username="synthetic-user",
        sftp_private_key="synthetic-renamed-download-key",
        sftp_password=None,
        sftp_private_key_passphrase=None,
        aws_secret_access_key=None,
        azure_storage_account_key=None,
    )
    current_block = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        label="renamed_download",
        download_target="sftp",
        navigation_goal="Download the configured file",
        url="https://new.example.invalid/file",
        sftp_source_path="/new/path",
        sftp_host="sftp.example.invalid",
        sftp_username="synthetic-user",
        sftp_private_key=None,
        sftp_password=None,
        sftp_private_key_passphrase=None,
        aws_secret_access_key=None,
        azure_storage_account_key=None,
    )
    source_block = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        label="renamed_download",
        download_target="sftp",
        navigation_goal="Download the configured file",
        url="https://new.example.invalid/file",
        sftp_source_path="/new/path",
        sftp_host="sftp.example.invalid",
        sftp_username="synthetic-user",
        model_fields_set={
            "block_type",
            "label",
            "download_target",
            "navigation_goal",
            "url",
            "sftp_source_path",
            "sftp_host",
            "sftp_username",
        },
    )

    preserve_workflow_definition_secrets(
        SimpleNamespace(blocks=[current_block]),
        SimpleNamespace(blocks=[previous_block]),
        SimpleNamespace(blocks=[source_block]),
    )

    assert current_block.sftp_private_key == "synthetic-renamed-download-key"


def test_omitted_sftp_source_secret_is_preserved_when_delivery_target_changes() -> None:
    secret = "synthetic-sftp-source-private-key"
    previous_block = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        label="download",
        download_target="website",
        sftp_source_path="/source/path",
        sftp_host="sftp.example.invalid",
        sftp_port=22,
        sftp_username="synthetic-user",
        sftp_host_key="synthetic-host-key",
        sftp_private_key=secret,
        sftp_password=None,
        sftp_private_key_passphrase=None,
    )
    current_block = deepcopy(previous_block)
    current_block.download_target = "s3"
    current_block.s3_bucket = "synthetic-output-bucket"
    current_block.sftp_private_key = None
    source_block = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        label="download",
        download_target="s3",
        sftp_source_path="/source/path",
        sftp_host="sftp.example.invalid",
        sftp_port=22,
        sftp_username="synthetic-user",
        sftp_host_key="synthetic-host-key",
        model_fields_set={
            "block_type",
            "label",
            "download_target",
            "sftp_source_path",
            "sftp_host",
            "sftp_port",
            "sftp_username",
            "sftp_host_key",
            "s3_bucket",
        },
    )

    preserve_workflow_definition_secrets(
        SimpleNamespace(blocks=[current_block]),
        SimpleNamespace(blocks=[previous_block]),
        SimpleNamespace(blocks=[source_block]),
    )

    assert current_block.sftp_private_key == secret


def test_omitted_secret_rejects_renamed_block_when_sftp_binding_changes() -> None:
    previous_block = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        label="download",
        sftp_host="old-sftp.example.invalid",
        sftp_username="synthetic-user",
        sftp_private_key="synthetic-old-destination-key",
    )
    current_block = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        label="renamed_download",
        sftp_host="new-sftp.example.invalid",
        sftp_username="synthetic-user",
        sftp_private_key=None,
    )
    source_block = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        label="renamed_download",
        sftp_host="new-sftp.example.invalid",
        sftp_username="synthetic-user",
        model_fields_set={"block_type", "label", "sftp_host", "sftp_username"},
    )

    with pytest.raises(SkyvernHTTPException) as exc_info:
        preserve_workflow_definition_secrets(
            SimpleNamespace(blocks=[current_block]),
            SimpleNamespace(blocks=[previous_block]),
            SimpleNamespace(blocks=[source_block]),
        )

    assert exc_info.value.error_code == "workflow_secret_preservation_failed"
    assert "renamed_download" in (exc_info.value.message or "")
    assert "Re-enter the secret" in (exc_info.value.message or "")


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    [("sftp_port", 2222), ("sftp_host_key", "synthetic-changed-host-key")],
)
def test_masked_sftp_secret_rejects_changed_endpoint_identity(field_name: str, changed_value: int | str) -> None:
    previous_block = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        label="download",
        download_target="sftp",
        sftp_host="sftp.example.invalid",
        sftp_port=22,
        sftp_username="synthetic-user",
        sftp_host_key="synthetic-original-host-key",
        sftp_private_key="synthetic-sftp-endpoint-key",
    )
    current_block = deepcopy(previous_block)
    setattr(current_block, field_name, changed_value)
    current_block.sftp_private_key = WORKFLOW_SECRET_MASK

    with pytest.raises(SkyvernHTTPException) as exc_info:
        preserve_workflow_definition_secrets(
            SimpleNamespace(blocks=[current_block]),
            SimpleNamespace(blocks=[previous_block]),
        )

    assert exc_info.value.error_code == "workflow_secret_preservation_failed"
    assert current_block.sftp_private_key == WORKFLOW_SECRET_MASK


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    [
        ("endpoint_url", "https://s3-new.example.invalid"),
        ("region_name", "eu-west-1"),
    ],
)
def test_masked_aws_secret_rejects_changed_endpoint_identity(field_name: str, changed_value: str) -> None:
    previous_block = SimpleNamespace(
        block_type=BlockType.FILE_UPLOAD,
        label="upload",
        storage_type="s3",
        s3_bucket="synthetic-bucket",
        aws_access_key_id="synthetic-access-key-id",
        region_name="us-east-1",
        endpoint_url="https://s3-old.example.invalid",
        aws_secret_access_key="synthetic-aws-endpoint-secret",
    )
    current_block = deepcopy(previous_block)
    setattr(current_block, field_name, changed_value)
    current_block.aws_secret_access_key = WORKFLOW_SECRET_MASK

    with pytest.raises(SkyvernHTTPException) as exc_info:
        preserve_workflow_definition_secrets(
            SimpleNamespace(blocks=[current_block]),
            SimpleNamespace(blocks=[previous_block]),
        )

    assert exc_info.value.error_code == "workflow_secret_preservation_failed"
    assert current_block.aws_secret_access_key == WORKFLOW_SECRET_MASK


def test_new_upload_does_not_fail_on_unrelated_omitted_secret_fields() -> None:
    previous = {
        "blocks": [
            {
                "block_type": BlockType.FILE_UPLOAD,
                "label": "old-upload",
                "s3_bucket": "old-bucket",
                "aws_secret_access_key": "synthetic-old-upload-key",
            }
        ]
    }
    current = {
        "blocks": [
            {
                "block_type": BlockType.FILE_UPLOAD,
                "label": "new-upload",
                "s3_bucket": "new-bucket",
                "aws_secret_access_key": "synthetic-new-upload-key",
            }
        ]
    }

    preserve_workflow_definition_secrets(current, previous)

    assert current["blocks"][0]["aws_secret_access_key"] == "synthetic-new-upload-key"


def test_new_s3_upload_before_masked_sftp_upload_is_order_independent() -> None:
    previous = {
        "blocks": [
            {
                "block_type": BlockType.FILE_UPLOAD,
                "label": "sftp_upload",
                "storage_type": "sftp",
                "sftp_host": "sftp.example.invalid",
                "sftp_username": "synthetic-user",
                "sftp_password": "synthetic-existing-sftp-password",
            }
        ]
    }
    current = {
        "blocks": [
            {
                "block_type": BlockType.FILE_UPLOAD,
                "label": "new_s3_upload",
                "storage_type": "s3",
                "s3_bucket": "new-bucket",
                "aws_access_key_id": "synthetic-new-access-key-id",
                "aws_secret_access_key": "synthetic-new-s3-secret",
            },
            {
                "block_type": BlockType.FILE_UPLOAD,
                "label": "sftp_upload",
                "storage_type": "sftp",
                "sftp_host": "sftp.example.invalid",
                "sftp_username": "synthetic-user",
                "sftp_password": WORKFLOW_SECRET_MASK,
            },
        ]
    }

    preserve_workflow_definition_secrets(current, previous, current)

    assert current["blocks"][1]["sftp_password"] == "synthetic-existing-sftp-password"


def test_masked_sftp_fields_are_cleared_when_switching_upload_destination() -> None:
    previous = {
        "blocks": [
            {
                "block_type": BlockType.FILE_UPLOAD,
                "label": "upload",
                "storage_type": "sftp",
                "sftp_host": "sftp.example.invalid",
                "sftp_username": "synthetic-user",
                "sftp_private_key": "synthetic-old-sftp-key",
            }
        ]
    }
    current = {
        "blocks": [
            {
                "block_type": BlockType.FILE_UPLOAD,
                "label": "upload",
                "storage_type": "s3",
                "s3_bucket": "synthetic-new-bucket",
                "aws_access_key_id": "synthetic-new-access-key-id",
                "aws_secret_access_key": "synthetic-new-s3-secret",
                "sftp_private_key": WORKFLOW_SECRET_MASK,
            }
        ]
    }

    preserve_workflow_definition_secrets(current, previous, current)

    assert current["blocks"][0]["aws_secret_access_key"] == "synthetic-new-s3-secret"
    assert current["blocks"][0]["sftp_private_key"] is None


def test_keyless_s3_upload_before_masked_existing_s3_upload_is_order_independent() -> None:
    previous = {
        "blocks": [
            {
                "block_type": BlockType.FILE_UPLOAD,
                "label": "existing_s3_upload",
                "storage_type": "s3",
                "s3_bucket": "existing-bucket",
                "aws_access_key_id": "synthetic-access-key-id",
                "region_name": "us-east-1",
                "endpoint_url": "https://s3.example.invalid",
                "aws_secret_access_key": "synthetic-existing-s3-secret",
            }
        ]
    }
    current = {
        "blocks": [
            {
                "block_type": BlockType.FILE_UPLOAD,
                "label": "new_s3_upload",
                "storage_type": "s3",
                "s3_bucket": "new-bucket",
                "aws_access_key_id": "synthetic-new-access-key-id",
                "region_name": "us-east-1",
                "endpoint_url": "https://s3.example.invalid",
            },
            {
                "block_type": BlockType.FILE_UPLOAD,
                "label": "existing_s3_upload",
                "storage_type": "s3",
                "s3_bucket": "existing-bucket",
                "aws_access_key_id": "synthetic-access-key-id",
                "region_name": "us-east-1",
                "endpoint_url": "https://s3.example.invalid",
                "aws_secret_access_key": WORKFLOW_SECRET_MASK,
            },
        ]
    }

    preserve_workflow_definition_secrets(current, previous, current)

    assert "aws_secret_access_key" not in current["blocks"][0]
    assert current["blocks"][1]["aws_secret_access_key"] == "synthetic-existing-s3-secret"


def test_renamed_send_email_block_ignores_regenerated_smtp_parameters() -> None:
    secret = "synthetic-renamed-smtp-password"
    previous_block = SimpleNamespace(
        block_type=BlockType.SEND_EMAIL,
        label="send_email",
        sender="sender@example.invalid",
        recipients=["recipient@example.invalid"],
        subject="Synthetic subject",
        body="Synthetic body",
        custom_smtp_host="smtp.example.invalid",
        custom_smtp_port=587,
        custom_smtp_username="synthetic-user",
        custom_smtp_password=secret,
        smtp_host=SimpleNamespace(parameter_id="old-host-id"),
        smtp_port=SimpleNamespace(parameter_id="old-port-id"),
        smtp_username=SimpleNamespace(parameter_id="old-username-id"),
        smtp_password=SimpleNamespace(parameter_id="old-password-id"),
    )
    previous = SimpleNamespace(blocks=[previous_block])
    current_block = deepcopy(previous_block)
    current_block.label = "renamed_send_email"
    current_block.smtp_host.parameter_id = "new-host-id"
    current_block.smtp_port.parameter_id = "new-port-id"
    current_block.smtp_username.parameter_id = "new-username-id"
    current_block.smtp_password.parameter_id = "new-password-id"
    current = SimpleNamespace(blocks=[current_block])
    mask_workflow_definition_secrets(current)

    preserve_workflow_definition_secrets(current, previous)

    assert current_block.custom_smtp_password == secret


def test_masked_smtp_password_survives_email_content_edits() -> None:
    secret = "synthetic-smtp-content-edit-password"
    previous_block = SimpleNamespace(
        block_type=BlockType.SEND_EMAIL,
        label="send_email",
        custom_smtp_host="smtp.example.invalid",
        custom_smtp_port=587,
        custom_smtp_username="synthetic-user",
        custom_smtp_password=secret,
        recipients=["old@example.invalid"],
        subject="Old subject",
        body="Old body",
    )
    current_block = deepcopy(previous_block)
    current_block.subject = "New subject"
    current_block.body = "New body"
    current_block.recipients = ["new@example.invalid"]
    current_block.custom_smtp_password = WORKFLOW_SECRET_MASK

    preserve_workflow_definition_secrets(
        SimpleNamespace(blocks=[current_block]),
        SimpleNamespace(blocks=[previous_block]),
    )

    assert current_block.custom_smtp_password == secret


def test_masked_smtp_password_rejects_changed_server_binding() -> None:
    previous_block = SimpleNamespace(
        block_type=BlockType.SEND_EMAIL,
        label="send_email",
        custom_smtp_host="smtp.old.example.invalid",
        custom_smtp_port=587,
        custom_smtp_username="synthetic-user",
        custom_smtp_password="synthetic-smtp-server-change-password",
    )
    current_block = deepcopy(previous_block)
    current_block.custom_smtp_host = "smtp.new.example.invalid"
    current_block.custom_smtp_password = WORKFLOW_SECRET_MASK

    with pytest.raises(SkyvernHTTPException) as exc_info:
        preserve_workflow_definition_secrets(
            SimpleNamespace(blocks=[current_block]),
            SimpleNamespace(blocks=[previous_block]),
        )

    assert exc_info.value.error_code == "workflow_secret_preservation_failed"
    assert current_block.custom_smtp_password == WORKFLOW_SECRET_MASK


@pytest.mark.parametrize("field_was_supplied", [False, True], ids=["omitted", "explicit-null"])
def test_workflow_secret_null_preserves_only_when_omitted(field_was_supplied: bool) -> None:
    secret = "synthetic-null-clear-private-key"
    block = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        label="download",
        sftp_host="sftp.example.invalid",
        sftp_username="synthetic-user",
        sftp_private_key=None,
        sftp_password=None,
        sftp_private_key_passphrase=None,
        aws_secret_access_key=None,
        azure_storage_account_key=None,
    )
    supplied_fields = {"block_type", "label"}
    if field_was_supplied:
        supplied_fields.add("sftp_private_key")
    source_block = SimpleNamespace(
        block_type=BlockType.FILE_DOWNLOAD,
        label="download",
        model_fields_set=supplied_fields,
    )
    definition = SimpleNamespace(blocks=[block])
    previous = SimpleNamespace(blocks=[SimpleNamespace(**{**vars(block), "sftp_private_key": secret})])
    source = SimpleNamespace(blocks=[source_block])

    preserve_workflow_definition_secrets(definition, previous, source)

    assert block.sftp_private_key == (None if field_was_supplied else secret)


@pytest.mark.asyncio
async def test_encrypt_workflow_definition_secrets_encrypts_send_email_custom_smtp_password(
    enabled_encryption: None,
) -> None:
    send_email = SimpleNamespace(
        block_type=BlockType.SEND_EMAIL,
        custom_smtp_host="smtp.example.com",
        custom_smtp_username="user@example.com",
        custom_smtp_password="send-email-test-value",
    )
    templated_send_email = SimpleNamespace(
        block_type=BlockType.SEND_EMAIL,
        custom_smtp_host=None,
        custom_smtp_username=None,
        custom_smtp_password="{{ smtp_password_param }}",
    )
    definition = SimpleNamespace(blocks=[send_email, templated_send_email])

    await encrypt_workflow_definition_secrets(definition, organization_id="o_x")

    assert is_encrypted_secret(send_email.custom_smtp_password)
    assert "send-email-test-value" not in send_email.custom_smtp_password
    assert send_email.custom_smtp_host == "smtp.example.com"
    assert send_email.custom_smtp_username == "user@example.com"
    assert templated_send_email.custom_smtp_password == "{{ smtp_password_param }}"
    assert (
        await decrypt_secret_field_value(
            send_email.custom_smtp_password,
            organization_id="o_x",
            field_name="custom_smtp_password",
        )
        == "send-email-test-value"
    )


@pytest.mark.asyncio
async def test_send_email_literal_password_with_jinja_chars_is_still_encrypted(enabled_encryption: None) -> None:
    send_email = SimpleNamespace(
        block_type=BlockType.SEND_EMAIL,
        custom_smtp_host="smtp.example.com",
        custom_smtp_username="user@example.com",
        custom_smtp_password="pa{{7*7}}ss",
    )
    definition = SimpleNamespace(blocks=[send_email])

    await encrypt_workflow_definition_secrets(definition, organization_id="o_x")

    assert is_encrypted_secret(send_email.custom_smtp_password)
    assert (
        await decrypt_secret_field_value(
            send_email.custom_smtp_password,
            organization_id="o_x",
            field_name="custom_smtp_password",
        )
        == "pa{{7*7}}ss"
    )


@pytest.mark.asyncio
async def test_send_email_password_that_is_entirely_a_jinja_expression_is_encrypted(
    enabled_encryption: None,
) -> None:
    # Only a bare parameter reference ("{{ name }}") may stay a template. A password that
    # happens to be a full Jinja EXPRESSION ("{{7*7}}") must be treated as a literal:
    # encrypted at rest, and never rendered (rendering would corrupt it to "49").
    send_email = SimpleNamespace(
        block_type=BlockType.SEND_EMAIL,
        custom_smtp_host="smtp.example.com",
        custom_smtp_username="user@example.com",
        custom_smtp_password="{{7*7}}",
    )
    definition = SimpleNamespace(blocks=[send_email])

    await encrypt_workflow_definition_secrets(definition, organization_id="o_x")

    assert is_encrypted_secret(send_email.custom_smtp_password)
    assert (
        await decrypt_secret_field_value(
            send_email.custom_smtp_password,
            organization_id="o_x",
            field_name="custom_smtp_password",
        )
        == "{{7*7}}"
    )


@pytest.mark.asyncio
async def test_send_email_bare_parameter_reference_stays_a_template(enabled_encryption: None) -> None:
    send_email = SimpleNamespace(
        block_type=BlockType.SEND_EMAIL,
        custom_smtp_host="smtp.example.com",
        custom_smtp_username="user@example.com",
        custom_smtp_password="{{ smtp_password_param }}",
    )
    definition = SimpleNamespace(blocks=[send_email])

    await encrypt_workflow_definition_secrets(definition, organization_id="o_x")

    assert send_email.custom_smtp_password == "{{ smtp_password_param }}"


def test_workflow_read_mask_covers_inline_secret_fields() -> None:
    file_destination = SimpleNamespace(
        block_type=BlockType.FILE_UPLOAD,
        label="upload",
        aws_secret_access_key="aws-secret",
        azure_storage_account_key="azure-key",
        sftp_password="sftp-password",
        sftp_private_key="sftp-key",
        sftp_private_key_passphrase="sftp-key-passphrase",
        aws_access_key_id="public-access-key-id",
        sftp_host_key="public-host-key",
    )
    send_email = SimpleNamespace(
        block_type=BlockType.SEND_EMAIL,
        label="email",
        custom_smtp_password="smtp-password",
    )

    mask_workflow_definition_secrets(SimpleNamespace(blocks=[file_destination, send_email]))

    assert [
        file_destination.aws_secret_access_key,
        file_destination.azure_storage_account_key,
        file_destination.sftp_password,
        file_destination.sftp_private_key,
        file_destination.sftp_private_key_passphrase,
        send_email.custom_smtp_password,
    ] == [WORKFLOW_SECRET_MASK] * 6
    assert file_destination.aws_access_key_id == "public-access-key-id"
    assert file_destination.sftp_host_key == "public-host-key"


def test_mask_workflow_for_api_only_copies_when_a_literal_secret_needs_masking() -> None:
    ordinary_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(block_type=BlockType.GOTO_URL, label="open_site")])
    )
    assert mask_workflow_for_api(ordinary_workflow) is ordinary_workflow

    stored_secret = "synthetic-api-mask-copy-key"
    stored_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(
            blocks=[
                SimpleNamespace(
                    block_type=BlockType.FILE_DOWNLOAD,
                    label="download",
                    sftp_private_key=stored_secret,
                )
            ]
        )
    )

    visible_workflow = mask_workflow_for_api(stored_workflow)

    assert visible_workflow is not stored_workflow
    assert visible_workflow.workflow_definition.blocks[0].sftp_private_key == WORKFLOW_SECRET_MASK
    assert stored_workflow.workflow_definition.blocks[0].sftp_private_key == stored_secret


@pytest.mark.asyncio
async def test_destination_fields_keep_partial_template_behavior(enabled_encryption: None) -> None:
    # FileUpload/FileDownload sensitive fields keep the historical rule: any Jinja-looking
    # value skips encryption so partial template composition keeps working.
    upload = SimpleNamespace(
        block_type=BlockType.FILE_UPLOAD,
        aws_secret_access_key=None,
        azure_storage_account_key=None,
        sftp_password="prefix-{{ sftp_password_param }}",
        sftp_private_key=None,
        sftp_private_key_passphrase=None,
    )
    definition = SimpleNamespace(blocks=[upload])

    await encrypt_workflow_definition_secrets(definition, organization_id="o_x")

    assert upload.sftp_password == "prefix-{{ sftp_password_param }}"
