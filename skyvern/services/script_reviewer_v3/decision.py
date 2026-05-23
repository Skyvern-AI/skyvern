"""Terminal-decision types for the v3 agent loops.

Both mid-run and post-run agents signal the end of their loop by calling a
terminal skill. The skill's handler produces a :class:`Decision`. The agent
loop caller inspects the decision type to know how to update the episode and
whether to fall through to agent fallback.

Design note: the set of terminal decisions is small and closed. Adding new
types requires updating callers AND the skill catalog AND the dashboard.
Extend deliberately.
"""

from __future__ import annotations

import dataclasses
from typing import Literal

# Mid-run terminals.
#   - declare_success: v3 applied a fix in-flight via a successful live_try_*
#     call. Class A. Episode marked reviewed=True.
#   - give_up / budget_exhausted / loop_error: Class B. Episode stays
#     reviewed=False. Hook falls through to agent fallback. Post-run v3 picks
#     the episode up later for retrospective analysis.
MidRunDecisionType = Literal[
    "declare_success",
    "give_up",
    "budget_exhausted",
    "loop_error",
]

# Post-run per-episode terminals.
#   - declare_review_complete: episode done, reviewed=True.
#   - give_up_episode: post-run couldn't analyze THIS episode; reviewed stays
#     False; future post-run passes may revisit.
#   - demote_class_a: mid-run Class A episode flagged as a likely false
#     positive. reviewer_version is set to 'v3-post-run-demoted'. reviewed
#     stays True (the invariant that only mark_episode_reviewed toggles
#     reviewed is preserved). Analytics reads the tag; no re-review trigger.
PostRunEpisodeDecisionType = Literal[
    "declare_review_complete",
    "give_up_episode",
    "demote_class_a",
]

# Post-run global terminals — end the whole post-run pass.
#   - declare_post_run_complete: agent finished the whole review.
#   - abandon_post_run: agent gave up on the whole pass (e.g., context-loading
#     failure). Any episodes not individually addressed stay reviewed=False.
#   - budget_exhausted / loop_error: hit per-review cap or an exception;
#     equivalent to abandon_post_run semantically.
PostRunGlobalDecisionType = Literal[
    "declare_post_run_complete",
    "abandon_post_run",
    "budget_exhausted",
    "loop_error",
]

DecisionType = Literal[
    "declare_success",
    "give_up",
    "budget_exhausted",
    "loop_error",
    "declare_review_complete",
    "give_up_episode",
    "demote_class_a",
    "declare_post_run_complete",
    "abandon_post_run",
]


@dataclasses.dataclass(frozen=True)
class Decision:
    """Terminal decision emitted by one of the v3 agent loops.

    Not every field applies to every decision type — consumers switch on
    ``type`` and inspect the relevant subset. See
    ``docs/plans/code-v3-agentic-reviewer/architecture.md`` ("Data Structures")
    for the per-type contract.
    """

    type: DecisionType
    reason: str

    # Set when the decision refers to a specific episode (post-run per-episode
    # terminals). None for mid-run terminals and post-run global terminals.
    episode_id: str | None = None

    # Free-form 2-3 sentence summary of what the agent investigated. Surfaced
    # in internal dashboards for human spot-check.
    investigation_summary: str | None = None

    # Describes the in-flight fix (for declare_success) or the would-be fix
    # (for other terminals). Schema intentionally loose — agent-authored JSON.
    applied_fix_description: dict | None = None

    # Populated when a persist_block_edit or persist_script_rewrite skill ran
    # during the review and produced a new script version.
    new_script_revision_id: str | None = None

    @classmethod
    def declare_success(
        cls,
        reason: str,
        investigation_summary: str | None = None,
        applied_fix_description: dict | None = None,
        new_script_revision_id: str | None = None,
    ) -> Decision:
        return cls(
            type="declare_success",
            reason=reason,
            investigation_summary=investigation_summary,
            applied_fix_description=applied_fix_description,
            new_script_revision_id=new_script_revision_id,
        )

    @classmethod
    def give_up(cls, reason: str) -> Decision:
        return cls(type="give_up", reason=reason)

    @classmethod
    def budget_exhausted(cls, reason: str) -> Decision:
        return cls(type="budget_exhausted", reason=reason)

    @classmethod
    def loop_error(cls, reason: str) -> Decision:
        return cls(type="loop_error", reason=reason)

    @classmethod
    def declare_review_complete(
        cls,
        episode_id: str,
        investigation_summary: str | None = None,
        new_script_revision_id: str | None = None,
    ) -> Decision:
        return cls(
            type="declare_review_complete",
            reason="review_complete",
            episode_id=episode_id,
            investigation_summary=investigation_summary,
            new_script_revision_id=new_script_revision_id,
        )

    @classmethod
    def give_up_episode(cls, episode_id: str, reason: str) -> Decision:
        return cls(type="give_up_episode", reason=reason, episode_id=episode_id)

    @classmethod
    def demote_class_a(cls, episode_id: str, reason: str) -> Decision:
        return cls(type="demote_class_a", reason=reason, episode_id=episode_id)

    @classmethod
    def declare_post_run_complete(cls, reason: str, investigation_summary: str | None = None) -> Decision:
        return cls(
            type="declare_post_run_complete",
            reason=reason,
            investigation_summary=investigation_summary,
        )

    @classmethod
    def abandon_post_run(cls, reason: str) -> Decision:
        return cls(type="abandon_post_run", reason=reason)

    # Classification helpers used by agent-loop callers.
    def is_midrun_class_a(self) -> bool:
        """Class A = the mid-run review completed with an in-flight fix.
        Episode gets reviewed=True."""
        return self.type == "declare_success"

    def is_midrun_class_b(self) -> bool:
        """Class B = the mid-run review didn't complete. Hook falls through
        to agent fallback; post-run v3 picks up the episode later."""
        return self.type in ("give_up", "budget_exhausted", "loop_error")

    def is_post_run_per_episode(self) -> bool:
        return self.type in ("declare_review_complete", "give_up_episode", "demote_class_a")

    def is_post_run_global(self) -> bool:
        return self.type in (
            "declare_post_run_complete",
            "abandon_post_run",
            "budget_exhausted",
            "loop_error",
        )
