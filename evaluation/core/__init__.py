import asyncio
import json
import os
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
import requests
from pydantic import BaseModel

from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import create_folder_if_not_exist
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, TaskV2Request
from skyvern.forge.sdk.schemas.tasks import TaskRequest, TaskResponse, TaskStatus
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody, WorkflowRunResponseBase, WorkflowRunStatus
from skyvern.schemas.runs import ProxyLocation


class TaskOutput(BaseModel):
    extracted_information: list | dict[str, Any] | str | None
    final_screenshot: bytes | None


class SkyvernClient:
    def __init__(self, base_url: str, credentials: str):
        self.base_url = base_url
        self.v2_base_url = base_url.replace("/api/v1", "/api/v2")
        self.credentials = credentials

    def generate_curl_params(self, request_body: BaseModel, max_steps: int | None = None) -> tuple[dict, dict]:
        payload = request_body.model_dump_json()
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.credentials,
        }
        if max_steps is not None:
            headers["x-max-steps-override"] = str(max_steps)

        return payload, headers

    def create_task(self, task_request_body: TaskRequest, max_steps: int | None = None) -> str:
        url = f"{self.base_url}/tasks"
        payload, headers = self.generate_curl_params(task_request_body, max_steps=max_steps)
        response = requests.post(url, headers=headers, data=payload)
        assert "task_id" in response.json(), f"Failed to create task: {response.text}"
        return response.json()["task_id"]

    def create_workflow_run(
        self, workflow_pid: str, workflow_request_body: WorkflowRequestBody, max_steps: int | None = None
    ) -> str:
        url = f"{self.base_url}/workflows/{workflow_pid}/run/"
        payload, headers = self.generate_curl_params(workflow_request_body, max_steps=max_steps)
        response = requests.post(url, headers=headers, data=payload)
        assert "workflow_run_id" in response.json(), f"Failed to create workflow run: {response.text}"
        return response.json()["workflow_run_id"]

    def create_task_v2(self, task_v2_request: TaskV2Request, max_steps: int | None = None) -> TaskV2:
        url = f"{self.v2_base_url}/tasks"
        payload, headers = self.generate_curl_params(task_v2_request, max_steps=max_steps)
        response = requests.post(url, headers=headers, data=payload)
        assert "task_id" in response.json(), f"Failed to create task v2: {response.text}"
        return TaskV2.model_validate(response.json())

    def get_task(self, task_id: str) -> TaskResponse:
        """Get a task by id."""
        url = f"{self.base_url}/tasks/{task_id}"
        headers = {"x-api-key": self.credentials}
        response = requests.get(url, headers=headers)
        assert response.status_code == 200, f"Expected to get task response status 200, but got {response.status_code}"
        return TaskResponse(**response.json())

    async def get_workflow_run(self, workflow_pid: str, workflow_run_id: str) -> WorkflowRunResponseBase:
        url = f"{self.base_url}/workflows/{workflow_pid}/runs/{workflow_run_id}"
        headers = {"x-api-key": self.credentials}
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers)
            assert response.status_code == 200, (
                f"Expected to get workflow run response status 200, but got {response.status_code}"
            )
            return WorkflowRunResponseBase(**response.json())


class Evaluator:
    def __init__(self, client: SkyvernClient, artifact_folder: str) -> None:
        self.client = client
        self.artifact_folder = artifact_folder

    async def _wait_for_task_finish(self, task_id: str) -> None:
        while True:
            task_response = self.client.get_task(task_id)
            if task_response.status.is_final():
                return
            await asyncio.sleep(20)

    @staticmethod
    def _download_screenshot(url: str) -> bytes | None:
        if url.startswith("file://"):
            file_path = urlparse(url).path
            with open(file_path, "rb") as f:
                return f.read()

        elif url.startswith("http://") or url.startswith("https://"):
            response = requests.get(url)
            assert response.status_code == 200, (
                f"Expected screenshot download response is 200, but got {response.status_code}"
            )
            return response.content

        return None

    def _get_final_screenshot_from_task(self, task_response: TaskResponse) -> bytes | None:
        screenshot_url: str = ""

        if task_response.screenshot_url is not None:
            screenshot_url = task_response.screenshot_url

        if (
            not screenshot_url
            and task_response.action_screenshot_urls is not None
            and len(task_response.action_screenshot_urls) > 0
        ):
            screenshot_url = task_response.action_screenshot_urls[0]

        assert screenshot_url, (
            f"{task_response.task_id} Expected final screenshot is not None, but got NONE: {task_response.model_dump_json(indent=2)}"
        )

        if screenshot_url.startswith("file://"):
            file_path = urlparse(screenshot_url).path
            with open(file_path, "rb") as f:
                return f.read()

        elif screenshot_url.startswith("http://") or screenshot_url.startswith("https://"):
            response = requests.get(screenshot_url)
            assert response.status_code == 200, (
                f"Expected screenshot download response is 200, but got {response.status_code}"
            )
            return response.content

        return None

    def _save_artifact(self, file_name: str, data: bytes) -> None:
        if not self.artifact_folder:
            return

        create_folder_if_not_exist(self.artifact_folder)
        file_path = os.path.join(self.artifact_folder, f"{int(datetime.now().timestamp())}-{file_name}")
        with open(file_path, "wb") as f:
            f.write(data)

    async def _execute_eval(
        self,
        question: str,
        answer: str,
        extracted_information: list | dict[str, Any] | str | None,
        final_screenshot: bytes,
        is_updated: bool = False,
    ) -> tuple[bool, str]:
        if extracted_information is None:
            extracted_information = ""

        if not isinstance(extracted_information, str):
            extracted_information = json.dumps(extracted_information)

        prompt = prompt_engine.load_prompt(
            "evaluate-prompt",
            ques=question,
            answer=answer,
            is_updated=is_updated,
            extracted_information=extracted_information,
        )
        self._save_artifact("prompt_evaluate_result.txt", prompt.encode())
        self._save_artifact("screenshot_evaluate_result.png", final_screenshot)

        json_response = await app.LLM_API_HANDLER(
            prompt=prompt, screenshots=[final_screenshot], prompt_name="evaluate-prompt"
        )
        self._save_artifact("llm_response_evaluate_result.json", json.dumps(json_response, indent=2).encode())

        verdict = json_response.get("verdict")
        return verdict == "SUCCESS", json_response.get("thoughts", "")

    async def generate_task_request(self, user_prompt: str, proxy: ProxyLocation | None = None) -> TaskRequest:
        prompt = prompt_engine.load_prompt("generate-task", user_prompt=user_prompt)
        self._save_artifact("prompt_generate_task.txt", prompt.encode())

        json_response = await app.LLM_API_HANDLER(prompt=prompt, prompt_name="generate-task")
        self._save_artifact("llm_response_generate_task.json", json.dumps(json_response, indent=2).encode())
        task = TaskRequest(
            title=json_response["suggested_title"],
            url=json_response["url"],
            navigation_goal=json_response["navigation_goal"],
            navigation_payload=json_response["navigation_payload"],
            data_extraction_goal=json_response["data_extraction_goal"],
            proxy_location=proxy,
        )
        return task

    def queue_skyvern_task(self, task: TaskRequest, max_step: int | None = None) -> str:
        task_id = self.client.create_task(task_request_body=task, max_steps=max_step)
        self._save_artifact("task_id.txt", task_id.encode())
        assert task_id
        return task_id

    def queue_skyvern_workflow(
        self, workflow_pid: str, workflow_request: WorkflowRequestBody, max_step: int | None = None
    ) -> str:
        workflow_run_id = self.client.create_workflow_run(
            workflow_pid=workflow_pid, workflow_request_body=workflow_request, max_steps=max_step
        )
        self._save_artifact("workflow_run_id.txt", workflow_run_id.encode())
        assert workflow_run_id
        return workflow_run_id

    def queue_skyvern_task_v2(self, cruise_request: TaskV2Request, max_step: int | None = None) -> TaskV2:
        cruise = self.client.create_task_v2(task_v2_request=cruise_request, max_steps=max_step)
        self._save_artifact("cruise.json", cruise.model_dump_json(indent=2).encode())
        return cruise

    async def eval_skyvern_task(
        self,
        task_id: str,
        question: str,
        answer: str,
    ) -> None:
        task_response = self.client.get_task(task_id=task_id)
        assert task_response.status == TaskStatus.completed, f"{task_id} Expected completed, but {task_response.status}"
        final_screenshot = self._get_final_screenshot_from_task(task_response=task_response)
        assert final_screenshot is not None, f"{task_id} Expected final screenshot, but got None"

        ok, reasoning = await self._execute_eval(
            question=question,
            answer=answer,
            extracted_information=task_response.extracted_information,
            final_screenshot=final_screenshot,
        )
        assert ok, f"{task_id} failed due to {reasoning}"

    async def eval_skyvern_workflow_run(
        self,
        workflow_pid: str,
        workflow_run_id: str,
        question: str,
        answer: str,
        is_updated: bool,
    ) -> None:
        workflow_run_response = await self.client.get_workflow_run(
            workflow_pid=workflow_pid, workflow_run_id=workflow_run_id
        )
        assert workflow_run_response.status == WorkflowRunStatus.completed, (
            f"Expected {workflow_pid + '/' + workflow_run_id} completed, but {workflow_run_response.status}"
        )
        assert workflow_run_response.screenshot_urls and len(workflow_run_response.screenshot_urls) > 0, (
            f"Expected {workflow_pid + '/' + workflow_run_id} with screenshots, but got empty"
        )
        final_screenshot = self._download_screenshot(workflow_run_response.screenshot_urls[0])
        assert final_screenshot is not None, (
            f"Expected {workflow_pid + '/' + workflow_run_id} final screenshot, but got None"
        )

        extracted_information: list | dict[str, Any] | str | None = None
        if workflow_run_response.task_v2 is None:
            assert workflow_run_response.outputs and len(workflow_run_response.outputs) > 0, (
                f"Expected {workflow_pid + '/' + workflow_run_id} with output, but got empty output"
            )

            label, result = workflow_run_response.outputs.popitem()
            if isinstance(result, dict):
                extracted_information = result.get("extracted_information")
            else:
                # FIXME: improve this when the last block is loop block
                extracted_information = result
        else:
            workflow_run_response.task_v2.summary
            workflow_run_response.task_v2.output
            summary = f"{('summary:' + workflow_run_response.task_v2.summary) if workflow_run_response.task_v2.summary else ''}"
            output = f"{('output: ' + json.dumps(workflow_run_response.task_v2.output)) if workflow_run_response.task_v2.output else ''}"
            extracted_information = ""
            if summary:
                extracted_information = summary

            if output:
                if extracted_information:
                    extracted_information = extracted_information + "\n" + output
                else:
                    extracted_information = output

        ok, reasoning = await self._execute_eval(
            question=question,
            answer=answer,
            extracted_information=extracted_information,
            final_screenshot=final_screenshot,
            is_updated=is_updated,
        )
        assert ok, f"{workflow_pid + '/' + workflow_run_id} failed due to {reasoning}"

    async def create_and_eval_skyvern_task(
        self, task: TaskRequest, question: str, answer: str, max_step: int | None = None
    ) -> None:
        task_id = self.client.create_task(task_request_body=task, max_steps=max_step)
        self._save_artifact("task_id.txt", task_id.encode())
        await self._wait_for_task_finish(task_id=task_id)
        # (?) looks like there's a bug on agent side:
        # sometimes the screenshot_url is NONE if the task is finished. but if we query again later, the value appeared.
        await asyncio.sleep(30)
        await self.eval_skyvern_task(task_id=task_id, question=question, answer=answer)
