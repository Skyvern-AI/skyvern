from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TagSource(StrEnum):
    """Provenance source for a tag-event row."""

    MANUAL = "manual"  # set by a person through the UI or API
    BULK_APPLY = "bulk_apply"  # set as part of a multi-workflow bulk operation
    BACKFILL = "backfill"  # written by a one-off migration/script over existing rows
    INHERITED = "inherited"  # copied from a parent (e.g. folder) rather than set directly
    IMPORT = "import"  # ingested from an external system
    SYSTEM = "system"  # written by Skyvern-owned automation, not a public caller


class TagEventType(StrEnum):
    """Kind of state change recorded in the event log.

    DELETE events have value=NULL and carry their own attribution
    (set_by / set_at / source).
    """

    SET = "set"
    DELETE = "delete"


class CallerType(StrEnum):
    USER = "user"
    API_KEY = "api_key"
    SYSTEM = "system"


@dataclass(frozen=True)
class TagWriteContext:
    """Attribution persisted on each tag event row.

    ``caller_type`` and ``set_at`` are nullable for backfill scripts.
    """

    caller_id: str
    source: TagSource
    caller_type: CallerType | None = None
    set_at: datetime | None = None
