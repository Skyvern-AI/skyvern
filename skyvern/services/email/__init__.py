from skyvern.services.email import gmail, outlook
from skyvern.services.email.gmail_client import GmailAPIError
from skyvern.services.email.inbox import EmailMatchResult, list_folder_messages, match_email
from skyvern.services.email.outlook import OutlookAPIError
from skyvern.services.email.types import EmailAttachment, EmailMessage

__all__ = [
    "EmailAttachment",
    "EmailMatchResult",
    "EmailMessage",
    "GmailAPIError",
    "OutlookAPIError",
    "gmail",
    "list_folder_messages",
    "match_email",
    "outlook",
]
