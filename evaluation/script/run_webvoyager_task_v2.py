"""Run paired WebVoyager Task V2 evaluations with exact model cost reporting."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import typer
from dotenv import load_dotenv

from evaluation.core import Evaluator, SkyvernClient
from evaluation.core.utils import WebVoyagerTestCase, load_webvoyager_case_from_json
from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.forge_app_initializer import start_forge_app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Request
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunResponseBase, WorkflowRunStatus

load_dotenv()

FIXED_JUDGE_LLM_KEY = "VERTEX_GEMINI_3.5_FLASH"


def _web_name(case: WebVoyagerTestCase) -> str:
    """Return the benchmark website name encoded in a WebVoyager case ID."""
    return case.id.split("--", 1)[0]


def _merge_counts(count_maps: Any) -> dict[str, int]:
    """Sum string-keyed count maps across evaluation cases."""
    merged: dict[str, int] = {}
    for count_map in count_maps:
        for key, value in count_map.items():
            merged[key] = merged.get(key, 0) + value
    return merged


async def _normalize_cases(
    dataset_path: str,
    output_path: Path,
    limit: int | None = None,
    web_name: str | None = None,
) -> list[WebVoyagerTestCase]:
    """Normalize each WebVoyager prompt once using the fixed judge model."""
    group_id = str(uuid4())
    cases: list[WebVoyagerTestCase] = []
    for case in load_webvoyager_case_from_json(dataset_path, group_id=group_id):
        if web_name and _web_name(case).casefold() != web_name.casefold():
            continue
        if limit is not None and len(cases) >= limit:
            break
        prompt = prompt_engine.load_prompt(
            "check-evaluation-goal",
            user_goal=case.question,
            local_datetime=datetime.now().isoformat(),
        )
        response = await app.LLM_API_HANDLER(prompt=prompt, prompt_name="check-evaluation-goal")
        tweaked_user_goal = response.get("tweaked_user_goal")
        if isinstance(tweaked_user_goal, str) and tweaked_user_goal.strip():
            case.is_updated = tweaked_user_goal != case.question
            case.question = tweaked_user_goal
        cases.append(case)

    with output_path.open("w", encoding="utf-8") as output_file:
        for case in cases:
            output_file.write(case.model_dump_json() + "\n")
    return cases


async def _submit_case(
    client: SkyvernClient,
    case: WebVoyagerTestCase,
    model_name: str,
    semaphore: asyncio.Semaphore,
    proxy_url: str | None,
) -> dict[str, Any]:
    """Submit one case to one target model."""
    async with semaphore:
        task = await asyncio.to_thread(
            client.create_task_v2,
            TaskV2Request(
                url=case.url,
                user_prompt=case.question,
                proxy_location={"url": proxy_url} if proxy_url else None,
                model={"model_name": model_name},
            ),
            case.max_steps,
        )
    return {
        "id": case.id,
        "model": model_name,
        "question": case.question,
        "answer": case.answer,
        "is_updated": case.is_updated,
        "workflow_permanent_id": task.workflow_permanent_id,
        "workflow_run_id": task.workflow_run_id,
        "task_v2_id": task.observer_cruise_id,
    }


async def _wait_for_completion(
    client: SkyvernClient,
    record: dict[str, Any],
    semaphore: asyncio.Semaphore,
    poll_interval_seconds: float,
) -> WorkflowRunResponseBase:
    """Poll one workflow run until it reaches a terminal state."""
    while True:
        async with semaphore:
            response = await client.get_workflow_run(
                workflow_pid=record["workflow_permanent_id"],
                workflow_run_id=record["workflow_run_id"],
            )
        if response.status.is_final():
            return response
        await asyncio.sleep(poll_interval_seconds)


async def _score_record(
    client: SkyvernClient,
    record: dict[str, Any],
    workflow_run_response: WorkflowRunResponseBase,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """Score one completed run and collect exact target-agent cost."""
    passed = False
    failure_reason = workflow_run_response.failure_reason or ""
    if workflow_run_response.status == WorkflowRunStatus.completed:
        evaluator = Evaluator(client=client, artifact_folder="")
        try:
            async with semaphore:
                await evaluator.eval_skyvern_workflow_run(
                    workflow_pid=record["workflow_permanent_id"],
                    workflow_run_id=record["workflow_run_id"],
                    question=record["question"],
                    answer=record["answer"],
                    is_updated=record["is_updated"],
                )
            passed = True
        except Exception as exc:  # noqa: BLE001 - each case must produce a result
            failure_reason = str(exc)
    else:
        failure_reason = failure_reason or f"workflow ended with {workflow_run_response.status}"

    async with semaphore:
        cost = await client.get_workflow_run_evaluation_cost(
            workflow_pid=record["workflow_permanent_id"],
            workflow_run_id=record["workflow_run_id"],
        )
    return {
        "id": record["id"],
        "model": record["model"],
        "workflow_run_id": record["workflow_run_id"],
        "status": str(workflow_run_response.status),
        "passed": passed,
        "failure_reason": failure_reason,
        "agent_cost_usd": cost.agent_cost_usd,
        "input_tokens": cost.input_tokens,
        "output_tokens": cost.output_tokens,
        "reasoning_tokens": cost.reasoning_tokens,
        "image_tokens": cost.image_tokens,
        "tokenless_request_count": cost.tokenless_request_count,
        "cost_status": cost.cost_status,
        "planner_call_count": cost.planner_call_count,
        "check_completion_call_count": cost.check_completion_call_count,
        "generate_extraction_task_call_count": cost.generate_extraction_task_call_count,
        "generate_task_block_call_count": cost.generate_task_block_call_count,
        "extract_actions_call_count": cost.extract_actions_call_count,
        "iteration_count": cost.iteration_count,
        "loop_item_count": cost.loop_item_count,
        "retry_count": cost.retry_count,
        "model_call_counts": cost.model_call_counts,
        "prompt_call_counts": cost.prompt_call_counts,
        "llm_calls": [call.model_dump() for call in cost.llm_calls],
    }


async def run_eval(
    base_url: str,
    cred: str,
    dataset_path: str,
    output_dir: str,
    model_names: list[str],
    concurrency: int,
    poll_interval_seconds: float,
    limit: int | None = None,
    web_name: str | None = None,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """Run paired WebVoyager cases and write compact result files."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    manifest_path = output_path / "normalized_manifest.jsonl"
    cases = await _normalize_cases(dataset_path, manifest_path, limit=limit, web_name=web_name)
    if not cases:
        selection = f" for website {web_name!r}" if web_name else ""
        raise ValueError(f"No WebVoyager cases selected{selection}")
    client = SkyvernClient(base_url=base_url, credentials=cred)
    semaphore = asyncio.Semaphore(concurrency)

    submissions = await asyncio.gather(
        *(_submit_case(client, case, model_name, semaphore, proxy_url) for case in cases for model_name in model_names)
    )
    completed_runs = await asyncio.gather(
        *(_wait_for_completion(client, record, semaphore, poll_interval_seconds) for record in submissions)
    )
    results = await asyncio.gather(
        *(
            _score_record(client, record, workflow_run_response, semaphore)
            for record, workflow_run_response in zip(submissions, completed_runs, strict=True)
        )
    )

    summary: dict[str, Any] = {
        "dataset": dataset_path,
        "judge_model": FIXED_JUDGE_LLM_KEY,
        "models": {},
        "total_cases_per_model": len(cases),
        "concurrency": concurrency,
        "web_name": web_name,
        "proxy_configured": bool(proxy_url),
    }
    for model_name in model_names:
        model_results = [result for result in results if result["model"] == model_name]
        passed_cases = sum(1 for result in model_results if result["passed"])
        costs = [result["agent_cost_usd"] for result in model_results]
        cost_status = "exact" if all(result["cost_status"] == "exact" for result in model_results) else "incomplete"
        summary["models"][model_name] = {
            "total_cases": len(model_results),
            "passed_cases": passed_cases,
            "pass_rate": passed_cases / len(model_results) if model_results else 0.0,
            "agent_cost_usd": sum(costs) if cost_status == "exact" else None,
            "input_tokens": sum(result["input_tokens"] for result in model_results),
            "output_tokens": sum(result["output_tokens"] for result in model_results),
            "reasoning_tokens": sum(result["reasoning_tokens"] for result in model_results),
            "image_tokens": sum(result["image_tokens"] for result in model_results),
            "tokenless_request_count": sum(result["tokenless_request_count"] for result in model_results),
            "planner_call_count": sum(result["planner_call_count"] for result in model_results),
            "check_completion_call_count": sum(
                result["check_completion_call_count"] for result in model_results
            ),
            "generate_extraction_task_call_count": sum(
                result["generate_extraction_task_call_count"] for result in model_results
            ),
            "generate_task_block_call_count": sum(
                result["generate_task_block_call_count"] for result in model_results
            ),
            "extract_actions_call_count": sum(result["extract_actions_call_count"] for result in model_results),
            "iteration_count": sum(result["iteration_count"] for result in model_results),
            "loop_item_count": sum(result["loop_item_count"] for result in model_results),
            "retry_count": sum(result["retry_count"] for result in model_results),
            "model_call_counts": _merge_counts(result["model_call_counts"] for result in model_results),
            "prompt_call_counts": _merge_counts(result["prompt_call_counts"] for result in model_results),
            "cost_status": cost_status,
        }

    with (output_path / "results.jsonl").open("w", encoding="utf-8") as result_file:
        for result in results:
            result_file.write(json.dumps(result) + "\n")
    with (output_path / "summary.json").open("w", encoding="utf-8") as summary_file:
        json.dump(summary, summary_file, indent=2)
        summary_file.write("\n")

    for tokenless_model_name in settings.get_openai_compatible_model_names():
        tokenless_summary = summary["models"].get(tokenless_model_name)
        if tokenless_summary and tokenless_summary["cost_status"] != "exact":
            raise RuntimeError(
                f"Tokenless cost resolution is incomplete for {tokenless_model_name}; "
                "refusing to report the evaluation as exact"
            )
    return summary


def main(
    base_url: str = typer.Option(
        ...,
        "--base-url",
        help="Skyvern API base URL, e.g. http://localhost:8000/api/v1",
    ),
    cred: str = typer.Option(..., "--cred", help="Skyvern organization API credential"),
    dataset_path: str = typer.Option(
        "evaluation/datasets/webvoyager_tasks.jsonl",
        "--dataset-path",
        help="WebVoyager dataset JSONL path",
    ),
    output_dir: str = typer.Option("evaluation/results/webvoyager_task_v2", "--output-dir"),
    models: str = typer.Option(
        "gemini-3.5-flash,tokenless-pro,tokenless-ultra-saver",
        "--models",
    ),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Run only the first N selected dataset cases"),
    web_name: str | None = typer.Option(
        None,
        "--web-name",
        help="Restrict the run to one benchmark website, e.g. ArXiv or Allrecipes",
    ),
    proxy_url: str | None = typer.Option(
        None,
        "--proxy-url",
        envvar="WEBVOYAGER_PROXY_URL",
        help="Custom HTTP/SOCKS proxy URL for anti-bot-sensitive websites",
    ),
    concurrency: int = typer.Option(
        8,
        "--concurrency",
        min=1,
        help="Maximum active submissions/polls/scores",
    ),
    poll_interval_seconds: float = typer.Option(5.0, "--poll-interval-seconds", min=0.1),
) -> None:
    """Run the paired evaluation with Gemini 3.5 Flash as the fixed ADC judge."""
    start_forge_app()
    app.LLM_API_HANDLER = LLMAPIHandlerFactory.get_llm_api_handler(FIXED_JUDGE_LLM_KEY)
    model_names = [model.strip() for model in models.split(",") if model.strip()]
    if not model_names:
        raise typer.BadParameter("--models must contain at least one model")
    summary = asyncio.run(
        run_eval(
            base_url=base_url,
            cred=cred,
            dataset_path=dataset_path,
            output_dir=output_dir,
            model_names=model_names,
            concurrency=concurrency,
            poll_interval_seconds=poll_interval_seconds,
            limit=limit,
            web_name=web_name,
            proxy_url=proxy_url,
        )
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    typer.run(main)
