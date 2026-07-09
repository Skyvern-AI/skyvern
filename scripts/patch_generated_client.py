#!/usr/bin/env python3
"""Reapply manual patches to the Fern-generated Python client.

Run automatically by scripts/fern_build_python_sdk.sh after every regen.
Idempotent: running it twice leaves the files unchanged.

Patches:
1. Loop-block circular imports — Fern v4.31.1 emits bottom-cross-imports that
   deadlock at module load (for_loop <-> while_loop <-> *_loop_blocks_item).
   Wrap them in try/except ImportError; the symmetric *_loop_blocks_item module
   back-resolves once both unions are fully defined.
2. update_forward_refs — Pydantic v2 can raise internal schema-gathering
   KeyErrors for Fern-generated recursive unions even with raise_errors=False.
   Suppress KeyErrors that mention the "definitions" key.
"""

from __future__ import annotations

import sys
from pathlib import Path

CLIENT = Path(__file__).resolve().parent.parent / "skyvern" / "client"

LOOP_BLOCK_PATCHES = {
    "types/for_loop_block.py": (
        """from .while_loop_block import WhileLoopBlock  # noqa: E402, F401, I001
from .for_loop_block_loop_blocks_item import ForLoopBlockLoopBlocksItem  # noqa: E402, F401, I001""",
        """
# Manual patch: Fern v4.31.1 emits bottom-cross-imports that deadlock at module
# load (for_loop <-> while_loop <-> *_loop_blocks_item). Catch the mid-load
# ImportError; the symmetric *_loop_blocks_item module back-resolves once both
# unions are fully defined. Reapplied on every regen by scripts/patch_generated_client.py.
try:
    from .while_loop_block import WhileLoopBlock  # noqa: E402, F401, I001
    from .for_loop_block_loop_blocks_item import ForLoopBlockLoopBlocksItem  # noqa: E402, F401, I001
except ImportError:
    pass""",
    ),
    "types/while_loop_block.py": (
        """from .for_loop_block import ForLoopBlock  # noqa: E402, F401, I001
from .while_loop_block_loop_blocks_item import WhileLoopBlockLoopBlocksItem  # noqa: E402, F401, I001""",
        """
# Manual patch: Fern v4.31.1 emits bottom-cross-imports that deadlock at module
# load. See for_loop_block.py for the explanation.
try:
    from .for_loop_block import ForLoopBlock  # noqa: E402, F401, I001
    from .while_loop_block_loop_blocks_item import WhileLoopBlockLoopBlocksItem  # noqa: E402, F401, I001
except ImportError:
    pass""",
    ),
    "types/for_loop_block_yaml.py": (
        """from .while_loop_block_yaml import WhileLoopBlockYaml  # noqa: E402, F401, I001
from .for_loop_block_yaml_loop_blocks_item import ForLoopBlockYamlLoopBlocksItem  # noqa: E402, F401, I001""",
        """
# Manual patch: Fern v4.31.1 emits bottom-cross-imports that deadlock at module
# load. See for_loop_block.py for the explanation.
try:
    from .while_loop_block_yaml import WhileLoopBlockYaml  # noqa: E402, F401, I001
    from .for_loop_block_yaml_loop_blocks_item import ForLoopBlockYamlLoopBlocksItem  # noqa: E402, F401, I001
except ImportError:
    pass""",
    ),
    "types/while_loop_block_yaml.py": (
        """from .for_loop_block_yaml import ForLoopBlockYaml  # noqa: E402, F401, I001
from .while_loop_block_yaml_loop_blocks_item import WhileLoopBlockYamlLoopBlocksItem  # noqa: E402, F401, I001""",
        """
# Manual patch: Fern v4.31.1 emits bottom-cross-imports that deadlock at module
# load. See for_loop_block.py for the explanation.
try:
    from .for_loop_block_yaml import ForLoopBlockYaml  # noqa: E402, F401, I001
    from .while_loop_block_yaml_loop_blocks_item import WhileLoopBlockYamlLoopBlocksItem  # noqa: E402, F401, I001
except ImportError:
    pass""",
    ),
}

FORWARD_REFS_OLD = """def update_forward_refs(model: Type["Model"], **localns: Any) -> None:
    if IS_PYDANTIC_V2:
        model.model_rebuild(raise_errors=False)  # type: ignore[attr-defined]
    else:
        model.update_forward_refs(**localns)"""

FORWARD_REFS_NEW = """def update_forward_refs(model: Type["Model"], **localns: Any) -> None:
    if IS_PYDANTIC_V2:
        try:
            model.model_rebuild(raise_errors=False)  # type: ignore[attr-defined]
        except KeyError as exc:
            # Manual patch (reapplied by scripts/patch_generated_client.py):
            # Pydantic v2 can still raise internal schema-gathering KeyErrors
            # for Fern-generated recursive unions even with raise_errors=False.
            # Match on the "definitions" key rather than the exact args tuple so a
            # Pydantic format change that adds context can't reintroduce the crash.
            if "definitions" not in exc.args:
                raise
    else:
        model.update_forward_refs(**localns)"""


def patch_file(rel_path: str, old: str, new: str) -> str:
    path = CLIENT / rel_path
    text = path.read_text()
    if new in text:
        return "already patched"
    if old not in text:
        return "PATTERN NOT FOUND"
    path.write_text(text.replace(old, new, 1))
    return "patched"


def main() -> int:
    failed = False
    for rel_path, (old, new) in LOOP_BLOCK_PATCHES.items():
        result = patch_file(rel_path, old, new)
        print(f"{rel_path}: {result}")
        failed |= result == "PATTERN NOT FOUND"

    result = patch_file("core/pydantic_utilities.py", FORWARD_REFS_OLD, FORWARD_REFS_NEW)
    print(f"core/pydantic_utilities.py: {result}")
    failed |= result == "PATTERN NOT FOUND"

    if failed:
        print(
            "ERROR: some patch patterns no longer match the generated code. "
            "The Fern generator output changed shape - update this script.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
