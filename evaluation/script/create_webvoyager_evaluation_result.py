import csv
import json
from typing import Any

import typer
from dotenv import load_dotenv

from evaluation.core import SkyvernClient
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


def main(
    base_url: str = typer.Option(..., "--base-url", help="base url for Skyvern client"),
    cred: str = typer.Option(..., "--cred", help="credential for Skyvern organization"),
    workflow_pid: str = typer.Option(..., "--workflow-pid", help="workflow pid to execute the evaluation test"),
    record_json_path: str = typer.Option(..., "--record-json", help="record json path for evaluation run"),
    output_csv_path: str = typer.Option("output.csv", "--output-path", help="output csv path for evaluation run"),
) -> None:
    client = SkyvernClient(base_url=base_url, credentials=cred)

    with open(record_json_path, encoding="utf-8") as file:
        with open(output_csv_path, newline="", mode="w", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=csv_headers)
            writer.writeheader()

            for line in file:
                one_record: dict[str, Any] = json.loads(line)
                workflow_run_id: str = one_record.get("workflow_run_id", "")

                workflow_run_response = client.get_workflow_run(
                    workflow_pid=workflow_pid, workflow_run_id=workflow_run_id
                )
                one_record.update(
                    {
                        "workflow_permanent_id": workflow_pid,
                        "status": str(workflow_run_response.status),
                        "summary": workflow_run_response.task_v2.summary,
                        "output": workflow_run_response.task_v2.output,
                        "assertion": workflow_run_response.status == WorkflowRunStatus.completed,
                        "failure_reason": workflow_run_response.failure_reason or "",
                    }
                )
                csv_data = {key: one_record[key] for key in csv_headers}
                print(
                    f"{workflow_run_id}(id={one_record.get('id')}) {workflow_run_response.status}. Saving to the output csv.."
                )
                writer.writerow(csv_data)

    print(f"Exported all records in {output_csv_path}")


if __name__ == "__main__":
    typer.run(main)
