import json
import os

import aiofiles

from skyvern.forge.sdk.api.files import get_skyvern_temp_dir


def get_skyvern_state_file_path() -> str:
    return f"{get_skyvern_temp_dir()}/current.json"


async def initialize_skyvern_state_file(
    task_id: str | None = None, workflow_run_id: str | None = None, organization_id: str | None = None
) -> None:
    # create the file if it doesn't exist
    async with aiofiles.open(get_skyvern_state_file_path(), "w") as json_file:
        await json_file.write(
            json.dumps({"task_id": task_id, "workflow_run_id": workflow_run_id, "organization_id": organization_id})
        )


def get_json_from_file(file_path: str) -> dict[str, str]:
    # check if file exists
    if not os.path.exists(file_path):
        return {}

    with open(file_path) as json_file:
        return json.load(json_file)
