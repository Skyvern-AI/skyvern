from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from skyvern.forge.sdk.workflow.block_notes import (
    carry_block_notes_from_versions,
    carry_forward_block_notes,
    definition_step_notes,
)
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import Block
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from skyvern.forge.sdk.workflow.service import WorkflowService, _get_workflow_definition_core_data
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.workflows import WorkflowDefinitionYAML
from skyvern.utils.yaml_loader import dump_workflow_yaml, safe_load_no_dates


def _nav(label: str, **extra: Any) -> dict[str, Any]:
    return {"block_type": "navigation", "label": label, "url": "https://example.com", "navigation_goal": label, **extra}


def _definition(blocks: list[dict[str, Any]], **extra: Any) -> WorkflowDefinition:
    yaml_definition = WorkflowDefinitionYAML.model_validate({"parameters": [], "blocks": blocks, **extra})
    return convert_workflow_definition(workflow_definition_yaml=yaml_definition, workflow_id="w_test")


def _run_context(definition: WorkflowDefinition) -> WorkflowRunContext:
    now = datetime.now(UTC)
    workflow = Workflow(
        workflow_id="w_test",
        organization_id="o_test",
        title="test",
        workflow_permanent_id="wpid_test",
        version=1,
        is_saved_task=False,
        workflow_definition=definition,
        created_at=now,
        modified_at=now,
    )
    return WorkflowRunContext(
        workflow_title="test",
        workflow_id="w_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=MagicMock(),
        workflow=workflow,
    )


def _all_blocks(blocks: list[Block]) -> list[Block]:
    result: list[Block] = []
    for block in blocks:
        result.append(block)
        result.extend(_all_blocks(getattr(block, "loop_blocks", [])))
    return result


def _prompts(definition: WorkflowDefinition) -> dict[str, str | None]:
    context = _run_context(definition)
    prompts: dict[str, str | None] = {}
    for block in _all_blocks(definition.blocks):
        block._apply_workflow_system_prompt(context)
        prompts[block.label] = block.workflow_system_prompt
    return prompts


def test_a_note_reaches_only_the_steps_on_either_side_of_it() -> None:
    definition = _definition(
        [_nav("a"), _nav("b", notes="Only invoices from {{ workflow_title }}."), _nav("c", trailing_notes="Done.")],
        workflow_system_prompt="Be careful.",
    )

    prompts = _prompts(definition)

    assert prompts["a"] == "Be careful.\n\nNotes for this step:\nOnly invoices from test."
    assert prompts["b"] == "Be careful.\n\nNotes for this step:\nOnly invoices from test."
    assert prompts["c"] == "Be careful.\n\nNotes for this step:\nDone."


def test_a_finally_block_mid_list_does_not_split_the_steps_around_it() -> None:
    definition = _definition(
        [_nav("a"), _nav("cleanup", notes="Log out."), _nav("b", notes="Use the left menu."), _nav("c", notes="Last.")],
        finally_block_label="cleanup",
    )

    prompts = _prompts(definition)

    assert prompts["a"] == "Notes for this step:\nUse the left menu."
    assert prompts["b"] == "Notes for this step:\nUse the left menu.\n\nLast."
    assert prompts["cleanup"] == "Notes for this step:\nLog out."


def test_opting_out_of_the_workflow_prompt_keeps_the_step_notes() -> None:
    definition = _definition(
        [_nav("a", ignore_workflow_system_prompt=True), _nav("b", notes="Use the billing address.")],
        workflow_system_prompt="Be careful.",
    )

    assert _prompts(definition)["a"] == "Notes for this step:\nUse the billing address."


@pytest.mark.parametrize("note", ["The total was {{ a_output.total }}.", "Pay {{ total if it is over 10."])
def test_a_note_that_cannot_render_saves_and_is_sent_as_written_instead_of_failing_the_step(note: str) -> None:
    definition = _definition([_nav("a"), _nav("b", notes=note)])

    assert _prompts(definition)["a"] == f"Notes for this step:\n{note}"


def test_loop_values_render_in_notes() -> None:
    definition = _definition([_nav("a", notes="Skip it if {{ current_item.status }} is draft.")])
    context = _run_context(definition)
    context.update_block_metadata("a", {"current_item": {"status": "archived"}})

    assert context.resolve_block_notes_section("a") == "Notes for this step:\nSkip it if archived is draft."


def test_explicit_note_deletion_survives_a_dump_and_revalidate_round_trip() -> None:
    submitted = WorkflowDefinitionYAML.model_validate({"parameters": [], "blocks": [_nav("a", notes=None)]})
    round_tripped = WorkflowDefinitionYAML.model_validate(submitted.model_dump(exclude_unset=True))
    previous = _definition([_nav("a", notes="old")])

    assert carry_forward_block_notes(round_tripped.blocks, previous.blocks) == {}


@pytest.mark.parametrize("clear_value", [None, ""])
def test_explicit_note_clear_survives_conversion_and_save(clear_value: str | None) -> None:
    submitted = WorkflowDefinitionYAML.model_validate({"parameters": [], "blocks": [_nav("a", notes=clear_value)]})
    converted = convert_workflow_definition(workflow_definition_yaml=submitted, workflow_id="w_test")
    converted_block = converted.model_dump(mode="json")["blocks"][0]
    saved = _yaml(_nav("a", notes="old"))
    editor = _yaml(converted_block)

    staged, _, _ = carry_block_notes_from_versions(editor, None, saved)

    assert converted_block["notes"] == clear_value
    assert _staged_notes(staged) == {"a": clear_value}


def test_explicit_description_clear_survives_conversion_and_save() -> None:
    submitted = WorkflowDefinitionYAML.model_validate({"parameters": [], "blocks": [_nav("a", description=None)]})
    converted = convert_workflow_definition(workflow_definition_yaml=submitted, workflow_id="w_test")
    converted_block = converted.model_dump(mode="json")["blocks"][0]
    saved = _yaml(_nav("a", description="old"))
    editor = _yaml(converted_block)

    staged, _, _ = carry_block_notes_from_versions(editor, None, saved)

    assert converted_block["description"] is None
    assert safe_load_no_dates(staged)["workflow_definition"]["blocks"][0]["description"] is None


def test_workflows_without_notes_are_unchanged() -> None:
    definition = _definition([_nav("a"), _nav("b")], workflow_system_prompt="Be careful.")
    equivalent = _definition([_nav("a"), _nav("b")], workflow_system_prompt="Be careful.")

    assert _prompts(definition) == {"a": "Be careful.", "b": "Be careful."}
    dumped = definition.model_dump(mode="json")["blocks"][0]
    assert not {"notes", "trailing_notes", "description"} & dumped.keys()
    before = _get_workflow_definition_core_data(definition)
    after = _get_workflow_definition_core_data(equivalent)
    assert before == after
    assert (
        WorkflowService._determine_cache_invalidation(before["blocks"], after["blocks"]).block_labels_to_disable == []
    )


def test_implicit_branch_merge_exposes_successor_notes_to_terminal_step() -> None:
    definition = _definition(
        [
            {
                "block_type": "conditional",
                "label": "check",
                "next_block_label": "after",
                "branch_conditions": [{"is_default": True, "next_block_label": "terminal"}],
            },
            _nav("terminal"),
            _nav("after", notes="Check the merged result."),
        ],
        workflow_system_prompt="Be careful.",
    )

    assert _prompts(definition)["terminal"] == ("Be careful.\n\nNotes for this step:\nCheck the merged result.")


def test_finally_edges_are_stripped_before_resolving_branch_merge_notes() -> None:
    definition = _definition(
        [
            {
                "block_type": "conditional",
                "label": "check",
                "next_block_label": "after",
                "branch_conditions": [
                    {
                        "criteria": {"criteria_type": "prompt", "expression": "is it cleanup?"},
                        "next_block_label": "cleanup",
                    },
                    {"is_default": True, "next_block_label": "entry"},
                ],
            },
            _nav("entry", next_block_label="terminal"),
            _nav("terminal", notes="Continue with the merged result.", next_block_label="cleanup"),
            _nav("after", notes="Check the merged result."),
            _nav("cleanup", notes="Always log out."),
        ],
        finally_block_label="cleanup",
    )

    prompts = _prompts(definition)

    assert prompts["terminal"] == ("Notes for this step:\nContinue with the merged result.\n\nCheck the merged result.")
    assert prompts["entry"] == "Notes for this step:\nContinue with the merged result."
    assert prompts["cleanup"] == "Notes for this step:\nAlways log out."


def test_definition_mapping_uses_the_runtime_resolved_explicit_branch_successor() -> None:
    definition = {
        "blocks": [
            {
                "block_type": "conditional",
                "label": "check",
                "next_block_label": "after",
                "branch_conditions": [{"is_default": True, "next_block_label": "terminal"}],
            },
            _nav("terminal", next_block_label="after"),
            _nav("after", notes="Check the merged result."),
        ]
    }

    assert definition_step_notes(definition)["terminal"] == "Check the merged result."


def test_branches_and_loop_bodies_are_their_own_sequences() -> None:
    definition = _definition(
        [
            {
                "block_type": "conditional",
                "label": "check",
                "next_block_label": "after",
                "branch_conditions": [
                    {"criteria": {"criteria_type": "prompt", "expression": "is it late?"}, "next_block_label": "late"},
                    {"is_default": True, "next_block_label": "on_time"},
                ],
            },
            _nav("late", next_block_label="after"),
            _nav("on_time", notes="Start of the on-time branch.", next_block_label="after"),
            _nav("after", notes="After the branches.", next_block_label="loop"),
            {
                "block_type": "for_loop",
                "label": "loop",
                "loop_variable_reference": "{{ after_output }}",
                "loop_blocks": [_nav("first"), _nav("second", notes="Inside the loop.")],
            },
        ],
        version=2,
    )

    prompts = _prompts(definition)

    assert prompts["late"] == "Notes for this step:\nAfter the branches."
    assert prompts["on_time"] == ("Notes for this step:\nStart of the on-time branch.\n\nAfter the branches.")
    assert prompts["check"] == "Notes for this step:\nAfter the branches."
    assert prompts["after"] == "Notes for this step:\nAfter the branches."
    assert prompts["first"] == "Notes for this step:\nInside the loop."
    assert prompts["loop"] is None


def test_omitted_notes_and_descriptions_are_not_carried_without_a_proven_step_identity() -> None:
    previous = _definition(
        [
            _nav("a", notes="keep me", description="Opens the site"),
            _nav("b", notes="drop me", description="drop me too"),
            _nav("c", trailing_notes="end"),
        ]
    )
    submitted = WorkflowDefinitionYAML.model_validate(
        {
            "parameters": [],
            "blocks": [_nav("a"), _nav("b", notes=None, description=""), _nav("c", trailing_notes="")],
        }
    )

    kept = carry_forward_block_notes(submitted.blocks, previous.blocks)
    saved = convert_workflow_definition(workflow_definition_yaml=submitted, workflow_id="w_test")

    assert kept == {}
    assert [(block.notes, block.trailing_notes, block.description) for block in saved.blocks] == [
        (None, None, None),
        (None, None, ""),
        (None, "", None),
    ]


def test_notes_are_not_carried_to_a_reused_label_with_a_different_block_type() -> None:
    previous = [_nav("block_3", notes="old note")]
    submitted = [
        {
            "block_type": "extraction",
            "label": "block_3",
            "data_extraction_goal": "Extract the total.",
            "data_schema": {"type": "object", "properties": {}},
        }
    ]

    assert carry_forward_block_notes(submitted, previous, same_step_labels={"block_3"}) == {}
    assert "notes" not in submitted[0]


def test_rest_save_carries_omitted_notes_only_to_a_same_type_stored_step() -> None:
    previous = [_nav("a", notes="keep me")]
    submitted = [_nav("a")]

    assert carry_forward_block_notes(submitted, previous, same_step_labels={"a"}) == {"a": ["notes"]}
    assert submitted[0]["notes"] == "keep me"


def test_a_client_that_omits_notes_does_not_inherit_text_on_a_same_type_reused_label() -> None:
    previous = _definition([_nav("block_3", notes="old note", trailing_notes="old end", description="old description")])
    stated = WorkflowDefinitionYAML.model_validate(
        {"parameters": [], "blocks": [_nav("block_3", notes=None, trailing_notes=None, description=None)]}
    )
    silent = WorkflowDefinitionYAML.model_validate({"parameters": [], "blocks": [_nav("block_3")]})

    assert carry_forward_block_notes(stated.blocks, previous.blocks) == {}
    saved = convert_workflow_definition(workflow_definition_yaml=stated, workflow_id="w_test")
    assert [(block.notes, block.trailing_notes, block.description) for block in saved.blocks] == [(None, None, None)]

    # Label and type are insufficient identity proof for a replacement step.
    assert carry_forward_block_notes(silent.blocks, previous.blocks) == {}
    assert (silent.blocks[0].notes, silent.blocks[0].trailing_notes, silent.blocks[0].description) == (None, None, None)


def test_long_notes_are_rejected() -> None:
    with pytest.raises(ValidationError, match="at most 2000 characters"):
        WorkflowDefinitionYAML.model_validate({"parameters": [], "blocks": [_nav("a", notes="x" * 2001)]})


def test_credential_references_in_notes_are_saved_and_stay_literal_at_run_time() -> None:
    parameters = [{"parameter_type": "workflow", "key": "login", "workflow_parameter_type": "credential_id"}]
    definition = _definition([_nav("a", notes="Sign in as {{ login.username }}")], parameters=parameters)

    assert (
        _run_context(definition).resolve_block_notes_section("a")
        == "Notes for this step:\nSign in as {{ login.username }}"
    )


def test_cache_clears_from_the_step_above_a_changed_note_and_never_for_a_description() -> None:
    def blocks(note: str, description: str) -> list[dict[str, Any]]:
        schema = {"type": "object", "properties": {"total": {"type": "number", "description": "Invoice total"}}}
        return [
            _nav("a", description=description),
            _nav("b", notes=note),
            {"block_type": "extraction", "label": "c", "data_extraction_goal": "x", "data_schema": schema},
        ]

    before = _get_workflow_definition_core_data(_definition(blocks("old", "Opens the site")))
    note_changed = _get_workflow_definition_core_data(_definition(blocks("new", "Opens the site")))
    description_changed = _get_workflow_definition_core_data(_definition(blocks("old", "Something else")))

    plan = WorkflowService._determine_cache_invalidation(before["blocks"], note_changed["blocks"])
    assert plan.block_labels_to_disable == ["a", "b", "c"]
    assert description_changed == before
    assert description_changed["blocks"][2]["data_schema"]["properties"]["total"]["description"] == "Invoice total"


def _yaml(*blocks: dict[str, Any]) -> str:
    return dump_workflow_yaml({"title": "t", "workflow_definition": {"parameters": [], "blocks": list(blocks)}})


def _staged_notes(workflow_yaml: str) -> dict[str, Any]:
    blocks = safe_load_no_dates(workflow_yaml)["workflow_definition"]["blocks"]
    return {block["label"]: block.get("notes") for block in blocks}


def test_a_deletion_in_the_draft_is_not_undone_by_the_saved_version() -> None:
    saved = _yaml(_nav("a", notes="saved a"), _nav("b", notes="saved b"))
    draft = _yaml(_nav("a", notes=None), _nav("b"))

    staged, kept, dropped = carry_block_notes_from_versions(_yaml(_nav("a"), _nav("b")), draft, saved)

    assert _staged_notes(staged) == {"a": None, "b": "saved b"}
    assert (kept, dropped) == ({"b": ["notes"]}, [])


def test_notes_lost_by_a_rename_are_reported_unless_they_moved_with_it() -> None:
    saved = _yaml(_nav("a", notes="keep a"), _nav("b", notes="keep b"))
    editor = _yaml(_nav("a"), _nav("b"))

    _, _, dropped = carry_block_notes_from_versions(_yaml(_nav("a2", notes="keep a"), _nav("c")), editor, saved)

    assert dropped == ["b"]


def test_identical_notes_on_a_surviving_block_do_not_hide_a_dropped_note() -> None:
    saved = _yaml(_nav("a", notes="Same instruction."), _nav("b", notes="Same instruction."))
    draft = _yaml(_nav("a", notes="Same instruction."))

    _, _, dropped = carry_block_notes_from_versions(draft, saved, saved)

    assert dropped == ["b"]


def test_a_blank_saved_note_is_not_reported_as_kept() -> None:
    previous = _definition([_nav("a", notes="   ")])
    submitted = WorkflowDefinitionYAML.model_validate({"parameters": [], "blocks": [_nav("a")]})

    assert carry_forward_block_notes(submitted.blocks, previous.blocks) == {}
