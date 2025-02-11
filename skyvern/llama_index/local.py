from typing import Any, Dict
from llama_index.core.tools import FunctionTool

from skyvern.agent.local import Agent
from skyvern.agent.parameter import TaskV1Request, TaskV2Request

run_task_v1 = FunctionTool.from_defaults(
    async_fn=lambda **kwargs: Agent().run_task_v1(TaskV1Request(**kwargs)),
    name="run-local-skyvern-simple-task",
    description="Use local Skyvern to run a v1 task. v1 task is usually used for the simple tasks.",
    fn_schema=TaskV1Request,
)

run_task_v2 = FunctionTool.from_defaults(
    async_fn=lambda **kwargs: Agent().run_task_v2(TaskV2Request(**kwargs)),
    name="run-local-skyvern-complicated-task",
    description="Use local Skyvern to run a v2 task. v2 task is usually used for the complicated tasks.",
    fn_schema=TaskV2Request,
)
