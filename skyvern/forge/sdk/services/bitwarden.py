import json
import os
import subprocess
from enum import StrEnum

import structlog

from skyvern.exceptions import (
    BitwardenListItemsError,
    BitwardenLoginError,
    BitwardenLogoutError,
    BitwardenTOTPError,
    BitwardenUnlockError,
)

LOG = structlog.get_logger()


class BitwardenConstants(StrEnum):
    CLIENT_ID = "BW_CLIENT_ID"
    CLIENT_SECRET = "BW_CLIENT_SECRET"
    MASTER_PASSWORD = "BW_MASTER_PASSWORD"
    URL = "BW_URL"

    USERNAME = "BW_USERNAME"
    PASSWORD = "BW_PASSWORD"
    TOTP = "BW_TOTP"


class BitwardenService:
    @staticmethod
    def run_command(command: list[str], additional_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        """
        Run a CLI command with the specified additional environment variables and return the result.
        """
        env = os.environ.copy()  # Copy the current environment
        # Make sure node isn't returning warnings. Warnings are sent through stderr and we raise exceptions on stderr.
        env["NODE_NO_WARNINGS"] = "1"
        if additional_env:
            env.update(additional_env)  # Update with any additional environment variables

        return subprocess.run(command, capture_output=True, text=True, env=env)

    @staticmethod
    def get_secret_value_from_url(
        client_id: str,
        client_secret: str,
        master_password: str,
        url: str,
    ) -> dict[str, str]:
        """
        Get the secret value from the Bitwarden CLI.
        """
        # Step 1: Set up environment variables and log in
        try:
            env = {
                "BW_CLIENTID": client_id,
                "BW_CLIENTSECRET": client_secret,
                "BW_PASSWORD": master_password,
            }
            login_command = ["bw", "login", "--apikey"]
            login_result = BitwardenService.run_command(login_command, env)

            # Print both stdout and stderr for debugging
            if login_result.stderr and "You are already logged in as" not in login_result.stderr:
                raise BitwardenLoginError(login_result.stderr)

            # Step 2: Unlock the vault
            unlock_command = ["bw", "unlock", "--passwordenv", "BW_PASSWORD"]
            unlock_result = BitwardenService.run_command(unlock_command, env)

            # This is a part of Bitwarden's client-side telemetry
            # TODO -- figure out how to disable this telemetry so we never get this error
            # https://github.com/bitwarden/clients/blob/9d10825dbd891c0f41fe1b4c4dd3ca4171f63be5/libs/common/src/services/api.service.ts#L1473
            if unlock_result.stderr and "Event post failed" not in unlock_result.stderr:
                raise BitwardenUnlockError(unlock_result.stderr)

            # Extract session key
            try:
                session_key = unlock_result.stdout.split('"')[1]
            except IndexError:
                raise BitwardenUnlockError("Unable to extract session key.")

            if not session_key:
                raise BitwardenUnlockError("Session key is empty.")

            # Step 3: Retrieve the items
            list_command = [
                "bw",
                "list",
                "items",
                "--url",
                url,
                "--session",
                session_key,
            ]
            items_result = BitwardenService.run_command(list_command)

            if items_result.stderr and "Event post failed" not in items_result.stderr:
                raise BitwardenListItemsError(items_result.stderr)

            # Parse the items and extract credentials
            try:
                items = json.loads(items_result.stdout)
            except json.JSONDecodeError:
                raise BitwardenListItemsError("Failed to parse items JSON. Output: " + items_result.stdout)

            if not items:
                raise BitwardenListItemsError("No items found in Bitwarden.")

            totp_command = ["bw", "get", "totp", url, "--session", session_key]
            totp_result = BitwardenService.run_command(totp_command)

            if totp_result.stderr and "Event post failed" not in totp_result.stderr:
                LOG.warning(
                    "Bitwarden TOTP Error",
                    error=totp_result.stderr,
                    e=BitwardenTOTPError(totp_result.stderr),
                )
            totp_code = totp_result.stdout

            credentials: list[dict[str, str]] = [
                {
                    BitwardenConstants.USERNAME: item["login"]["username"],
                    BitwardenConstants.PASSWORD: item["login"]["password"],
                    BitwardenConstants.TOTP: totp_code,
                }
                for item in items
                if "login" in item
            ]

            # Todo: Handle multiple credentials, for now just return the last one
            return credentials[-1] if credentials else {}
        finally:
            # Step 4: Log out
            BitwardenService.logout()

    @staticmethod
    def logout() -> None:
        """
        Log out of the Bitwarden CLI.
        """
        logout_command = ["bw", "logout"]
        logout_result = BitwardenService.run_command(logout_command)
        if logout_result.stderr:
            raise BitwardenLogoutError(logout_result.stderr)
