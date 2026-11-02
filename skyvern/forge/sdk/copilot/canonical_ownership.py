"""Ownership of a copilot turn's canonical workflow write.

An interrupted turn may only roll back a canonical write it still owns. Ownership is
recorded, not inferred: the writer stamps the fingerprint of what it wrote onto the turn's
pending marker, and reconcile rolls back only while canonical still carries that content.
A later write by anyone — another chat, a manual edit, a subsequent turn — changes the
fingerprint and forfeits the rollback.
"""

import hashlib
import json
from typing import Any

# A write always moves these, so they carry no information about who wrote it. ``title`` is here
# because background naming renames the row out of band, which is not a content write.
VOLATILE_WORKFLOW_FIELDS = frozenset({"workflow_id", "version", "created_at", "modified_at", "title"})
# Records which surface wrote the row, not what it holds. Hashing it would also make every fingerprint stored
# before the column existed stop matching an unchanged workflow.
ATTRIBUTION_ONLY_WORKFLOW_FIELDS = frozenset({"build_source"})


def workflow_content(workflow: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in workflow.items()
        if key not in VOLATILE_WORKFLOW_FIELDS and key not in ATTRIBUTION_ONLY_WORKFLOW_FIELDS
    }


def workflow_content_fingerprint(workflow: dict[str, Any]) -> str:
    canonical = json.dumps(workflow_content(workflow), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()
