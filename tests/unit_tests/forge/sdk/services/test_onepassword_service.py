import asyncio
import json
import pytest
from unittest.mock import patch, AsyncMock # Use AsyncMock for async methods

from skyvern.forge.sdk.services.onepassword import (
    OnePasswordService,
    OnePasswordCLIError,
    OnePasswordItemNotFoundError,
    OnePasswordServiceError,
    RunCommandResult,
    OnePasswordConstants,
)

# Mark all tests in this module as asyncio
pytestmark = pytest.mark.asyncio


@pytest.fixture
def op_service():
    """Fixture to create an instance of OnePasswordService."""
    return OnePasswordService()

# --- Test Cases ---

async def test_get_login_credentials_success_all_fields(op_service: OnePasswordService):
    """
    Test successful retrieval of login credentials when all fields (username, password, TOTP) are present.
    """
    mock_item_name = "TestLogin"
    mock_vault_name = "TestVault"
    mock_username = "testuser"
    mock_password = "testpassword"
    mock_totp_code = "123456"

    mock_op_output = {
        "id": "item_id_abc",
        "title": mock_item_name,
        "fields": [
            {"id": "username_field", "label": OnePasswordConstants.USERNAME_FIELD_LABEL, "value": mock_username, "type": "STRING"},
            {"id": "password_field", "label": OnePasswordConstants.PASSWORD_FIELD_LABEL, "value": mock_password, "type": "CONCEALED"},
            {"id": "totp_field", "label": "one-time password", "designation": OnePasswordConstants.TOTP_FIELD_DESIGNATION_OTP, "value": mock_totp_code, "type": "OTP"},
        ],
    }
    mock_stdout = json.dumps(mock_op_output)

    with patch.object(op_service, '_run_command', new_callable=AsyncMock) as mock_run_cmd:
        mock_run_cmd.return_value = RunCommandResult(stdout=mock_stdout, stderr="", returncode=0)

        credentials = await op_service.get_login_credentials(
            item_id_or_name=mock_item_name,
            vault_id_or_name=mock_vault_name
        )

        mock_run_cmd.assert_called_once_with(
            [
                *OnePasswordConstants.ITEM_GET_CMD,
                mock_item_name,
                OnePasswordConstants.VAULT_PARAM,
                mock_vault_name,
                OnePasswordConstants.FORMAT_JSON,
            ],
            additional_env=None,
            suppress_error_logging=True
        )

        assert credentials["username"] == mock_username
        assert credentials["password"] == mock_password
        assert credentials["totp"] == mock_totp_code


async def test_get_login_credentials_success_no_totp(op_service: OnePasswordService):
    """
    Test successful retrieval when TOTP field is missing.
    """
    mock_item_name = "LoginNoTOTP"
    mock_vault_name = "TestVault"
    mock_username = "user_no_totp"
    mock_password = "password123"

    mock_op_output = {
        "id": "item_id_def",
        "title": mock_item_name,
        "fields": [
            {"id": "username_field", "label": OnePasswordConstants.USERNAME_FIELD_LABEL, "value": mock_username, "type": "STRING"},
            {"id": "password_field", "label": OnePasswordConstants.PASSWORD_FIELD_LABEL, "value": mock_password, "type": "CONCEALED"},
        ],
    }
    mock_stdout = json.dumps(mock_op_output)

    with patch.object(op_service, '_run_command', new_callable=AsyncMock) as mock_run_cmd:
        mock_run_cmd.return_value = RunCommandResult(stdout=mock_stdout, stderr="", returncode=0)

        credentials = await op_service.get_login_credentials(
            item_id_or_name=mock_item_name,
            vault_id_or_name=mock_vault_name
        )

        mock_run_cmd.assert_called_once()
        assert credentials["username"] == mock_username
        assert credentials["password"] == mock_password
        assert credentials["totp"] is None


async def test_get_item_details_json_parse_fails(op_service: OnePasswordService):
    """
    Test scenario where `op item get` returns non-JSON output.
    """
    mock_item_name = "BadJSONItem"
    mock_vault_name = "TestVault"
    invalid_json_stdout = "This is not JSON"

    with patch.object(op_service, '_run_command', new_callable=AsyncMock) as mock_run_cmd:
        mock_run_cmd.return_value = RunCommandResult(stdout=invalid_json_stdout, stderr="", returncode=0)

        with pytest.raises(OnePasswordServiceError, match=f"Failed to parse JSON output for item '{mock_item_name}'."):
            await op_service.get_item_details(
                item_id_or_name=mock_item_name,
                vault_id_or_name=mock_vault_name
            )
        mock_run_cmd.assert_called_once()


async def test_get_item_details_cli_item_not_found(op_service: OnePasswordService):
    """
    Test scenario where `op` CLI indicates item not found.
    """
    mock_item_name = "NonExistentItem"
    mock_vault_name = "TestVault"
    stderr_item_not_found = "[ERROR] no item matching 'NonExistentItem' found"

    with patch.object(op_service, '_run_command', new_callable=AsyncMock) as mock_run_cmd:
        mock_run_cmd.return_value = RunCommandResult(stdout="", stderr=stderr_item_not_found, returncode=1)

        with pytest.raises(OnePasswordItemNotFoundError, match=f"Item '{mock_item_name}' not found in vault '{mock_vault_name}'."):
            await op_service.get_item_details(
                item_id_or_name=mock_item_name,
                vault_id_or_name=mock_vault_name
            )
        mock_run_cmd.assert_called_once()


async def test_get_item_details_cli_vault_not_found(op_service: OnePasswordService):
    """
    Test scenario where `op` CLI indicates vault not found.
    """
    mock_item_name = "SomeItem"
    mock_vault_name = "NonExistentVault"
    stderr_vault_not_found = "[ERROR] vault not found" # Example, actual message might vary

    with patch.object(op_service, '_run_command', new_callable=AsyncMock) as mock_run_cmd:
        mock_run_cmd.return_value = RunCommandResult(stdout="", stderr=stderr_vault_not_found, returncode=1)

        with pytest.raises(OnePasswordItemNotFoundError, match=f"Item '{mock_item_name}' not found in vault '{mock_vault_name}'."):
             await op_service.get_item_details(
                item_id_or_name=mock_item_name,
                vault_id_or_name=mock_vault_name
            )
        mock_run_cmd.assert_called_once()


async def test_get_item_details_cli_other_error(op_service: OnePasswordService):
    """
    Test scenario where `op` CLI returns a generic error not specifically handled as 'not found'.
    """
    mock_item_name = "ItemWithOtherError"
    mock_vault_name = "TestVault"
    stderr_other_error = "[ERROR] A generic CLI error occurred, not item or vault not found"

    with patch.object(op_service, '_run_command', new_callable=AsyncMock) as mock_run_cmd:
        mock_run_cmd.return_value = RunCommandResult(stdout="", stderr=stderr_other_error, returncode=1)

        with pytest.raises(OnePasswordCLIError, match=f"Failed to get item '{mock_item_name}' from vault '{mock_vault_name}'.*"):
            await op_service.get_item_details(
                item_id_or_name=mock_item_name,
                vault_id_or_name=mock_vault_name
            )
        mock_run_cmd.assert_called_once()


async def test_get_item_details_cli_timeout(op_service: OnePasswordService):
    """
    Test scenario where the `op` CLI command times out.
    """
    mock_item_name = "ItemTimeout"
    mock_vault_name = "TestVault"

    # Mock _run_command to simulate a timeout by raising the specific exception _run_command would raise
    with patch.object(op_service, '_run_command', new_callable=AsyncMock) as mock_run_cmd:
        # The _run_command itself raises OnePasswordCLIError for timeouts.
        mock_run_cmd.side_effect = OnePasswordCLIError(
            message=f"Command timed out after 60 seconds", # timeout value is default in _run_command
            command=["op", "item", "get", "..."], # placeholder command
            run_result=RunCommandResult(stdout="", stderr="Timeout occurred", returncode=-1)
        )

        with pytest.raises(OnePasswordCLIError, match="Command timed out"):
            await op_service.get_item_details(
                item_id_or_name=mock_item_name,
                vault_id_or_name=mock_vault_name
            )
        mock_run_cmd.assert_called_once()
```
