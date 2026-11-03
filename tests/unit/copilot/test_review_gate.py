from __future__ import annotations

import json
import textwrap
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.code_block_steps import derive_code_block_steps_in_yaml
from skyvern.forge.sdk.copilot.data_write_defaults import DATA_WRITE_BLOCK_TYPES
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.review_gate import (
    DESTINATION_ADAPTERS,
    NarrativeReviewProjection,
    build_review_projection,
    parse_execution_receipts,
    serialize_execution_receipts,
    workflow_block_fingerprints,
)
from skyvern.forge.sdk.copilot.workflow_yaml import _process_workflow_yaml, workflow_to_copilot_yaml
from tests.unit.copilot_test_helpers import make_copilot_ctx


def _workflow(blocks: str, *, parameters: str = "[]") -> str:
    rendered_blocks = blocks or "    []\n"
    return f"""
title: Review fixture
workflow_definition:
  parameters: {parameters}
  blocks:
{rendered_blocks}
"""


def test_projection_classifies_all_changes_and_uses_cumulative_execution_evidence() -> None:
    persisted = _workflow(
        """    - block_type: task
      label: unchanged_step
      next_block_label: changed_step
      navigation_goal: Keep this
    - block_type: task
      label: changed_step
      next_block_label: removed_step
      navigation_goal: Before
    - block_type: task
      label: removed_step
      navigation_goal: Remove me
"""
    )
    staged = _workflow(
        """    - block_type: task
      label: unchanged_step
      next_block_label: changed_step
      navigation_goal: Keep this
    - block_type: task
      label: changed_step
      next_block_label: added_step
      navigation_goal: After
    - block_type: task
      label: added_step
      navigation_goal: New
"""
    )

    projection = build_review_projection(persisted, staged, {"unchanged_step", "changed_step"})

    assert projection is not None
    assert projection["blocks"] == [
        {
            "label": "unchanged_step",
            "blockType": "task",
            "change": "unchanged",
            "neverTested": False,
            "coverage": "current_source",
        },
        {
            "label": "changed_step",
            "blockType": "task",
            "change": "changed",
            "neverTested": False,
            "coverage": "current_source",
        },
        {
            "label": "added_step",
            "blockType": "task",
            "change": "added",
            "neverTested": True,
            "coverage": "never_run",
        },
        {"label": "removed_step", "blockType": "task", "change": "removed"},
    ]


def test_projection_marks_reordered_and_rewired_blocks_as_changed() -> None:
    persisted = _workflow(
        """    - block_type: task
      label: first
      prompt: First
    - block_type: task
      label: second
      prompt: Second
"""
    )
    staged = _workflow(
        """    - block_type: task
      label: second
      next_block_label: first
      prompt: Second
    - block_type: task
      label: first
      prompt: First
"""
    )

    projection = build_review_projection(persisted, staged, {"first", "second"})

    assert projection is not None
    assert [(row["label"], row["change"]) for row in projection["blocks"]] == [
        ("second", "changed"),
        ("first", "changed"),
    ]


def test_projection_ignores_chain_rewiring_caused_by_insertion_and_deletion() -> None:
    persisted = _workflow(
        """    - block_type: task
      label: first
      next_block_label: removed
      prompt: First
    - block_type: task
      label: removed
      next_block_label: last
      prompt: Removed
    - block_type: task
      label: last
      prompt: Last
"""
    )
    staged = _workflow(
        """    - block_type: task
      label: first
      next_block_label: added
      prompt: First
    - block_type: task
      label: added
      next_block_label: last
      prompt: Added
    - block_type: task
      label: last
      prompt: Last
"""
    )

    projection = build_review_projection(persisted, staged, set())

    assert projection is not None
    assert [(row["label"], row["change"]) for row in projection["blocks"]] == [
        ("first", "unchanged"),
        ("added", "added"),
        ("last", "unchanged"),
        ("removed", "removed"),
    ]


def test_projection_marks_a_true_common_block_rewire_as_changed() -> None:
    persisted = _workflow(
        """    - block_type: task
      label: first
      next_block_label: second
      prompt: First
    - block_type: task
      label: second
      prompt: Second
    - block_type: task
      label: third
      prompt: Third
"""
    )
    staged = persisted.replace("next_block_label: second", "next_block_label: third")

    projection = build_review_projection(persisted, staged, set())

    assert projection is not None
    assert projection["blocks"][0]["change"] == "changed"


def test_projection_binds_execution_evidence_to_the_exact_block_shape() -> None:
    tested = _workflow("""    - block_type: task
      label: step
      prompt: Before
""")
    edited = _workflow("""    - block_type: task
      label: step
      prompt: After
""")

    projection = build_review_projection(tested, edited, workflow_block_fingerprints(tested))

    assert projection is not None
    assert projection["blocks"][0]["neverTested"] is True


def test_a_drafted_sheets_append_is_never_tested_until_it_actually_runs() -> None:
    persisted = _workflow("""    - block_type: task
      label: count_posts
      prompt: Count the posts
""")
    staged = _workflow("""    - block_type: task
      label: count_posts
      next_block_label: append_count
      prompt: Count the posts
    - block_type: google_sheets_write
      label: append_count
      spreadsheet_url: https://docs.google.com/spreadsheets/d/sheet
      sheet_name: Sheet1
      range: C1
      write_mode: append
      credential_id: goac_active
      values: "[[\\"{{ count_posts_output }}\\"]]"
""")

    counted_only = {"count_posts": workflow_block_fingerprints(staged)["count_posts"]}

    projection = build_review_projection(persisted, staged, counted_only)

    assert projection is not None
    assert projection["blocks"] == [
        {
            "label": "count_posts",
            "blockType": "task",
            "change": "unchanged",
            "neverTested": False,
            "coverage": "current_source",
        },
        {
            "label": "append_count",
            "blockType": "google_sheets_write",
            "change": "added",
            "neverTested": True,
            "coverage": "never_run",
        },
    ]


def test_parameter_default_change_invalidates_change_and_execution_identity() -> None:
    tested = _workflow(
        """    - block_type: task
      label: step
      prompt: Use {{ account }}
""",
        parameters="[{key: account, parameter_type: workflow, default_value: primary}]",
    )
    staged = tested.replace("default_value: primary", "default_value: backup")

    projection = build_review_projection(tested, staged, workflow_block_fingerprints(tested))

    assert projection is not None
    assert projection["blocks"][0] == {
        "label": "step",
        "blockType": "task",
        "change": "changed",
        "neverTested": True,
        "coverage": "different_source",
    }


def test_projection_lists_each_input_change_by_key_without_its_values() -> None:
    step = """    - block_type: task
      label: step
      prompt: Open the portal
"""
    persisted = _workflow(
        step,
        parameters="[{key: api_token, parameter_type: workflow, default_value: sk-old-secret}, "
        "{key: seed_rows, parameter_type: workflow, default_value: seed-secret-rows}, "
        "{key: region, parameter_type: workflow, default_value: east, description: Before}]",
    )
    staged = _workflow(
        step,
        parameters="[{key: api_token, parameter_type: workflow, default_value: sk-new-secret}, "
        "{key: region, parameter_type: workflow, default_value: east, description: After}, "
        "{key: report_key, parameter_type: workflow, default_value: rk-added-secret}]",
    )

    projection = build_review_projection(persisted, staged, set())

    assert projection is not None
    assert projection["inputs"] == [
        {"key": "api_token", "change": "changed"},
        {"key": "region", "change": "changed"},
        {"key": "report_key", "change": "added"},
        {"key": "seed_rows", "change": "removed"},
    ]
    wire = json.dumps(projection)
    for value in ("sk-old-secret", "sk-new-secret", "seed-secret-rows", "rk-added-secret", "east", "Before", "After"):
        assert value not in wire


def test_an_input_whose_only_difference_is_an_explicit_null_is_not_changed() -> None:
    step = """    - block_type: task
      label: step
      prompt: Open the portal
"""
    persisted = _workflow(step, parameters="[{key: region, parameter_type: workflow, default_value: null}]")
    staged = _workflow(step, parameters="[{key: region, parameter_type: workflow}]")

    projection = build_review_projection(persisted, staged, set())

    assert projection is not None
    assert projection["inputs"] == []


def test_an_input_differing_only_in_stored_row_fields_is_not_changed() -> None:
    step = """    - block_type: task
      label: step
      prompt: Open the portal
"""
    staged = _workflow(step, parameters="[{key: region, parameter_type: workflow, workflow_parameter_type: string}]")
    persisted = _workflow(
        step,
        parameters="[{key: region, parameter_type: workflow, workflow_parameter_type: string, "
        "workflow_parameter_id: wp_1, workflow_id: w_1, created_at: '2026-01-01T00:00:00', "
        "modified_at: '2026-01-02T00:00:00'}]",
    )

    projection = build_review_projection(persisted, staged, set())

    assert projection is not None
    assert projection["inputs"] == []


def test_a_json_default_changed_from_true_to_one_is_a_changed_input() -> None:
    step = """    - block_type: task
      label: step
      prompt: Open the portal
"""
    persisted = _workflow(
        step, parameters="[{key: flags, parameter_type: workflow, workflow_parameter_type: json, default_value: true}]"
    )
    staged = persisted.replace("default_value: true", "default_value: 1")

    projection = build_review_projection(persisted, staged, set())

    assert projection is not None
    assert projection["inputs"] == [{"key": "flags", "change": "changed"}]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_saved_workflow")
async def test_inputs_saved_through_the_converter_match_the_yaml_they_came_from() -> None:
    staged = _workflow(
        """    - block_type: task
      label: step
      prompt: Open the portal
""",
        parameters="[{key: page_size, parameter_type: workflow, workflow_parameter_type: integer, default_value: '5'}, "
        "{key: dry_run, parameter_type: workflow, workflow_parameter_type: boolean, default_value: 'true'}, "
        "{key: ratio, parameter_type: workflow, workflow_parameter_type: float, default_value: 2}, "
        """{key: seed_rows, parameter_type: workflow, workflow_parameter_type: json, default_value: '[{"id": 1}]'}]""",
    )
    saved = workflow_to_copilot_yaml(await _process_workflow_yaml("w", "wp", "o", staged))

    projection = build_review_projection(saved, staged, set())

    assert projection is not None
    assert projection["inputs"] == []


def test_missing_code_steps_are_canonicalized_before_execution_fingerprinting() -> None:
    without_steps = _workflow(
        """    - block_type: code
      label: calculate
      code: |-
        total = 1 + 1
"""
    )
    accepted_shape = derive_code_block_steps_in_yaml(without_steps)

    assert workflow_block_fingerprints(without_steps) == workflow_block_fingerprints(accepted_shape)


_READ_PRICE_BLOCK = """- block_type: code
  label: read_price
  code: |-
    price = await page.locator('.price').inner_text()
"""
_OLD_WORDING_STEPS = """  steps:
    - description: Extract information from the page
      action_type: extract
      line_start: 1
"""
_NEW_WORDING_STEPS = """  steps:
    - description: Extract price
      action_type: extract
      line_start: 1
      line_end: 1
"""


def _top_level(block: str) -> str:
    return _workflow(textwrap.indent(block, "    "))


def _inside_loop(block: str) -> str:
    loop = "- block_type: for_loop\n  label: loop\n  loop_over_parameter_key: items\n  loop_blocks:\n"
    return _workflow(textwrap.indent(loop + textwrap.indent(block, "    "), "    "))


@pytest.mark.parametrize("wrap", [_top_level, _inside_loop])
def test_tested_block_fingerprint_ignores_code_steps_at_every_depth(wrap: Callable[[str], str]) -> None:
    old = wrap(_READ_PRICE_BLOCK + _OLD_WORDING_STEPS)
    new = wrap(_READ_PRICE_BLOCK + _NEW_WORDING_STEPS)

    assert workflow_block_fingerprints(old) == workflow_block_fingerprints(new)


@pytest.mark.parametrize("wrap", [_top_level, _inside_loop])
def test_projection_treats_old_wording_steps_as_unchanged(wrap: Callable[[str], str]) -> None:
    persisted = wrap(_READ_PRICE_BLOCK + _OLD_WORDING_STEPS)
    staged = wrap(_READ_PRICE_BLOCK)

    projection = build_review_projection(persisted, staged, workflow_block_fingerprints(persisted))

    assert projection is not None
    assert [(block["change"], block["coverage"]) for block in projection["blocks"]] == [
        ("unchanged", "current_source")
    ] * len(projection["blocks"])


def test_projection_retains_every_tested_version_when_a_block_reverts() -> None:
    version_a = _workflow("""    - block_type: task
      label: step
      prompt: Version A
""")
    version_b = version_a.replace("Version A", "Version B")
    receipts = workflow_block_fingerprints(version_a)
    for label, fingerprints in workflow_block_fingerprints(version_b).items():
        receipts.setdefault(label, set()).update(fingerprints)

    projection = build_review_projection(version_b, version_a, receipts)

    assert projection is not None
    assert projection["blocks"][0]["neverTested"] is False


def test_nested_block_label_change_invalidates_parent_tested_version() -> None:
    tested = _workflow("""    - block_type: for_loop
      label: loop
      loop_over_parameter_key: items
      loop_blocks:
        - block_type: task
          label: child_before
          prompt: Work
""")
    staged = tested.replace("child_before", "child_after")

    projection = build_review_projection(tested, staged, workflow_block_fingerprints(tested))

    assert projection is not None
    assert projection["blocks"][0] == {
        "label": "loop",
        "blockType": "for_loop",
        "change": "changed",
        "neverTested": True,
        "coverage": "different_source",
    }


def test_projection_treats_a_rename_as_remove_plus_add() -> None:
    persisted = _workflow("""    - block_type: task
      label: old_name
      prompt: Same content
""")
    staged = _workflow("""    - block_type: task
      label: new_name
      prompt: Same content
""")

    projection = build_review_projection(persisted, staged, set())

    assert projection is not None
    assert [(row["label"], row["change"]) for row in projection["blocks"]] == [
        ("new_name", "added"),
        ("old_name", "removed"),
    ]


def test_projection_returns_none_when_either_workflow_cannot_be_parsed() -> None:
    valid = _workflow("""    - block_type: task
      label: step
      prompt: valid
""")

    assert build_review_projection("::: not yaml", valid, set()) is None
    assert build_review_projection(valid, "::: not yaml", set()) is None


def test_destination_registry_stays_complete_for_every_data_write_type() -> None:
    assert set(DESTINATION_ADAPTERS) == DATA_WRITE_BLOCK_TYPES


def test_a_note_change_marks_both_neighbours_changed_and_untested_but_a_description_change_does_not() -> None:
    def workflow(note: str, description: str) -> str:
        return _workflow(f"""    - block_type: task
      label: above
      prompt: First
      description: {description}
    - block_type: task
      label: below
      prompt: Second
      notes: {note}
    - block_type: task
      label: elsewhere
      prompt: Third
""")

    tested = workflow("Old note", "Opens the site")
    receipts = workflow_block_fingerprints(tested)

    note_edit = build_review_projection(tested, workflow("New note", "Opens the site"), receipts)
    description_edit = build_review_projection(tested, workflow("Old note", "Something else"), receipts)

    assert note_edit is not None and description_edit is not None
    assert [(block["label"], block["change"], block["neverTested"]) for block in note_edit["blocks"]] == [
        ("above", "changed", True),
        ("below", "changed", True),
        ("elsewhere", "unchanged", False),
    ]
    assert all(block["change"] == "unchanged" and not block["neverTested"] for block in description_edit["blocks"])

    empty_note = workflow("Old note", "Opens the site").replace(
        "      description:", "      notes: null\n      description:"
    )
    empty_note_edit = build_review_projection(tested, empty_note, receipts)
    assert empty_note_edit is not None
    assert all(block["change"] == "unchanged" for block in empty_note_edit["blocks"])


def test_sheets_identity_resolves_account_and_ignores_representational_url_query() -> None:
    parameters = """
    - key: sheets_account
      parameter_type: workflow
      workflow_parameter_type: credential_id
      default_value: cred_primary
"""
    staged = _workflow(
        """    - block_type: google_sheets_write
      label: append_primary
      spreadsheet_url: https://docs.google.com/spreadsheets/d/spreadsheet-reference-123/edit
      sheet_name: Sales / Marketing
      range: A1:C
      credential_id: '{{ sheets_account }}'
      values: '{{ source_a }}'
    - block_type: google_sheets_write
      label: append_backup
      spreadsheet_url: https://docs.google.com/spreadsheets/d/spreadsheet-reference-123/edit?gid=456&usp=sharing
      sheet_name: Sales / Marketing
      range: A1:C
      credential_id: '{{ sheets_account }}'
      values: '{{ source_b }}'
""",
        parameters=parameters,
    )

    projection = build_review_projection(_workflow(""), staged, set())

    assert projection is not None
    assert projection["duplicateWrites"] == [
        {
            "blockType": "google_sheets_write",
            "blockLabels": ["append_primary", "append_backup"],
        }
    ]
    wire = json.dumps(projection)
    for destination_value in ("cred_primary", "spreadsheet-reference-123", "Sales / Marketing", "A1:C"):
        assert destination_value not in wire


def test_sheets_identity_resolves_fixed_credential_parameters() -> None:
    staged = _workflow(
        """    - block_type: google_sheets_write
      label: first
      spreadsheet_url: spreadsheet-reference-123456
      sheet_name: Summary
      credential_id: '{{ account }}'
      values: one
    - block_type: google_sheets_write
      label: second
      spreadsheet_url: spreadsheet-reference-123456
      sheet_name: Summary
      credential_id: '{{ account }}'
      values: two
""",
        parameters="""
    - key: account
      parameter_type: credential
      credential_id: cred_primary
""",
    )

    projection = build_review_projection(_workflow(""), staged, set())

    assert projection is not None
    assert projection["duplicateWrites"] == [{"blockType": "google_sheets_write", "blockLabels": ["first", "second"]}]


def test_sheets_identity_abstains_without_a_configured_account() -> None:
    staged = _workflow(
        """    - block_type: google_sheets_write
      label: first
      spreadsheet_url: spreadsheet-reference-123456
      sheet_name: Summary
      values: one
    - block_type: google_sheets_write
      label: second
      spreadsheet_url: spreadsheet-reference-123456
      sheet_name: Summary
      values: two
"""
    )

    projection = build_review_projection(_workflow(""), staged, set())

    assert projection is not None
    assert projection["duplicateWrites"] == []


def test_sheets_identity_compares_shared_required_runtime_parameters_symbolically() -> None:
    staged = _workflow(
        """    - block_type: google_sheets_write
      label: first
      spreadsheet_url: '{{ sheet_url }}'
      sheet_name: '{{ sheet_name }}'
      credential_id: '{{ account }}'
      values: one
    - block_type: google_sheets_write
      label: second
      spreadsheet_url: '{{ sheet_url }}'
      sheet_name: '{{ sheet_name }}'
      credential_id: '{{ account }}'
      values: two
""",
        parameters="""
    - key: sheet_url
      parameter_type: workflow
      workflow_parameter_type: string
    - key: sheet_name
      parameter_type: workflow
      workflow_parameter_type: string
    - key: account
      parameter_type: workflow
      workflow_parameter_type: credential_id
""",
    )

    projection = build_review_projection(_workflow(""), staged, set())

    assert projection is not None
    assert projection["duplicateWrites"] == [{"blockType": "google_sheets_write", "blockLabels": ["first", "second"]}]


def test_sheets_identity_keeps_account_and_tab_distinct_but_not_range() -> None:
    staged = _workflow(
        """    - block_type: google_sheets_write
      label: first
      spreadsheet_url: https://docs.google.com/spreadsheets/d/shared-spreadsheet-123/edit?gid=1
      sheet_name: First
      range: A1:B
      credential_id: cred_a
      values: one
    - block_type: google_sheets_write
      label: other_account
      spreadsheet_url: https://docs.google.com/spreadsheets/d/shared-spreadsheet-123/edit?gid=1
      sheet_name: First
      range: A1:B
      credential_id: cred_b
      values: two
    - block_type: google_sheets_write
      label: other_tab
      spreadsheet_url: https://docs.google.com/spreadsheets/d/shared-spreadsheet-123/edit?gid=2
      sheet_name: Second
      range: A1:B
      credential_id: cred_a
      values: three
    - block_type: google_sheets_write
      label: other_range
      spreadsheet_url: https://docs.google.com/spreadsheets/d/shared-spreadsheet-123/edit?gid=1
      sheet_name: First
      range: D1:E
      credential_id: cred_a
      values: four
"""
    )

    projection = build_review_projection(_workflow(""), staged, set())

    assert projection is not None
    assert projection["duplicateWrites"] == [
        {
            "blockType": "google_sheets_write",
            "blockLabels": ["first", "other_range"],
        }
    ]


def test_sheets_identity_uses_a1_tab_and_ignores_url_gid() -> None:
    staged = _workflow(
        """    - block_type: google_sheets_write
      label: first
      spreadsheet_url: https://docs.google.com/spreadsheets/d/shared-spreadsheet-123/edit?gid=1
      range: First!A:C
      credential_id: cred_a
      values: one
    - block_type: google_sheets_write
      label: same_runtime_tab
      spreadsheet_url: https://docs.google.com/spreadsheets/d/shared-spreadsheet-123/edit?gid=999
      range: First!D:F
      credential_id: cred_a
      values: two
    - block_type: google_sheets_write
      label: other_runtime_tab
      spreadsheet_url: https://docs.google.com/spreadsheets/d/shared-spreadsheet-123/edit?gid=1
      range: Second!A:C
      credential_id: cred_a
      values: three
"""
    )

    projection = build_review_projection(_workflow(""), staged, set())

    assert projection is not None
    assert projection["duplicateWrites"] == [
        {
            "blockType": "google_sheets_write",
            "blockLabels": ["first", "same_runtime_tab"],
        }
    ]


def test_sheets_identity_uses_runtime_reference_forms_and_literal_credentials() -> None:
    sheet_id = "spreadsheet-reference-123456"
    staged = _workflow(
        f"""    - block_type: google_sheets_write
      label: url_form
      spreadsheet_url: https://docs.google.com/spreadsheets/u/0/d/{sheet_id}/edit
      sheet_name: Summary
      credential_id: '{{{{ sheets_account }}}}'
      values: one
    - block_type: google_sheets_write
      label: bare_id_form
      spreadsheet_url: {sheet_id}
      sheet_name: Summary
      credential_id: sheets_account
      values: two
""",
        parameters="""
    - key: sheets_account
      parameter_type: workflow
      workflow_parameter_type: credential_id
      default_value: cred_primary
""",
    )

    projection = build_review_projection(_workflow(""), staged, set())

    assert projection is not None
    assert projection["duplicateWrites"] == []


def test_sheets_identity_abstains_when_rich_input_selects_the_tab() -> None:
    staged = _workflow(
        """    - block_type: google_sheets_write
      label: rich_a
      spreadsheet_url: spreadsheet-reference-123456
      credential_id: cred_a
      values: '{{ rich_a }}'
    - block_type: google_sheets_write
      label: rich_b
      spreadsheet_url: spreadsheet-reference-123456
      credential_id: cred_a
      values: '{{ rich_b }}'
"""
    )

    projection = build_review_projection(_workflow(""), staged, set())

    assert projection is not None
    assert projection["duplicateWrites"] == []


def test_destination_template_errors_abstain_instead_of_breaking_review() -> None:
    staged = _workflow(
        """    - block_type: google_sheets_write
      label: malformed_destination
      spreadsheet_url: '{{ 1 / 0 }}'
      sheet_name: Summary
      credential_id: cred_a
      values: one
"""
    )

    projection = build_review_projection(_workflow(""), staged, set())

    assert projection is not None
    assert projection["duplicateWrites"] == []


def test_google_drive_blank_folder_is_the_my_drive_root() -> None:
    staged = _workflow(
        """    - block_type: file_upload
      label: upload_a
      storage_type: google_drive
      google_credential_id: cred_drive
      google_drive_folder_id:
      path: /tmp/a.csv
    - block_type: file_upload
      label: upload_b
      storage_type: google_drive
      google_credential_id: cred_drive
      google_drive_folder_id: ''
      path: /tmp/b.csv
"""
    )

    projection = build_review_projection(_workflow(""), staged, set())

    assert projection is not None
    assert projection["duplicateWrites"] == [{"blockType": "file_upload", "blockLabels": ["upload_a", "upload_b"]}]


def test_google_drive_identity_normalizes_bare_and_url_folder_references() -> None:
    staged = _workflow(
        """    - block_type: file_upload
      label: upload_a
      storage_type: google_drive
      google_credential_id: cred_drive
      google_drive_folder_id: folder_123
      path: /tmp/a.csv
    - block_type: file_upload
      label: upload_b
      storage_type: google_drive
      google_credential_id: cred_drive
      google_drive_folder_id: https://drive.google.com/drive/u/0/folders/folder_123
      path: /tmp/b.csv
"""
    )

    projection = build_review_projection(_workflow(""), staged, set())

    assert projection is not None
    assert projection["duplicateWrites"] == [{"blockType": "file_upload", "blockLabels": ["upload_a", "upload_b"]}]


def test_file_upload_identity_covers_s3_azure_and_sftp_destinations() -> None:
    staged = _workflow(
        """    - &s3
      block_type: file_upload
      label: s3_a
      storage_type: s3
      s3_bucket: reports
      aws_access_key_id: account_a
      aws_secret_access_key: secret_a
      path: exports
    - <<: *s3
      label: s3_b
    - &azure
      block_type: file_upload
      label: azure_a
      storage_type: azure
      azure_storage_account_name: reportaccount
      azure_storage_account_key: azure_secret
      azure_blob_container_name: reports
      path: exports
    - <<: *azure
      label: azure_b
    - &sftp
      block_type: file_upload
      label: sftp_a
      storage_type: sftp
      sftp_host: files.example.com
      sftp_username: uploader
      sftp_password: sftp_secret
      sftp_remote_path: /reports
    - <<: *sftp
      label: sftp_b
"""
    )

    projection = build_review_projection(_workflow(""), staged, set())

    assert projection is not None
    assert projection["duplicateWrites"] == [
        {"blockType": "file_upload", "blockLabels": ["s3_a", "s3_b"]},
        {"blockType": "file_upload", "blockLabels": ["azure_a", "azure_b"]},
        {"blockType": "file_upload", "blockLabels": ["sftp_a", "sftp_b"]},
    ]


def test_file_upload_identity_does_not_collide_across_storage_backends() -> None:
    staged = _workflow(
        """    - block_type: file_upload
      label: s3_upload
      storage_type: s3
      aws_access_key_id: files.example.com
      aws_secret_access_key: secret
      s3_bucket: "22"
      region_name: uploader
      path: /reports
    - block_type: file_upload
      label: sftp_upload
      storage_type: sftp
      sftp_host: files.example.com
      sftp_port: "22"
      sftp_username: uploader
      sftp_password: secret
      sftp_remote_path: /reports
"""
    )

    projection = build_review_projection(_workflow(""), staged, set())

    assert projection is not None
    assert projection["duplicateWrites"] == []


def test_execution_receipts_round_trip_for_durable_proposal_storage() -> None:
    receipts = {"step": {"version_b", "version_a"}}

    wire = serialize_execution_receipts(receipts)

    assert wire == {"step": ["version_a", "version_b"]}
    assert parse_execution_receipts(wire) == receipts
    assert parse_execution_receipts(serialize_execution_receipts({"step": set()})) == {"step": set()}
    assert parse_execution_receipts(serialize_execution_receipts({})) == {}


def test_run_scoped_write_outputs_abstain_from_duplicate_claims() -> None:
    staged = _workflow(
        """    - block_type: upload_to_s3
      label: upload_a
      path: /tmp/result.csv
    - block_type: upload_to_s3
      label: upload_b
      path: /tmp/result.csv
    - block_type: split_pdf
      label: split_a
      file_url: https://files.example/input.pdf
      prompt: split it
    - block_type: split_pdf
      label: split_b
      file_url: https://files.example/input.pdf
      prompt: split it
"""
    )

    projection = build_review_projection(_workflow(""), staged, set())

    assert projection is not None
    assert projection["duplicateWrites"] == []


def test_a_comment_only_edit_reports_the_prior_receipt_as_a_different_source() -> None:
    tested = _workflow("""    - block_type: code
      label: sign_in
      code: |
        await page.click("#submit")
""")
    edited = _workflow("""    - block_type: code
      label: sign_in
      code: |
        # retry the submit button
        await page.click("#submit")
""")

    projection = build_review_projection(tested, edited, workflow_block_fingerprints(tested))

    assert projection is not None
    assert projection["blocks"][0]["coverage"] == "different_source"
    assert projection["blocks"][0]["change"] == "changed"


def test_a_renamed_block_loses_its_receipt_while_siblings_keep_theirs() -> None:
    tested = _workflow("""    - block_type: task
      label: sign_in
      next_block_label: read_metric
      prompt: Sign in
    - block_type: task
      label: read_metric
      prompt: Read it
""")
    renamed = _workflow("""    - block_type: task
      label: sign_in_v2
      next_block_label: read_metric
      prompt: Sign in
    - block_type: task
      label: read_metric
      prompt: Read it
""")

    projection = build_review_projection(tested, renamed, workflow_block_fingerprints(tested))

    assert projection is not None
    assert [(row["label"], row.get("coverage")) for row in projection["blocks"]] == [
        ("sign_in_v2", "unknown"),
        ("read_metric", "current_source"),
        ("sign_in", None),
    ]


def test_editing_one_of_two_blocks_leaves_the_untouched_block_current() -> None:
    tested = _workflow("""    - block_type: task
      label: sign_in
      next_block_label: read_metric
      prompt: Sign in
    - block_type: task
      label: read_metric
      prompt: Read it
""")
    edited = tested.replace("prompt: Read it", "prompt: Read it twice")

    projection = build_review_projection(tested, edited, workflow_block_fingerprints(tested))

    assert projection is not None
    assert [(row["label"], row["coverage"]) for row in projection["blocks"]] == [
        ("sign_in", "current_source"),
        ("read_metric", "different_source"),
    ]


def test_tested_block_fingerprint_ignores_the_goal_ownership_facts() -> None:
    hand_edited = _top_level(
        _READ_PRICE_BLOCK + "  user_owned_goal: true\n  goal_needs_regeneration: false\n  code_edited_by_hand: true\n"
    )
    resubmitted = _top_level(_READ_PRICE_BLOCK)

    assert workflow_block_fingerprints(hand_edited) == workflow_block_fingerprints(resubmitted)
    projection = build_review_projection(hand_edited, resubmitted, workflow_block_fingerprints(hand_edited))
    assert projection is not None
    assert [(block["change"], block["coverage"]) for block in projection["blocks"]] == [("unchanged", "current_source")]


_CAPTURE_DIR = Path(__file__).parent / "fixtures" / "review_projection_capture"


def _changes(projection: NarrativeReviewProjection | None) -> dict[str, str]:
    assert projection is not None
    return {block["label"]: block["change"] for block in projection["blocks"]}


def test_a_canvas_spelled_proposal_marks_only_the_edited_block_changed() -> None:
    persisted = (_CAPTURE_DIR / "persisted.yaml").read_text()
    staged = (_CAPTURE_DIR / "staged.yaml").read_text()

    projection = build_review_projection(persisted, staged, {})

    assert _changes(projection) == {"read_statement_number": "unchanged", "read_amount_due": "changed"}


def test_blank_storage_fields_from_the_canvas_leave_an_upload_block_unchanged() -> None:
    capture = Path(__file__).parent / "fixtures" / "review_projection_capture_file_upload"
    persisted = (capture / "persisted.yaml").read_text()
    staged = (capture / "staged.yaml").read_text()

    assert _changes(build_review_projection(persisted, staged, {})) == {
        "open_vendor_hub": "unchanged",
        "read_certificate": "changed",
        "store_certificate": "unchanged",
    }
    credentialed = staged.replace("aws_access_key_id: ''", "aws_access_key_id: '{{ aws_key }}'")
    assert _changes(build_review_projection(persisted, credentialed, {}))["store_certificate"] == "changed"
    cleared = staged.replace("s3_bucket: qa-capture-bucket", "s3_bucket: ''")
    assert _changes(build_review_projection(persisted, cleared, {}))["store_certificate"] == "changed"
    unpinned = persisted.replace("code_version: 2\n", "")
    assert set(_changes(build_review_projection(unpinned, staged, {})).values()) == {"changed"}


def test_an_editor_staged_code_edit_leaves_an_unset_upload_block_unchanged() -> None:
    capture = Path(__file__).parent / "fixtures" / "review_projection_capture_code_arm"
    persisted = (capture / "persisted.yaml").read_text()
    staged = (capture / "staged.yaml").read_text()

    assert _changes(build_review_projection(persisted, staged, {})) == {
        "open_vendor_hub": "unchanged",
        "read_certificate": "changed",
        "upload_certificate": "unchanged",
    }
    bucketed = staged.replace("s3_bucket: ''", "s3_bucket: qa-bucket")
    assert _changes(build_review_projection(persisted, bucketed, {}))["upload_certificate"] == "changed"


def test_a_blank_task_url_still_differs_from_an_unset_one() -> None:
    task = "    - block_type: task\n      label: inherit_page\n      navigation_goal: Open the reports page\n"

    projection = build_review_projection(_workflow(task), _workflow(task + "      url: ''\n"), {})

    assert _changes(projection) == {"inherit_page": "changed"}


def test_an_explicit_workflow_setting_change_still_marks_every_block_changed() -> None:
    persisted = (_CAPTURE_DIR / "persisted.yaml").read_text()
    staged = (
        (_CAPTURE_DIR / "staged.yaml")
        .read_text()
        .replace("mask_secrets: false", "mask_secrets: false\nenable_self_healing: true")
    )

    projection = build_review_projection(persisted, staged, {})

    assert set(_changes(projection).values()) == {"changed"}


@pytest.mark.parametrize(("draft_mask_secrets", "expected"), [("false", "changed"), ("true", "unchanged")])
@pytest.mark.asyncio
@pytest.mark.usefixtures("no_saved_workflow")
async def test_a_surfaced_draft_reads_omitted_settings_from_the_draft_it_inherits(
    draft_mask_secrets: str, expected: str
) -> None:
    blocks = """    - block_type: navigation
      label: open_portal
      next_block_label: read_total
      url: https://example.com
      navigation_goal: Open the portal
    - block_type: extraction
      label: read_total
      data_extraction_goal: Read the total
"""
    proposal = _workflow(blocks)
    saved = await _process_workflow_yaml("wf-1", "wfp-1", "org-1", proposal + "mask_secrets: true\n")
    candidate = await _process_workflow_yaml(
        "wf-1", "wfp-1", "org-1", proposal, settings_fallback_yaml=proposal + f"mask_secrets: {draft_mask_secrets}\n"
    )
    ctx = make_copilot_ctx(
        persisted_workflow_yaml=workflow_to_copilot_yaml(saved),
        last_workflow=candidate,
        last_workflow_yaml=proposal,
        request_policy=RequestPolicy(allow_update_workflow=True, allow_run_blocks=True),
        stream=None,
    )
    result = SimpleNamespace(
        final_output=json.dumps({"type": "REPLY", "user_response": "Here is the workflow."}),
        new_items=[],
    )
    chat_request = SimpleNamespace(
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_copilot_chat_id="chat-1",
        workflow_run_id=None,
        workflow_yaml="",
        product_action=None,
    )

    agent_result = await agent_module._translate_to_agent_result(
        result, ctx, global_llm_context=None, chat_request=chat_request, organization_id="org-1"
    )

    assert agent_result.narrative_payload is not None
    assert _changes(agent_result.narrative_payload.get("review")) == {"open_portal": expected, "read_total": expected}


_READER_AND_BYSTANDER = """    - block_type: navigation
      label: reader
      next_block_label: bystander
      navigation_goal: Search for {{ account }}
    - block_type: navigation
      label: bystander
      navigation_goal: Open the reports page
"""
_ACCOUNT = "{key: account, parameter_type: workflow, workflow_parameter_type: string, default_value: primary}"
_OTHER = "{key: region, parameter_type: workflow, workflow_parameter_type: string, default_value: east}"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (f"[{_OTHER}]", f"[{_OTHER}, {_ACCOUNT}]"),
        (f"[{_OTHER}, {_ACCOUNT}]", f"[{_OTHER}]"),
        (f"[{_ACCOUNT}]", f"[{_ACCOUNT.replace('primary', 'backup')}]"),
    ],
    ids=["added", "removed", "changed"],
)
def test_an_input_change_marks_only_the_blocks_that_read_it(before: str, after: str) -> None:
    persisted = _workflow(_READER_AND_BYSTANDER, parameters=before)
    staged = _workflow(_READER_AND_BYSTANDER, parameters=after)

    assert _changes(build_review_projection(persisted, staged, {})) == {"reader": "changed", "bystander": "unchanged"}


def test_an_input_description_edit_lists_the_input_but_marks_no_block() -> None:
    persisted = _workflow(_READER_AND_BYSTANDER, parameters=f"[{_ACCOUNT}]")
    staged = _workflow(_READER_AND_BYSTANDER, parameters=f"[{_ACCOUNT[:-1]}, description: The account to search}}]")

    projection = build_review_projection(persisted, staged, {})

    assert projection is not None
    assert projection["inputs"] == [{"key": "account", "change": "changed"}]
    assert _changes(projection) == {"reader": "unchanged", "bystander": "unchanged"}


def _watched(default: str) -> str:
    return f"{{key: watched, parameter_type: workflow, workflow_parameter_type: string, default_value: {default}}}"


_BYSTANDER = "    - block_type: navigation\n      label: bystander\n      navigation_goal: Open the reports page\n"


@pytest.mark.parametrize(
    ("reader", "extra_parameter"),
    [
        (
            "    - block_type: login\n      label: reader\n      next_block_label: bystander\n"
            "      navigation_goal: Sign in as {{ watched_real_username }}\n",
            "",
        ),
        (
            "    - block_type: for_loop\n      label: reader\n      next_block_label: bystander\n"
            "      loop_over_parameter_key: watched\n      loop_blocks:\n"
            "        - block_type: navigation\n          label: visit\n          navigation_goal: Open the row\n",
            "",
        ),
        (
            "    - block_type: goto_url\n      label: reader\n      next_block_label: bystander\n      url: watched\n",
            "",
        ),
        (
            "    - block_type: navigation\n      label: reader\n      next_block_label: bystander\n"
            "      navigation_goal: Read {{ 'a' ~ watched | json }}\n",
            "",
        ),
        (
            "    - block_type: login\n      label: reader\n      next_block_label: bystander\n"
            "      navigation_goal: Sign in\n      parameter_keys: [chosen_login]\n",
            ", {key: chosen_login, parameter_type: credential, credential_id: '{{ watched }}'}",
        ),
        (
            "    - block_type: conditional\n      label: reader\n      branch_conditions:\n"
            "        - criteria: {criteria_type: prompt, expression: 'Is the page ready?'}\n"
            "          next_block_label: bystander\n"
            "        - is_default: true\n          next_block_label: bystander\n",
            "",
        ),
    ],
    ids=[
        "credential-real-suffix",
        "loop-over-key",
        "bare-name-url",
        "json-filter-second-operand",
        "credential-parameter-bound-to-input",
        "natural-language-condition",
    ],
)
def test_a_block_reading_the_changed_input_is_changed(reader: str, extra_parameter: str) -> None:
    persisted = _workflow(reader + _BYSTANDER, parameters=f"[{_watched('before')}{extra_parameter}]")
    staged = _workflow(reader + _BYSTANDER, parameters=f"[{_watched('after')}{extra_parameter}]")

    changes = _changes(build_review_projection(persisted, staged, {}))

    assert changes["reader"] == "changed"
    assert changes["bystander"] == "unchanged"


def test_an_input_named_like_a_jinja_global_is_still_read() -> None:
    blocks = _READER_AND_BYSTANDER.replace("{{ account }}", "{{ dict }}")
    dict_input = "{key: dict, parameter_type: workflow, workflow_parameter_type: string, default_value: DEFAULT}"

    projection = build_review_projection(
        _workflow(blocks, parameters=f"[{dict_input.replace('DEFAULT', 'a')}]"),
        _workflow(blocks, parameters=f"[{dict_input.replace('DEFAULT', 'b')}]"),
        {},
    )

    assert _changes(projection) == {"reader": "changed", "bystander": "unchanged"}


def test_a_neighbours_note_that_names_the_input_marks_both_readers_changed() -> None:
    blocks = """    - block_type: navigation
      label: above
      next_block_label: below
      navigation_goal: First
    - block_type: navigation
      label: below
      next_block_label: elsewhere
      navigation_goal: Second
      notes: Use the {{ watched }} account
    - block_type: navigation
      label: elsewhere
      navigation_goal: Third
"""

    projection = build_review_projection(
        _workflow(blocks, parameters=f"[{_watched('before')}]"),
        _workflow(blocks, parameters=f"[{_watched('after')}]"),
        {},
    )

    assert _changes(projection) == {"above": "changed", "below": "changed", "elsewhere": "unchanged"}


@pytest.mark.parametrize(
    ("blocks", "parameters_before", "parameters_after"),
    [
        (
            "    - block_type: navigation\n      label: quiet\n      navigation_goal: Open the page\n"
            "      description: Mentions watched by name\n",
            f"[{_watched('a')}]",
            f"[{_watched('b')}]",
        ),
        (
            "    - block_type: goto_url\n      label: quiet\n      url: https://example.com/start\n",
            "[]",
            "[{key: url, parameter_type: workflow, workflow_parameter_type: string}]",
        ),
        (
            "    - block_type: navigation\n      label: quiet\n      navigation_goal: Find {{ customer_name }}\n",
            "[{key: customer_name, parameter_type: workflow, workflow_parameter_type: string}]",
            "[{key: customer_name, parameter_type: workflow, workflow_parameter_type: string}, "
            "{key: string, parameter_type: workflow, workflow_parameter_type: string}]",
        ),
        (
            "    - block_type: conditional\n      label: quiet\n      branch_conditions:\n"
            "        - criteria: {criteria_type: jinja2_template, expression: \"{{ mode == 'fast' }}\"}\n"
            "          next_block_label: after\n"
            "        - is_default: true\n          next_block_label: after\n"
            "    - block_type: navigation\n      label: after\n      navigation_goal: Continue\n",
            f"[{{key: mode, parameter_type: workflow, workflow_parameter_type: string, default_value: fast}}, {_watched('a')}]",
            f"[{{key: mode, parameter_type: workflow, workflow_parameter_type: string, default_value: fast}}, {_watched('b')}]",
        ),
    ],
    ids=["description-only", "input-named-like-a-field", "input-named-like-a-type", "template-condition"],
)
def test_a_block_that_does_not_read_the_changed_input_stays_unchanged(
    blocks: str, parameters_before: str, parameters_after: str
) -> None:
    projection = build_review_projection(
        _workflow(blocks, parameters=parameters_before), _workflow(blocks, parameters=parameters_after), {}
    )

    assert projection is not None and projection["inputs"]
    assert set(_changes(projection).values()) == {"unchanged"}


@pytest.mark.parametrize(
    "setting",
    [
        "  workflow_system_prompt: Always act for {{ watched }}\n",
        "  error_code_mapping: {MISSING: 'The {{ watched }} record is missing'}\n",
    ],
    ids=["system-prompt", "error-code-mapping"],
)
def test_an_input_a_workflow_setting_reads_marks_every_block_changed(setting: str) -> None:
    blocks = _READER_AND_BYSTANDER.replace("{{ account }}", "the customer")

    def workflow(default: str) -> str:
        return (
            "title: Review fixture\nworkflow_definition:\n"
            f"{setting}"
            f"  parameters: [{_watched(default)}]\n  blocks:\n{blocks}"
        )

    assert set(_changes(build_review_projection(workflow("a"), workflow("b"), {})).values()) == {"changed"}


def test_an_input_a_root_setting_reads_marks_every_block_changed() -> None:
    blocks = _READER_AND_BYSTANDER.replace("{{ account }}", "the customer")

    def workflow(default: str) -> str:
        return (
            "title: Review fixture\nbrowser_profile_key: '{{ watched }}'\nworkflow_definition:\n"
            f"  parameters: [{_watched(default)}]\n  blocks:\n{blocks}"
        )

    assert set(_changes(build_review_projection(workflow("a"), workflow("b"), {})).values()) == {"changed"}


def test_an_explicit_null_setting_inherits_like_an_omitted_one() -> None:
    persisted = "mask_secrets: true\n" + _workflow(_READER_AND_BYSTANDER)
    staged = "mask_secrets: null\n" + _workflow(_READER_AND_BYSTANDER)

    assert set(_changes(build_review_projection(persisted, staged, {})).values()) == {"unchanged"}


def test_binding_a_stored_code_block_to_the_input_its_code_reads_is_a_change() -> None:
    def workflow(parameter_keys: str) -> str:
        return _workflow(
            "    - block_type: code\n      label: compute\n      next_block_label: bystander\n"
            f'      code: "result = watched"\n{parameter_keys}' + _BYSTANDER,
            parameters=f"[{_watched('a')}]",
        )

    projection = build_review_projection(workflow(""), workflow("      parameter_keys: [watched]\n"), {})

    assert _changes(projection) == {"compute": "changed", "bystander": "unchanged"}


def test_a_loop_over_the_changed_input_marks_its_children_changed() -> None:
    blocks = (
        "    - block_type: for_loop\n      label: each_row\n      next_block_label: bystander\n"
        "      loop_over_parameter_key: watched\n      loop_blocks:\n"
        "        - block_type: goto_url\n          label: visit\n          url: '{{ current_value.url }}'\n"
        + _BYSTANDER
    )

    projection = build_review_projection(
        _workflow(blocks, parameters=f"[{_watched('before')}]"),
        _workflow(blocks, parameters=f"[{_watched('after')}]"),
        {},
    )

    assert _changes(projection) == {"each_row": "changed", "visit": "changed", "bystander": "unchanged"}


_THREE_STEPS = """    - block_type: navigation
      label: first
      next_block_label: middle
      navigation_goal: First step
    - block_type: navigation
      label: middle
      next_block_label: last
      navigation_goal: Middle step
    - block_type: navigation
      label: last
      navigation_goal: Last step
"""


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("block_type: navigation\n      label: middle", "block_type: task\n      label: middle"),
        ("navigation_goal: Middle step", "navigation_goal: Middle step\n      max_retries: 2"),
        ("navigation_goal: Middle step", "navigation_goal: A different middle step"),
    ],
    ids=["type", "setting", "prompt"],
)
def test_a_real_edit_to_a_block_marks_only_that_block_changed(old: str, new: str) -> None:
    persisted = _workflow(_THREE_STEPS)

    projection = build_review_projection(persisted, persisted.replace(old, new), {})

    assert _changes(projection) == {"first": "unchanged", "middle": "changed", "last": "unchanged"}


def test_moving_a_block_marks_it_changed() -> None:
    persisted = _workflow(_THREE_STEPS)
    moved = _workflow("""    - block_type: navigation
      label: middle
      next_block_label: first
      navigation_goal: Middle step
    - block_type: navigation
      label: first
      next_block_label: last
      navigation_goal: First step
    - block_type: navigation
      label: last
      navigation_goal: Last step
""")

    changes = _changes(build_review_projection(persisted, moved, {}))

    assert changes["middle"] == "changed" and changes["last"] == "unchanged"


def test_a_code_edit_marks_only_the_code_block_changed() -> None:
    persisted = _workflow("""    - block_type: goto_url
      label: open_portal
      next_block_label: compute
      url: https://example.com/portal
    - block_type: code
      label: compute
      next_block_label: upload
      code: "result = 1"
    - block_type: file_upload
      label: upload
      storage_type: s3
      s3_bucket: reports
      region_name: us-east-1
      aws_access_key_id: key
      aws_secret_access_key: secret
""")

    projection = build_review_projection(persisted, persisted.replace("result = 1", "result = 2"), {})

    assert _changes(projection) == {"open_portal": "unchanged", "compute": "changed", "upload": "unchanged"}


_THIRTEEN_LABELS = [
    "open_portal",
    "accept_terms",
    "sign_in",
    "find_account",
    "open_statements",
    "pick_period",
    "read_balance",
    "read_due_date",
    "download_pdf",
    "rename_file",
    "summarize",
    "notify_team",
    "sign_out",
]


def _thirteen_block_yaml(goals: dict[str, str], parameters: str) -> str:
    blocks = ""
    for index, label in enumerate(_THIRTEEN_LABELS):
        blocks += f"    - block_type: navigation\n      label: {label}\n"
        if index + 1 < len(_THIRTEEN_LABELS):
            blocks += f"      next_block_label: {_THIRTEEN_LABELS[index + 1]}\n"
        blocks += f"      navigation_goal: {goals.get(label, f'Do the {label} step for {{{{ account }}}}')}\n"
    return _workflow(blocks, parameters=parameters)


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_saved_workflow")
async def test_thirteen_saved_blocks_with_three_edits_and_an_unread_input_mark_exactly_three_changed() -> None:
    account = "{key: account, parameter_type: workflow, workflow_parameter_type: string, default_value: primary}"
    unread = "{key: unread_flag, parameter_type: workflow, workflow_parameter_type: string, default_value: off}"
    edits = {
        label: f"Redo the {label} step for {{{{ account }}}}"
        for label in ("pick_period", "download_pdf", "notify_team")
    }
    before = await _process_workflow_yaml(
        "w_before", "wpid_fixture", "o_fixture", _thirteen_block_yaml({}, f"[{account}]")
    )
    after = await _process_workflow_yaml(
        "w_after", "wpid_fixture", "o_fixture", _thirteen_block_yaml(edits, f"[{account}, {unread}]")
    )

    projection = build_review_projection(workflow_to_copilot_yaml(before), workflow_to_copilot_yaml(after), {})

    changes = _changes(projection)
    assert projection is not None and projection["inputs"] == [{"key": "unread_flag", "change": "added"}]
    assert sorted(label for label, change in changes.items() if change == "changed") == sorted(edits)
    assert list(changes.values()).count("unchanged") == 10
