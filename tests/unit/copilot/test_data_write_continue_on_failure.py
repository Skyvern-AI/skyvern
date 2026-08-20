from __future__ import annotations

import textwrap
from typing import Any

from skyvern.forge.sdk.copilot.data_write_defaults import (
    DATA_WRITE_BLOCK_TYPES,
    default_data_write_continue_on_failure,
)
from skyvern.forge.sdk.copilot.workflow_credential_utils import parse_workflow_yaml, workflow_blocks


def _continue_on_failure_by_label(workflow_yaml: str) -> dict[str, Any]:
    parsed = parse_workflow_yaml(workflow_yaml)
    assert isinstance(parsed, dict)
    return {block.get("label"): block.get("continue_on_failure") for block in workflow_blocks(parsed)}


def _wf(blocks_yaml: str) -> str:
    return "workflow_definition:\n  blocks:\n" + textwrap.indent(textwrap.dedent(blocks_yaml).strip() + "\n", "  ")


def test_new_data_write_block_with_true_is_forced_false() -> None:
    new_yaml = _wf(
        """
        - block_type: google_sheets_write
          label: write_rows
          continue_on_failure: true
        """
    )
    result = default_data_write_continue_on_failure(new_yaml, None)
    assert _continue_on_failure_by_label(result)["write_rows"] is False


def test_existing_explicit_true_is_preserved() -> None:
    saved = _wf(
        """
        - block_type: google_sheets_write
          label: write_rows
          continue_on_failure: true
        """
    )
    # Same label re-submitted (e.g. an unrelated edit elsewhere) keeps the user's value.
    result = default_data_write_continue_on_failure(saved, saved)
    assert _continue_on_failure_by_label(result)["write_rows"] is True
    assert result == saved  # untouched -> no re-dump


def test_non_write_block_is_untouched() -> None:
    new_yaml = _wf(
        """
        - block_type: google_sheets_read
          label: read_rows
          continue_on_failure: true
        """
    )
    result = default_data_write_continue_on_failure(new_yaml, None)
    assert result == new_yaml
    assert _continue_on_failure_by_label(result)["read_rows"] is True


def test_nested_loop_blocks_and_branches_are_recursed() -> None:
    new_yaml = _wf(
        """
        - block_type: for_loop
          label: loop
          loop_blocks:
          - block_type: file_upload
            label: upload_in_loop
            continue_on_failure: true
        - block_type: conditional
          label: cond
          branches:
          - blocks:
            - block_type: google_sheets_write
              label: write_in_branch
              continue_on_failure: true
        """
    )
    by_label = _continue_on_failure_by_label(default_data_write_continue_on_failure(new_yaml, None))
    assert by_label["upload_in_loop"] is False
    assert by_label["write_in_branch"] is False


def test_no_prior_forces_all_new_write_blocks_false() -> None:
    new_yaml = _wf(
        """
        - block_type: upload_to_s3
          label: up
          continue_on_failure: true
        - block_type: download_to_s3
          label: down
          continue_on_failure: true
        """
    )
    by_label = _continue_on_failure_by_label(default_data_write_continue_on_failure(new_yaml, None))
    assert by_label["up"] is False
    assert by_label["down"] is False


def test_absent_continue_on_failure_is_passthrough_no_redump() -> None:
    new_yaml = _wf(
        """
        - block_type: google_sheets_write
          label: write_rows
        """
    )
    # Already-safe default (absent) must not trigger a YAML round-trip.
    assert default_data_write_continue_on_failure(new_yaml, None) == new_yaml


def test_new_write_block_added_to_existing_workflow_is_defaulted() -> None:
    prior = _wf(
        """
        - block_type: google_sheets_write
          label: existing_write
          continue_on_failure: true
        """
    )
    new_yaml = _wf(
        """
        - block_type: google_sheets_write
          label: existing_write
          continue_on_failure: true
        - block_type: file_upload
          label: brand_new_upload
          continue_on_failure: true
        """
    )
    by_label = _continue_on_failure_by_label(default_data_write_continue_on_failure(new_yaml, prior))
    assert by_label["existing_write"] is True  # untouched
    assert by_label["brand_new_upload"] is False  # new -> defaulted


def test_same_turn_recreated_block_is_redefaulted_not_preserved() -> None:
    # The prior is the staged draft after the copilot already defaulted this block to
    # false earlier in the turn; a re-emitted true must be re-defaulted, not preserved.
    prior = _wf(
        """
        - block_type: google_sheets_write
          label: write_rows
          continue_on_failure: false
        """
    )
    new_yaml = _wf(
        """
        - block_type: google_sheets_write
          label: write_rows
          continue_on_failure: true
        """
    )
    result = _continue_on_failure_by_label(default_data_write_continue_on_failure(new_yaml, prior))
    assert result["write_rows"] is False


def test_malformed_yaml_returns_input_unchanged() -> None:
    assert default_data_write_continue_on_failure("not: [valid", None) == "not: [valid"
    assert default_data_write_continue_on_failure("", None) == ""


def test_coexisting_code_block_content_survives_redump() -> None:
    new_yaml = _wf(
        """
        - block_type: code
          label: compute
          code: |
            x = 1
            return {"x": x}
        - block_type: google_sheets_write
          label: write_rows
          continue_on_failure: true
        """
    )
    result = default_data_write_continue_on_failure(new_yaml, None)
    parsed = parse_workflow_yaml(result)
    assert isinstance(parsed, dict)
    by_label = {block.get("label"): block for block in workflow_blocks(parsed)}
    assert by_label["write_rows"].get("continue_on_failure") is False
    assert by_label["compute"].get("code") == 'x = 1\nreturn {"x": x}\n'


def test_data_write_set_excludes_reads_and_notifications() -> None:
    assert "google_sheets_read" not in DATA_WRITE_BLOCK_TYPES
    assert "send_email" not in DATA_WRITE_BLOCK_TYPES
    assert "http_request" not in DATA_WRITE_BLOCK_TYPES
    assert "google_sheets_write" in DATA_WRITE_BLOCK_TYPES
    assert "split_pdf" in DATA_WRITE_BLOCK_TYPES


def test_split_pdf_block_with_true_is_forced_false() -> None:
    new_yaml = _wf(
        """
        - block_type: split_pdf
          label: split_docs
          continue_on_failure: true
        """
    )
    result = default_data_write_continue_on_failure(new_yaml, None)
    assert _continue_on_failure_by_label(result)["split_docs"] is False
