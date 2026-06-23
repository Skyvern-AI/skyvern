from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class GmailOTPVerificationContext:
    """Per-poll Gmail OTP lookup cache passed through the AgentFunction hook."""

    credential_ids: list[str] | None = None
    credential_ids_loaded_at: datetime | None = None
    last_searched_at_by_credential: dict[str, datetime] = field(default_factory=dict)
    seen_message_ids: set[str] = field(default_factory=set)
