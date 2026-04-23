"""Wrap copilot-v2-generated block intent fields (``navigation_goal``,
``complete_criterion``, ``terminate_criterion``) with the user's original
chat message as "big goal" context, mirroring the TaskV2 pattern that
applies ``MINI_GOAL_TEMPLATE`` at every mini-goal construction site.

Without this wrap:
  - The Skyvern verifier (``complete_verify``) has no user-intent context
    when a navigation block finishes on a confirmation surface.
  - The validation-block prompt (``decisive-criterion-validate.j2``) sees
    only a terse criterion (often a verbatim slice of the user prompt) and
    reads it as a literal string to match.
"""

from __future__ import annotations

from typing import Any

import yaml

from skyvern.constants import MINI_GOAL_TEMPLATE
from skyvern.utils.yaml_loader import safe_load_no_dates

# Block fields whose value expresses the LLM's "mini goal" — what it should
# do or what it should check for. Wrapped in MINI_GOAL_TEMPLATE alongside
# the user's chat message so the downstream LLMs can reason about intent.
# navigation_goal is carried by Task, Action, Navigation, Login, and
# FileDownload blocks; complete_criterion / terminate_criterion by Validation,
# Navigation, and Login blocks.
_WRAPPABLE_FIELDS: tuple[str, ...] = (
    "navigation_goal",
    "complete_criterion",
    "terminate_criterion",
)

# The template's constant prefix — everything before the ``{mini_goal}``
# placeholder. Presence of this prefix in a wrapped field means it was
# wrapped on a prior invocation; used for idempotency so repeated tool
# calls don't stack wrappers. Deriving from the template (rather than a
# hard-coded substring) keeps idempotency intact if the template's wording
# changes.
_WRAPPED_PREFIX = MINI_GOAL_TEMPLATE.partition("{mini_goal}")[0]


def wrap_block_goals(workflow_yaml: str, user_message: str) -> str:
    """Return ``workflow_yaml`` with each block's ``navigation_goal``,
    ``complete_criterion``, and ``terminate_criterion`` wrapped via
    :data:`skyvern.constants.MINI_GOAL_TEMPLATE`.

    Blocks whose fields are missing, empty, or already wrapped are left
    untouched. Recurses into ``ForLoopBlockYAML.loop_blocks``. No-ops when
    ``user_message`` is empty or the YAML is malformed (malformed input is
    surfaced by the downstream ``_process_workflow_yaml`` call, same as
    today).
    """
    if not user_message:
        return workflow_yaml
    # Skip the parse+dump round-trip when the YAML can't contain any wrappable
    # field. False positives (field name appearing inside a value) are harmless:
    # we'd fall through to the full path and mutate nothing.
    if not any(field in workflow_yaml for field in _WRAPPABLE_FIELDS):
        return workflow_yaml
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except yaml.YAMLError:
        return workflow_yaml
    if not isinstance(parsed, dict):
        return workflow_yaml
    definition = parsed.get("workflow_definition")
    if not isinstance(definition, dict):
        return workflow_yaml
    blocks = definition.get("blocks")
    if not isinstance(blocks, list):
        return workflow_yaml
    if not _wrap_blocks_in_place(blocks, user_message):
        return workflow_yaml
    # parse/mutate/dump: any YAML comments in workflow_yaml are stripped on re-serialize.
    return yaml.safe_dump(parsed, sort_keys=False)


def _wrap_blocks_in_place(blocks: list[Any], user_message: str) -> bool:
    """Recursively wrap every field in :data:`_WRAPPABLE_FIELDS` on every
    block in ``blocks``; returns ``True`` if at least one field was mutated."""
    mutated = False
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for field_name in _WRAPPABLE_FIELDS:
            value = block.get(field_name)
            if isinstance(value, str) and value and _WRAPPED_PREFIX not in value:
                block[field_name] = MINI_GOAL_TEMPLATE.format(
                    mini_goal=value,
                    main_goal=user_message,
                )
                mutated = True
        loop_blocks = block.get("loop_blocks")
        if isinstance(loop_blocks, list):
            mutated = _wrap_blocks_in_place(loop_blocks, user_message) or mutated
    return mutated
