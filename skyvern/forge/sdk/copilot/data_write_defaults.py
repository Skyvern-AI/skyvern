from __future__ import annotations

from typing import Any

import yaml

from skyvern.forge.sdk.copilot.workflow_credential_utils import parse_workflow_yaml, workflow_blocks
from skyvern.schemas.workflows import BlockType

# Block types that persist a payload to an external store, where a swallowed
# failure reads back as a completed run with nothing written.
DATA_WRITE_BLOCK_TYPES: frozenset[str] = frozenset(
    {
        BlockType.GOOGLE_SHEETS_WRITE.value,
        BlockType.FILE_UPLOAD.value,
        BlockType.UPLOAD_TO_S3.value,
        BlockType.DOWNLOAD_TO_S3.value,
    }
)


def _block_type_name(block: dict[str, Any]) -> str:
    return str(block.get("block_type") or "").strip().lower()


def _data_write_labels_with_truthy_continue_on_failure(parsed: Any) -> set[str]:
    if not isinstance(parsed, dict):
        return set()
    return {
        label
        for block in workflow_blocks(parsed)
        if _block_type_name(block) in DATA_WRITE_BLOCK_TYPES
        and block.get("continue_on_failure")
        and isinstance((label := block.get("label")), str)
    }


def default_data_write_continue_on_failure(new_yaml: str, prior_yaml: str | None) -> str:
    """Force continue_on_failure=false on data-write blocks unless the prior workflow
    already set it truthy on a block with the same label. Keying on the prior flag value
    (not just the label) means a block first added earlier in the same turn, already
    defaulted to false in the staged draft, is re-defaulted rather than treated as a
    pre-existing explicit value. Pure; recurses into loop_blocks/branches and returns the
    input unchanged when nothing needs overriding."""
    parsed = parse_workflow_yaml(new_yaml)
    if not isinstance(parsed, dict):
        return new_yaml
    prior_truthy_labels = _data_write_labels_with_truthy_continue_on_failure(
        parse_workflow_yaml(prior_yaml) if prior_yaml else None
    )
    changed = False
    for block in workflow_blocks(parsed):
        if _block_type_name(block) not in DATA_WRITE_BLOCK_TYPES:
            continue
        if not block.get("continue_on_failure"):
            continue
        label = block.get("label")
        if isinstance(label, str) and label in prior_truthy_labels:
            continue
        block["continue_on_failure"] = False
        changed = True
    return yaml.safe_dump(parsed, sort_keys=False) if changed else new_yaml
