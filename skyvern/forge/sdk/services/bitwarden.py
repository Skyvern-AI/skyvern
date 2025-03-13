import asyncio
import json
import os
import re
import urllib.parse
from enum import IntEnum, StrEnum
from typing import Tuple

import structlog
import tldextract
from pydantic import BaseModel

from skyvern.config import settings
from skyvern.exceptions import (
    BitwardenAccessDeniedError,
    BitwardenCreateCollectionError,
    BitwardenCreateCreditCardItemError,
    BitwardenCreateLoginItemError,
    BitwardenGetItemError,
    BitwardenListItemsError,
    BitwardenLoginError,
    BitwardenLogoutError,
    BitwardenSecretError,
    BitwardenSyncError,
    BitwardenUnlockError,
)
from skyvern.forge.sdk.api.aws import aws_client
from skyvern.forge.sdk.core.aiohttp_helper import aiohttp_delete, aiohttp_get_json, aiohttp_post
from skyvern.forge.sdk.schemas.credentials import (
    CredentialItem,
    CredentialType,
    CreditCardCredential,
    PasswordCredential,
)

LOG = structlog.get_logger()
BITWARDEN_SERVER_BASE_URL = f"{settings.BITWARDEN_SERVER}:{settings.BITWARDEN_SERVER_PORT or 8002}"


class BitwardenItemType(IntEnum):
    LOGIN = 1
    SECURE_NOTE = 2
    CREDIT_CARD = 3
    IDENTITY = 4


def get_bitwarden_item_type_code(item_type: BitwardenItemType) -> int:
    if item_type == BitwardenItemType.LOGIN:
        return 1
    elif item_type == BitwardenItemType.SECURE_NOTE:
        return 2
    elif item_type == BitwardenItemType.CREDIT_CARD:
        return 3
    elif item_type == BitwardenItemType.IDENTITY:
        return 4


def get_list_response_item_from_bitwarden_item(item: dict) -> CredentialItem:
    if item["type"] == BitwardenItemType.LOGIN:
        login = item["login"]
        return CredentialItem(
            item_id=item["id"],
            credential=PasswordCredential(
                username=login["username"] or "",
                password=login["password"] or "",
                totp=login["totp"],
            ),
            name=item["name"],
            credential_type=CredentialType.PASSWORD,
        )
    elif item["type"] == BitwardenItemType.CREDIT_CARD:
        card = item["card"]
        return CredentialItem(
            item_id=item["id"],
            credential=CreditCardCredential(
                card_holder_name=card["cardholderName"],
                card_number=card["number"],
                card_exp_month=card["expMonth"],
                card_exp_year=card["expYear"],
                card_cvv=card["code"],
                card_brand=card["brand"],
            ),
            name=item["name"],
            credential_type=CredentialType.CREDIT_CARD,
        )
    else:
        raise BitwardenGetItemError(f"Unsupported item type: {item['type']}")


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
    BW_ITEM_ID = "BW_ITEM_ID"

    USERNAME = "BW_USERNAME"
    PASSWORD = "BW_PASSWORD"
    TOTP = "BW_TOTP"

    CREDIT_CARD_HOLDER_NAME = "BW_CREDIT_CARD_HOLDER_NAME"
    CREDIT_CARD_NUMBER = "BW_CREDIT_CARD_NUMBER"
    CREDIT_CARD_EXPIRATION_MONTH = "BW_CREDIT_CARD_EXPIRATION_MONTH"
    CREDIT_CARD_EXPIRATION_YEAR = "BW_CREDIT_CARD_EXPIRATION_YEAR"
    CREDIT_CARD_CVV = "BW_CREDIT_CARD_CVV"
    CREDIT_CARD_BRAND = "BW_CREDIT_CARD_BRAND"

    SKYVERN_AUTH_BITWARDEN_ORGANIZATION_ID = "SKYVERN_AUTH_BITWARDEN_ORGANIZATION_ID"
    SKYVERN_AUTH_BITWARDEN_MASTER_PASSWORD = "SKYVERN_AUTH_BITWARDEN_MASTER_PASSWORD"
    SKYVERN_AUTH_BITWARDEN_CLIENT_ID = "SKYVERN_AUTH_BITWARDEN_CLIENT_ID"
    SKYVERN_AUTH_BITWARDEN_CLIENT_SECRET = "SKYVERN_AUTH_BITWARDEN_CLIENT_SECRET"


class BitwardenQueryResult(BaseModel):
    credential: dict[str, str]
    uris: list[str]


class RunCommandResult(BaseModel):
    stdout: str
    stderr: str
    returncode: int


class BitwardenService:
    @staticmethod
    async def run_command(
        command: list[str], additional_env: dict[str, str] | None = None, timeout: int = 60
    ) -> RunCommandResult:
        """
        Run a CLI command with the specified additional environment variables and return the result.
        """
        env = os.environ.copy()  # Copy the current environment
        # Make sure node isn't returning warnings. Warnings are sent through stderr and we raise exceptions on stderr.
        env["NODE_NO_WARNINGS"] = "1"
        if additional_env:
            env.update(additional_env)  # Update with any additional environment variables

        try:
            async with asyncio.timeout(timeout):
                shell_subprocess = await asyncio.create_subprocess_shell(
                    " ".join(command),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, stderr = await shell_subprocess.communicate()
                return RunCommandResult(
                    stdout=stdout.decode(),
                    stderr=stderr.decode(),
                    returncode=shell_subprocess.returncode,
                )
        except asyncio.TimeoutError as e:
            LOG.error(f"Bitwarden command timed out after {timeout} seconds", exc_info=True)
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
        url: str | None = None,
        collection_id: str | None = None,
        item_id: str | None = None,
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
                        item_id=item_id,
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
    def extract_totp_secret(totp_value: str) -> str:
        """
        Extract the TOTP secret from either a raw secret or a TOTP URI.

        Args:
            totp_value: Raw TOTP secret or URI (otpauth://totp/...)

        Returns:
            The extracted TOTP secret

        Example:
            >>> BitwardenService.extract_totp_secret("AAAAAABBBBBBB")
            "AAAAAABBBBBBB"
            >>> BitwardenService.extract_totp_secret("otpauth://totp/user@domain.com?secret=AAAAAABBBBBBB")
            "AAAAAABBBBBBB"
        """
        if not totp_value:
            return ""

        # Handle TOTP URI format
        if totp_value.startswith("otpauth://"):
            try:
                # Parse the URI to extract the secret
                query = urllib.parse.urlparse(totp_value).query
                params = dict(urllib.parse.parse_qsl(query))
                return params.get("secret", "")
            except Exception:
                LOG.error(
                    "Failed to parse TOTP URI",
                    totp_value=totp_value,
                    exc_info=True,
                )
                return ""

        return totp_value

    @staticmethod
    async def _get_secret_value_from_url(
        client_id: str,
        client_secret: str,
        master_password: str,
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
        url: str | None = None,
        collection_id: str | None = None,
        item_id: str | None = None,
        timeout: int = 60,
    ) -> dict[str, str]:
        """
        Get the secret value from the Bitwarden CLI.
        """
        try:
            await BitwardenService.login(client_id, client_secret)
            await BitwardenService.sync()
            session_key = await BitwardenService.unlock(master_password)

            if item_id:  # if item_id provided, get single item by item id
                command = ["bw", "get", "item", item_id, "--session", session_key]
                item_result = await BitwardenService.run_command(command)
                if item_result.stderr:
                    raise BitwardenGetItemError(
                        f"Failed to get the bitwarden item {item_id}. Error: {item_result.stderr}"
                    )
                try:
                    item = json.loads(item_result.stdout)
                except json.JSONDecodeError:
                    raise BitwardenGetItemError(f"Failed to parse item JSON for item ID: {item_id}")
                return {
                    BitwardenConstants.USERNAME: item["login"]["username"],
                    BitwardenConstants.PASSWORD: item["login"]["password"],
                    BitwardenConstants.TOTP: item["login"]["totp"],
                }
            elif not url:
                # if item_id is not provided, we need a url to search for items
                raise BitwardenGetItemError("No url or item ID provided")

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
            items_result = await BitwardenService.run_command(list_command, timeout=timeout)

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

            bitwarden_result: list[BitwardenQueryResult] = []
            for item in items:
                if "login" not in item:
                    continue

                login = item["login"]
                totp = BitwardenService.extract_totp_secret(login.get("totp", ""))

                bitwarden_result.append(
                    BitwardenQueryResult(
                        credential={
                            BitwardenConstants.USERNAME: login.get("username", ""),
                            BitwardenConstants.PASSWORD: login.get("password", ""),
                            BitwardenConstants.TOTP: totp,
                        },
                        uris=[uri.get("uri") for uri in login.get("uris", []) if "uri" in uri],
                    )
                )

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
            await BitwardenService.logout()

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
            await BitwardenService.login(client_id, client_secret)
            await BitwardenService.sync()
            session_key = await BitwardenService.unlock(master_password)

            if not bw_organization_id and not collection_id:
                raise BitwardenAccessDeniedError()

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
            items_result = await BitwardenService.run_command(list_command)

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
            await BitwardenService.logout()

    @staticmethod
    async def login(client_id: str, client_secret: str) -> None:
        """
        Log in to the Bitwarden CLI.
        """
        env = {
            "BW_CLIENTID": client_id,
            "BW_CLIENTSECRET": client_secret,
        }
        login_command = ["bw", "login", "--apikey"]
        login_result = await BitwardenService.run_command(login_command, env)

        # Validate the login result
        if login_result.stdout and "You are logged in!" not in login_result.stdout:
            raise BitwardenLoginError(f"Failed to log in. stdout: {login_result.stdout} stderr: {login_result.stderr}")

        if login_result.stderr and "You are already logged in as" not in login_result.stderr:
            raise BitwardenLoginError(f"Failed to log in. stdout: {login_result.stdout} stderr: {login_result.stderr}")

        LOG.info("Bitwarden login successful")

    @staticmethod
    async def unlock(master_password: str) -> str:
        """
        Unlock the Bitwarden CLI.
        """
        env = {
            "BW_PASSWORD": master_password,
        }
        unlock_command = ["bw", "unlock", "--passwordenv", "BW_PASSWORD"]
        unlock_result = await BitwardenService.run_command(unlock_command, env)

        # Validate the unlock result
        if unlock_result.stdout and "Your vault is now unlocked!" not in unlock_result.stdout:
            raise BitwardenUnlockError(
                f"Failed to unlock vault. stdout: {unlock_result.stdout} stderr: {unlock_result.stderr}"
            )

        # Extract session key
        try:
            session_key = BitwardenService._extract_session_key(unlock_result.stdout)
        except Exception as e:
            raise BitwardenUnlockError(f"Unable to extract session key: {str(e)}. stderr: {unlock_result.stderr}")

        if not session_key:
            raise BitwardenUnlockError(f"Session key is empty. stderr: {unlock_result.stderr}")

        return session_key

    @staticmethod
    async def sync() -> None:
        """
        Sync the Bitwarden CLI.
        """
        sync_command = ["bw", "sync"]
        LOG.info("Bitwarden CLI sync started")
        sync_result = await BitwardenService.run_command(sync_command)
        LOG.info("Bitwarden CLI sync completed")
        if sync_result.stderr:
            raise BitwardenSyncError(sync_result.stderr)

    @staticmethod
    async def logout() -> None:
        """
        Log out of the Bitwarden CLI.
        """
        logout_command = ["bw", "logout"]
        logout_result = await BitwardenService.run_command(logout_command)
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
            await BitwardenService.login(client_id, client_secret)
            await BitwardenService.sync()
            session_key = await BitwardenService.unlock(master_password)

            # Step 3: Get the item
            get_command = [
                "bw",
                "get",
                "item",
                item_id,
                "--session",
                session_key,
            ]

            # Bitwarden CLI doesn't support filtering by organization ID or collection ID for credit card data so we just raise an error if no collection ID or organization ID is provided
            if not bw_organization_id and not collection_id:
                LOG.error("No collection ID or organization ID provided -- this is required")
                raise BitwardenAccessDeniedError()

            item_result = await BitwardenService.run_command(get_command)

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
            await BitwardenService.logout()

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

    @staticmethod
    async def _unlock_using_server(master_password: str) -> None:
        status_response = await aiohttp_get_json(f"{BITWARDEN_SERVER_BASE_URL}/status")
        status = status_response["data"]["template"]["status"]
        if status != "unlocked":
            await aiohttp_post(f"{BITWARDEN_SERVER_BASE_URL}/unlock", data={"password": master_password})

    @staticmethod
    async def _get_login_item_by_id_using_server(item_id: str) -> PasswordCredential:
        response = await aiohttp_get_json(f"{BITWARDEN_SERVER_BASE_URL}/object/item/{item_id}")
        if not response or response.get("success") is False:
            raise BitwardenGetItemError(f"Failed to get login item by ID: {item_id}")

        login = response["data"]["login"]

        if not login:
            raise BitwardenGetItemError(f"Item with ID: {item_id} is not a login item")

        return PasswordCredential(
            username=login["username"] or "",
            password=login["password"] or "",
            totp=login["totp"],
        )

    @staticmethod
    async def _create_login_item_using_server(
        bw_organization_id: str,
        collection_id: str,
        name: str,
        credential: PasswordCredential,
    ) -> str:
        item_template = await aiohttp_get_json(f"{BITWARDEN_SERVER_BASE_URL}/object/template/item")
        login_template = await aiohttp_get_json(f"{BITWARDEN_SERVER_BASE_URL}/object/template/item.login")

        item_template = item_template["data"]["template"]
        login_template = login_template["data"]["template"]

        login_template["username"] = credential.username
        login_template["password"] = credential.password
        login_template["totp"] = credential.totp

        item_template["type"] = get_bitwarden_item_type_code(BitwardenItemType.LOGIN)
        item_template["name"] = name
        item_template["login"] = login_template
        item_template["collectionIds"] = [collection_id]
        item_template["organizationId"] = bw_organization_id

        response = await aiohttp_post(f"{BITWARDEN_SERVER_BASE_URL}/object/item", data=item_template)
        if not response or response.get("success") is False:
            raise BitwardenCreateLoginItemError("Failed to create login item")

        return response["data"]["id"]

    @staticmethod
    async def _create_credit_card_item_using_server(
        bw_organization_id: str,
        collection_id: str,
        name: str,
        credential: CreditCardCredential,
    ) -> str:
        item_template = await aiohttp_get_json(f"{BITWARDEN_SERVER_BASE_URL}/object/template/item")
        credit_card_template = await aiohttp_get_json(f"{BITWARDEN_SERVER_BASE_URL}/object/template/item.card")

        item_template = item_template["data"]["template"]
        credit_card_template = credit_card_template["data"]["template"]

        credit_card_template["cardholderName"] = credential.card_holder_name
        credit_card_template["number"] = credential.card_number
        credit_card_template["expMonth"] = credential.card_exp_month
        credit_card_template["expYear"] = credential.card_exp_year
        credit_card_template["code"] = credential.card_cvv
        credit_card_template["brand"] = credential.card_brand

        item_template["type"] = get_bitwarden_item_type_code(BitwardenItemType.CREDIT_CARD)
        item_template["name"] = name
        item_template["card"] = credit_card_template
        item_template["collectionIds"] = [collection_id]
        item_template["organizationId"] = bw_organization_id

        response = await aiohttp_post(f"{BITWARDEN_SERVER_BASE_URL}/object/item", data=item_template)
        if not response or response.get("success") is False:
            raise BitwardenCreateCreditCardItemError("Failed to create credit card item")

        return response["data"]["id"]

    @staticmethod
    async def create_credential_item(
        collection_id: str,
        name: str,
        credential: PasswordCredential | CreditCardCredential,
    ) -> str:
        try:
            master_password, bw_organization_id, _, _ = await BitwardenService._get_skyvern_auth_secrets()

            await BitwardenService._unlock_using_server(master_password)
            if isinstance(credential, PasswordCredential):
                return await BitwardenService._create_login_item_using_server(
                    bw_organization_id=bw_organization_id,
                    collection_id=collection_id,
                    name=name,
                    credential=credential,
                )
            else:
                return await BitwardenService._create_credit_card_item_using_server(
                    bw_organization_id=bw_organization_id,
                    collection_id=collection_id,
                    name=name,
                    credential=credential,
                )
        except Exception as e:
            raise e

    @staticmethod
    async def _get_skyvern_auth_master_password() -> str:
        master_password = settings.SKYVERN_AUTH_BITWARDEN_MASTER_PASSWORD
        if not master_password:
            master_password = await aws_client.get_secret(BitwardenConstants.SKYVERN_AUTH_BITWARDEN_MASTER_PASSWORD)
        if not master_password:
            raise BitwardenSecretError("Skyvern auth master password is not set")
        return master_password

    @staticmethod
    async def _get_skyvern_auth_organization_id() -> str:
        bw_organization_id = settings.SKYVERN_AUTH_BITWARDEN_ORGANIZATION_ID
        if not bw_organization_id:
            bw_organization_id = await aws_client.get_secret(BitwardenConstants.SKYVERN_AUTH_BITWARDEN_ORGANIZATION_ID)
        if not bw_organization_id:
            raise BitwardenSecretError("Skyvern auth organization ID is not set")
        return bw_organization_id

    @staticmethod
    async def _get_skyvern_auth_client_id() -> str:
        client_id = settings.SKYVERN_AUTH_BITWARDEN_CLIENT_ID
        if not client_id:
            client_id = await aws_client.get_secret(BitwardenConstants.SKYVERN_AUTH_BITWARDEN_CLIENT_ID)
        if not client_id:
            raise BitwardenSecretError("Skyvern auth client ID is not set")
        return client_id

    @staticmethod
    async def _get_skyvern_auth_client_secret() -> str:
        client_secret = settings.SKYVERN_AUTH_BITWARDEN_CLIENT_SECRET
        if not client_secret:
            client_secret = await aws_client.get_secret(BitwardenConstants.SKYVERN_AUTH_BITWARDEN_CLIENT_SECRET)
        if not client_secret:
            raise BitwardenSecretError("Skyvern auth client secret is not set")
        return client_secret

    @staticmethod
    async def create_collection(
        name: str,
    ) -> str:
        """
        Create a collection in Bitwarden and return the collection ID.
        """
        try:
            master_password, bw_organization_id, _, _ = await BitwardenService._get_skyvern_auth_secrets()

            await BitwardenService._unlock_using_server(master_password)
            return await BitwardenService._create_collection_using_server(bw_organization_id, name)

        except Exception as e:
            raise e

    @staticmethod
    async def _create_collection_using_server(bw_organization_id: str, name: str) -> str:
        collection_template_response = await aiohttp_get_json(f"{BITWARDEN_SERVER_BASE_URL}/object/template/collection")
        collection_template = collection_template_response["data"]["template"]

        collection_template["name"] = name
        collection_template["organizationId"] = bw_organization_id

        response = await aiohttp_post(
            f"{BITWARDEN_SERVER_BASE_URL}/object/org-collection?organizationId={bw_organization_id}",
            data=collection_template,
        )
        if not response or response.get("success") is False:
            raise BitwardenCreateCollectionError("Failed to create collection")

        return response["data"]["id"]

    @staticmethod
    async def _get_skyvern_auth_secrets() -> Tuple[str, str, str, str]:
        master_password, bw_organization_id, client_id, client_secret = await asyncio.gather(
            BitwardenService._get_skyvern_auth_master_password(),
            BitwardenService._get_skyvern_auth_organization_id(),
            BitwardenService._get_skyvern_auth_client_id(),
            BitwardenService._get_skyvern_auth_client_secret(),
        )
        return master_password, bw_organization_id, client_id, client_secret

    @staticmethod
    async def get_items_by_item_ids(
        item_ids: list[str],
    ) -> list[CredentialItem]:
        try:
            master_password, _, _, _ = await BitwardenService._get_skyvern_auth_secrets()
            await BitwardenService._unlock_using_server(master_password)
            return await BitwardenService._get_items_by_item_ids_using_server(item_ids)
        except Exception as e:
            raise e

    @staticmethod
    async def _get_items_by_item_ids_using_server(item_ids: list[str]) -> list[CredentialItem]:
        responses = await asyncio.gather(
            *[aiohttp_get_json(f"{BITWARDEN_SERVER_BASE_URL}/object/item/{item_id}") for item_id in item_ids]
        )
        if not responses or any(response.get("success") is False for response in responses):
            raise BitwardenGetItemError("Failed to get collection items")

        return [get_list_response_item_from_bitwarden_item(response["data"]) for response in responses]

    @staticmethod
    async def get_collection_items(
        collection_id: str,
    ) -> list[CredentialItem]:
        try:
            master_password, _, _, _ = await BitwardenService._get_skyvern_auth_secrets()
            await BitwardenService._unlock_using_server(master_password)
            return await BitwardenService._get_collection_items_using_server(collection_id)
        except Exception as e:
            raise e

    @staticmethod
    async def _get_collection_items_using_server(collection_id: str) -> list[CredentialItem]:
        response = await aiohttp_get_json(f"{BITWARDEN_SERVER_BASE_URL}/list/object/items?collectionId={collection_id}")
        if not response or response.get("success") is False:
            raise BitwardenGetItemError("Failed to get collection items")

        items = response["data"]["data"]
        items = map(lambda item: get_list_response_item_from_bitwarden_item(item), items)
        return list(items)

    @staticmethod
    async def get_credential_item(
        item_id: str,
    ) -> CredentialItem:
        try:
            master_password, _, _, _ = await BitwardenService._get_skyvern_auth_secrets()
            await BitwardenService._unlock_using_server(master_password)
            return await BitwardenService._get_credential_item_by_id_using_server(item_id)
        except Exception as e:
            raise e

    @staticmethod
    async def _get_credential_item_by_id_using_server(item_id: str) -> CredentialItem:
        response = await aiohttp_get_json(f"{BITWARDEN_SERVER_BASE_URL}/object/item/{item_id}")
        if not response or response.get("success") is False:
            raise BitwardenGetItemError(f"Failed to get credential item by ID: {item_id}")

        if response["data"]["type"] == BitwardenItemType.LOGIN:
            login_item = response["data"]["login"]
            name = response["data"]["name"]
            return CredentialItem(
                item_id=item_id,
                credential_type=CredentialType.PASSWORD,
                name=name,
                credential=PasswordCredential(
                    username=login_item["username"] or "",
                    password=login_item["password"] or "",
                    totp=login_item["totp"],
                ),
            )
        elif response["data"]["type"] == BitwardenItemType.CREDIT_CARD:
            credit_card_item = response["data"]["card"]
            name = response["data"]["name"]
            return CredentialItem(
                item_id=item_id,
                credential_type=CredentialType.CREDIT_CARD,
                name=name,
                credential=CreditCardCredential(
                    card_holder_name=credit_card_item["cardholderName"],
                    card_number=credit_card_item["number"],
                    card_exp_month=credit_card_item["expMonth"],
                    card_exp_year=credit_card_item["expYear"],
                    card_cvv=credit_card_item["code"],
                    card_brand=credit_card_item["brand"],
                ),
            )
        else:
            raise BitwardenGetItemError(f"Unsupported item type: {response['data']['type']}")

    @staticmethod
    async def delete_credential_item(
        item_id: str,
    ) -> None:
        try:
            master_password, _, _, _ = await BitwardenService._get_skyvern_auth_secrets()
            await BitwardenService._unlock_using_server(master_password)
            await BitwardenService._delete_credential_item_using_server(item_id)
        except Exception as e:
            raise e

    @staticmethod
    async def _delete_credential_item_using_server(item_id: str) -> None:
        await aiohttp_delete(f"{BITWARDEN_SERVER_BASE_URL}/object/item/{item_id}")
