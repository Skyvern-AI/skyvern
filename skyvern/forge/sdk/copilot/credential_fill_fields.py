"""Shared credential field constants for copilot credential flows."""

from __future__ import annotations

from typing import Literal, get_args

# Security-sensitive credential subkeys that appear in credential placeholders and
# fill tooling. Kept in one place so drift cannot silently broaden or narrow handling.
# These two constants must remain distinct:
# - CREDENTIAL_FILL_FIELDS includes totp for credential placeholders/flows.
# - LIVE_SCOUT_CREDENTIAL_FIELDS excludes totp because it tracks fields requiring
#   live scout fill-credit behavior. Folding them together silently widens one set
#   and narrows the other, changing enforcement and persistence behavior.
CredentialFillField = Literal["username", "password", "totp"]
CREDENTIAL_FILL_FIELDS: frozenset[str] = frozenset(get_args(CredentialFillField))
LIVE_SCOUT_CREDENTIAL_FIELDS: frozenset[str] = CREDENTIAL_FILL_FIELDS - {"totp"}
