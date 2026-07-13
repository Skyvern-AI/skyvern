from __future__ import annotations

from skyvern.forge.sdk.workflow.models.block import BaseTaskBlock, Block, TaskV2Block
from skyvern.schemas.workflows import BlockType


def _task_output_envelope_block_types() -> frozenset[str]:
    # Almost every envelope block extends BaseTaskBlock; TaskV2Block subclasses Block
    # directly but emits the same TaskOutput envelope, so seed the walk with both roots.
    block_types: set[str] = set()
    pending: list[type[Block]] = [BaseTaskBlock, TaskV2Block]
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        field = cls.model_fields.get("block_type")
        default = field.default if field is not None else None
        if isinstance(default, BlockType):
            block_types.add(default.value.upper())
    return frozenset(block_types)


# Block types whose ``block.output`` is a ``TaskOutput.from_task()`` envelope rather than the raw
# payload, so meaningful-data and floor-rekeyed backing checks slice/project only
# ``_TASK_OUTPUT_PAYLOAD_FIELDS`` and always-populated metadata (task_id, status) reads no signal.
# Derived from the block subclass tree so a newly added task-backed block can't fall out of sync.
_TASK_ENVELOPE_BLOCK_TYPES: frozenset[str] = _task_output_envelope_block_types()


# Payload fields inside a ``TaskOutput.from_task()`` envelope carrying real produced content; the
# rest is always-populated metadata.
_TASK_OUTPUT_PAYLOAD_FIELDS: tuple[str, ...] = (
    "extracted_information",
    "downloaded_files",
    "downloaded_file_urls",
)
