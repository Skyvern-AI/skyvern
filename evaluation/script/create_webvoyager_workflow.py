import asyncio
import json
from datetime import datetime
from typing import Optional
from uuid import uuid4

import typer
from dotenv import load_dotenv

from evaluation.core import Evaluator, SkyvernClient
from evaluation.core.utils import load_webvoyager_case_from_json
from skyvern.forge import app
from skyvern.forge.forge_app_initializer import start_forge_app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody
from skyvern.schemas.runs import ProxyLocation

load_dotenv()


async def create_workflow_run(
    base_url: str,
    cred: str,
    workflow_pid: str,
    proxy_location: ProxyLocation | None = None,
) -> None:
    client = SkyvernClient(base_url=base_url, credentials=cred)
    group_id = uuid4()

    cnt = 0
    record_file_path = f"{group_id}-webvoyager-record.jsonl"
    with open(record_file_path, "w", encoding="utf-8") as f:
        for case_data in load_webvoyager_case_from_json(
            file_path="evaluation/datasets/webvoyager_tasks.jsonl", group_id=str(group_id)
        ):
            prompt = prompt_engine.load_prompt(
                "check-evaluation-goal", user_goal=case_data.question, local_datetime=datetime.now().isoformat()
            )
            response = await app.LLM_API_HANDLER(prompt=prompt, prompt_name="check-evaluation-goal")
            tweaked_user_goal = response.get("tweaked_user_goal")
            case_data.is_updated = tweaked_user_goal != case_data.question
            case_data.question = tweaked_user_goal

            evaluator = Evaluator(client=client, artifact_folder=f"test/artifacts/{case_data.group_id}/{case_data.id}")
            request_body = WorkflowRequestBody(
                data={
                    "url": case_data.url,
                    "instruction": case_data.question,
                    "answer": case_data.answer,
                },
                proxy_location=proxy_location,
            )
            workflow_run_id = evaluator.queue_skyvern_workflow(
                workflow_pid=workflow_pid, workflow_request=request_body, max_step=case_data.max_steps
            )
            dumped_data = case_data.model_dump()
            dumped_data.update({"workflow_run_id": workflow_run_id})
            print(f"Queued {workflow_run_id} for {case_data.model_dump_json()}")
            f.write(json.dumps(dumped_data) + "\n")
            cnt += 1

    print(f"Queued {cnt} workflows to launch webvoyager evaluation test. saving the records file in {record_file_path}")


def main(
    base_url: str = typer.Option(..., "--base-url", help="base url for Skyvern client"),
    cred: str = typer.Option(..., "--cred", help="credential for Skyvern organization"),
    workflow_pid: str = typer.Option(..., "--workflow-pid", help="workflow pid to execute the evaluation test"),
    proxy_location: Optional[ProxyLocation] = typer.Option(
        None, "--proxy-location", help="overwrite the workflow proxy location"
    ),
) -> None:
    start_forge_app()

    asyncio.run(
        create_workflow_run(base_url=base_url, cred=cred, workflow_pid=workflow_pid, proxy_location=proxy_location)
    )


if __name__ == "__main__":
    typer.run(main)
