"""Block-scoped edits: the model changes one block instead of retyping the workflow.

OSS-synced: RFC-2606 example.* only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.workflow_yaml import (
    BlockEditError,
    apply_block_edit,
    delete_block_from_workflow,
)

_WORKFLOW = """title: Lookup
workflow_definition:
  blocks:
    - block_type: code
      label: open_portal
      code: |
        await page.goto("https://example.test/")
      next_block_label: read_total
    - block_type: code
      label: read_total
      code: |
        total = await page.inner_text("#total")
        return {"total": total}
"""


class TestAnchoredCodeEdit:
    def test_edits_only_the_named_block(self) -> None:
        out = apply_block_edit(_WORKFLOW, "read_total", expected_code='"#total"', replacement_code='"#grand-total"')
        assert "#grand-total" in out
        # the untouched block survives byte-for-byte
        assert 'await page.goto("https://example.test/")' in out

    def test_a_stale_anchor_fails_instead_of_overwriting(self) -> None:
        """The property the whole design turns on: an edit written against a copy of the block that
        has since changed must be refused, not applied over whatever is there now."""
        with pytest.raises(BlockEditError) as exc:
            apply_block_edit(_WORKFLOW, "read_total", expected_code='"#stale"', replacement_code='"#x"')
        assert "changed since you read it" in str(exc.value)

    def test_an_ambiguous_anchor_is_refused(self) -> None:
        workflow = _WORKFLOW.replace(
            'total = await page.inner_text("#total")',
            'a = await page.inner_text("#c")\n        b = await page.inner_text("#c")',
        )
        with pytest.raises(BlockEditError) as exc:
            apply_block_edit(workflow, "read_total", expected_code='"#c"', replacement_code='"#d"')
        assert "appears 2 times" in str(exc.value)

    def test_a_half_specified_code_edit_is_refused(self) -> None:
        with pytest.raises(BlockEditError):
            apply_block_edit(_WORKFLOW, "read_total", expected_code="total")

    def test_unknown_label_names_what_exists(self) -> None:
        with pytest.raises(BlockEditError) as exc:
            apply_block_edit(_WORKFLOW, "ghost", fields={"code": "x"})
        assert "open_portal" in str(exc.value) and "read_total" in str(exc.value)


class TestFieldEdit:
    def test_sets_only_the_named_fields(self) -> None:
        out = apply_block_edit(_WORKFLOW, "open_portal", fields={"continue_on_failure": True})
        assert "continue_on_failure: true" in out
        assert "read_total" in out


class TestDelete:
    def test_removes_the_block_and_unlinks_what_pointed_at_it(self) -> None:
        out = delete_block_from_workflow(_WORKFLOW, "read_total")
        assert "label: read_total" not in out
        assert "label: open_portal" in out
        assert "next_block_label: null" in out, "a block pointing at the deleted one must be unlinked"

    def test_deleting_an_absent_block_is_an_error_not_a_no_op(self) -> None:
        """Deletion is an operation. Silently succeeding would repeat the failure mode where a block
        left out of a submission could not be told apart from one meant to be removed."""
        with pytest.raises(BlockEditError):
            delete_block_from_workflow(_WORKFLOW, "never_existed")

    def test_a_deleted_block_stays_deleted_when_the_result_is_re_applied(self) -> None:
        once = delete_block_from_workflow(_WORKFLOW, "read_total")
        with pytest.raises(BlockEditError):
            delete_block_from_workflow(once, "read_total")


class TestWholeWorkflowSubmissionIsSteeredToBlockEdits:
    """A repair that re-sends every block to change some of them is the cycle SKY-13133 exists to
    stop. Structural changes still need the whole workflow, since edit_block only reaches blocks
    that already exist — so the discriminator is whether the set of labels changed."""

    @staticmethod
    def _ctx(stored: str) -> SimpleNamespace:
        return SimpleNamespace(last_workflow_yaml=stored, workflow_yaml=stored)

    def test_content_only_change_is_steered(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _submission_only_changes_existing_blocks

        changed = _WORKFLOW.replace('"#total"', '"#grand-total"')
        assert _submission_only_changes_existing_blocks(self._ctx(_WORKFLOW), changed) is True

    def test_adding_a_block_still_goes_through(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _submission_only_changes_existing_blocks

        added = (
            _WORKFLOW
            + """    - block_type: code
      label: save_result
      code: |
        return {"saved": True}
"""
        )
        assert _submission_only_changes_existing_blocks(self._ctx(_WORKFLOW), added) is False

    def test_removing_a_block_still_goes_through(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _submission_only_changes_existing_blocks

        removed = delete_block_from_workflow(_WORKFLOW, "read_total")
        assert _submission_only_changes_existing_blocks(self._ctx(_WORKFLOW), removed) is False

    def test_first_draft_is_never_steered(self) -> None:
        """There is nothing to edit on the creating turn, so the whole workflow is the only option."""
        from skyvern.forge.sdk.copilot.tools import _submission_only_changes_existing_blocks

        assert _submission_only_changes_existing_blocks(self._ctx(""), _WORKFLOW) is False

    def test_unparseable_input_is_never_steered(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _submission_only_changes_existing_blocks

        assert _submission_only_changes_existing_blocks(self._ctx(_WORKFLOW), "{{ not yaml") is False
