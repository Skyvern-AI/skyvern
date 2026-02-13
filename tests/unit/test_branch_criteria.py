from __future__ import annotations

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.workflow.exceptions import FailedToFormatJinjaStyleParameter, MissingJinjaVariables
from skyvern.forge.sdk.workflow.models.block import BranchEvaluationContext, JinjaBranchCriteria


class FakeWorkflowRunContext:
    def __init__(
        self,
        *,
        values: dict,
        secrets: dict | None = None,
        include_secrets_in_templates: bool = False,
        block_metadata: dict[str, dict] | None = None,
    ) -> None:
        self.values = dict(values)
        self.secrets = secrets or {}
        self.include_secrets_in_templates = include_secrets_in_templates
        self._block_metadata = block_metadata or {}

        # Minimal workflow identifiers
        self.workflow_title = "wf-title"
        self.workflow_id = "wf-id"
        self.workflow_permanent_id = "wf-perm-id"
        self.workflow_run_id = "wf-run-id"

    def get_block_metadata(self, label: str) -> dict:
        return dict(self._block_metadata.get(label, {}))


@pytest.mark.asyncio
async def test_jinja_branch_criteria_evaluates_truthy_with_workflow_context():
    fake_ctx = FakeWorkflowRunContext(
        values={"params": {"foo": "bar"}, "extra": "value"},
        block_metadata={"conditional": {"current_index": 1, "custom": "meta"}},
    )
    branch_ctx = BranchEvaluationContext(
        workflow_run_context=fake_ctx,  # ensures template_data matches block parameter rendering
        block_label="conditional",
    )
    criteria = JinjaBranchCriteria(expression="{{ params.foo == 'bar' and current_index == 1 }}")

    assert await criteria.evaluate(branch_ctx) is True


@pytest.mark.asyncio
async def test_jinja_branch_criteria_raises_on_missing_variable_strict(monkeypatch):
    monkeypatch.setattr(settings, "WORKFLOW_TEMPLATING_STRICTNESS", "strict")
    branch_ctx = BranchEvaluationContext()
    criteria = JinjaBranchCriteria(expression="{{ missing_value }}")

    with pytest.raises(MissingJinjaVariables):
        await criteria.evaluate(branch_ctx)


@pytest.mark.asyncio
async def test_jinja_branch_criteria_raises_on_template_error():
    branch_ctx = BranchEvaluationContext()
    criteria = JinjaBranchCriteria(expression="{% for %}")  # invalid Jinja syntax

    with pytest.raises(FailedToFormatJinjaStyleParameter):
        await criteria.evaluate(branch_ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expression,expected",
    [
        # Boolean-like strings (case insensitive)
        ("{{ 'true' }}", True),
        ("{{ 'True' }}", True),
        ("{{ 'TRUE' }}", True),
        ("{{ 'false' }}", False),
        ("{{ 'False' }}", False),
        ("{{ 'FALSE' }}", False),
        # Numeric strings
        ("{{ '1' }}", True),
        ("{{ '0' }}", False),
        ("{{ '42' }}", True),
        ("{{ '-1' }}", True),
        ("{{ '0.0' }}", False),
        ("{{ '0.1' }}", True),
        ("{{ '-0.5' }}", True),
        # Yes/No variants
        ("{{ 'yes' }}", True),
        ("{{ 'Yes' }}", True),
        ("{{ 'YES' }}", True),
        ("{{ 'y' }}", True),
        ("{{ 'Y' }}", True),
        ("{{ 'no' }}", False),
        ("{{ 'No' }}", False),
        ("{{ 'NO' }}", False),
        ("{{ 'n' }}", False),
        ("{{ 'N' }}", False),
        # On/Off
        ("{{ 'on' }}", True),
        ("{{ 'ON' }}", True),
        ("{{ 'off' }}", False),
        ("{{ 'OFF' }}", False),
        # Null variants
        ("{{ 'null' }}", False),
        ("{{ 'Null' }}", False),
        ("{{ 'NULL' }}", False),
        ("{{ 'none' }}", False),
        ("{{ 'None' }}", False),
        # Empty and whitespace
        ("{{ '' }}", False),
        ("{{ '   ' }}", False),
        ("{{ '\t\n' }}", False),
        # Arbitrary strings (non-empty = truthy)
        ("{{ 'some text' }}", True),
        ("{{ 'anything' }}", True),
        # Direct boolean comparisons (common use case)
        ("{{ 5 > 3 }}", True),
        ("{{ 1 == 0 }}", False),
    ],
)
async def test_jinja_branch_criteria_truthy_falsy_evaluation(expression: str, expected: bool):
    """Test that rendered template strings are properly evaluated as boolean."""
    fake_ctx = FakeWorkflowRunContext(values={})
    branch_ctx = BranchEvaluationContext(workflow_run_context=fake_ctx, block_label="test")
    criteria = JinjaBranchCriteria(expression=expression)

    result = await criteria.evaluate(branch_ctx)
    assert result is expected, f"Expression {expression} should evaluate to {expected}, got {result}"


@pytest.mark.asyncio
async def test_jinja_branch_criteria_with_variable_comparison():
    """Test realistic scenario with variable comparisons."""
    fake_ctx = FakeWorkflowRunContext(
        values={
            "comment_count": 150,
            "threshold": 100,
            "status": "active",
        }
    )
    branch_ctx = BranchEvaluationContext(workflow_run_context=fake_ctx, block_label="test")

    # Numeric comparison
    criteria = JinjaBranchCriteria(expression="{{ comment_count > threshold }}")
    assert await criteria.evaluate(branch_ctx) is True

    # String comparison
    criteria = JinjaBranchCriteria(expression="{{ status == 'active' }}")
    assert await criteria.evaluate(branch_ctx) is True

    # Combined logic
    criteria = JinjaBranchCriteria(expression="{{ comment_count > threshold and status == 'active' }}")
    assert await criteria.evaluate(branch_ctx) is True
