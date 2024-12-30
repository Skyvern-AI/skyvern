import asyncio
import json
import os
import re
import subprocess
from enum import StrEnum

import structlog
import tldextract
from pydantic import BaseModel

from skyvern.config import settings
from skyvern.exceptions import (
    BitwardenAccessDeniedError,
    BitwardenListItemsError,
    BitwardenLoginError,
    BitwardenLogoutError,
    BitwardenSyncError,
    BitwardenUnlockError,
)

LOG = structlog.get_logger()


def is_valid_email(email: str | None) -> bool:
    if not email:
        return False
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return re.match(pattern, email) is not None


class BitwardenConstants(StrEnum):
    BW_ORGANIZATION_ID = "BW_ORGANIZATION_ID"
    BW_COLLECTION_IDS = "BW_COLLECTION_IDS"

    CLIENT_ID = "BW_CLIENT_ID"
    CLIENT_SECRET = "BW_CLIENT_SECRET"
    MASTER_PASSWORD = "BW_MASTER_PASSWORD"
    URL = "BW_URL"
    BW_COLLECTION_ID = "BW_COLLECTION_ID"
    IDENTITY_KEY = "BW_IDENTITY_KEY"
    ITEM_ID = "BW_ITEM_ID"

    USERNAME = "BW_USERNAME"
    PASSWORD = "BW_PASSWORD"
    TOTP = "BW_TOTP"

    CREDIT_CARD_HOLDER_NAME = "BW_CREDIT_CARD_HOLDER_NAME"
    CREDIT_CARD_NUMBER = "BW_CREDIT_CARD_NUMBER"
    CREDIT_CARD_EXPIRATION_MONTH = "BW_CREDIT_CARD_EXPIRATION_MONTH"
    CREDIT_CARD_EXPIRATION_YEAR = "BW_CREDIT_CARD_EXPIRATION_YEAR"
    CREDIT_CARD_CVV = "BW_CREDIT_CARD_CVV"
    CREDIT_CARD_BRAND = "BW_CREDIT_CARD_BRAND"


class BitwardenQueryResult(BaseModel):
    credential: dict[str, str]
    uris: list[str]


class BitwardenService:
    @staticmethod
    def run_command(
        command: list[str], additional_env: dict[str, str] | None = None, timeout: int = 60
    ) -> subprocess.CompletedProcess:
        """
        Run a CLI command with the specified additional environment variables and return the result.
        """
        env = os.environ.copy()  # Copy the current environment
        # Make sure node isn't returning warnings. Warnings are sent through stderr and we raise exceptions on stderr.
        env["NODE_NO_WARNINGS"] = "1"
        if additional_env:
            env.update(additional_env)  # Update with any additional environment variables

        try:
            return subprocess.run(command, capture_output=True, text=True, env=env, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            LOG.error(f"Bitwarden command timed out after {timeout} seconds", stdout=e.stdout, stderr=e.stderr)
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
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
        url: str,
        collection_id: str | None = None,
        max_retries: int = settings.BITWARDEN_MAX_RETRIES,
        timeout: int = settings.BITWARDEN_TIMEOUT_SECONDS,
    ) -> dict[str, str]:
        """
        Get the secret value from the Bitwarden CLI.
        """
        fail_reasons: list[str] = []
        if not bw_organization_id and bw_collection_ids and collection_id not in bw_collection_ids:
            raise BitwardenAccessDeniedError()

        for i in range(max_retries):
            # FIXME: just simply double the timeout for the second try. maybe a better backoff policy when needed
            timeout = (i + 1) * timeout
            try:
                async with asyncio.timeout(timeout):
                    return await BitwardenService._get_secret_value_from_url(
                        client_id=client_id,
                        client_secret=client_secret,
                        master_password=master_password,
                        bw_organization_id=bw_organization_id,
                        bw_collection_ids=bw_collection_ids,
                        url=url,
                        collection_id=collection_id,
                        timeout=timeout,
                    )
            except BitwardenAccessDeniedError as e:
                raise e
            except Exception as e:
                LOG.info("Failed to get secret value from Bitwarden", tried_times=i + 1, exc_info=True)
                fail_reasons.append(f"{type(e).__name__}: {str(e)}")
        else:
            raise BitwardenListItemsError(
                f"Bitwarden CLI failed after all retry attempts. Fail reasons: {fail_reasons}"
            )

    @staticmethod
    async def _get_secret_value_from_url(
        client_id: str,
        client_secret: str,
        master_password: str,
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
        url: str,
        collection_id: str | None = None,
        timeout: int = 60,
    ) -> dict[str, str]:
        """
        Get the secret value from the Bitwarden CLI.
        """
        try:
            BitwardenService.login(client_id, client_secret)
            BitwardenService.sync()
            session_key = BitwardenService.unlock(master_password)

            # Extract the domain from the URL and search for items in Bitwarden with that domain
            extract_url = tldextract.extract(url)
            domain = extract_url.domain
            list_command = [
                "bw",
                "list",
                "items",
                "--search",
                domain,
                "--session",
                session_key,
            ]
            if bw_organization_id:
                LOG.info(
                    "Organization ID is provided, filtering items by organization ID",
                    bw_organization_id=bw_organization_id,
                )
                list_command.extend(["--organizationid", bw_organization_id])
            elif collection_id:
                LOG.info("Collection ID is provided, filtering items by collection ID", collection_id=collection_id)
                list_command.extend(["--collectionid", collection_id])
            else:
                LOG.error("No collection ID or organization ID provided -- this is required")
                raise BitwardenListItemsError("No collection ID or organization ID provided -- this is required")
            items_result = BitwardenService.run_command(list_command, timeout=timeout)

            if items_result.stderr and "Event post failed" not in items_result.stderr:
                raise BitwardenListItemsError(items_result.stderr)

            # Parse the items and extract credentials
            try:
                items = json.loads(items_result.stdout)
            except json.JSONDecodeError:
                raise BitwardenListItemsError("Failed to parse items JSON. Output: " + items_result.stdout)

            # Since Bitwarden can't AND multiple filters, we only use organization id in the list command
            # but we still need to filter the items by collection id here
            if bw_organization_id and collection_id:
                filtered_items = []
                for item in items:
                    if "collectionIds" in item and collection_id in item["collectionIds"]:
                        filtered_items.append(item)
                items = filtered_items

            if not items:
                collection_id_str = f" in collection with ID: {collection_id}" if collection_id else ""
                raise BitwardenListItemsError(f"No items found in Bitwarden for URL: {url}{collection_id_str}")

            bitwarden_result: list[BitwardenQueryResult] = [
                BitwardenQueryResult(
                    credential={
                        BitwardenConstants.USERNAME: item.get("login", {}).get("username", ""),
                        BitwardenConstants.PASSWORD: item.get("login", {}).get("password", ""),
                        BitwardenConstants.TOTP: item.get("login", {}).get("totp", "") or "",
                    },
                    uris=[uri.get("uri") for uri in item.get("login", {}).get("uris", []) if "uri" in uri],
                )
                for item in items
                if "login" in item
            ]

            if len(bitwarden_result) == 0:
                return {}

            if len(bitwarden_result) == 1:
                return bitwarden_result[0].credential

            # Choose multiple credentials according to the defined rule,
            # if no cred matches the rule, return the first one.
            # TODO: For now hard code to choose the first matched result
            for single_result in bitwarden_result:
                # check the username is a valid email
                if is_valid_email(single_result.credential.get(BitwardenConstants.USERNAME)):
                    for uri in single_result.uris:
                        # check if the register_domain is the same
                        if extract_url.registered_domain == tldextract.extract(uri).registered_domain:
                            return single_result.credential
            LOG.warning("No credential in Bitwarden matches the rule, returning the first match")
            return bitwarden_result[0].credential
        finally:
            # Step 4: Log out
            BitwardenService.logout()

    @staticmethod
    async def get_sensitive_information_from_identity(
        client_id: str,
        client_secret: str,
        master_password: str,
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
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
        if not bw_organization_id and bw_collection_ids and collection_id not in bw_collection_ids:
            raise BitwardenAccessDeniedError()
        try:
            async with asyncio.timeout(timeout):
                return await BitwardenService._get_sensitive_information_from_identity(
                    client_id=client_id,
                    client_secret=client_secret,
                    master_password=master_password,
                    bw_organization_id=bw_organization_id,
                    bw_collection_ids=bw_collection_ids,
                    collection_id=collection_id,
                    identity_key=identity_key,
                    identity_fields=identity_fields,
                )
        except BitwardenAccessDeniedError as e:
            raise e
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
                bw_organization_id=bw_organization_id,
                bw_collection_ids=bw_collection_ids,
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
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
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
            if bw_organization_id:
                list_command.extend(["--organizationid", bw_organization_id])
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

            # We may want to filter it by type in the future, but for now we just take the first item and check its identity fields
            # identity_items = [item for item in items if item["type"] == 4]

            identity_item = items[0]

            sensitive_information: dict[str, str] = {}
            for field in identity_fields:
                # The identity item may store sensitive information in custom fields or default fields
                # Custom fields are prioritized over default fields
                # TODO (kerem): Make this case insensitive?
                for item in identity_item["fields"]:
                    if item["name"] == field:
                        sensitive_information[field] = item["value"]
                        break

                if (
                    "identity" in identity_item
                    and field in identity_item["identity"]
                    and field not in sensitive_information
                ):
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

    @staticmethod
    async def _get_credit_card_data(
        client_id: str,
        client_secret: str,
        master_password: str,
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
        collection_id: str,
        item_id: str,
    ) -> dict[str, str]:
        """
        Get the credit card data from the Bitwarden CLI.
        """
        try:
            BitwardenService.login(client_id, client_secret)
            BitwardenService.sync()
            session_key = BitwardenService.unlock(master_password)

            # Step 3: Get the item
            get_command = [
                "bw",
                "get",
                "item",
                item_id,
                "--session",
                session_key,
            ]
            item_result = BitwardenService.run_command(get_command)

            # Parse the item and extract credit card data
            try:
                item = json.loads(item_result.stdout)
            except json.JSONDecodeError:
                raise BitwardenListItemsError(f"Failed to parse item JSON for item ID: {item_id}")

            if not item:
                raise BitwardenListItemsError(f"No item found in Bitwarden for item ID: {item_id}")

            # Check if the bw_organization_id matches
            if bw_organization_id:
                item_organization_id = item.get("organizationId")
                if item_organization_id != bw_organization_id:
                    raise BitwardenAccessDeniedError()

            if bw_collection_ids:
                item_collection_ids = item.get("collectionIds")
                if item_collection_ids and collection_id not in bw_collection_ids:
                    raise BitwardenAccessDeniedError()

            # Check if the item is a credit card
            # https://bitwarden.com/help/cli/#create lists the type of the credit card items as 3
            if item["type"] != 3:
                raise BitwardenListItemsError(f"Item with ID: {item_id} is not a credit card type")

            credit_card_data = item["card"]

            mapped_credit_card_data: dict[str, str] = {
                BitwardenConstants.CREDIT_CARD_HOLDER_NAME: credit_card_data["cardholderName"],
                BitwardenConstants.CREDIT_CARD_NUMBER: credit_card_data["number"],
                BitwardenConstants.CREDIT_CARD_EXPIRATION_MONTH: credit_card_data["expMonth"],
                BitwardenConstants.CREDIT_CARD_EXPIRATION_YEAR: credit_card_data["expYear"],
                BitwardenConstants.CREDIT_CARD_CVV: credit_card_data["code"],
                BitwardenConstants.CREDIT_CARD_BRAND: credit_card_data["brand"],
            }

            return mapped_credit_card_data
        finally:
            # Step 4: Log out
            BitwardenService.logout()

    @staticmethod
    async def get_credit_card_data(
        client_id: str,
        client_secret: str,
        master_password: str,
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
        collection_id: str,
        item_id: str,
        remaining_retries: int = settings.BITWARDEN_MAX_RETRIES,
        fail_reasons: list[str] = [],
    ) -> dict[str, str]:
        """
        Get the credit card data from the Bitwarden CLI.
        """
        if not bw_organization_id and not bw_collection_ids:
            raise BitwardenAccessDeniedError()
        try:
            async with asyncio.timeout(settings.BITWARDEN_TIMEOUT_SECONDS):
                return await BitwardenService._get_credit_card_data(
                    client_id=client_id,
                    client_secret=client_secret,
                    master_password=master_password,
                    bw_organization_id=bw_organization_id,
                    bw_collection_ids=bw_collection_ids,
                    collection_id=collection_id,
                    item_id=item_id,
                )
        except BitwardenAccessDeniedError as e:
            raise e
        except Exception as e:
            if remaining_retries <= 0:
                raise BitwardenListItemsError(
                    f"Bitwarden CLI failed after all retry attempts. Fail reasons: {fail_reasons}"
                )

            remaining_retries -= 1
            LOG.info("Retrying to get credit card data from Bitwarden", remaining_retries=remaining_retries)
            return await BitwardenService.get_credit_card_data(
                client_id=client_id,
                client_secret=client_secret,
                master_password=master_password,
                bw_organization_id=bw_organization_id,
                bw_collection_ids=bw_collection_ids,
                collection_id=collection_id,
                item_id=item_id,
                remaining_retries=remaining_retries,
                fail_reasons=fail_reasons + [f"{type(e).__name__}: {str(e)}"],
            )
