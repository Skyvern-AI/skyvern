<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->

- [Skyvern Langchain](#skyvern-langchain)
  - [Installation](#installation)
  - [Basic Usage](#basic-usage)
    - [Run a task(sync) locally in your local environment](#run-a-tasksync-locally-in-your-local-environment)
    - [Run a task(async) locally in your local environment](#run-a-taskasync-locally-in-your-local-environment)
    - [Get a task locally in your local environment](#get-a-task-locally-in-your-local-environment)
    - [Run a task(sync) by calling skyvern APIs](#run-a-tasksync-by-calling-skyvern-apis)
    - [Run a task(async) by calling skyvern APIs](#run-a-taskasync-by-calling-skyvern-apis)
    - [Get a task by calling skyvern APIs](#get-a-task-by-calling-skyvern-apis)
  - [Agent Usage](#agent-usage)
    - [Run a task(async) locally in your local environment and wait until the task is finished](#run-a-taskasync-locally-in-your-local-environment-and-wait-until-the-task-is-finished)
    - [Run a task(async) by calling skyvern APIs and wait until the task is finished](#run-a-taskasync-by-calling-skyvern-apis-and-wait-until-the-task-is-finished)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Skyvern Langchain

This is a langchain integration for Skyvern.

## Installation

```bash
pip install skyvern-langchain
```

To run the example scenarios, you might need to install other langchain dependencies.
```bash
pip install langchain-openai
pip install langchain-community
```

## Basic Usage

This is the only basic usage of skyvern langchain tool. If you want a full langchain integration experience, please refer to the [Agent Usage](#agent-usage) section to play with langchain agent.

Go to [Langchain Tools](https://python.langchain.com/v0.1/docs/modules/tools/) to see more advanced langchain tool usage.


### Run a task(sync) locally in your local environment
> sync task won't return until the task is finished.

:warning: :warning: if you want to run this code block, you need to run `skyvern init` command in your terminal to set up skyvern first.


```python
import asyncio
from skyvern_langchain.agent import RunTask

run_task = RunTask()

async def main():
    # to run skyvern agent locally, must run `skyvern init` first
    print(await run_task.ainvoke("Navigate to the Hacker News homepage and get the top 3 posts."))


if __name__ == "__main__":
    asyncio.run(main())
```

### Run a task(async) locally in your local environment
> async task will return immediately and the task will be running in the background.

:warning: :warning: if you want to run the task in the background, you need to keep the script running until the task is finished, otherwise the task will be killed when the script is finished.

:warning: :warning: if you want to run this code block, you need to run `skyvern init` command in your terminal to set up skyvern first.

```python
import asyncio
from skyvern_langchain.agent import DispatchTask

dispatch_task = DispatchTask()

async def main():
    # to run skyvern agent locally, must run `skyvern init` first
    print(await dispatch_task.ainvoke("Navigate to the Hacker News homepage and get the top 3 posts."))

    # keep the script running until the task is finished
    await asyncio.sleep(600)


if __name__ == "__main__":
    asyncio.run(main())

```

### Get a task locally in your local environment

:warning: :warning: if you want to run this code block, you need to run `skyvern init` command in your terminal to set up skyvern first.

```python
import asyncio
from skyvern_langchain.agent import GetTask

get_task = GetTask()

async def main():
    # to run skyvern agent locally, must run `skyvern init` first
    print(await get_task.ainvoke("<task_id>"))


if __name__ == "__main__":
    asyncio.run(main())

```

### Run a task(sync) by calling skyvern APIs
> sync task won't return until the task is finished.

no need to run `skyvern init` command in your terminal to set up skyvern before using this integration.

```python
import asyncio
from skyvern_langchain.client import RunTask

run_task = RunTask(
    api_key="<your_organization_api_key>",
)
# or you can load the api_key from SKYVERN_API_KEY in .env
# run_task = RunTask()

async def main():
    print(await run_task.ainvoke("Navigate to the Hacker News homepage and get the top 3 posts."))


if __name__ == "__main__":
    asyncio.run(main())
```

### Run a task(async) by calling skyvern APIs
> async task will return immediately and the task will be running in the background.

no need to run `skyvern init` command in your terminal to set up skyvern before using this integration.

the task is actually running in the skyvern cloud service, so you don't need to keep your script running until the task is finished.

```python
import asyncio
from skyvern_langchain.client import DispatchTask

dispatch_task = DispatchTask(
    api_key="<your_organization_api_key>",
)
# or you can load the api_key from SKYVERN_API_KEY in .env
# dispatch_task = DispatchTask()

async def main():
    print(await dispatch_task.ainvoke("Navigate to the Hacker News homepage and get the top 3 posts."))


if __name__ == "__main__":
    asyncio.run(main())
```


### Get a task by calling skyvern APIs
> async task will return immediately and the task will be running in the background.

no need to run `skyvern init` command in your terminal to set up skyvern before using this integration.

the task is actually running in the skyvern cloud service, so you don't need to keep your script running until the task is finished.

```python
import asyncio
from skyvern_langchain.client import GetTask

get_task = GetTask(
    api_key="<your_organization_api_key>",
)
# or you can load the api_key from SKYVERN_API_KEY in .env
# get_task = GetTask()

async def main():
    print(await get_task.ainvoke("<task_id>"))


if __name__ == "__main__":
    asyncio.run(main())
```

## Agent Usage

Langchain is more powerful when used with [Langchain Agents](https://python.langchain.com/v0.1/docs/modules/agents/).

The following two examples show how to build an agent that executes a specified task, waits for its completion, and then returns the results. For example, the agent is tasked with navigating to the Hacker News homepage and retrieving the top three posts.


### Run a task(async) locally in your local environment and wait until the task is finished

> async task will return immediately and the task will be running in the background. You can use `GetTask` tool to poll the task information until the task is finished.

:warning: :warning: if you want to run this code block, you need to run `skyvern init` command in your terminal to set up skyvern first.

```python
import asyncio
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.agents import initialize_agent, AgentType
from skyvern_langchain.agent import DispatchTask, GetTask

from langchain_community.tools.sleep.tool import SleepTool

# load OpenAI API key from .env
load_dotenv()

llm = ChatOpenAI(model="gpt-4o", temperature=0)

dispatch_task = DispatchTask()
get_task = GetTask()

agent = initialize_agent(
    llm=llm,
    tools=[
        dispatch_task,
        get_task,
        SleepTool(),
    ],
    verbose=True,
    agent=AgentType.STRUCTURED_CHAT_ZERO_SHOT_REACT_DESCRIPTION,
)


async def main():
    # use sleep tool to set up the polling logic until the task is completed, if you only want to dispatch a task, you can remove the sleep tool
    print(await agent.ainvoke("Run a task with Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.' Then, get this task information until it's completed. The task information re-get interval should be 60s."))


if __name__ == "__main__":
    asyncio.run(main())

```

### Run a task(async) by calling skyvern APIs and wait until the task is finished

> async task will return immediately and the task will be running in the background. You can use `GetTask` tool to poll the task information until the task is finished.

no need to run `skyvern init` command in your terminal to set up skyvern before using this integration.

```python
import asyncio
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.agents import initialize_agent, AgentType
from skyvern_langchain.client import DispatchTask, GetTask

from langchain_community.tools.sleep.tool import SleepTool

# load OpenAI API key from .env
load_dotenv()

llm = ChatOpenAI(model="gpt-4o", temperature=0)

dispatch_task = DispatchTask(
    api_key="<your_organization_api_key>",
)
# or you can load the api_key from SKYVERN_API_KEY in .env
# dispatch_task = DispatchTask()

get_task = GetTask(
    api_key="<your_organization_api_key>",
)
# or you can load the api_key from SKYVERN_API_KEY in .env
# get_task = GetTask()

agent = initialize_agent(
    llm=llm,
    tools=[
        dispatch_task,
        get_task,
        SleepTool(),
    ],
    verbose=True,
    agent=AgentType.STRUCTURED_CHAT_ZERO_SHOT_REACT_DESCRIPTION,
)


async def main():
    # use sleep tool to set up the polling logic until the task is completed, if you only want to dispatch a task, you can remove the sleep tool
    print(await agent.ainvoke("Run a task with Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.' Then, get this task information until it's completed. The task information re-get interval should be 60s."))


if __name__ == "__main__":
    asyncio.run(main())
```