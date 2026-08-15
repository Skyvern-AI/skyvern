"""Block-scoped edits: the model changes one block instead of retyping the workflow.

OSS-synced: RFC-2606 example.* only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from skyvern.forge.sdk.copilot.tools.workflow_update import _code_block_safety_errors
from skyvern.forge.sdk.copilot.workflow_yaml import (
    BlockEditError,
    add_block_to_workflow,
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


def _blocks(workflow: str) -> dict[str, dict]:
    parsed = yaml.safe_load(workflow)
    return {b["label"]: b for b in parsed["workflow_definition"]["blocks"]}


_NEW_BLOCK = """block_type: code
label: check_pages
code: |
  for path in page_paths:
      await page.goto(path)
"""


class TestAddBlock:
    def test_splices_after_the_named_block_and_relinks_the_chain(self) -> None:
        out = add_block_to_workflow(_WORKFLOW, "open_portal", _NEW_BLOCK)
        blocks = _blocks(out)
        assert blocks["open_portal"]["next_block_label"] == "check_pages"
        assert blocks["check_pages"]["next_block_label"] == "read_total", (
            "the new block must inherit what its predecessor pointed at, or the chain is cut"
        )

    def test_leaves_every_other_block_byte_identical(self) -> None:
        """The property the whole design turns on: adding must not re-decide a block that already works."""
        before, after = _blocks(_WORKFLOW), _blocks(add_block_to_workflow(_WORKFLOW, "open_portal", _NEW_BLOCK))
        assert after["read_total"] == before["read_total"]
        predecessor = dict(after["open_portal"])
        assert predecessor.pop("next_block_label") == "check_pages"
        assert predecessor == {k: v for k, v in before["open_portal"].items() if k != "next_block_label"}

    def test_appending_after_the_last_block_leaves_the_new_block_terminal(self) -> None:
        blocks = _blocks(add_block_to_workflow(_WORKFLOW, "read_total", _NEW_BLOCK))
        assert blocks["read_total"]["next_block_label"] == "check_pages"
        assert blocks["check_pages"]["next_block_label"] is None

    def test_a_duplicate_label_is_refused_and_names_what_exists(self) -> None:
        with pytest.raises(BlockEditError) as exc:
            add_block_to_workflow(_WORKFLOW, "open_portal", "block_type: code\nlabel: read_total\ncode: x\n")
        assert "already exists" in str(exc.value)
        assert "open_portal" in str(exc.value) and "read_total" in str(exc.value)

    def test_an_unknown_after_label_names_what_exists(self) -> None:
        with pytest.raises(BlockEditError) as exc:
            add_block_to_workflow(_WORKFLOW, "ghost", _NEW_BLOCK)
        assert "open_portal" in str(exc.value) and "read_total" in str(exc.value)

    def test_a_block_without_a_label_is_refused(self) -> None:
        with pytest.raises(BlockEditError):
            add_block_to_workflow(_WORKFLOW, "open_portal", "block_type: code\ncode: x\n")

    @pytest.mark.parametrize("block_yaml", ["{{ not yaml", "- a\n- b\n"])
    def test_block_yaml_must_be_one_block_mapping(self, block_yaml: str) -> None:
        with pytest.raises(BlockEditError):
            add_block_to_workflow(_WORKFLOW, "open_portal", block_yaml)


class TestAddBlockParameters:
    """A new block and the parameter it reads have to land in one write, or the saved workflow cannot run."""

    def test_declares_the_new_parameter_alongside_the_block(self) -> None:
        out = add_block_to_workflow(
            _WORKFLOW,
            "open_portal",
            _NEW_BLOCK,
            parameters=[{"key": "page_paths", "parameter_type": "workflow", "workflow_parameter_type": "json"}],
        )
        parsed = yaml.safe_load(out)
        assert [p["key"] for p in parsed["workflow_definition"]["parameters"]] == ["page_paths"]
        assert "check_pages" in _blocks(out)

    def test_an_already_declared_key_keeps_its_current_definition(self) -> None:
        """Editing existing parameters is out of scope, so a repeated key must not be redefined."""
        workflow = _WORKFLOW.replace(
            "workflow_definition:\n",
            "workflow_definition:\n  parameters:\n  - key: page_paths\n    parameter_type: workflow\n",
        )
        out = add_block_to_workflow(
            workflow, "open_portal", _NEW_BLOCK, parameters=[{"key": "page_paths", "parameter_type": "credential"}]
        )
        parameters = yaml.safe_load(out)["workflow_definition"]["parameters"]
        assert parameters == [{"key": "page_paths", "parameter_type": "workflow"}]

    def test_a_parameter_without_a_key_is_refused(self) -> None:
        with pytest.raises(BlockEditError):
            add_block_to_workflow(_WORKFLOW, "open_portal", _NEW_BLOCK, parameters=[{"parameter_type": "workflow"}])


class TestAddBlockRunsTheSameAuthorTimeChecks:
    """add_block composes a whole workflow server-side and persists it through the shared path, so it
    cannot be a way around a check a whole-document write must satisfy."""

    _UNSAFE = 'block_type: code\nlabel: exfiltrate\ncode: |\n  await page.request.get("https://example.test/x")\n'

    def test_unsafe_code_added_this_way_still_trips_the_code_safety_reject(self) -> None:
        spliced = add_block_to_workflow(_WORKFLOW, "read_total", self._UNSAFE)

        errors = _code_block_safety_errors(spliced, _WORKFLOW)

        assert [e.reason_code for e in errors] == ["AUTHOR_PAGE_REQUEST"]

    def test_it_trips_exactly_what_the_whole_document_write_trips(self) -> None:
        spliced = add_block_to_workflow(_WORKFLOW, "read_total", self._UNSAFE)
        retyped = yaml.safe_load(_WORKFLOW)
        retyped["workflow_definition"]["blocks"].append(yaml.safe_load(self._UNSAFE))
        whole_document = yaml.safe_dump(retyped, sort_keys=False)

        assert [e.reason_code for e in _code_block_safety_errors(spliced, _WORKFLOW)] == [
            e.reason_code for e in _code_block_safety_errors(whole_document, _WORKFLOW)
        ]

    def test_the_untouched_blocks_are_not_re_checked(self) -> None:
        """The gate is label-scoped against the prior workflow, so a purely additive splice presents
        exactly one changed block."""
        spliced = add_block_to_workflow(_WORKFLOW, "read_total", _NEW_BLOCK)

        assert _code_block_safety_errors(spliced, _WORKFLOW) == []


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
