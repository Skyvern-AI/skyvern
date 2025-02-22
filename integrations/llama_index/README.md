<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents**  *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Skyvern LlamaIndex](#skyvern-llamaindex)
  - [Installation](#installation)
  - [Usage](#usage)
    - [Run a task(sync) with skyvern agent (calling skyvern agent function directly in the tool)](#run-a-tasksync-with-skyvern-agent-calling-skyvern-agent-function-directly-in-the-tool)
    - [Dispatch a task(async) with skyvern agent (calling skyvern agent function directly in the tool)](#dispatch-a-taskasync-with-skyvern-agent-calling-skyvern-agent-function-directly-in-the-tool)
    - [Run a task(sync) with skyvern client (calling skyvern OpenAPI in the tool)](#run-a-tasksync-with-skyvern-client-calling-skyvern-openapi-in-the-tool)
    - [Dispatch a task(async) with skyvern client (calling skyvern OpenAPI in the tool)](#dispatch-a-taskasync-with-skyvern-client-calling-skyvern-openapi-in-the-tool)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Skyvern LlamaIndex

This is a LlamaIndex integration for Skyvern.

## Installation

```bash
pip install skyvern-llamaindex
```

## Usage

### Run a task(sync) with skyvern agent (calling skyvern agent function directly in the tool)
> sync task won't return until the task is finished.

:warning: :warning: if you want to run this code block, you need to run `skyvern init --openai-api-key <your_openai_api_key>` command in your terminal to set up skyvern first.


```python
import asyncio
from dotenv import load_dotenv
from llama_index.agent.openai import OpenAIAgent
from llama_index.llms.openai import OpenAI
from skyvern_llamaindex.agent import SkyvernTaskToolSpec

# load OpenAI API key from .env
load_dotenv()

skyvern_tool = SkyvernTaskToolSpec()

tools = skyvern_tool.to_tool_list(["run"])

agent = OpenAIAgent.from_tools(
    tools=tools,
    llm=OpenAI(model="gpt-4o"),
    verbose=True,
    max_function_calls=10,
)

# to run skyvern agent locally, must run `skyvern init` first
response = agent.chat("Run the task with skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.'")
print(response)
```

### Dispatch a task(async) with skyvern agent (calling skyvern agent function directly in the tool)
> dispatch task will return immediately and the task will be running in the background. You can use `get` tool to poll the task information until the task is finished.

:warning: :warning: if you want to run this code block, you need to run `skyvern init --openai-api-key <your_openai_api_key>` command in your terminal to set up skyvern first.

```python
import asyncio
from dotenv import load_dotenv
from llama_index.agent.openai import OpenAIAgent
from llama_index.llms.openai import OpenAI
from llama_index.core.tools import FunctionTool
from skyvern_llamaindex.agent import SkyvernTaskToolSpec

async def sleep(seconds: int) -> str:
    await asyncio.sleep(seconds)
    return f"Slept for {seconds} seconds"

# load OpenAI API key from .env
load_dotenv()

skyvern_tool = SkyvernTaskToolSpec()

sleep_tool = FunctionTool.from_defaults(
    async_fn=sleep,
    description="Sleep for a given number of seconds",
    name="sleep",
)

tools = skyvern_tool.to_tool_list(["dispatch", "get"])
tools.append(sleep_tool)

agent = OpenAIAgent.from_tools(
    tools=tools,
    llm=OpenAI(model="gpt-4o"),
    verbose=True,
    max_function_calls=10,
)

response = agent.chat("Run a task with Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.' Then, get this task information until it's completed. The task information re-get interval should be 60s.")
print(response)

```

### Run a task(sync) with skyvern client (calling skyvern OpenAPI in the tool)
> sync task won't return until the task is finished.

no need to run `skyvern init` command in your terminal to set up skyvern before using this integration.

```python
import asyncio
from dotenv import load_dotenv
from llama_index.agent.openai import OpenAIAgent
from llama_index.llms.openai import OpenAI
from skyvern_llamaindex.client import SkyvernTaskToolSpec


async def sleep(seconds: int) -> str:
    await asyncio.sleep(seconds)
    return f"Slept for {seconds} seconds"

# load OpenAI API key from .env
load_dotenv()

skyvern_client_tool = SkyvernTaskToolSpec(
    credential="<your_organization_api_key>",
)

tools = skyvern_client_tool.to_tool_list(["run"])

agent = OpenAIAgent.from_tools(
    tools=tools,
    llm=OpenAI(model="gpt-4o"),
    verbose=True,
    max_function_calls=10,
)

response = agent.chat("Run the task with skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.'")
print(response)

```

### Dispatch a task(async) with skyvern client (calling skyvern OpenAPI in the tool)
> dispatch task will return immediately and the task will be running in the background. You can use `get` tool to poll the task information until the task is finished.

no need to run `skyvern init` command in your terminal to set up skyvern before using this integration.

```python
import asyncio
from dotenv import load_dotenv
from llama_index.agent.openai import OpenAIAgent
from llama_index.llms.openai import OpenAI
from llama_index.core.tools import FunctionTool
from skyvern_llamaindex.client import SkyvernTaskToolSpec


async def sleep(seconds: int) -> str:
    await asyncio.sleep(seconds)
    return f"Slept for {seconds} seconds"

# load OpenAI API key from .env
load_dotenv()

skyvern_client_tool = SkyvernTaskToolSpec(
    credential="<your_organization_api_key>",
)

sleep_tool = FunctionTool.from_defaults(
    async_fn=sleep,
    description="Sleep for a given number of seconds",
    name="sleep",
)

tools = skyvern_client_tool.to_tool_list(["dispatch", "get"])
tools.append(sleep_tool)

agent = OpenAIAgent.from_tools(
    tools=tools,
    llm=OpenAI(model="gpt-4o"),
    verbose=True,
    max_function_calls=10,
)

response = agent.chat("Run a task with Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.' Then, get this task information until it's completed. The task information re-get interval should be 60s.")
print(response)

```