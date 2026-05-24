"""Skill catalog for the v3 agentic reviewer.

Each module under this package exposes one family of skills. Families:

- ``interact``  — live Playwright operations (mid-run only)
- ``investigate`` — DB + repo queries (both)
- ``investigate_artifacts`` — run artifacts (post-run only; cloud-backed)
- ``validate`` — block- and script-level validators (both)
- ``persist`` — persist edits as new script versions (both)
- ``terminal`` — agent-loop terminal markers (both, role-tuned)

See ``docs/plans/code-v3-agentic-reviewer/architecture.md`` for the per-skill
contract. The :class:`Skill` protocol below is the executor-facing interface.
"""

from skyvern.services.script_reviewer_v3.skills.base import (
    Skill,
    SkillError,
    SkillRegistry,
    SkillResult,
)

__all__ = ["Skill", "SkillError", "SkillRegistry", "SkillResult"]
