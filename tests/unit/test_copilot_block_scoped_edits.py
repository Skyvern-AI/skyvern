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
    stored_block_code,
    stored_workflow_yaml,
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

    def test_a_failed_anchor_carries_the_current_code(self) -> None:
        """Without the current code in hand the cheapest next move is resending the same edit, which
        a repeated-failure loop guard then counts as being stuck."""
        with pytest.raises(BlockEditError) as exc:
            apply_block_edit(_WORKFLOW, "read_total", expected_code='"#stale"', replacement_code='"#x"')
        assert 'total = await page.inner_text("#total")' in str(exc.value)

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


class TestStoredBlockCode:
    """What a surface must show the model so its next anchor is not written against a stale copy."""

    def test_returns_the_text_an_anchor_is_matched_against(self) -> None:
        code = stored_block_code(_WORKFLOW, "read_total")
        assert code is not None
        applied = apply_block_edit(_WORKFLOW, "read_total", expected_code=code, replacement_code='return {"total": 1}')
        assert 'return {"total": 1}' in applied

    def test_follows_the_rewrite_a_repair_cycle_applied(self) -> None:
        rewritten = _WORKFLOW.replace('"#total"', '"#grand-total"')
        ctx = SimpleNamespace(last_workflow_yaml=rewritten, workflow_yaml=_WORKFLOW)
        code = stored_block_code(stored_workflow_yaml(ctx), "read_total")
        assert code is not None and "#grand-total" in code
        assert '#total"' not in code

    def test_falls_back_to_the_turns_draft_before_any_write(self) -> None:
        ctx = SimpleNamespace(last_workflow_yaml=None, workflow_yaml=_WORKFLOW)
        assert stored_block_code(stored_workflow_yaml(ctx), "open_portal") is not None

    @pytest.mark.parametrize(
        ("stored", "label"),
        [
            (_WORKFLOW, "ghost"),
            (_WORKFLOW, ""),
            ("{{ not yaml", "read_total"),
            ("", "read_total"),
            (_WORKFLOW.replace("      code: |\n        total", "      x: |\n        total"), "read_total"),
        ],
    )
    def test_says_nothing_rather_than_guessing(self, stored: str, label: str) -> None:
        assert stored_block_code(stored, label) is None

    def test_a_duplicated_label_resolves_to_nothing(self) -> None:
        """apply_block_edit refuses a duplicated label, so showing one of the two would be a copy no
        edit can be anchored against."""
        duplicated = _WORKFLOW + _WORKFLOW.split("blocks:\n")[1]
        assert stored_block_code(duplicated, "read_total") is None


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
