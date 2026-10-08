from typing import Any

from skyvern.schemas.workflows import BlockType

SCRIPT_TASK_BLOCKS = {
    "task",
    "file_download",
    "navigation",
    "action",
    "extraction",
    "login",
}
SCRIPT_TASK_BLOCKS_WITH_COMPLETE_ACTION = {
    "task",
    "navigation",
    "login",
}


# Loop children that must always run through the engine: codegen emits them as a
# no-op comment, so a cached loop would silently skip them.
_ENGINE_ONLY_LOOP_CHILD_TYPES = {
    BlockType.WEB_SEARCH,
    BlockType.TERMINATE,
    BlockType.CONDITIONAL,
    BlockType.HUMAN_INTERACTION,
    BlockType.DATA_EXPORT,
    BlockType.DOWNLOAD_TO_S3,
    BlockType.UPLOAD_TO_S3,
    BlockType.EMAIL_INBOX,
    BlockType.GOOGLE_SHEETS_READ,
    BlockType.GOOGLE_SHEETS_WRITE,
    BlockType.PDF_FILL,
    BlockType.PRINT_PAGE,
    BlockType.SPLIT_PDF,
}


def engine_only_loop_child_types(block: Any) -> set[BlockType]:
    """Engine-only block types nested at any depth inside a loop; empty for any other block."""
    block_type = block.get("block_type") if isinstance(block, dict) else getattr(block, "block_type", None)
    if block_type not in {BlockType.FOR_LOOP, BlockType.WHILE_LOOP}:
        return set()
    found: set[BlockType] = set()
    for child in block.get("loop_blocks", []) if isinstance(block, dict) else block.loop_blocks:
        child_type = child.get("block_type") if isinstance(child, dict) else getattr(child, "block_type", None)
        if child_type in _ENGINE_ONLY_LOOP_CHILD_TYPES:
            found.add(BlockType(child_type))
        found |= engine_only_loop_child_types(child)
    return found
