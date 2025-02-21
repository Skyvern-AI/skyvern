<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents**  *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Skyvern Langchain](#skyvern-langchain)
  - [Installation](#installation)
  - [Usage](#usage)
    - [Run a task(sync) with skyvern agent (calling skyvern agent function directly in the tool)](#run-a-tasksync-with-skyvern-agent-calling-skyvern-agent-function-directly-in-the-tool)
    - [Run a task(async) with skyvern agent (calling skyvern agent function directly in the tool)](#run-a-taskasync-with-skyvern-agent-calling-skyvern-agent-function-directly-in-the-tool)
    - [Run a task(sync) with skyvern client (calling skyvern OpenAPI in the tool)](#run-a-tasksync-with-skyvern-client-calling-skyvern-openapi-in-the-tool)
    - [Run a task(async) with skyvern client (calling skyvern OpenAPI in the tool)](#run-a-taskasync-with-skyvern-client-calling-skyvern-openapi-in-the-tool)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Skyvern Langchain

This is a langchain integration for Skyvern.

## Installation

```bash
pip install skyvern-langchain
```

## Usage

### Run a task(sync) with skyvern agent (calling skyvern agent function directly in the tool)
> sync task won't return until the task is finished.

:warning: :warning: if you want to run this code block, you need to run `skyvern init` command in your terminal to set up skyvern first.


```python
import asyncio
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.agents import initialize_agent, AgentType
from skyvern_langchain.agent import run_task_v2

load_dotenv()

llm = ChatOpenAI(model="gpt-4o", temperature=0)

agent = initialize_agent(
    llm=llm,
    tools=[run_task_v2],
    verbose=True,
    agent=AgentType.STRUCTURED_CHAT_ZERO_SHOT_REACT_DESCRIPTION,
)


async def main():
    # to run skyvern agent locally, must run `skyvern init` first
    print(await agent.ainvoke("Run a task with Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.'"))


if __name__ == "__main__":
    asyncio.run(main())
```

### Run a task(async) with skyvern agent (calling skyvern agent function directly in the tool)
> async task will return immediately and the task will be running in the background. You can use `get_task_v2` tool to poll the task information until the task is finished.

:warning: :warning: if you want to run this code block, you need to run `skyvern init` command in your terminal to set up skyvern first.

```python
import asyncio
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.agents import initialize_agent, AgentType
from skyvern_langchain.agent import queue_task_v2, get_task_v2

from langchain_community.tools.sleep.tool import SleepTool

load_dotenv()

llm = ChatOpenAI(model="gpt-4o", temperature=0)

agent = initialize_agent(
    llm=llm,
    tools=[
        queue_task_v2,
        get_task_v2,
        SleepTool(),
    ],
    verbose=True,
    agent=AgentType.STRUCTURED_CHAT_ZERO_SHOT_REACT_DESCRIPTION,
)


async def main():
    # use sleep tool to set up the polling logic until the task is completed, if you only want to queue a task, you can remove the sleep tool
    print(await agent.ainvoke("Queue a task with Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.' Then, get this task information until it's completed. The task information re-get interval should be 60s."))


if __name__ == "__main__":
    asyncio.run(main())

```

### Run a task(sync) with skyvern client (calling skyvern OpenAPI in the tool)
> sync task won't return until the task is finished.

no need to run `skyvern init` command in your terminal to set up skyvern before using this integration.

```python
import asyncio
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.agents import initialize_agent, AgentType
from skyvern_langchain.client import RunSkyvernClientTaskV2Tool

load_dotenv()

llm = ChatOpenAI(model="gpt-4o", temperature=0)

run_task_v2 = RunSkyvernClientTaskV2Tool(
    credential="<your_organization_api_key>",
)

agent = initialize_agent(
    llm=llm,
    tools=[run_task_v2],
    verbose=True,
    agent=AgentType.STRUCTURED_CHAT_ZERO_SHOT_REACT_DESCRIPTION,
)

async def main():
    print(await agent.ainvoke("Run a task with Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.'"))


if __name__ == "__main__":
    asyncio.run(main())
```

### Run a task(async) with skyvern client (calling skyvern OpenAPI in the tool)
> async task will return immediately and the task will be running in the background. You can use `GetSkyvernClientTaskV2Tool` tool to poll the task information until the task is finished.

no need to run `skyvern init` command in your terminal to set up skyvern before using this integration.

```python
import asyncio
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.agents import initialize_agent, AgentType
from skyvern_langchain.client import (
    QueueSkyvernClientTaskV2Tool,
    GetSkyvernClientTaskV2Tool,
)

from langchain_community.tools.sleep.tool import SleepTool

load_dotenv()

llm = ChatOpenAI(model="gpt-4o", temperature=0)

queue_task_v2 = QueueSkyvernClientTaskV2Tool(
    credential="<your_organization_api_key>",
)

get_task_v2 = GetSkyvernClientTaskV2Tool(
    credential="<your_organization_api_key>",
)

agent = initialize_agent(
    llm=llm,
    tools=[
        queue_task_v2,
        get_task_v2,
        SleepTool(),
    ],
    verbose=True,
    agent=AgentType.STRUCTURED_CHAT_ZERO_SHOT_REACT_DESCRIPTION,
)


async def main():
    # use sleep tool to set up the polling logic until the task is completed, if you only want to queue a task, you can remove the sleep tool
    print(await agent.ainvoke("Queue a task with Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.' Then, get this task information until it's completed. The task information re-get interval should be 60s."))


if __name__ == "__main__":
    asyncio.run(main())
```