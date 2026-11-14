import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from pydantic import ValidationError

from skyvern.forge import app
from skyvern.forge.sdk.db.agent_db import AgentDB, _build_engine
from skyvern.forge.sdk.workflow.block_notes import (
    carry_block_notes_from_versions,
    carry_forward_block_notes,
    definition_step_notes,
    drop_submitted_note_fields,
)
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import InvalidWorkflowDefinition
from skyvern.forge.sdk.workflow.models.block import Block
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from skyvern.forge.sdk.workflow.service import (
    WorkflowService,
    _get_workflow_definition_core_data,
)
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest, WorkflowDefinitionYAML
from skyvern.services import uploaded_file_service
from skyvern.services.uploaded_file_service import BLOCK_IMAGE_MAX_BYTES
from skyvern.utils.yaml_loader import dump_workflow_yaml, safe_load_no_dates
from tests.unit._uploaded_files_fake import FakeUploadedFilesRepository
from tests.unit.conftest import SqliteEngineFactory


def _nav(label: str, **extra: Any) -> dict[str, Any]:
    return {"block_type": "navigation", "label": label, "url": "https://example.com", "navigation_goal": label, **extra}


def _loop(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {
        "block_type": "for_loop",
        "label": "loop",
        "loop_variable_reference": "{{ items }}",
        "loop_blocks": list(blocks),
    }


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


@pytest.mark.boundary
@pytest.mark.parametrize(
    "case",
    json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "skyvern-frontend/src/routes/workflows/editor/doc/workflow_note_readings.json"
        ).read_text()
    ),
    ids=lambda case: case["name"],
)
def test_shared_canvas_note_readings_match_runtime(case: dict[str, Any]) -> None:
    assert definition_step_notes(case["definition"]) == case["expected"]


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


def _image(file_id: str, caption: str | None = None) -> dict[str, Any]:
    return {"file_id": file_id, "caption": caption, "placement": "block"}


def test_block_images_survive_conversion_and_a_save_while_unknown_keys_are_dropped() -> None:
    submitted = WorkflowDefinitionYAML.model_validate(
        {
            "parameters": [],
            "blocks": [
                _nav("a", doc_images=[_image("file_901", "The total")], prompt_images=[_image("file_902")], extra=1)
            ],
        }
    )
    saved = convert_workflow_definition(workflow_definition_yaml=submitted, workflow_id="w_test")
    stored = saved.model_dump(mode="json")["blocks"][0]
    reloaded = WorkflowDefinition.model_validate(saved.model_dump(mode="json")).blocks[0]

    assert "extra" not in stored
    assert stored["doc_images"] == [_image("file_901", "The total")]
    assert stored["prompt_images"] == [_image("file_902")]
    assert [image.file_id for image in reloaded.doc_images or []] == ["file_901"]
    assert [image.file_id for image in reloaded.prompt_images or []] == ["file_902"]


def test_doc_images_never_change_the_cache_key_and_prompt_images_do() -> None:
    def key(**images: Any) -> dict[str, Any]:
        return _get_workflow_definition_core_data(_definition([_nav("a", **images), _nav("b")]))

    assert key(doc_images=[_image("file_1")]) == key() == key(doc_images=[_image("file_2", "other")])
    assert key(prompt_images=[]) == key()
    assert key(prompt_images=[_image("file_1")]) != key()
    assert key(prompt_images=[_image("file_1")]) != key(prompt_images=[_image("file_2")])


@pytest.mark.parametrize(("field", "cap"), [("doc_images", 10), ("prompt_images", 3)])
def test_block_image_lists_are_capped(field: str, cap: int) -> None:
    def submit_ids(*file_ids: str) -> WorkflowDefinitionYAML:
        images = [_image(file_id) for file_id in file_ids]
        return WorkflowDefinitionYAML.model_validate({"parameters": [], "blocks": [_nav("a", **{field: images})]})

    def submit(count: int) -> WorkflowDefinitionYAML:
        return submit_ids(*(f"file_{index}" for index in range(count)))

    assert len(getattr(submit(cap).blocks[0], field)) == cap
    with pytest.raises(ValidationError, match=f"at most {cap} items"):
        submit(cap + 1)
    assert getattr(submit_ids("file_123").blocks[0], field)[0].file_id == "file_123"
    for not_an_upload in ("https://example.com/x.png", "file_doc", "file_"):
        with pytest.raises(ValidationError, match="is not an uploaded file id"):
            submit_ids(not_an_upload)


def test_omitted_block_images_carry_forward_and_an_explicit_clear_wins() -> None:
    previous = _definition([_nav("a", doc_images=[_image("file_901")], prompt_images=[_image("file_902")])])
    silent = WorkflowDefinitionYAML.model_validate({"parameters": [], "blocks": [_nav("a")]})
    cleared = WorkflowDefinitionYAML.model_validate({"parameters": [], "blocks": [_nav("a", doc_images=None)]})

    carry_forward_block_notes(silent.blocks, previous.blocks, same_step_labels={"a"})
    carry_forward_block_notes(cleared.blocks, previous.blocks, same_step_labels={"a"})
    kept = convert_workflow_definition(workflow_definition_yaml=silent, workflow_id="w_test").blocks[0]
    dropped = convert_workflow_definition(workflow_definition_yaml=cleared, workflow_id="w_test").blocks[0]

    assert [image.file_id for image in kept.doc_images or []] == ["file_901"]
    assert [image.file_id for image in kept.prompt_images or []] == ["file_902"]
    assert dropped.doc_images is None
    assert [image.file_id for image in dropped.prompt_images or []] == ["file_902"]


def test_a_copilot_that_cannot_see_notes_submits_no_image_fields_so_the_saved_ones_are_kept() -> None:
    blocks = [_nav("a", doc_images=[], prompt_images=[_image("file_903")])]
    submitted = dump_workflow_yaml({"title": "t", "workflow_definition": {"parameters": [], "blocks": blocks}})

    block = safe_load_no_dates(drop_submitted_note_fields(submitted))["workflow_definition"]["blocks"][0]

    assert "doc_images" not in block
    assert "prompt_images" not in block


class _PastImageCheck(Exception):
    pass


@pytest.mark.asyncio
async def test_an_upload_past_its_retention_counts_as_unknown_before_the_purge_retires_it() -> None:
    repo = FakeUploadedFilesRepository()
    expired = repo.seed("o_saver", expires_at=datetime.now(UTC) - timedelta(minutes=1))
    live = repo.seed("o_saver", expires_at=datetime.now(UTC) + timedelta(days=1))
    kept = repo.seed("o_saver")
    with patch.object(uploaded_file_service, "app", SimpleNamespace(DATABASE=SimpleNamespace(uploaded_files=repo))):
        found = await uploaded_file_service.live_uploaded_files(
            file_ids=[expired, live, kept], organization_id="o_saver"
        )
    assert set(found) == {live, kept}


@pytest.mark.asyncio
async def test_an_update_without_an_organization_checks_images_against_the_workflow_owner() -> None:
    repo = FakeUploadedFilesRepository()
    own = repo.seed("o_saver")
    other = repo.seed("o_other")

    async def update(file_id: str, owner: Any) -> None:
        definition = _definition([_nav("a", doc_images=[_image(file_id)])])
        workflows = SimpleNamespace(get_workflow=AsyncMock(return_value=owner))
        database = SimpleNamespace(uploaded_files=repo, workflows=workflows)
        with (
            patch.object(uploaded_file_service, "app", SimpleNamespace(DATABASE=database)),
            patch("skyvern.forge.sdk.workflow.service.app", SimpleNamespace(DATABASE=database)),
            patch.object(WorkflowService, "_validate_block_engines", side_effect=_PastImageCheck),
        ):
            await WorkflowService().update_workflow_definition(
                workflow_id="w_test",
                organization_id=None,
                workflow_definition=definition,
                prior_definition=_definition([]),
            )

    with pytest.raises(InvalidWorkflowDefinition, match=f"Block a references image {other}"):
        await update(other, SimpleNamespace(organization_id="o_saver"))
    with pytest.raises(InvalidWorkflowDefinition, match="need the workflow's organization"):
        await update(own, None)
    with pytest.raises(_PastImageCheck):
        await update(own, SimpleNamespace(organization_id="o_saver"))


@pytest_asyncio.fixture
async def image_save_lab(
    monkeypatch: pytest.MonkeyPatch, sqlite_engine_factory: SqliteEngineFactory
) -> AsyncIterator[SimpleNamespace]:
    database = AgentDB(str((engine := sqlite_engine_factory(build_engine=_build_engine)).url), db_engine=engine)
    organization = await database.organizations.create_organization(organization_name="t", organization_id="o_saver")
    repo = FakeUploadedFilesRepository()
    database.uploaded_files = cast(Any, repo)
    service = WorkflowService()
    lookups = AsyncMock(side_effect=uploaded_file_service.live_uploaded_files)
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", service)
    monkeypatch.setattr(uploaded_file_service, "live_uploaded_files", lookups)
    try:
        yield SimpleNamespace(service=service, organization=organization, repo=repo, lookups=lookups)
    finally:
        await database.engine.dispose()


def _image_request(*file_ids: str) -> WorkflowCreateYAMLRequest:
    return WorkflowCreateYAMLRequest(
        title="t",
        workflow_definition=WorkflowDefinitionYAML.model_validate(
            {"parameters": [], "blocks": [_loop(_nav("a", doc_images=[_image(file_id) for file_id in file_ids]))]}
        ),
    )


@pytest.mark.asyncio
async def test_a_create_and_an_update_that_adds_an_image_each_look_their_images_up_once(
    image_save_lab: SimpleNamespace,
) -> None:
    lab = image_save_lab
    own = lab.repo.seed("o_saver")
    created = await lab.service.create_workflow_from_request(organization=lab.organization, request=_image_request(own))
    assert lab.lookups.await_count == 1

    added = lab.repo.seed("o_saver")
    await lab.service.update_workflow_definition(
        workflow_id=created.workflow_id,
        organization_id="o_saver",
        workflow_definition=convert_workflow_definition(
            workflow_definition_yaml=_image_request(own, added).workflow_definition, workflow_id=created.workflow_id
        ),
    )
    assert lab.lookups.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("retire", ["expire", "delete"])
async def test_a_retried_create_returns_the_first_workflow_after_its_image_is_gone(
    image_save_lab: SimpleNamespace, retire: str
) -> None:
    lab = image_save_lab
    own = lab.repo.seed("o_saver")

    async def create() -> tuple[Workflow, tuple[str, ...]]:
        return await lab.service.create_workflow_from_request(
            organization=lab.organization,
            request=_image_request(own),
            new_workflow_permanent_id="wpid_new",
            return_write_result=True,
        )

    first, first_writes = await create()
    if retire == "expire":
        lab.repo.rows[own].expires_at = datetime.now(UTC) - timedelta(minutes=1)
    else:
        await lab.repo.claim_uploaded_file_for_deletion(own, "o_saver")
    retried, retried_writes = await create()

    assert (first_writes, retried_writes) == (("workflow_definition",), ())
    assert retried.workflow_id == first.workflow_id
    assert lab.lookups.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("permanent_id", [None, "wpid_new"])
async def test_a_create_with_an_unknown_image_raises_and_writes_no_workflow(
    image_save_lab: SimpleNamespace, permanent_id: str | None
) -> None:
    lab = image_save_lab
    other = lab.repo.seed("o_other")
    for rejected in (other, "file_904"):
        with pytest.raises(InvalidWorkflowDefinition, match=f"Block a references image {rejected}"):
            await lab.service.create_workflow_from_request(
                organization=lab.organization, request=_image_request(rejected), new_workflow_permanent_id=permanent_id
            )
    assert await lab.service.get_workflows_by_organization_id(organization_id="o_saver") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("size_bytes", "accepted"),
    [(BLOCK_IMAGE_MAX_BYTES, True), (BLOCK_IMAGE_MAX_BYTES + 1, False), (None, True)],
)
async def test_a_save_rejects_a_new_image_larger_than_4_mib(
    image_save_lab: SimpleNamespace, size_bytes: int | None, accepted: bool
) -> None:
    lab = image_save_lab
    image = lab.repo.seed("o_saver", size_bytes=size_bytes)
    create = lab.service.create_workflow_from_request(organization=lab.organization, request=_image_request(image))
    if accepted:
        await create
    else:
        with pytest.raises(InvalidWorkflowDefinition, match=f"image {image} is larger than 4 MiB"):
            await create


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "accepted"),
    [
        *((name, True) for name in ("a.png", "a.jpg", "a.JPG", "a.jpeg", "a.jfif", "a.gif", "a.webp")),
        *(
            (name, False)
            for name in (
                "a.svg",
                "a.bmp",
                "a.tif",
                "a.tiff",
                "a.heic",
                "a.avif",
                "a.pdf",
                "a.mp4",
                "screenshot",
                "a.png.gz",
                "a.xyz",
            )
        ),
    ],
)
async def test_a_save_rejects_a_new_image_that_is_not_a_raster_upload(
    image_save_lab: SimpleNamespace, filename: str, accepted: bool
) -> None:
    lab = image_save_lab
    image = lab.repo.seed("o_saver", filename=filename)
    create = lab.service.create_workflow_from_request(organization=lab.organization, request=_image_request(image))
    if accepted:
        await create
    else:
        with pytest.raises(InvalidWorkflowDefinition, match=f"file {image} is not a png, jpeg, gif or webp image"):
            await create


@pytest.mark.asyncio
async def test_a_large_non_image_is_refused_for_its_type_first(image_save_lab: SimpleNamespace) -> None:
    lab = image_save_lab
    image = lab.repo.seed("o_saver", filename="a.pdf", size_bytes=BLOCK_IMAGE_MAX_BYTES + 1)
    with pytest.raises(InvalidWorkflowDefinition, match="is not a png, jpeg, gif or webp image"):
        await lab.service.create_workflow_from_request(organization=lab.organization, request=_image_request(image))


@pytest.mark.asyncio
async def test_an_update_checks_only_the_images_it_adds(image_save_lab: SimpleNamespace) -> None:
    lab = image_save_lab
    oversized, deleted = lab.repo.seed("o_saver"), lab.repo.seed("o_saver")
    created = await lab.service.create_workflow_from_request(
        organization=lab.organization, request=_image_request(oversized, deleted)
    )
    lab.repo.rows[oversized].size_bytes = BLOCK_IMAGE_MAX_BYTES + 1
    await lab.repo.claim_uploaded_file_for_deletion(deleted, "o_saver")
    other = lab.repo.seed("o_other")

    async def update(*file_ids: str) -> None:
        await lab.service.update_workflow_definition(
            workflow_id=created.workflow_id,
            organization_id="o_saver",
            workflow_definition=convert_workflow_definition(
                workflow_definition_yaml=_image_request(*file_ids).workflow_definition,
                workflow_id=created.workflow_id,
            ),
        )

    with pytest.raises(InvalidWorkflowDefinition, match=f"Block a references image {other}"):
        await update(oversized, deleted, other)
    await update(oversized, deleted)


def test_image_fields_inside_a_loop_are_stripped_carried_and_dropped() -> None:
    def key(**images: Any) -> dict[str, Any]:
        return _get_workflow_definition_core_data(_definition([_loop(_nav("a", **images))]))

    previous = _definition([_loop(_nav("a", doc_images=[_image("file_901")]))])
    silent = WorkflowDefinitionYAML.model_validate({"parameters": [], "blocks": [_loop(_nav("a"))]})
    carry_forward_block_notes(silent.blocks, previous.blocks, same_step_labels={"loop", "a"})
    kept = convert_workflow_definition(workflow_definition_yaml=silent, workflow_id="w_test").blocks[0]
    submitted = dump_workflow_yaml(
        {"title": "t", "workflow_definition": {"parameters": [], "blocks": [_loop(_nav("a", doc_images=[]))]}}
    )
    dropped = safe_load_no_dates(drop_submitted_note_fields(submitted))["workflow_definition"]["blocks"][0]

    assert key(doc_images=[_image("file_1")]) == key()
    assert key(prompt_images=[_image("file_1")]) != key()
    assert [image.file_id for image in kept.loop_blocks[0].doc_images or []] == ["file_901"]
    assert "doc_images" not in dropped["loop_blocks"][0]
