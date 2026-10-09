from __future__ import annotations

from collections import Counter
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.copilot.ask_user import (
    CREDENTIAL_DELETE_OUTCOME_NOTE,
    MAX_CREDENTIALS_PER_CARD,
    CredentialDeleteReview,
    CredentialDeleteRow,
    QuestionInteraction,
    wait_for_interaction,
)
from skyvern.forge.sdk.copilot.context import CopilotContext

DELETE_TOOL_DESCRIPTION = (
    "Delete saved credentials (passwords, cards, secrets) from this organization's Skyvern vault, after the "
    "user confirms on a card in this chat."
    "\n\n"
    "Pass the credential ids to delete, from list_credentials. The card shows each entry's name and type and "
    "how many credentials the organization has in total; the user can uncheck entries, then confirms or "
    f"cancels. Deletion is permanent. At most {MAX_CREDENTIALS_PER_CARD} ids per card; to delete more, call again "
    "with the rest after "
    "this card is answered. For all credentials, collect every id from list_credentials until has_more is false."
    "\n\n"
    "The result lists an outcome per entry: deleted, not_found, or failed. "
    + CREDENTIAL_DELETE_OUTCOME_NOTE
    + " Report exactly those outcomes. If the user cancels, nothing was deleted. "
    "Signing out of a website in a browser deletes no saved credential, and deleting a credential signs "
    "nothing out. This does not delete passwords stored on other websites, revoke sessions, or rotate "
    "Skyvern API keys."
)


class CredentialDeletionRefusal(BaseModel):
    ok: Literal[False] = False
    error: str
    deleted: Literal[False] = False


def credential_delete_enabled() -> bool:
    return settings.COPILOT_CREDENTIAL_DELETE_ENABLED


async def build_delete_review(ctx: CopilotContext, credential_ids: list[str]) -> CredentialDeleteReview | str:
    if not credential_ids:
        return "Select at least one credential id."
    if len(credential_ids) > MAX_CREDENTIALS_PER_CARD:
        return (
            f"{len(credential_ids)} ids were passed and one card holds at most {MAX_CREDENTIALS_PER_CARD}. Nothing "
            f"was deleted. Pass the first {MAX_CREDENTIALS_PER_CARD}, then the rest on another card."
        )
    duplicates = sorted(credential_id for credential_id, count in Counter(credential_ids).items() if count > 1)
    if duplicates:
        return f"These credential ids are listed more than once: {duplicates}. Nothing was deleted."
    found = {
        credential.credential_id: credential
        for credential in await app.DATABASE.credentials.get_credentials_by_ids(
            credential_ids, organization_id=ctx.organization_id
        )
    }
    missing = [credential_id for credential_id in credential_ids if credential_id not in found]
    if missing:
        return f"These ids are not saved credentials in this organization: {missing}. Nothing was deleted."
    return CredentialDeleteReview(
        rows=[
            CredentialDeleteRow(
                credential_id=credential_id,
                name=found[credential_id].name,
                credential_type=found[credential_id].credential_type,
            )
            for credential_id in credential_ids
        ],
        total_credential_count=await app.DATABASE.credentials.count_credentials(ctx.organization_id),
    )


async def delete_saved_credentials(
    ctx: CopilotContext, *, tool_call_id: str, credential_ids: list[str]
) -> dict[str, Any]:
    if not credential_delete_enabled():
        return CredentialDeletionRefusal(error="Deleting credentials from Copilot is turned off.").model_dump()
    review = await build_delete_review(ctx, credential_ids)
    if isinstance(review, str):
        return CredentialDeletionRefusal(error=review).model_dump()
    recorded = await wait_for_interaction(
        ctx,
        QuestionInteraction(
            interaction_id=uuid4().hex,
            turn_id=ctx.turn_id,
            tool_call_id=tool_call_id,
            parts=[],
            credential_delete_review=review,
        ),
    )
    return recorded.tool_result()
