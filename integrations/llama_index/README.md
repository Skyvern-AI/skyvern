<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->

- [Skyvern LlamaIndex](#skyvern-llamaindex)
  - [Installation](#installation)
  - [Basic Usage](#basic-usage)
    - [Run a task(sync) locally in your local environment](#run-a-tasksync-locally-in-your-local-environment)
    - [Run a task(async) locally in your local environment](#run-a-taskasync-locally-in-your-local-environment)
    - [Get a task locally in your local environment](#get-a-task-locally-in-your-local-environment)
    - [Run a task(sync) by calling skyvern APIs](#run-a-tasksync-by-calling-skyvern-apis)
    - [Run a task(async) by calling skyvern APIs](#run-a-taskasync-by-calling-skyvern-apis)
    - [Get a task by calling skyvern APIs](#get-a-task-by-calling-skyvern-apis)
  - [Advanced Usage](#advanced-usage)
    - [Dispatch a task(async) locally in your local environment and wait until the task is finished](#dispatch-a-taskasync-locally-in-your-local-environment-and-wait-until-the-task-is-finished)
    - [Dispatch a task(async) by calling skyvern APIs and wait until the task is finished](#dispatch-a-taskasync-by-calling-skyvern-apis-and-wait-until-the-task-is-finished)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Skyvern LlamaIndex

This is a LlamaIndex integration for Skyvern.

## Installation

```bash
pip install skyvern-llamaindex
```

## Basic Usage

### Run a task(sync) locally in your local environment
> sync task won't return until the task is finished.

:warning: :warning: if you want to run this code block, you need to run `skyvern init` command in your terminal to set up skyvern first.


```python
from dotenv import load_dotenv
from llama_index.agent.openai import OpenAIAgent
from llama_index.llms.openai import OpenAI
from skyvern_llamaindex.agent import SkyvernTool

# load OpenAI API key from .env
load_dotenv()

skyvern_tool = SkyvernTool()

agent = OpenAIAgent.from_tools(
    tools=[skyvern_tool.run_task()],
    llm=OpenAI(model="gpt-4o"),
    verbose=True,
)

response = agent.chat("Run a task with Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.'")
print(response)
```

### Run a task(async) locally in your local environment
> async task will return immediately and the task will be running in the background.

:warning: :warning: if you want to run the task in the background, you need to keep the agent running until the task is finished, otherwise the task will be killed when the agent finished the chat.

:warning: :warning: if you want to run this code block, you need to run `skyvern init` command in your terminal to set up skyvern first.

```python
import asyncio
from dotenv import load_dotenv
from llama_index.agent.openai import OpenAIAgent
from llama_index.llms.openai import OpenAI
from skyvern_llamaindex.agent import SkyvernTool
from llama_index.core.tools import FunctionTool

# load OpenAI API key from .env
load_dotenv()

async def sleep(seconds: int) -> str:
    await asyncio.sleep(seconds)
    return f"Slept for {seconds} seconds"

# define a sleep tool to keep the agent running until the task is finished
sleep_tool = FunctionTool.from_defaults(
    async_fn=sleep,
    description="Sleep for a given number of seconds",
    name="sleep",
)

skyvern_tool = SkyvernTool()

agent = OpenAIAgent.from_tools(
    tools=[skyvern_tool.dispatch_task(), sleep_tool],
    llm=OpenAI(model="gpt-4o"),
    verbose=True,
)

response = agent.chat("Run a task with Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.' Then, sleep for 10 minutes.")
print(response)
```

### Get a task locally in your local environment

:warning: :warning: if you want to run this code block, you need to run `skyvern init` command in your terminal to set up skyvern first.

```python
from dotenv import load_dotenv
from llama_index.agent.openai import OpenAIAgent
from llama_index.llms.openai import OpenAI
from skyvern_llamaindex.agent import SkyvernTool

# load OpenAI API key from .env
load_dotenv()

skyvern_tool = SkyvernTool()

agent = OpenAIAgent.from_tools(
    tools=[skyvern_tool.get_task()],
    llm=OpenAI(model="gpt-4o"),
    verbose=True,
)

response = agent.chat("Get the task information with Skyvern. The task id is '<task_id>'.")
print(response)
```

### Run a task(sync) by calling skyvern APIs
> sync task won't return until the task is finished.

no need to run `skyvern init` command in your terminal to set up skyvern before using this integration.

```python
from dotenv import load_dotenv
from llama_index.agent.openai import OpenAIAgent
from llama_index.llms.openai import OpenAI
from skyvern_llamaindex.client import SkyvernTool

# load OpenAI API key from .env
load_dotenv()

skyvern_tool = SkyvernTool(api_key="<your_organization_api_key>")
# or you can load the api_key from SKYVERN_API_KEY in .env
# skyvern_tool = SkyvernTool()

agent = OpenAIAgent.from_tools(
    tools=[skyvern_tool.run_task()],
    llm=OpenAI(model="gpt-4o"),
    verbose=True,
)

response = agent.chat("Run a task with Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.'")
print(response)
```

### Run a task(async) by calling skyvern APIs
> async task will return immediately and the task will be running in the background.

no need to run `skyvern init` command in your terminal to set up skyvern before using this integration.

the task is actually running in the skyvern cloud service, so you don't need to keep your agent running until the task is finished.

```python
from dotenv import load_dotenv
from llama_index.agent.openai import OpenAIAgent
from llama_index.llms.openai import OpenAI
from skyvern_llamaindex.client import SkyvernTool

# load OpenAI API key from .env
load_dotenv()

skyvern_tool = SkyvernTool(api_key="<your_organization_api_key>")
# or you can load the api_key from SKYVERN_API_KEY in .env
# skyvern_tool = SkyvernTool()

agent = OpenAIAgent.from_tools(
    tools=[skyvern_tool.dispatch_task()],
    llm=OpenAI(model="gpt-4o"),
    verbose=True,
)

response = agent.chat("Run a task with Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.'")
print(response)
```


### Get a task by calling skyvern APIs

no need to run `skyvern init` command in your terminal to set up skyvern before using this integration.

```python
from dotenv import load_dotenv
from llama_index.agent.openai import OpenAIAgent
from llama_index.llms.openai import OpenAI
from skyvern_llamaindex.client import SkyvernTool

# load OpenAI API key from .env
load_dotenv()

skyvern_tool = SkyvernTool(api_key="<your_organization_api_key>")
# or you can load the api_key from SKYVERN_API_KEY in .env
# skyvern_tool = SkyvernTool()

agent = OpenAIAgent.from_tools(
    tools=[skyvern_tool.get_task()],
    llm=OpenAI(model="gpt-4o"),
    verbose=True,
)

response = agent.chat("Get the task information with Skyvern. The task id is '<task_id>'.")
print(response)
```

## Advanced Usage

To provide some examples of how to integrate Skyvern with other llama-index tools in the agent.

### Dispatch a task(async) locally in your local environment and wait until the task is finished
> dispatch task will return immediately and the task will be running in the background. You can use `get_task` tool to poll the task information until the task is finished.

:warning: :warning: if you want to run this code block, you need to run `skyvern init` command in your terminal to set up skyvern first.

```python
import asyncio
from dotenv import load_dotenv
from llama_index.agent.openai import OpenAIAgent
from llama_index.llms.openai import OpenAI
from llama_index.core.tools import FunctionTool
from skyvern_llamaindex.agent import SkyvernTool

# load OpenAI API key from .env
load_dotenv()

async def sleep(seconds: int) -> str:
    await asyncio.sleep(seconds)
    return f"Slept for {seconds} seconds"

sleep_tool = FunctionTool.from_defaults(
    async_fn=sleep,
    description="Sleep for a given number of seconds",
    name="sleep",
)

skyvern_tool = SkyvernTool()

agent = OpenAIAgent.from_tools(
    tools=[skyvern_tool.dispatch_task(), skyvern_tool.get_task(), sleep_tool],
    llm=OpenAI(model="gpt-4o"),
    verbose=True,
    max_function_calls=10,
)

response = agent.chat("Run a task with Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.' Then, get this task information until it's completed. The task information re-get interval should be 60s.")
print(response)

```

### Dispatch a task(async) by calling skyvern APIs and wait until the task is finished
> dispatch task will return immediately and the task will be running in the background. You can use `get_task` tool to poll the task information until the task is finished.

no need to run `skyvern init` command in your terminal to set up skyvern before using this integration.

```python
import asyncio
from dotenv import load_dotenv
from llama_index.agent.openai import OpenAIAgent
from llama_index.llms.openai import OpenAI
from llama_index.core.tools import FunctionTool
from skyvern_llamaindex.client import SkyvernTool

# load OpenAI API key from .env
load_dotenv()

async def sleep(seconds: int) -> str:
    await asyncio.sleep(seconds)
    return f"Slept for {seconds} seconds"

sleep_tool = FunctionTool.from_defaults(
    async_fn=sleep,
    description="Sleep for a given number of seconds",
    name="sleep",
)

skyvern_tool = SkyvernTool(api_key="<your_organization_api_key>")
# or you can load the api_key from SKYVERN_API_KEY in .env
# skyvern_tool = SkyvernTool()

agent = OpenAIAgent.from_tools(
    tools=[skyvern_tool.dispatch_task(), skyvern_tool.get_task(), sleep_tool],
    llm=OpenAI(model="gpt-4o"),
    verbose=True,
    max_function_calls=10,
)

response = agent.chat("Run a task with Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.' Then, get this task information until it's completed. The task information re-get interval should be 60s.")
print(response)

```