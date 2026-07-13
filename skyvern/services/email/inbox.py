import httpx
import structlog
from pydantic import BaseModel, ValidationError

from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.services.email import gmail, outlook
from skyvern.services.email.types import EmailMessage

LOG = structlog.get_logger()

_BODY_LIMIT = 4000


class EmailMatchResult(BaseModel):
    reasoning: str
    matches: bool


async def list_folder_messages(
    *,
    email_client: str,
    access_token: str,
    folder: str,
    sender: str | None,
    subject: str | None,
    newer_than_days: int | None,
    max_results: int,
    include_body: bool,
    client: httpx.AsyncClient | None = None,
) -> list[EmailMessage]:
    if email_client == "gmail":
        return await gmail.list_folder_messages(
            access_token=access_token,
            label=folder,
            sender=sender,
            subject=subject,
            newer_than_days=newer_than_days,
            max_results=max_results,
            include_body=include_body,
            client=client,
        )
    if email_client == "outlook":
        return await outlook.list_folder_messages(
            access_token=access_token,
            folder=folder,
            sender=sender,
            subject=subject,
            newer_than_days=newer_than_days,
            max_results=max_results,
            include_body=include_body,
            client=client,
        )
    raise ValueError(f"Unsupported email_client: {email_client}")


async def match_email(*, criteria: str, email: EmailMessage, organization_id: str) -> bool:
    body = email.body_text or email.body_html or ""
    prompt = prompt_engine.load_prompt(
        "match-email",
        criteria=criteria,
        subject=email.subject,
        sender=email.from_email,
        snippet=email.snippet,
        body=body[:_BODY_LIMIT],
    )
    resp = await app.SECONDARY_LLM_API_HANDLER(
        prompt=prompt,
        prompt_name="match-email",
        organization_id=organization_id,
    )
    if not isinstance(resp, dict):
        LOG.warning(
            "Failed to match email with LLM",
            email_id=email.id,
            organization_id=organization_id,
            response_type=type(resp).__name__,
        )
        return False
    try:
        result = EmailMatchResult.model_validate(resp)
    except ValidationError:
        LOG.warning(
            "Failed to match email with LLM",
            email_id=email.id,
            organization_id=organization_id,
            exc_info=True,
        )
        return False
    return result.matches
