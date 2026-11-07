---
title: "Surprise Launch week – Day 3 - LangChain + LlamaIndex support is here!"
description: null
excerpt: "Today we’re releasing two small Python packages that let you trigger Skyvern browser tasks directly from LangChain or LlamaIndex code.\n\n * skyvern-langchain – LangChain Tool + Agent helpers\n * skyvern-llamaindex – LlamaIndex Tool + Runnable helpers\n\nBoth follow the same pattern: create a task, wait for it if you like, or hand it off to Skyvern Cloud. Nothing else to set up.\n\n\nInstallation\n\npip install skyvern-langchain            # LangChain\npip install skyvern-llamaindex           # LlamaIndex\n"
slug: "launch-week-day-3-langchain-llamaindex-support-is-here"
publicationState: "published"
publishedAt: "2025-06-04T14:59:26.000Z"
updatedAt: "2025-06-05T04:56:04.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/ee10b1f41464d862cb0e4bfba2d546106f69725cdde1734bd1067090cc921115-image-71.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "Today we’re releasing two small Python packages that let you trigger Skyvern browser tasks directly from LangChain or LlamaIndex code.\n\n * skyvern-langchain – LangChain Tool + Agent helpers\n * skyvern-llamaindex – LlamaIndex Tool + Runnable helpers\n\nBoth follow the same pattern: create a task, wait for it if you like, or hand it"
---
Today we’re releasing two small Python packages that let you trigger Skyvern browser tasks directly from LangChain or LlamaIndex code.

-   <strong>skyvern-langchain</strong> – LangChain Tool + Agent helpers
-   <strong>skyvern-llamaindex</strong> – LlamaIndex Tool + Runnable helpers

Both follow the same pattern: create a task, wait for it if you like, or hand it off to Skyvern Cloud. Nothing else to set up.

* * *



<h3 id="installation"><strong>Installation</strong></h3>





<pre><code>pip install skyvern-langchain            # LangChain
pip install skyvern-llamaindex           # LlamaIndex
</code></pre>



* * *



<h3 id="minimal-langchain-example"><strong>Minimal LangChain example</strong></h3>





<pre><code>import asyncio
from skyvern_langchain.agent import RunTask  # local, blocking

async def main():
    result = await RunTask().ainvoke(
        "Navigate to Hacker News and list the top 3 posts."
    )
    print(result)

asyncio.run(main())
</code></pre>



Running against the cloud (returns immediately):



<pre><code>from skyvern_langchain.client import DispatchTask
task_id = await DispatchTask(api_key="sk-...").ainvoke(
    "Navigate to Hacker News and list the top 3 posts."
)
</code></pre>



If you need agent-style reasoning, initialise an agent with the supplied tools:



<pre><code>from langchain.agents import initialize_agent, AgentType
from skyvern_langchain.agent import DispatchTask, GetTask
agent = initialize_agent(
    tools=[DispatchTask(), GetTask()],
    llm=ChatOpenAI(model="gpt-4o"),
    agent=AgentType.STRUCTURED_CHAT_ZERO_SHOT_REACT_DESCRIPTION,
)
</code></pre>



* * *



<h3 id="minimal-llamaindex-example"><strong>Minimal LlamaIndex example</strong></h3>





<pre><code>skyvern_tool = SkyvernTool()

agent = OpenAIAgent.from_tools(
    tools=[skyvern_tool.run_task()],
    llm=OpenAI(model="gpt-4o"),
    verbose=True,
)

response = agent.chat("Navigate to the Hacker News homepage and get the top 3 posts.")
</code></pre>



* * *

The packages are early but functional; feedback or bug reports are very welcome.

-   LangChain adapter repo → skyvern-langchain on GitHub
-   LlamaIndex adapter repo → skyvern-llamaindex on GitHub
-   Docs have a few more examples and edge-case notes.

Thanks for taking a look—let us know what you think.
