import asyncio
import os
import subprocess

import structlog
import typer

from skyvern.forge import app
from skyvern.forge.sdk.api.files import get_skyvern_temp_dir
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.utils.files import get_json_from_file, get_skyvern_state_file_path, initialize_skyvern_state_file

INTERVAL = 1
LOG = structlog.get_logger()


async def run() -> None:
    await initialize_skyvern_state_file(task_id=None, workflow_run_id=None, organization_id=None)

    while True:
        await asyncio.sleep(INTERVAL)
        try:
            current_json = get_json_from_file(get_skyvern_state_file_path())
        except Exception:
            continue

        task_id = current_json.get("task_id")
        workflow_run_id = current_json.get("workflow_run_id")
        organization_id = current_json.get("organization_id")
        if not organization_id or (not task_id and not workflow_run_id):
            continue

        try:
            if workflow_run_id:
                workflow_run = await app.DATABASE.get_workflow_run(workflow_run_id=workflow_run_id)
                if not workflow_run or workflow_run.status in [
                    WorkflowRunStatus.completed,
                    WorkflowRunStatus.failed,
                    WorkflowRunStatus.terminated,
                ]:
                    continue
                file_name = f"{workflow_run_id}.png"

            elif task_id:
                task = await app.DATABASE.get_task(task_id=task_id, organization_id=organization_id)
                if not task or task.status.is_final():
                    continue
                file_name = f"{task_id}.png"
            else:
                continue
        except Exception:
            LOG.exception(
                "Failed to get task or workflow run while taking streaming screenshot in worker",
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            continue

        # create f"{get_skyvern_temp_dir()}/{organization_id}" directory if it does not exists
        os.makedirs(f"{get_skyvern_temp_dir()}/{organization_id}", exist_ok=True)
        png_file_path = f"{get_skyvern_temp_dir()}/{organization_id}/{file_name}"

        # run subprocess to take screenshot
        subprocess.run(
            f"xwd -root | xwdtopnm 2>/dev/null | pnmtopng > {png_file_path}", shell=True, env={"DISPLAY": ":99"}
        )

        try:
            await app.STORAGE.save_streaming_file(organization_id, file_name)
        except Exception:
            LOG.debug("Failed to upload screenshot", organization_id=organization_id, file_name=file_name)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    typer.run(main)
