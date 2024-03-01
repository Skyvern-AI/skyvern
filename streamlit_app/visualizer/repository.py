from typing import Any, Optional

from streamlit_app.visualizer.api import SkyvernClient


class TaskRepository:
    def __init__(self, client: SkyvernClient):
        self.client = client

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self.client.get_task(task_id)

    def get_tasks(self, page: int = 1, page_size: int = 15) -> dict[str, Any]:
        """Get tasks with pagination."""
        return self.client.get_agent_tasks(page=page, page_size=page_size)

    def get_task_steps(self, task_id: str) -> list[dict[str, Any]]:
        """Get steps for a specific task with pagination."""
        return self.client.get_agent_task_steps(task_id)

    def get_artifacts(self, task_id: str, step_id: str) -> list[dict[str, Any]]:
        """Get artifacts for a specific task and steps."""
        return self.client.get_agent_artifacts(task_id, step_id)

    def get_task_recording_uri(self, task: dict[str, Any]) -> Optional[str]:
        """Get the recording URI for a task."""
        video_artifact = self.client.get_agent_task_video_artifact(task["task_id"])
        if video_artifact is None:
            return None
        return video_artifact["uri"]
