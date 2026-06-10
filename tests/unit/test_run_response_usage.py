from types import SimpleNamespace

from skyvern.forge.sdk.workflow.service import build_workflow_usage_response
from skyvern.schemas.runs import RunUsageResponse
from skyvern.services.run_service import _build_task_run_usage
from skyvern.services.workflow_service import build_workflow_run_usage


def test_build_task_run_usage_sums_recorded_cost_columns() -> None:
    usage = _build_task_run_usage(
        SimpleNamespace(
            duration_ms=12_345,
            compute_cost=0.42,
            llm_cost=1.15,
            proxy_cost=None,
            captcha_cost=0.08,
        )
    )

    assert usage == RunUsageResponse(
        source="task_run",
        duration_ms=12_345,
        total_cost_usd=1.65,
        compute_cost_usd=0.42,
        llm_cost_usd=1.15,
        proxy_cost_usd=None,
        captcha_cost_usd=0.08,
    )


def test_build_task_run_usage_keeps_duration_without_costs() -> None:
    usage = _build_task_run_usage(
        SimpleNamespace(
            duration_ms=2500,
            compute_cost=None,
            llm_cost=None,
            proxy_cost=None,
            captcha_cost=None,
        )
    )

    assert usage == RunUsageResponse(source="task_run", duration_ms=2500)


def test_build_task_run_usage_omits_empty_rows() -> None:
    usage = _build_task_run_usage(
        SimpleNamespace(
            duration_ms=None,
            compute_cost=None,
            llm_cost=None,
            proxy_cost=None,
            captcha_cost=None,
        )
    )

    assert usage is None


def test_workflow_usage_reports_billable_and_cached_credit_ledger() -> None:
    usage = build_workflow_run_usage(credits_used=7, cached_credits_used=3)

    assert usage == RunUsageResponse(
        source="workflow_run",
        billable_credits_used=7,
        cached_credits_used=3,
        total_credits_used=10,
    )


def test_sdk_workflow_usage_normalizes_null_credit_counters() -> None:
    usage = build_workflow_usage_response(credits_used=None, cached_credits_used=None)

    assert usage == RunUsageResponse(
        source="workflow_run",
        billable_credits_used=0,
        cached_credits_used=0,
        total_credits_used=0,
    )
