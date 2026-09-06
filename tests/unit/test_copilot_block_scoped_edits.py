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

_NESTED_WORKFLOW = """# keep this document comment
title: Nested lookup
workflow_definition:
  parameters: []
  blocks:
    - block_type: for_loop # keep loop comment
      label: Records
      loop_over_parameter_key: rows
      loop_blocks:
        - block_type: task
          label: search_property_records
          prompt: Find the original record # keep field comment
        - block_type: code
          label: normalize_record
          code: &record_code |- # keep code scalar metadata
            record["status"] = "original"
    - block_type: conditional
      label: Route record
      branch_conditions:
        - condition: "{{ record.active }}"
          blocks:
            - block_type: task
              label: notify_owner
              prompt: Send the original notice
            - block_type: code
              label: active_result
              code: >
                return {"status": "active"}
    - block_type: code
      label: finish
      code: |
        return {"done": True}
"""


class TestAnchoredCodeEdit:
    def test_edits_only_the_named_block(self) -> None:
        out = apply_block_edit(_WORKFLOW, "read_total", expected_code='"#total"', replacement_code='"#grand-total"')
        assert "#grand-total" in out
        # the untouched block survives byte-for-byte
        assert 'await page.goto("https://example.test/")' in out

    def test_leaves_every_non_target_byte_identical(self) -> None:
        replacement = '"#grand-total"'

        out = apply_block_edit(_WORKFLOW, "read_total", expected_code='"#total"', replacement_code=replacement)

        assert out.replace(replacement, '"#total"') == _WORKFLOW

    def test_preserves_comments_parameters_metadata_and_unrelated_schema_errors(self) -> None:
        workflow = """# retained document comment
title: Lookup
workflow_definition:
  parameters:
    - key: query
      parameter_type: workflow
      workflow_parameter_type: string
  blocks:
    - block_type: code
      label: open_portal
      code: |
        await page.goto("https://example.test/")
      unknown_future_field: keep-me # accepted by an earlier schema
      next_block_label: read_total
    - block_type: code
      label: read_total
      code: |
        total = await page.inner_text("#total")
        return {"total": total}
      parameter_keys: [query]
  code_artifact_metadata:
    read_total: {artifact_id: artifact-1, note: keep-style}
"""

        out = apply_block_edit(workflow, "read_total", expected_code='"#total"', replacement_code='"#amount"')

        assert out.replace('"#amount"', '"#total"') == workflow

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

    def test_empty_replacement_remains_valid_yaml_before_a_following_key(self) -> None:
        out = apply_block_edit(
            _WORKFLOW,
            "open_portal",
            expected_code='await page.goto("https://example.test/")\n',
            replacement_code="",
        )

        parsed = yaml.safe_load(out)
        first_block = parsed["workflow_definition"]["blocks"][0]
        assert first_block["code"] == ""
        assert first_block["next_block_label"] == "read_total"
        assert "code: ''\n      next_block_label: read_total" in out

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

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("normalize_record", 'record["status"] = "original"'),
            ("active_result", 'return {"status": "active"}\n'),
        ],
    )
    def test_reads_code_nested_in_loops_and_branches(self, label: str, expected: str) -> None:
        assert stored_block_code(_NESTED_WORKFLOW, label) == expected


class TestFieldEdit:
    def test_sets_only_the_named_fields(self) -> None:
        out = apply_block_edit(_WORKFLOW, "open_portal", fields={"continue_on_failure": True})
        assert "continue_on_failure: true" in out
        assert "read_total" in out

    @pytest.mark.parametrize(
        ("label", "before", "after"),
        [
            ("search_property_records", "Find the original record", "Find the corrected record"),
            ("notify_owner", "Send the original notice", "Send the corrected notice"),
        ],
    )
    def test_nested_field_edit_leaves_every_other_byte_identical(self, label: str, before: str, after: str) -> None:
        out = apply_block_edit(_NESTED_WORKFLOW, label, fields={"prompt": after})

        assert out.replace(after, before) == _NESTED_WORKFLOW

    def test_nested_new_field_leaves_every_other_byte_identical(self) -> None:
        inserted = "          continue_on_failure: true\n"

        out = apply_block_edit(_NESTED_WORKFLOW, "search_property_records", fields={"continue_on_failure": True})

        assert inserted in out
        assert out.replace(inserted, "") == _NESTED_WORKFLOW

    @pytest.mark.parametrize("label", ["Records", "Route record"])
    def test_adds_field_after_a_container_value_without_nesting_it(self, label: str) -> None:
        out = apply_block_edit(_NESTED_WORKFLOW, label, fields={"continue_on_failure": True})

        parsed = yaml.safe_load(out)
        target = next(block for block in parsed["workflow_definition"]["blocks"] if block["label"] == label)
        assert target["continue_on_failure"] is True
        assert parsed["workflow_definition"]["blocks"][-1]["label"] == "finish"

    @pytest.mark.parametrize(
        "last_field_source",
        [
            '      prompt: "first line\n        second line"\n',
            "      metadata: {first: one,\n        second: two}\n",
        ],
    )
    def test_adds_field_after_a_multiline_value_that_ends_mid_line(self, last_field_source: str) -> None:
        workflow = f"""workflow_definition:
  blocks:
    - block_type: task
      label: target
{last_field_source}"""

        out = apply_block_edit(workflow, "target", fields={"continue_on_failure": True})

        target = yaml.safe_load(out)["workflow_definition"]["blocks"][0]
        assert target["continue_on_failure"] is True
        assert target.get("prompt") == "first line second line" or target.get("metadata") == {
            "first": "one",
            "second": "two",
        }

    def test_appends_a_field_after_an_aliased_last_value(self) -> None:
        workflow = """shared: &shared Original
workflow_definition:
  blocks:
    - block_type: task
      label: target
      prompt: *shared
"""

        out = apply_block_edit(workflow, "target", fields={"continue_on_failure": True})

        parsed = yaml.safe_load(out)
        assert parsed["shared"] == "Original"
        assert parsed["workflow_definition"]["blocks"][0]["continue_on_failure"] is True

    def test_appends_after_a_container_with_an_aliased_trailing_descendant(self) -> None:
        workflow = """shared: &shared Original
workflow_definition:
  blocks:
    - block_type: task
      label: target
      metadata:
        prompt: *shared
    - block_type: code
      label: finish
      code: return {}
"""

        out = apply_block_edit(workflow, "target", fields={"continue_on_failure": True})

        parsed = yaml.safe_load(out)
        target, finish = parsed["workflow_definition"]["blocks"]
        assert parsed["shared"] == "Original"
        assert target["metadata"] == {"prompt": "Original"}
        assert target["continue_on_failure"] is True
        assert finish["label"] == "finish"

    def test_appends_after_a_trailing_anchor_definition_used_by_another_block(self) -> None:
        workflow = """workflow_definition:
  blocks:
    - block_type: task
      label: target
      prompt: &shared Original
    - block_type: task
      label: consumer
      prompt: *shared
"""

        out = apply_block_edit(workflow, "target", fields={"continue_on_failure": True})

        target, consumer = yaml.safe_load(out)["workflow_definition"]["blocks"]
        assert target["prompt"] == "Original"
        assert target["continue_on_failure"] is True
        assert consumer["prompt"] == "Original"

    @pytest.mark.parametrize(
        ("last_field_source", "field", "expected"),
        [
            ("      code: |-\n        return {}", "code", "return {}"),
            (
                "      metadata: {first: one,\n        second: two}",
                "metadata",
                {"first": "one", "second": "two"},
            ),
        ],
    )
    def test_appends_after_a_multiline_value_at_end_of_file(
        self, last_field_source: str, field: str, expected: object
    ) -> None:
        workflow = f"""workflow_definition:
  blocks:
    - block_type: code
      label: target
{last_field_source}"""

        out = apply_block_edit(workflow, "target", fields={"continue_on_failure": True})

        target = yaml.safe_load(out)["workflow_definition"]["blocks"][0]
        assert target[field] == expected
        assert target["continue_on_failure"] is True

    def test_appends_after_a_multiline_flow_value_with_aliased_descendant_at_end_of_file(self) -> None:
        workflow = """shared: &shared Original
workflow_definition:
  blocks:
    - block_type: task
      label: target
      metadata: {first: one,
        prompt: *shared}"""

        out = apply_block_edit(workflow, "target", fields={"continue_on_failure": True})

        parsed = yaml.safe_load(out)
        target = parsed["workflow_definition"]["blocks"][0]
        assert parsed["shared"] == "Original"
        assert target["metadata"] == {"first": "one", "prompt": "Original"}
        assert target["continue_on_failure"] is True

    @pytest.mark.parametrize(
        ("field", "before", "after"),
        [
            ("parameter_keys", ["before"], ["after"]),
            ("prompt", "before\nline\n", "after\nline\n"),
        ],
    )
    def test_replaces_block_style_field_without_consuming_the_following_key(
        self, field: str, before: object, after: object
    ) -> None:
        workflow = yaml.safe_dump(
            {
                "workflow_definition": {
                    "blocks": [
                        {
                            "block_type": "task",
                            "label": "target",
                            field: before,
                            "next_block_label": "finish",
                        },
                        {"block_type": "code", "label": "finish", "code": "return {}\n"},
                    ]
                }
            },
            sort_keys=False,
        )

        out = apply_block_edit(workflow, "target", fields={field: after})

        parsed = yaml.safe_load(out)
        target = parsed["workflow_definition"]["blocks"][0]
        assert target[field] == after
        assert target["next_block_label"] == "finish"

    def test_refuses_to_replace_an_anchored_value_used_by_an_alias(self) -> None:
        workflow = _WORKFLOW.replace(
            "      label: open_portal\n",
            "      label: open_portal\n      prompt: &shared Original\n      description: *shared\n",
        )

        with pytest.raises(BlockEditError, match="alias"):
            apply_block_edit(workflow, "open_portal", fields={"prompt": "Changed"})

    def test_refuses_to_edit_a_block_mapping_imported_through_an_alias(self) -> None:
        workflow = """template: &shared_block
  block_type: task
  label: shared
  prompt: Original
workflow_definition:
  blocks:
    - *shared_block
"""

        with pytest.raises(BlockEditError, match="alias"):
            apply_block_edit(workflow, "shared", fields={"prompt": "Changed"})


class TestNestedAnchoredCodeEdit:
    @pytest.mark.parametrize(
        ("label", "before", "after"),
        [
            ("normalize_record", '"original"', '"corrected"'),
            ("active_result", '"active"', '"matched"'),
        ],
    )
    def test_leaves_every_non_target_byte_identical(self, label: str, before: str, after: str) -> None:
        out = apply_block_edit(_NESTED_WORKFLOW, label, expected_code=before, replacement_code=after)

        assert out.replace(after, before) == _NESTED_WORKFLOW

    def test_missing_label_lists_top_level_loop_and_branch_labels(self) -> None:
        with pytest.raises(BlockEditError) as exc:
            apply_block_edit(_NESTED_WORKFLOW, "ghost", fields={"prompt": "x"})

        message = str(exc.value)
        assert all(
            label in message
            for label in (
                "Records",
                "search_property_records",
                "normalize_record",
                "Route record",
                "notify_owner",
                "active_result",
                "finish",
            )
        )

    def test_duplicate_nested_labels_report_both_structural_paths(self) -> None:
        duplicated = _NESTED_WORKFLOW.replace("label: active_result", "label: normalize_record")

        with pytest.raises(BlockEditError) as exc:
            apply_block_edit(duplicated, "normalize_record", expected_code="x", replacement_code="y")

        message = str(exc.value)
        assert "workflow_definition.blocks[0].loop_blocks[1]" in message
        assert "workflow_definition.blocks[1].branch_conditions[0].blocks[1]" in message

    def test_multiline_folded_code_keeps_semantic_newlines_after_edit(self) -> None:
        workflow = """workflow_definition:
  blocks:
    - block_type: code
      label: folded
      code: >
        x = 1

        return x
"""

        out = apply_block_edit(workflow, "folded", expected_code="x = 1", replacement_code="x = 2")

        assert yaml.safe_load(out)["workflow_definition"]["blocks"][0]["code"] == "x = 2\nreturn x\n"

    def test_changing_block_scalar_chomp_keeps_its_header_comment(self) -> None:
        workflow = _NESTED_WORKFLOW.replace(
            'record["status"] = "original"',
            'record["status"] = "original"\n',
        )

        out = apply_block_edit(
            workflow,
            "normalize_record",
            expected_code='record["status"] = "original"',
            replacement_code='record["status"] = "changed"\n',
        )

        assert "code: &record_code | # keep code scalar metadata" in out

    def test_refuses_to_replace_anchored_code_used_by_an_alias(self) -> None:
        workflow = _NESTED_WORKFLOW.replace(
            '    - block_type: code\n      label: finish\n      code: |\n        return {"done": True}\n',
            "    - block_type: code\n      label: finish\n      code: *record_code\n",
        )

        with pytest.raises(BlockEditError, match="alias"):
            apply_block_edit(workflow, "normalize_record", expected_code='"original"', replacement_code='"changed"')

    @pytest.mark.parametrize("branch_key", ["branch_conditions", "branches", "ordered_branches"])
    def test_all_branch_container_shapes_support_read_edit_and_remove(self, branch_key: str) -> None:
        nested_source = """          - block_type: code
            label: nested_code
            code: |-
              value = "before"
"""
        workflow = f"""workflow_definition:
  blocks:
    - block_type: conditional
      label: route
      {branch_key}:
        - blocks:
{nested_source}"""

        assert stored_block_code(workflow, "nested_code") == 'value = "before"'
        edited = apply_block_edit(
            workflow,
            "nested_code",
            expected_code='"before"',
            replacement_code='"after"',
        )
        assert edited.replace('"after"', '"before"') == workflow
        assert delete_block_from_workflow(workflow, "nested_code") == workflow.replace(
            nested_source, "            []\n"
        )


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

    def test_nested_after_label_remains_out_of_scope(self) -> None:
        with pytest.raises(BlockEditError) as exc:
            add_block_to_workflow(_NESTED_WORKFLOW, "normalize_record", _NEW_BLOCK)

        assert "No top-level block" in str(exc.value)

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

    @pytest.mark.parametrize(
        ("label", "removed_source"),
        [
            (
                "search_property_records",
                """        - block_type: task
          label: search_property_records
          prompt: Find the original record # keep field comment
""",
            ),
            (
                "notify_owner",
                """            - block_type: task
              label: notify_owner
              prompt: Send the original notice
""",
            ),
        ],
    )
    def test_nested_remove_leaves_every_other_byte_identical(self, label: str, removed_source: str) -> None:
        out = delete_block_from_workflow(_NESTED_WORKFLOW, label)

        assert out == _NESTED_WORKFLOW.replace(removed_source, "")

    def test_keeps_comments_and_blank_lines_before_the_following_block(self) -> None:
        workflow = _WORKFLOW.replace(
            "    - block_type: code\n      label: read_total\n",
            "    # keep this sibling comment\n\n    - block_type: code\n      label: read_total\n",
        )

        out = delete_block_from_workflow(workflow, "open_portal")

        assert "    # keep this sibling comment\n\n" in out
        assert "label: read_total" in out

    def test_clears_branch_condition_pointer_to_deleted_nested_block(self) -> None:
        workflow = _NESTED_WORKFLOW.replace(
            '        - condition: "{{ record.active }}"\n',
            '        - condition: "{{ record.active }}"\n          next_block_label: notify_owner\n',
        )

        out = delete_block_from_workflow(workflow, "notify_owner")

        branch = yaml.safe_load(out)["workflow_definition"]["blocks"][1]["branch_conditions"][0]
        assert branch["next_block_label"] is None

    def test_does_not_clear_next_block_label_shaped_parameter_data(self) -> None:
        workflow = _WORKFLOW.replace(
            "workflow_definition:\n",
            "workflow_definition:\n  parameters:\n    - key: payload\n      default_value: {next_block_label: read_total}\n",
        )

        out = delete_block_from_workflow(workflow, "read_total")

        parameter = yaml.safe_load(out)["workflow_definition"]["parameters"][0]
        assert parameter["default_value"]["next_block_label"] == "read_total"

    def test_refuses_deletion_when_the_target_defines_an_alias_used_by_a_survivor(self) -> None:
        workflow = _WORKFLOW.replace(
            "      label: open_portal\n",
            "      label: open_portal\n      prompt: &shared Original\n",
        ).replace("      label: read_total\n", "      label: read_total\n      description: *shared\n")

        with pytest.raises(BlockEditError, match="alias"):
            delete_block_from_workflow(workflow, "open_portal")

    def test_refuses_deletion_when_a_pointer_value_is_aliased(self) -> None:
        workflow = _WORKFLOW.replace(
            "      next_block_label: read_total\n",
            "      next_block_label: &destination read_total\n      description: *destination\n",
        )

        with pytest.raises(BlockEditError, match="alias"):
            delete_block_from_workflow(workflow, "read_total")

    def test_refuses_pointer_cleanup_on_an_aliased_owner_mapping(self) -> None:
        workflow = """first: &first
  block_type: code
  label: start
  code: x = 1
  next_block_label: inner
workflow_definition:
  blocks:
    - *first
    - block_type: code
      label: inner
      code: x = 2
"""

        with pytest.raises(BlockEditError, match="alias"):
            delete_block_from_workflow(workflow, "inner")

    def test_refuses_deletion_from_an_aliased_blocks_sequence(self) -> None:
        workflow = """shared: &shared_blocks
  - block_type: code
    label: inner
    code: x = 1
workflow_definition:
  blocks: *shared_blocks
"""

        with pytest.raises(BlockEditError, match="alias"):
            delete_block_from_workflow(workflow, "inner")

    def test_removing_sole_child_uses_parent_key_indent_across_a_blank_line(self) -> None:
        workflow = """workflow_definition:
  blocks:
  - block_type: for_loop
    label: Records
    loop_blocks:

    - block_type: code
      label: inner
      code: x = 1
"""

        out = delete_block_from_workflow(workflow, "inner")

        assert yaml.safe_load(out)["workflow_definition"]["blocks"][0]["loop_blocks"] == []

    def test_removing_sole_child_indents_flow_value_when_container_is_first_key(self) -> None:
        workflow = """workflow_definition:
  blocks:
  - loop_blocks:
    - block_type: code
      label: inner
      code: x = 1
    block_type: for_loop
    label: Records
"""

        out = delete_block_from_workflow(workflow, "inner")

        assert yaml.safe_load(out)["workflow_definition"]["blocks"][0]["loop_blocks"] == []

    def test_removing_the_only_nested_block_preserves_the_wrapper_and_following_block(self) -> None:
        workflow = _NESTED_WORKFLOW.replace(
            """        - block_type: task
          label: search_property_records
          prompt: Find the original record # keep field comment
""",
            "",
        )

        out = delete_block_from_workflow(workflow, "normalize_record")

        expected = workflow.replace(
            """        - block_type: code
          label: normalize_record
          code: &record_code |- # keep code scalar metadata
            record["status"] = "original"
""",
            "        []\n",
        )
        assert out == expected
        assert yaml.safe_load(out)["workflow_definition"]["blocks"][0]["loop_blocks"] == []

    @pytest.mark.parametrize("nested", [False, True])
    def test_removing_the_only_block_from_safe_dump_yaml_stays_parseable(self, nested: bool) -> None:
        block = {"block_type": "code", "label": "inner", "code": "x = 1\n"}
        blocks = [{"block_type": "for_loop", "label": "Records", "loop_blocks": [block]}] if nested else [block]
        workflow = yaml.safe_dump({"workflow_definition": {"blocks": blocks}}, sort_keys=False)

        out = delete_block_from_workflow(workflow, "inner")

        parsed = yaml.safe_load(out)
        remaining = parsed["workflow_definition"]["blocks"]
        assert remaining[0]["loop_blocks"] == [] if nested else remaining == []
