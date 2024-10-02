import asyncio
import json
import os
import re
import subprocess
from enum import StrEnum

import structlog
import tldextract

from skyvern.config import settings
from skyvern.exceptions import (
    BitwardenListItemsError,
    BitwardenLoginError,
    BitwardenLogoutError,
    BitwardenSyncError,
    BitwardenTOTPError,
    BitwardenUnlockError,
)

LOG = structlog.get_logger()


def is_valid_email(email: str | None) -> bool:
    if not email:
        return False
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return re.match(pattern, email) is not None


class BitwardenConstants(StrEnum):
    CLIENT_ID = "BW_CLIENT_ID"
    CLIENT_SECRET = "BW_CLIENT_SECRET"
    MASTER_PASSWORD = "BW_MASTER_PASSWORD"
    URL = "BW_URL"
    BW_COLLECTION_ID = "BW_COLLECTION_ID"
    IDENTITY_KEY = "BW_IDENTITY_KEY"

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

        try:
            return subprocess.run(command, capture_output=True, text=True, env=env, timeout=60)
        except subprocess.TimeoutExpired as e:
            LOG.error("Bitwarden command timed out after 60 seconds", stdout=e.stdout, stderr=e.stderr)
            raise e

    @staticmethod
    def _extract_session_key(unlock_cmd_output: str) -> str | None:
        # Split the text by lines
        lines = unlock_cmd_output.split("\n")

        # Look for the line containing the BW_SESSION
        for line in lines:
            if 'BW_SESSION="' in line:
                # Find the start and end positions of the session key
                start = line.find('BW_SESSION="') + len('BW_SESSION="')
                end = line.rfind('"', start)
                return line[start:end]

        return None

    @staticmethod
    async def get_secret_value_from_url(
        client_id: str,
        client_secret: str,
        master_password: str,
        url: str,
        collection_id: str | None = None,
        remaining_retries: int = settings.BITWARDEN_MAX_RETRIES,
        timeout: int = settings.BITWARDEN_TIMEOUT_SECONDS,
        fail_reasons: list[str] = [],
    ) -> dict[str, str]:
        """
        Get the secret value from the Bitwarden CLI.
        """
        try:
            async with asyncio.timeout(timeout):
                return await BitwardenService._get_secret_value_from_url(
                    client_id=client_id,
                    client_secret=client_secret,
                    master_password=master_password,
                    url=url,
                    collection_id=collection_id,
                )
        except Exception as e:
            if remaining_retries <= 0:
                raise BitwardenListItemsError(
                    f"Bitwarden CLI failed after all retry attempts. Fail reasons: {fail_reasons}"
                )

            remaining_retries -= 1
            LOG.info("Retrying to get secret value from Bitwarden", remaining_retries=remaining_retries)
            return await BitwardenService.get_secret_value_from_url(
                client_id=client_id,
                client_secret=client_secret,
                master_password=master_password,
                url=url,
                collection_id=collection_id,
                remaining_retries=remaining_retries,
                # Double the timeout for the next retry
                timeout=timeout * 2,
                fail_reasons=fail_reasons + [f"{type(e).__name__}: {str(e)}"],
            )

    @staticmethod
    async def _get_secret_value_from_url(
        client_id: str,
        client_secret: str,
        master_password: str,
        url: str,
        collection_id: str | None = None,
    ) -> dict[str, str]:
        """
        Get the secret value from the Bitwarden CLI.
        """
        try:
            BitwardenService.login(client_id, client_secret)
            BitwardenService.sync()
            session_key = BitwardenService.unlock(master_password)

            # Extract the domain from the URL and search for items in Bitwarden with that domain
            domain = tldextract.extract(url).domain
            list_command = [
                "bw",
                "list",
                "items",
                "--search",
                domain,
                "--session",
                session_key,
            ]
            if collection_id:
                LOG.info("Collection ID is provided, filtering items by collection ID", collection_id=collection_id)
                list_command.extend(["--collectionid", collection_id])
            items_result = BitwardenService.run_command(list_command)

            if items_result.stderr and "Event post failed" not in items_result.stderr:
                raise BitwardenListItemsError(items_result.stderr)

            # Parse the items and extract credentials
            try:
                items = json.loads(items_result.stdout)
            except json.JSONDecodeError:
                raise BitwardenListItemsError("Failed to parse items JSON. Output: " + items_result.stdout)

            if not items:
                collection_id_str = f" in collection with ID: {collection_id}" if collection_id else ""
                raise BitwardenListItemsError(f"No items found in Bitwarden for URL: {url}{collection_id_str}")

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

            if len(credentials) == 0:
                return {}

            if len(credentials) == 1:
                return credentials[0]

            # Choose multiple credentials according to the defined rule,
            # if no cred matches the rule, return the first one.
            # TODO: For now hard code to choose the first valid email username
            for cred in credentials:
                if is_valid_email(cred.get(BitwardenConstants.USERNAME)):
                    return cred
            LOG.warning("No credential in Bitwarden matches the rule, returning the first match")
            return credentials[0]
        finally:
            # Step 4: Log out
            BitwardenService.logout()

    @staticmethod
    async def get_sensitive_information_from_identity(
        client_id: str,
        client_secret: str,
        master_password: str,
        collection_id: str,
        identity_key: str,
        identity_fields: list[str],
        remaining_retries: int = settings.BITWARDEN_MAX_RETRIES,
        timeout: int = settings.BITWARDEN_TIMEOUT_SECONDS,
        fail_reasons: list[str] = [],
    ) -> dict[str, str]:
        """
        Get the secret value from the Bitwarden CLI.
        """
        try:
            async with asyncio.timeout(timeout):
                return await BitwardenService._get_sensitive_information_from_identity(
                    client_id=client_id,
                    client_secret=client_secret,
                    master_password=master_password,
                    collection_id=collection_id,
                    identity_key=identity_key,
                    identity_fields=identity_fields,
                )
        except Exception as e:
            if remaining_retries <= 0:
                raise BitwardenListItemsError(
                    f"Bitwarden CLI failed after all retry attempts. Fail reasons: {fail_reasons}"
                )

            remaining_retries -= 1
            LOG.info("Retrying to get sensitive information from Bitwarden", remaining_retries=remaining_retries)
            return await BitwardenService.get_sensitive_information_from_identity(
                client_id=client_id,
                client_secret=client_secret,
                master_password=master_password,
                collection_id=collection_id,
                identity_key=identity_key,
                identity_fields=identity_fields,
                remaining_retries=remaining_retries,
                # Double the timeout for the next retry
                timeout=timeout * 2,
                fail_reasons=fail_reasons + [f"{type(e).__name__}: {str(e)}"],
            )

    @staticmethod
    async def _get_sensitive_information_from_identity(
        client_id: str,
        client_secret: str,
        master_password: str,
        collection_id: str,
        identity_key: str,
        identity_fields: list[str],
    ) -> dict[str, str]:
        """
        Get the sensitive information from the Bitwarden CLI.
        """
        try:
            BitwardenService.login(client_id, client_secret)
            BitwardenService.sync()
            session_key = BitwardenService.unlock(master_password)

            # Step 3: Retrieve the items
            list_command = [
                "bw",
                "list",
                "items",
                "--search",
                identity_key,
                "--session",
                session_key,
                "--collectionid",
                collection_id,
            ]
            items_result = BitwardenService.run_command(list_command)

            # Parse the items and extract sensitive information
            try:
                items = json.loads(items_result.stdout)
            except json.JSONDecodeError:
                raise BitwardenListItemsError("Failed to parse items JSON. Output: " + items_result.stdout)

            if not items:
                raise BitwardenListItemsError(
                    f"No items found in Bitwarden for identity key: {identity_key} in collection with ID: {collection_id}"
                )

            # Filter the identity items
            # https://bitwarden.com/help/cli/#create lists the type of the identity items as 4
            identity_items = [item for item in items if item["type"] == 4]

            if len(identity_items) != 1:
                raise BitwardenListItemsError(
                    f"Expected exactly one identity item, but found {len(identity_items)} items for identity key: {identity_key} in collection with ID: {collection_id}"
                )

            identity_item = identity_items[0]

            sensitive_information: dict[str, str] = {}
            for field in identity_fields:
                # The identity item may store sensitive information in custom fields or default fields
                # Custom fields are prioritized over default fields
                # TODO (kerem): Make this case insensitive?
                for item in identity_item["fields"]:
                    if item["name"] == field:
                        sensitive_information[field] = item["value"]
                        break

                if field in identity_item["identity"] and field not in sensitive_information:
                    sensitive_information[field] = identity_item["identity"][field]

            return sensitive_information

        finally:
            # Step 4: Log out
            BitwardenService.logout()

    @staticmethod
    def login(client_id: str, client_secret: str) -> None:
        """
        Log in to the Bitwarden CLI.
        """
        env = {
            "BW_CLIENTID": client_id,
            "BW_CLIENTSECRET": client_secret,
        }
        login_command = ["bw", "login", "--apikey"]
        login_result = BitwardenService.run_command(login_command, env)

        # Validate the login result
        if login_result.stdout and "You are logged in!" not in login_result.stdout:
            raise BitwardenLoginError(f"Failed to log in. stdout: {login_result.stdout} stderr: {login_result.stderr}")

        if login_result.stderr and "You are already logged in as" not in login_result.stderr:
            raise BitwardenLoginError(f"Failed to log in. stdout: {login_result.stdout} stderr: {login_result.stderr}")

        LOG.info("Bitwarden login successful")

    @staticmethod
    def unlock(master_password: str) -> str:
        """
        Unlock the Bitwarden CLI.
        """
        env = {
            "BW_PASSWORD": master_password,
        }
        unlock_command = ["bw", "unlock", "--passwordenv", "BW_PASSWORD"]
        unlock_result = BitwardenService.run_command(unlock_command, env)

        # Validate the unlock result
        if unlock_result.stdout and "Your vault is now unlocked!" not in unlock_result.stdout:
            raise BitwardenUnlockError(
                f"Failed to unlock vault. stdout: {unlock_result.stdout} stderr: {unlock_result.stderr}"
            )

        # Extract session key
        try:
            session_key = BitwardenService._extract_session_key(unlock_result.stdout)
        except Exception as e:
            raise BitwardenUnlockError(f"Unable to extract session key: {str(e)}")

        if not session_key:
            raise BitwardenUnlockError("Session key is empty.")

        return session_key

    @staticmethod
    def sync() -> None:
        """
        Sync the Bitwarden CLI.
        """
        sync_command = ["bw", "sync"]
        LOG.info("Bitwarden CLI sync started")
        sync_result = BitwardenService.run_command(sync_command)
        LOG.info("Bitwarden CLI sync completed")
        if sync_result.stderr:
            raise BitwardenSyncError(sync_result.stderr)

    @staticmethod
    def logout() -> None:
        """
        Log out of the Bitwarden CLI.
        """
        logout_command = ["bw", "logout"]
        logout_result = BitwardenService.run_command(logout_command)
        if logout_result.stderr and "You are not logged in." not in logout_result.stderr:
            raise BitwardenLogoutError(logout_result.stderr)
