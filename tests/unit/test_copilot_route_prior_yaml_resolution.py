"""Tests for `_blockless_submission_fallback` and `_prior_copilot_workflow_yaml`."""

from __future__ import annotations

import textwrap

from skyvern.forge.sdk.routes.workflow_copilot import (
    _blockless_submission_fallback,
    _prior_copilot_workflow_yaml,
)

_PROPOSED_YAML = textwrap.dedent(
    """\
    title: t
    workflow_definition:
      parameters: []
      blocks:
        - block_type: goto_url
          label: open_site
          url: https://example.com
    """
)

_PERSISTED_YAML = textwrap.dedent(
    """\
    title: t
    workflow_definition:
      parameters: []
      blocks:
        - block_type: goto_url
          label: open_site
          url: https://example.com
        - block_type: navigation
          label: do_thing
          navigation_goal: Click the primary action.
    """
)

_USER_MODIFIED_YAML = _PROPOSED_YAML + (
    "    - block_type: text_prompt\n      label: summarize_result\n      llm_key: x\n      prompt: ok\n"
)


_BLOCKLESS_EXPLICIT_YAML = "title: t\nworkflow_definition:\n  parameters: []\n  blocks: []\n"


class TestBlocklessSubmissionFallback:
    def test_none_submission_with_prior_proposal_returns_fallback(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                submitted_workflow_yaml=None,
            )
            == _PROPOSED_YAML
        )

    def test_empty_string_submission_with_prior_proposal_returns_fallback(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                submitted_workflow_yaml="",
            )
            == _PROPOSED_YAML
        )

    def test_whitespace_only_submission_returns_fallback(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                submitted_workflow_yaml="   \n",
            )
            == _PROPOSED_YAML
        )

    def test_explicit_blocks_empty_submission_is_NOT_overwritten(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                submitted_workflow_yaml=_BLOCKLESS_EXPLICIT_YAML,
            )
            is None
        )

    def test_populated_submission_preserves_user_edit(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                submitted_workflow_yaml=_USER_MODIFIED_YAML,
            )
            is None
        )

    def test_no_proposal_returns_none(self) -> None:
        assert _blockless_submission_fallback(proposed_workflow=None, submitted_workflow_yaml="") is None

    def test_empty_dict_proposal_returns_none(self) -> None:
        assert _blockless_submission_fallback(proposed_workflow={}, submitted_workflow_yaml="") is None

    def test_non_string_copilot_yaml_returns_none(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": None},
                submitted_workflow_yaml="",
            )
            is None
        )

    def test_malformed_blockless_copilot_yaml_returns_none(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _BLOCKLESS_EXPLICIT_YAML},
                submitted_workflow_yaml="",
            )
            is None
        )


class TestPriorCopilotWorkflowYaml:
    def test_uses_proposal_when_present(self) -> None:
        assert (
            _prior_copilot_workflow_yaml(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                persisted_workflow_yaml=_PERSISTED_YAML,
            )
            == _PROPOSED_YAML
        )

    def test_falls_back_to_persisted_when_no_proposal(self) -> None:
        assert (
            _prior_copilot_workflow_yaml(
                proposed_workflow=None,
                persisted_workflow_yaml=_PERSISTED_YAML,
            )
            == _PERSISTED_YAML
        )

    def test_falls_back_to_persisted_when_proposal_has_no_copilot_yaml(self) -> None:
        assert (
            _prior_copilot_workflow_yaml(
                proposed_workflow={"some_other_field": "x"},
                persisted_workflow_yaml=_PERSISTED_YAML,
            )
            == _PERSISTED_YAML
        )

    def test_falls_back_to_persisted_when_copilot_yaml_is_blockless(self) -> None:
        assert (
            _prior_copilot_workflow_yaml(
                proposed_workflow={"_copilot_yaml": _BLOCKLESS_EXPLICIT_YAML},
                persisted_workflow_yaml=_PERSISTED_YAML,
            )
            == _PERSISTED_YAML
        )

    def test_returns_none_when_neither_has_blocks(self) -> None:
        assert (
            _prior_copilot_workflow_yaml(
                proposed_workflow={"_copilot_yaml": _BLOCKLESS_EXPLICIT_YAML},
                persisted_workflow_yaml=_BLOCKLESS_EXPLICIT_YAML,
            )
            is None
        )

    def test_returns_none_when_no_inputs(self) -> None:
        assert _prior_copilot_workflow_yaml(proposed_workflow=None, persisted_workflow_yaml=None) is None
