<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents**  *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Skyvern Langchain](#skyvern-langchain)
  - [Installation](#installation)
  - [Usage](#usage)
    - [Run a task(sync) with notebook style (code block)](#run-a-tasksync-with-notebook-style-code-block)
    - [Run a task(async) with notebook style (code block)](#run-a-taskasync-with-notebook-style-code-block)
    - [Run a task(sync) with client style (calling skyvern cloud api)](#run-a-tasksync-with-client-style-calling-skyvern-cloud-api)
    - [Run a task(async) with client style (calling skyvern cloud api)](#run-a-taskasync-with-client-style-calling-skyvern-cloud-api)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Skyvern Langchain

This is a langchain integration for Skyvern.

## Installation

```bash
pip install skyvern-langchain
```

## Usage

### Run a task(sync) with notebook style (code block)
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
    print(await agent.ainvoke("Create a task by Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.'"))


if __name__ == "__main__":
    asyncio.run(main())
```

### Run a task(async) with notebook style (code block)
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
    # use sleep tool to set up the polling logic until the task is completed, if you only want to run the create task, you can remove the sleep tool
    print(await agent.ainvoke("Create a task by Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.' Then, get this task information until it's completed. The task information re-get interval should be 60s."))


if __name__ == "__main__":
    asyncio.run(main())

```

### Run a task(sync) with client style (calling skyvern cloud api)
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
    print(await agent.ainvoke("Create a task by Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.'"))


if __name__ == "__main__":
    asyncio.run(main())
```

### Run a task(async) with client style (calling skyvern cloud api)
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
    # use sleep tool to set up the polling logic until the task is completed, if you only want to run the create task, you can remove the sleep tool
    print(await agent.ainvoke("Create a task by Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.' Then, get this task information until it's completed. The task information re-get interval should be 60s."))


if __name__ == "__main__":
    asyncio.run(main())
```