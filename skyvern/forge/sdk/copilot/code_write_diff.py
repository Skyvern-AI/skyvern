"""Per-write line diffs for the code blocks one copilot write changed."""

from __future__ import annotations

import difflib
from collections.abc import Callable, Mapping
from typing import Any

from typing_extensions import NotRequired, TypedDict


class CodeWriteDiff(TypedDict):
    label: str
    added: int
    removed: int
    patch: NotRequired[str]
    patchDropped: NotRequired[bool]


# The tools that can produce a code-block delta and therefore stash a diff. ``delete_block``
# also routes through ``_update_workflow`` but only removes blocks, so it never stashes one;
# a result outside this set leaves the stash for the write it belongs to.
CODE_WRITE_TOOL_NAMES = frozenset(
    {
        "update_workflow",
        "update_and_run_blocks",
        "edit_block",
        "edit_block_and_run",
        "add_block",
    }
)

# Fixed char caps rather than per-org tuning; revisit only if persisted narrative_payload size bites.
PER_PATCH_CHAR_CAP = 8_000
TURN_PATCH_CHAR_BUDGET = 24_000


def build_code_write_diffs(
    prior_by_label: Mapping[str, Mapping[str, Any]],
    changed: Mapping[str, str],
    *,
    scrub: Callable[[str], str],
    budget: int,
) -> tuple[list[CodeWriteDiff], int]:
    """Return one diff per changed code block plus the patch budget left for later writes; counts are
    the line delta of the redacted text the patch shows and never depend on the budget, so dropping an
    oversized patch cannot make a row understate what was written."""
    diffs: list[CodeWriteDiff] = []
    for label in sorted(changed):
        prior_block = prior_by_label.get(label)
        prior_code = prior_block.get("code") if prior_block is not None else None
        if not isinstance(prior_code, str):
            prior_code = ""
        new_code = changed[label]
        if prior_code == new_code:
            continue
        # Redact before diffing, never after: ``scrub`` matches a whole registered value by exact
        # substring, and ``unified_diff`` prefixes every line, so a value spanning lines is no longer
        # contiguous in the diff body and survives. Counts then describe the redacted text, which is
        # what both the row and the patch show.
        prior_code = scrub(prior_code)
        new_code = scrub(new_code)
        # Drop difflib's two ``---``/``+++`` file headers: they name no file here and the
        # renderer colours by leading ``+``/``-``, so emitting them paints a phantom added and
        # removed line above every hunk. Slicing rather than filtering by prefix also keeps a
        # removed line whose own text starts with ``--``.
        body = list(difflib.unified_diff(prior_code.splitlines(), new_code.splitlines(), lineterm="", n=3))[2:]
        added = sum(1 for line in body if line.startswith("+"))
        removed = sum(1 for line in body if line.startswith("-"))
        if added == 0 and removed == 0:
            continue
        diff: CodeWriteDiff = {"label": label, "added": added, "removed": removed}
        patch = "\n".join(body) if body else None
        if patch is None or len(patch) > PER_PATCH_CHAR_CAP or len(patch) > budget:
            diff["patchDropped"] = True
        else:
            diff["patch"] = patch
            budget -= len(patch)
        diffs.append(diff)
    return diffs, budget
