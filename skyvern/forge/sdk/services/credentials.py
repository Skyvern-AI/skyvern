import logging
from enum import StrEnum

LOG = logging.getLogger(__name__)


class OnePasswordConstants(StrEnum):
    """Constants for 1Password integration."""

    TOTP = "OP_TOTP"  # Special value to indicate a TOTP code
