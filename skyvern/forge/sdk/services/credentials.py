import logging
import os
from typing import Optional

from onepassword.client import Client

LOG = logging.getLogger(__name__)


async def resolve_secret(reference: str) -> str:
    """
    Resolve a 1Password secret reference.

    Args:
        reference: A 1Password reference in the format op://vault_id/item_id/field
                  or a custom format vault_id:item_id

    Returns:
        The resolved secret value
    """
    token = os.getenv("OP_SERVICE_ACCOUNT_TOKEN")
    if not token:
        raise ValueError("OP_SERVICE_ACCOUNT_TOKEN environment variable not set")

    client = await Client.authenticate(
        auth=token,
        integration_name="Skyvern 1Password",
        integration_version="v1.0.0",
    )

    # Handle standard op:// format
    if reference.startswith("op://"):
        return await client.secrets.resolve(reference)

    # Handle custom format (vault_id:item_id)
    if ":" in reference:
        vault_id, item_id = reference.split(":", 1)
        result = await get_1password_item_details(client, vault_id, item_id)
        return result

    raise ValueError(f"Invalid 1Password reference format: {reference}")


async def get_1password_item_details(client: Client, vault_id: str, item_id: str) -> str:
    """
    Get details of a 1Password item.

    Args:
        client: Authenticated 1Password client
        vault_id: The vault ID
        item_id: The item ID

    Returns:
        JSON string containing item fields and their values
    """
    try:
        item = await client.items.get(vault_id, item_id)

        # Create a dictionary of all fields
        result = {}

        # Debug: Log the structure of the item and fields
        LOG.info(f"1Password item structure: {dir(item)}")
        if hasattr(item, "fields") and item.fields:
            LOG.info(f"First field structure: {dir(item.fields[0])}")
            LOG.info(
                f"Field value example: {item.fields[0].value if hasattr(item.fields[0], 'value') else 'No value attribute'}"
            )

        # Add all fields with proper attribute checking
        for i, field in enumerate(item.fields):
            # Debug: Log each field's structure
            LOG.debug(f"Field {i} structure: {dir(field)}")

            if hasattr(field, "value") and field.value is not None:
                # Safely get field identifier - use id attribute or fallback to a default
                try:
                    # Try different possible attribute names for the field identifier
                    field_id = None

                    # Check all available attributes on the field object
                    field_attrs = dir(field)
                    LOG.debug(f"Field {i} attributes: {field_attrs}")

                    # Try to get the most appropriate identifier
                    if hasattr(field, "id") and field.id:
                        field_id = field.id
                        LOG.debug(f"Using field.id: {field_id}")
                    elif hasattr(field, "name") and field.name:
                        field_id = field.name
                        LOG.debug(f"Using field.name: {field_id}")
                    elif hasattr(field, "label") and field.label:
                        field_id = field.label
                        LOG.debug(f"Using field.label: {field_id}")
                    elif hasattr(field, "type") and field.type:
                        field_id = f"{field.type}_{i}"
                        LOG.debug(f"Using field.type: {field_id}")
                    else:
                        # If no identifier found, generate one based on index
                        field_id = f"field_{i}"
                        LOG.debug(f"Using generated id: {field_id}")

                    # Create a safe key name
                    key = str(field_id).lower().replace(" ", "_")
                    result[key] = field.value
                    LOG.debug(f"Added field with key '{key}' and value type: {type(field.value).__name__}")

                except Exception as field_err:
                    LOG.warning(f"Error processing field {i}: {field_err}")
                    # Still try to capture the value with a generic key
                    result[f"field_{i}"] = field.value

        # Explicitly look for username and password fields
        for i, field in enumerate(item.fields):
            try:
                # Check for username field using various possible attributes
                if "username" not in result:
                    if hasattr(field, "id") and field.id == "username" and hasattr(field, "value") and field.value:
                        result["username"] = field.value
                        LOG.debug(f"Found username field at index {i}")
                    elif (
                        hasattr(field, "purpose")
                        and field.purpose == "USERNAME"
                        and hasattr(field, "value")
                        and field.value
                    ):
                        result["username"] = field.value
                        LOG.debug(f"Found username field by purpose at index {i}")
                    elif (
                        hasattr(field, "type") and field.type == "USERNAME" and hasattr(field, "value") and field.value
                    ):
                        result["username"] = field.value
                        LOG.debug(f"Found username field by type at index {i}")

                # Check for password field using various possible attributes
                if "password" not in result:
                    if hasattr(field, "id") and field.id == "password" and hasattr(field, "value") and field.value:
                        result["password"] = field.value
                        LOG.debug(f"Found password field at index {i}")
                    elif (
                        hasattr(field, "purpose")
                        and field.purpose == "PASSWORD"
                        and hasattr(field, "value")
                        and field.value
                    ):
                        result["password"] = field.value
                        LOG.debug(f"Found password field by purpose at index {i}")
                    elif (
                        hasattr(field, "type") and field.type == "PASSWORD" and hasattr(field, "value") and field.value
                    ):
                        result["password"] = field.value
                        LOG.debug(f"Found password field by type at index {i}")
            except Exception as field_err:
                LOG.warning(f"Error processing username/password field at index {i}: {field_err}")

        # Add TOTP if available
        try:
            totp = await get_totp_for_item(client, vault_id, item_id)
            if totp:
                result["totp"] = totp
        except Exception as totp_err:
            LOG.warning(f"Error getting TOTP: {totp_err}")

        import json

        return json.dumps(result)
    except Exception as e:
        LOG.error(f"Error retrieving 1Password item {vault_id}:{item_id}: {str(e)}")
        raise


async def get_totp_for_item(client: Client, vault_id: str, item_id: str) -> Optional[str]:
    """
    Get the TOTP code for a 1Password item if available.

    Args:
        client: Authenticated 1Password client
        vault_id: The vault ID
        item_id: The item ID

    Returns:
        TOTP code if available, None otherwise
    """
    try:
        totp = await client.items.get_totp(vault_id, item_id)
        return totp
    except Exception:
        # TOTP might not be available for this item
        return None
