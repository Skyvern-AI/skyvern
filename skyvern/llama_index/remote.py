from llama_index.core.tools import FunctionTool

from skyvern.agent.parameter import GetTaskSchema, RunTaskV1Schema, RunTaskV2Schema, TaskV1Request, TaskV2Request
from skyvern.agent.remote import RemoteAgent

run_task_v1 = FunctionTool.from_defaults(
    async_fn=lambda task, api_key, endpoint="https://api.skyvern.com": RemoteAgent(api_key, endpoint).run_task_v1(
        TaskV1Request.model_validate(task)
    ),
    name="run-remote-skyvern-simple-task",
    description="Use remote Skyvern to run a v1 task. v1 task is usually used for the simple tasks.",
    fn_schema=RunTaskV1Schema,
)


get_task_v1 = FunctionTool.from_defaults(
    async_fn=lambda task_id, api_key, endpoint="https://api.skyvern.com": RemoteAgent(api_key, endpoint).get_task_v1(
        task_id
    ),
    name="get-remote-skyvern-simple-task",
    description="Use remote Skyvern to get a v1 task information. v1 task is usually used for the simple tasks.",
    fn_schema=GetTaskSchema,
)


run_task_v2 = FunctionTool.from_defaults(
    async_fn=lambda task, api_key, endpoint="https://api.skyvern.com": RemoteAgent(api_key, endpoint).run_task_v2(
        TaskV2Request.model_validate(task)
    ),
    name="run-remote-skyvern-complicated-task",
    description="Use remote Skyvern to run a v2 task. v2 task is usually used for the complicated tasks.",
    fn_schema=RunTaskV2Schema,
)


get_task_v2 = FunctionTool.from_defaults(
    async_fn=lambda task_id, api_key, endpoint="https://api.skyvern.com": RemoteAgent(api_key, endpoint).get_task_v2(
        task_id
    ),
    name="get-remote-skyvern-complicated-task",
    description="Use remote Skyvern to get a v2 task information. v2 task is usually used for the complicated tasks.",
    fn_schema=GetTaskSchema,
)
