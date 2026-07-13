from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from evaluation.core.utils import WebVoyagerTestCase
from evaluation.script.run_webvoyager_task_v2 import _submit_case, _web_name


def test_webvoyager_case_web_name_comes_from_case_id() -> None:
    case = WebVoyagerTestCase(
        group_id="group",
        id="ArXiv--7",
        url="https://arxiv.org/",
        question="Find a paper.",
        answer="A paper",
    )

    assert _web_name(case) == "ArXiv"


@pytest.mark.asyncio
async def test_submit_case_passes_custom_proxy_to_task_v2() -> None:
    client = Mock()
    client.create_task_v2.return_value = SimpleNamespace(
        workflow_permanent_id="wpid_1",
        workflow_run_id="wr_1",
        observer_cruise_id="tsk_v2_1",
    )
    case = WebVoyagerTestCase(
        group_id="group",
        id="Allrecipes--7",
        url="https://www.allrecipes.com/",
        question="Find a recipe.",
        answer="A recipe",
    )

    result = await _submit_case(client, case, "tokenless-pro", asyncio.Semaphore(1), "http://proxy:8080")

    request = client.create_task_v2.call_args.args[0]
    assert request.proxy_location == {"url": "http://proxy:8080"}
    assert result["workflow_run_id"] == "wr_1"
