import asyncio
import csv
import json
from typing import Any

import typer
from dotenv import load_dotenv

from evaluation.core import Evaluator, SkyvernClient
from skyvern.forge.forge_app_initializer import start_forge_app
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus

load_dotenv()

csv_headers = [
    "id",
    "status",
    "assertion",
    "failure_reason",
    "url",
    "question",
    "answer",
    "summary",
    "output",
    "is_updated",
    "workflow_permanent_id",
    "workflow_run_id",
]

BATCH_SIZE = 5


async def process_record(client: SkyvernClient, one_record: dict[str, Any]) -> dict[str, Any]:
    workflow_pid: str = one_record.get("workflow_permanent_id", "")
    workflow_run_id: str = one_record.get("workflow_run_id", "")
    workflow_run_response = await client.get_workflow_run(workflow_pid=workflow_pid, workflow_run_id=workflow_run_id)
    one_record.update(
        {
            "status": str(workflow_run_response.status),
            "summary": workflow_run_response.task_v2.summary,
            "output": workflow_run_response.task_v2.output,
        }
    )
    if workflow_run_response.status != WorkflowRunStatus.completed:
        one_record.update(
            {
                "assertion": False,
                "failure_reason": workflow_run_response.failure_reason,
            },
        )
    else:
        evaluator = Evaluator(
            client=client,
            artifact_folder=f"test/artifacts/{one_record.get('group_id', '')}/{one_record.get('id', '')}",
        )
        try:
            await evaluator.eval_skyvern_workflow_run(
                workflow_pid=workflow_pid,
                workflow_run_id=workflow_run_id,
                question=one_record.get("question", ""),
                answer=one_record.get("answer", ""),
                is_updated=one_record.get("is_updated", False),
            )
            one_record.update({"assertion": True, "failure_reason": ""})
        except Exception as e:
            one_record.update({"assertion": False, "failure_reason": str(e)})

    csv_data = {key: one_record[key] for key in csv_headers}
    print(
        f"{workflow_pid}/{workflow_run_id}(id={one_record.get('id')}) {workflow_run_response.status}. Saving to the output csv.."
    )
    return csv_data


async def run_eval(
    base_url: str,
    cred: str,
    record_json_path: str,
    output_csv_path: str,
) -> None:
    client = SkyvernClient(base_url=base_url, credentials=cred)

    with open(record_json_path, encoding="utf-8") as file:
        with open(output_csv_path, newline="", mode="w", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=csv_headers)
            writer.writeheader()

            current_batch = []
            for line in file:
                one_record: dict[str, Any] = json.loads(line)
                current_batch.append(one_record)

                if len(current_batch) >= BATCH_SIZE:
                    results = await asyncio.gather(*(process_record(client, record) for record in current_batch))
                    for result in results:
                        writer.writerow(result)
                    current_batch = []

            if current_batch:
                results = await asyncio.gather(*(process_record(client, record) for record in current_batch))
                for result in results:
                    writer.writerow(result)

    print(f"Exported all records in {output_csv_path}")


def main(
    base_url: str = typer.Option(..., "--base-url", help="base url for Skyvern client"),
    cred: str = typer.Option(..., "--cred", help="credential for Skyvern organization"),
    record_json_path: str = typer.Option(..., "--record-json", help="record json path for evaluation run"),
    output_csv_path: str = typer.Option("output.csv", "--output-path", help="output csv path for evaluation run"),
) -> None:
    start_forge_app()

    asyncio.run(
        run_eval(base_url=base_url, cred=cred, record_json_path=record_json_path, output_csv_path=output_csv_path)
    )


if __name__ == "__main__":
    typer.run(main)
