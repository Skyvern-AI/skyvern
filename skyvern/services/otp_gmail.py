from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

MAX_SEEN_GMAIL_MESSAGE_IDS = 500


@dataclass
class GmailOTPVerificationContext:
    """Per-poll Gmail OTP lookup cache passed through the AgentFunction hook."""

    credential_ids: list[str] | None = None
    credential_ids_loaded_at: datetime | None = None
    last_searched_at_by_credential: dict[str, datetime] = field(default_factory=dict)
    seen_message_ids: set[str] = field(default_factory=set)
    seen_message_id_order: deque[str] = field(default_factory=deque)

    def has_seen_message_id(self, message_id: str) -> bool:
        return message_id in self.seen_message_ids

    def remember_message_id(self, message_id: str) -> None:
        if message_id in self.seen_message_ids:
            return
        self.seen_message_ids.add(message_id)
        self.seen_message_id_order.append(message_id)
        while len(self.seen_message_id_order) > MAX_SEEN_GMAIL_MESSAGE_IDS:
            self.seen_message_ids.discard(self.seen_message_id_order.popleft())
