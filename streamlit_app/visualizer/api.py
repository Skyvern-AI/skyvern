import json
from typing import Any

import curlify
import requests
from requests import PreparedRequest

from skyvern.forge.sdk.schemas.tasks import TaskRequest


class SkyvernClient:
    def __init__(self, base_url: str, credentials: str):
        self.base_url = base_url
        self.credentials = credentials

    def generate_curl_params(self, task_request_body: TaskRequest) -> PreparedRequest:
        url = f"{self.base_url}/tasks"
        payload = task_request_body.model_dump()
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.credentials,
        }

        return url, payload, headers

    def create_task(self, task_request_body: TaskRequest) -> str | None:
        url, payload, headers = self.generate_curl_params(task_request_body)

        response = requests.post(url, headers=headers, data=json.dumps(payload))
        if "task_id" not in response.json():
            return None
        return response.json()["task_id"]

    def copy_curl(self, task_request_body: TaskRequest) -> str:
        url, payload, headers = self.generate_curl_params(task_request_body)

        req = requests.Request("POST", url, headers=headers, data=json.dumps(payload, indent=4))

        return curlify.to_curl(req.prepare())

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Get a task by id."""
        url = f"{self.base_url}/internal/tasks/{task_id}"
        headers = {"x-api-key": self.credentials}
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            return None
        return response.json()

    def get_agent_tasks(self, page: int = 1, page_size: int = 15) -> dict[str, Any]:
        """Get all tasks with pagination."""
        url = f"{self.base_url}/internal/tasks"
        params = {"page": page, "page_size": page_size}
        headers = {"x-api-key": self.credentials}
        response = requests.get(url, params=params, headers=headers)
        return response.json()

    def get_agent_task_steps(self, task_id: str, page: int = 1, page_size: int = 15) -> list[dict[str, Any]]:
        """Get all steps for a task with pagination."""
        url = f"{self.base_url}/tasks/{task_id}/steps"
        params = {"page": page, "page_size": page_size}
        headers = {"x-api-key": self.credentials}
        response = requests.get(url, params=params, headers=headers)
        steps = response.json()
        for step in steps:
            step["output"]["actions_and_results"] = json.dumps(step["output"]["actions_and_results"])
        return steps

    def get_agent_task_video_artifact(self, task_id: str) -> dict[str, Any] | None:
        """Get the video artifact from the first step artifact of the task."""
        steps = self.get_agent_task_steps(task_id)
        if not steps:
            return None

        first_step_id = steps[0]["step_id"]
        artifacts = self.get_agent_artifacts(task_id, first_step_id)
        for artifact in artifacts:
            if artifact["artifact_type"] == "recording":
                return artifact

        return None

    def get_agent_artifacts(self, task_id: str, step_id: str) -> list[dict[str, Any]]:
        """Get all artifacts for a list of steps."""
        url = f"{self.base_url}/tasks/{task_id}/steps/{step_id}/artifacts"
        headers = {"x-api-key": self.credentials}
        response = requests.get(url, headers=headers)
        return response.json()
