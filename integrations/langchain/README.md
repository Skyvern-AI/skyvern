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

To run the example scenarios, you might need to install other langchain dependencies.
```bash
pip install langchain-openai
pip install langchain-community
```

## Usage

### Run a task(sync) with skyvern agent (calling skyvern agent function directly in the tool)
> sync task won't return until the task is finished.

:warning: :warning: if you want to run this code block, you need to run `skyvern init --openai-api-key <your_openai_api_key>` command in your terminal to set up skyvern first.


```python
import asyncio
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.agents import initialize_agent, AgentType
from skyvern_langchain.agent import RunTask

# load OpenAI API key from .env
load_dotenv()

llm = ChatOpenAI(model="gpt-4o", temperature=0)

run_task = RunTask()

agent = initialize_agent(
    llm=llm,
    tools=[run_task],
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
> async task will return immediately and the task will be running in the background. You can use `GetTask` tool to poll the task information until the task is finished.

:warning: :warning: if you want to run this code block, you need to run `skyvern init --openai-api-key <your_openai_api_key>` command in your terminal to set up skyvern first.

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

### Run a task(sync) with skyvern client (calling skyvern OpenAPI in the tool)
> sync task won't return until the task is finished.

no need to run `skyvern init` command in your terminal to set up skyvern before using this integration.

```python
import asyncio
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.agents import initialize_agent, AgentType
from skyvern_langchain.client import RunTask

# load OpenAI API key from .env
load_dotenv()

llm = ChatOpenAI(model="gpt-4o", temperature=0)

run_task = RunTask(
    credential="<your_organization_api_key>",
)
# or you can load the credential from SKYVERN_CREDENTIAL in .env
# run_task = RunTask()

agent = initialize_agent(
    llm=llm,
    tools=[run_task],
    verbose=True,
    agent=AgentType.STRUCTURED_CHAT_ZERO_SHOT_REACT_DESCRIPTION,
)

async def main():
    print(await agent.ainvoke("Run a task with Skyvern. The task is about 'Navigate to the Hacker News homepage and get the top 3 posts.'"))


if __name__ == "__main__":
    asyncio.run(main())
```

### Run a task(async) with skyvern client (calling skyvern OpenAPI in the tool)
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
    credential="<your_organization_api_key>",
)
# or you can load the credential from SKYVERN_CREDENTIAL in .env
# dispatch_task = DispatchTask()

get_task = GetTask(
    credential="<your_organization_api_key>",
)
# or you can load the credential from SKYVERN_CREDENTIAL in .env
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