from __future__ import annotations

from enum import StrEnum

SLIM_LLM_OUTPUT_PROMPTS_FLAG = "SLIM_LLM_OUTPUT_PROMPTS"

SLIM_VARIANT_SAFE = "slim_safe"
SLIM_VARIANT_TERSE = "slim_terse"
SLIM_VARIANTS = frozenset({SLIM_VARIANT_SAFE, SLIM_VARIANT_TERSE})


class PromptFamily(StrEnum):
    EXTRACT_ACTIONS = "extract-actions"
    CHECK_USER_GOAL = "check-user-goal"
    AUTO_COMPLETION_CHOOSE_OPTION = "auto-completion-choose-option"
    PARSE_INPUT_OR_SELECT_CONTEXT = "parse-input-or-select-context"


# Template names and prompt_names are different namespaces (e.g. template
# "extract-action" logs as prompt_name "extract-actions"). Both the render-time
# helper and the metrics emitter resolve through these maps so they cannot diverge.
# extract-action-dynamic is mapped for completeness but carries no output schema;
# the slim conditionals live in extract-action(-static), and the split render is
# resolved once at the base-template call site.
TEMPLATES_BY_FAMILY: dict[PromptFamily, frozenset[str]] = {
    PromptFamily.EXTRACT_ACTIONS: frozenset({"extract-action", "extract-action-static", "extract-action-dynamic"}),
    PromptFamily.CHECK_USER_GOAL: frozenset({"check-user-goal", "check-user-goal-with-termination"}),
    PromptFamily.AUTO_COMPLETION_CHOOSE_OPTION: frozenset({"auto-completion-choose-option"}),
    PromptFamily.PARSE_INPUT_OR_SELECT_CONTEXT: frozenset({"parse-input-or-select-context"}),
}

PROMPT_NAMES_BY_FAMILY: dict[PromptFamily, frozenset[str]] = {
    PromptFamily.EXTRACT_ACTIONS: frozenset({"extract-actions"}),
    PromptFamily.CHECK_USER_GOAL: frozenset(
        {"check-user-goal", "check-user-goal-after-click", "check-user-goal-with-termination"}
    ),
    PromptFamily.AUTO_COMPLETION_CHOOSE_OPTION: frozenset({"auto-completion-choose-option"}),
    PromptFamily.PARSE_INPUT_OR_SELECT_CONTEXT: frozenset({"parse-input-or-select-context"}),
}

# Families whose templates honor the slim variants at runtime. A family is added
# only after passing its offline backtest gate (SKY-10075).
SLIM_ENABLED_FAMILIES: frozenset[PromptFamily] = frozenset({PromptFamily.EXTRACT_ACTIONS, PromptFamily.CHECK_USER_GOAL})

_FAMILY_BY_TEMPLATE: dict[str, PromptFamily] = {
    template: family for family, templates in TEMPLATES_BY_FAMILY.items() for template in templates
}
_FAMILY_BY_PROMPT_NAME: dict[str, PromptFamily] = {
    prompt_name: family for family, prompt_names in PROMPT_NAMES_BY_FAMILY.items() for prompt_name in prompt_names
}


def family_for_template(template_name: str | None) -> PromptFamily | None:
    if not template_name:
        return None
    return _FAMILY_BY_TEMPLATE.get(template_name)


def family_for_prompt_name(prompt_name: str | None) -> PromptFamily | None:
    if not prompt_name:
        return None
    return _FAMILY_BY_PROMPT_NAME.get(prompt_name)


def slim_variant_for_family(assigned: str | None, family: PromptFamily | None) -> str | None:
    """Single gating rule shared by rendering and telemetry: the assigned variant
    applies only to known, slim-enabled families; everything else is control."""
    if assigned is None or assigned not in SLIM_VARIANTS:
        return None
    if family is None or family not in SLIM_ENABLED_FAMILIES:
        return None
    return assigned


def effective_prompt_schema_variant(assigned: str | None, prompt_name: str | None) -> str | None:
    """Per-call cohort label: the run's assigned variant if this prompt's family renders
    slim, else None. Unknown prompt_names fail safe to None (control).

    Logged alongside the raw run-level assignment: a row with (assigned="slim_*",
    effective=None) means the run is in a treatment cohort but this prompt family
    rendered the control schema. Run-level analysis keys on assigned (intent-to-treat);
    per-prompt token/cost deltas key on effective."""
    return slim_variant_for_family(assigned, family_for_prompt_name(prompt_name))
