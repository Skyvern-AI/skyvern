from __future__ import annotations

import json

from skyvern.forge.sdk.copilot.code_block_steps import derive_code_block_steps_in_yaml
from skyvern.forge.sdk.copilot.data_write_defaults import DATA_WRITE_BLOCK_TYPES
from skyvern.forge.sdk.copilot.review_gate import (
    DESTINATION_ADAPTERS,
    build_review_projection,
    parse_execution_receipts,
    serialize_execution_receipts,
    workflow_block_fingerprints,
)


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
      prompt: Keep this
    - block_type: task
      label: changed_step
      next_block_label: removed_step
      prompt: Before
    - block_type: task
      label: removed_step
      prompt: Remove me
"""
    )
    staged = _workflow(
        """    - block_type: task
      label: unchanged_step
      next_block_label: changed_step
      prompt: Keep this
    - block_type: task
      label: changed_step
      next_block_label: added_step
      prompt: After
    - block_type: task
      label: added_step
      prompt: New
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
        },
        {"label": "changed_step", "blockType": "task", "change": "changed", "neverTested": False},
        {"label": "added_step", "blockType": "task", "change": "added", "neverTested": True},
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
    }


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
