from skyvern.forge.sdk.db.tag_filters import workflow_tag_wpid_subqueries


def test_workflow_tag_wpid_subqueries_empty() -> None:
    assert workflow_tag_wpid_subqueries(None) == []
    assert workflow_tag_wpid_subqueries([]) == []


def test_workflow_tag_wpid_subqueries_one_per_term_group() -> None:
    # Two exact terms under "env" collapse (OR within key) -> 1; group-only and
    # label-only each add 1 -> 3 subqueries AND-ed by the caller.
    subqueries = workflow_tag_wpid_subqueries(
        [("env", "prod"), ("env", "staging"), ("team", None), (None, "urgent")],
        organization_id="o_123",
    )
    assert len(subqueries) == 3
