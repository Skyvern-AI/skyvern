import json
from typing import Iterator
from uuid import uuid4

from pydantic import BaseModel


class WebVoyagerTestCase(BaseModel):
    group_id: str
    id: str
    url: str
    question: str
    answer: str
    is_updated: bool = False
    max_steps: int | None = None


class WorkflowRunResultRequest(BaseModel):
    id: str
    workflow_run_id: str


def load_webvoyager_case_from_json(file_path: str, group_id: str = "") -> Iterator[WebVoyagerTestCase]:
    with open("evaluation/datasets/webvoyager_reference_answer.json") as answer_file:
        webvoyager_answers: dict = json.load(answer_file)

    if not group_id:
        group_id = str(uuid4())

    with open(file_path, encoding="utf-8") as file:
        for line in file:
            test_case: dict[str, str] = json.loads(line)
            web_name, id = test_case["id"].split("--")
            for answer in webvoyager_answers[web_name]["answers"]:
                if str(answer["id"]) == id:
                    ans = answer["ans"]
                    yield WebVoyagerTestCase(
                        group_id=group_id,
                        id=test_case["id"],
                        url=test_case["web"],
                        question=test_case["ques"],
                        answer=ans,
                    )
                    break
            else:
                raise Exception("no answer for the task")


def load_records_from_json(file_path: str) -> Iterator[WorkflowRunResultRequest]:
    with open(file_path, encoding="utf-8") as f:
        for line in f:
            item: dict[str, str] = json.loads(line)
            id = item["id"]
            workflow_run_id = item["workflow_run_id"]
            yield WorkflowRunResultRequest(
                id=id,
                workflow_run_id=workflow_run_id,
            )
