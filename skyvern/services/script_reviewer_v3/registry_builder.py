"""Construct the SkillRegistry once per agent invocation.

Kept separate from the agent entry points so tests can build a registry with
mocked skills without touching the entry points.
"""

from __future__ import annotations

from skyvern.services.script_reviewer_v3.skills import SkillRegistry
from skyvern.services.script_reviewer_v3.skills.interact import all_interact_skills
from skyvern.services.script_reviewer_v3.skills.investigate import all_investigate_skills
from skyvern.services.script_reviewer_v3.skills.investigate_artifacts import all_artifact_skills
from skyvern.services.script_reviewer_v3.skills.persist import all_persist_skills
from skyvern.services.script_reviewer_v3.skills.terminal import all_terminal_skills
from skyvern.services.script_reviewer_v3.skills.validate import all_validate_skills


def build_registry() -> SkillRegistry:
    """Build the full v3 skill registry.

    Filtering by agent kind (mid-run vs post-run) is handled by
    :meth:`SkillRegistry.for_agent_kind`. Each :class:`Skill` declares its
    ``available_to`` set at construction time.
    """
    registry = SkillRegistry()
    registry.register_many(all_interact_skills())
    registry.register_many(all_investigate_skills())
    registry.register_many(all_artifact_skills())
    registry.register_many(all_persist_skills())
    registry.register_many(all_validate_skills())
    registry.register_many(all_terminal_skills())
    return registry


__all__ = ["build_registry"]
