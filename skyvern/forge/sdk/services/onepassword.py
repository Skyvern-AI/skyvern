import asyncio
import json
import logging
import subprocess # Not strictly needed if only using asyncio.subprocess
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

# Configure logging
logger = logging.getLogger(__name__)

class RunCommandResult(BaseModel):
    stdout: str
    stderr: str
    returncode: int

# Configure logging
logger = logging.getLogger(__name__)

class OnePasswordConstants:
    """
    Constants for 1Password CLI interaction.
    """
    # Example: OP_CLI_PATH = "op" # Path to the 1Password CLI executable
    # We will assume 'op' is in PATH for now.
    ITEM_GET_CMD = ["op", "item", "get"]
    FORMAT_JSON = "--format=json"
    VAULT_PARAM = "--vault"
    # Field names (these are assumptions and might need adjustment)
    USERNAME_FIELD_LABEL = "username"
    PASSWORD_FIELD_LABEL = "password"
    TOTP_FIELD_DESIGNATION_OTP = "OTP" # Common designation for TOTP fields in 1Password items


class OnePasswordServiceError(Exception):
    """Base exception for OnePasswordService errors."""
    pass

class OnePasswordCLIError(OnePasswordServiceError):
    """Exception raised for errors encountered while running the 1Password CLI."""
    def __init__(self, message: str, command: List[str], run_result: Optional[RunCommandResult] = None):
        super().__init__(message)
        self.command = command
        self.run_result = run_result

    def __str__(self):
        if self.run_result:
            return (
                f"{super().__str__()} (Command: '{' '.join(self.command)}', "
                f"Return Code: {self.run_result.returncode}, Stderr: {self.run_result.stderr or 'N/A'})"
            )
        return f"{super().__str__()} (Command: '{' '.join(self.command)}')"


class OnePasswordItemNotFoundError(OnePasswordServiceError):
    def __init__(self, item_id_or_name: str, vault_id_or_name: str, message: Optional[str] = None):
        self.item_id_or_name = item_id_or_name
        self.vault_id_or_name = vault_id_or_name
        super().__init__(message or f"Item '{item_id_or_name}' not found in vault '{vault_id_or_name}'.")

    """Exception raised when a 1Password item is not found."""
    pass


class OnePasswordService:
    """
    Service for interacting with the 1Password CLI.
    Assumes that the 1Password CLI (`op`) is installed and authenticated.
    """

    def __init__(self):
        # Authentication is assumed to be handled externally (e.g., op signin, service account tokens)
        logger.info("OnePasswordService initialized. CLI authentication is assumed.")

import os # Needed for os.environ.copy()

# ... (other imports remain the same)

# ... (RunCommandResult, OnePasswordConstants, Exceptions remain the same)

class OnePasswordService:
    """
    Service for interacting with the 1Password CLI.
    """

    def __init__(self):
        # Authentication can be handled by pre-configured CLI or by passing tokens via additional_env in calls
        logger.info("OnePasswordService initialized.")

    async def _run_command(
        self,
        command: List[str],
        additional_env: Optional[Dict[str, str]] = None,
        timeout: int = 60,
        suppress_error_logging: bool = False,
    ) -> RunCommandResult:
        """
        Asynchronously runs a shell command with optional additional environment variables
        and returns its stdout, stderr, and return code.
        """
        env = os.environ.copy()
        if additional_env:
            env.update(additional_env)

        logger.debug(f"Running command: {' '.join(command)}")
        try:
            async with asyncio.timeout(timeout):
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env, # Pass the modified environment
                )
                stdout_bytes, stderr_bytes = await process.communicate()
                stdout = stdout_bytes.decode().strip()
                stderr = stderr_bytes.decode().strip()
                returncode = process.returncode

                if returncode != 0 and not suppress_error_logging:
                    logger.error(
                        f"Command '{' '.join(command)}' failed with return code {returncode}. Stderr: {stderr}"
                    )

                return RunCommandResult(stdout=stdout, stderr=stderr, returncode=returncode)

        except asyncio.TimeoutError:
            logger.error(f"Command '{' '.join(command)}' timed out after {timeout} seconds.")
            # Mimic a RunCommandResult for timeout, though stderr and stdout are empty.
            # Alternatively, raise a specific timeout error.
            raise OnePasswordCLIError(
                f"Command timed out after {timeout} seconds",
                command=command,
                run_result=RunCommandResult(stdout="", stderr="Timeout occurred", returncode=-1) # Arbitrary error code for timeout
            )
        except Exception as e:
            logger.error(f"An unexpected error occurred while running command '{' '.join(command)}': {e}", exc_info=True)
            # For other unexpected errors, ensure a RunCommandResult-like structure in the exception
            raise OnePasswordCLIError(
                f"Unexpected error running command: {e}",
                command=command,
                run_result=RunCommandResult(stdout="", stderr=str(e), returncode=-2) # Arbitrary error code for other errors
            )

    async def get_item_details(
        self,
        item_id_or_name: str,
        vault_id_or_name: str,
        additional_env: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        Retrieves the details of an item from 1Password using the 'op' CLI.
        """
        command = [
            *OnePasswordConstants.ITEM_GET_CMD,
            item_id_or_name,
            OnePasswordConstants.VAULT_PARAM,
            vault_id_or_name,
            OnePasswordConstants.FORMAT_JSON,
        ]

        logger.info(f"Attempting to retrieve item '{item_id_or_name}' from vault '{vault_id_or_name}'.")

        run_result = await self._run_command(
            command, additional_env=additional_env, suppress_error_logging=True
        ) # Suppress here, handle below

        if run_result.returncode != 0:
            # More specific error checking can be added here based on stderr patterns
            if "no item matching" in run_result.stderr.lower() or \
               "isn't a valid item UUID or name" in run_result.stderr.lower() or \
               "vault not found" in run_result.stderr.lower(): # Crude check
                logger.warning(
                    f"Item '{item_id_or_name}' or vault '{vault_id_or_name}' not found or access denied. Stderr: {run_result.stderr}"
                )
                raise OnePasswordItemNotFoundError(item_id_or_name, vault_id_or_name)

            logger.error(
                f"CLI error while getting item '{item_id_or_name}'. Command: '{' '.join(command)}', "
                f"Return Code: {run_result.returncode}, Stderr: {run_result.stderr}"
            )
            raise OnePasswordCLIError(
                f"Failed to get item '{item_id_or_name}' from vault '{vault_id_or_name}'.",
                command=command,
                run_result=run_result,
            )

        try:
            item_data = json.loads(run_result.stdout)
            return item_data
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON output from 'op item get': {e}. Output: {run_result.stdout}")
            raise OnePasswordServiceError(f"Failed to parse JSON output for item '{item_id_or_name}'.")

    def _get_field_value_by_label_or_designation(
        self, item_details: Dict[str, Any], label: str, designation: Optional[str] = None
    ) -> Optional[str]:
        """Helper to find a field's value by its label or designation."""
        fields = item_details.get("fields", [])
        for field in fields:
            if field.get("label") == label:
                return field.get("value")
            if designation and field.get("designation") == designation:
                return field.get("value")
        return None

    def _parse_username(self, item_details: Dict[str, Any]) -> Optional[str]:
        """
        Parses the username from item details.
        The field is typically labeled 'username' or has designation 'username'.
        """
        return self._get_field_value_by_label_or_designation(
            item_details,
            OnePasswordConstants.USERNAME_FIELD_LABEL,
            designation="username"
        )

    def _parse_password(self, item_details: Dict[str, Any]) -> Optional[str]:
        """
        Parses the password from item details.
        The field is typically labeled 'password' or has designation 'password'.
        """
        return self._get_field_value_by_label_or_designation(
            item_details,
            OnePasswordConstants.PASSWORD_FIELD_LABEL,
            designation="password"
        )

    def _parse_totp(self, item_details: Dict[str, Any]) -> Optional[str]:
        """
        Parses the TOTP code from item details.
        Looks for a field with designation 'OTP' or a section containing a TOTP field.
        The actual TOTP code is often in field.value or field.totp if the CLI resolves it.
        `op item get` usually provides the TOTP code directly in the 'value' of an OTP-designated field.
        """
        fields = item_details.get("fields", [])
        for field in fields:
            # Check for standard OTP designation
            if field.get("designation") == OnePasswordConstants.TOTP_FIELD_DESIGNATION_OTP or \
               field.get("label", "").lower() == "one-time password" or \
               (field.get("type") == "OTP" and field.get("value")): # Type "OTP" often has the code in 'value'
                return field.get("value") # This is often the current OTP code if CLI generates it

            # Sometimes the TOTP setup URI is stored, not the code itself.
            # The CLI command `op item get <item> --otp` or specific field requests might be needed for the code itself.
            # For `op item get --format json`, if a field is of type OTP, its 'value' might be the current code.
            # If 'value' is a URI (otpauth://...), then this basic parser won't generate the code.
            # The current implementation assumes 'value' holds the code if designated as OTP.

        # Fallback: check sections for TOTP (less common with `op item get --format json` direct output)
        # sections = item_details.get("sections", [])
        # for section in sections:
        #     section_fields = section.get("fields", [])
        #     for field in section_fields:
        #         if field.get("t") == "TOTP" and field.get("v"): # Example, structure varies
        #             return field.get("v")

        logger.debug(f"No TOTP field found with designation '{OnePasswordConstants.TOTP_FIELD_DESIGNATION_OTP}' or type 'OTP' with a value.")
        return None

    async def get_login_credentials(
        self,
        item_id_or_name: str,
        vault_id_or_name: str,
        additional_env: Optional[Dict[str, str]] = None
    ) -> Dict[str, Optional[str]]:
        """
        Retrieves username, password, and TOTP for a login item.
        """
        details = await self.get_item_details(item_id_or_name, vault_id_or_name, additional_env=additional_env)
        username = self._parse_username(details)
        password = self._parse_password(details)
        totp = self._parse_totp(details)
        return {"username": username, "password": password, "totp": totp}

# Example usage (for testing purposes, would not be in production code like this)
# async def main():
#     service = OnePasswordService()
#     # Replace with actual item and vault IDs/names for testing
#     # item_details = await service.get_login_credentials("Test Item", "Private")
#     # print(item_details)

# if __name__ == "__main__":
# asyncio.run(main())
