# Skyvern AG2

[AG2](https://ag2.ai) multi-agent integration for [Skyvern](https://skyvern.com) browser automation.

This package exposes Skyvern's browser automation as AG2-registered tool functions, enabling multi-agent workflows that navigate websites, fill forms, and extract data.

## Installation

```bash
pip install skyvern-ag2
```

## Quick Start — Cloud Mode

Uses the Skyvern cloud API. Set `SKYVERN_API_KEY` in your environment or `.env` file.

```python
import os
from autogen import AssistantAgent, UserProxyAgent, LLMConfig
from skyvern_ag2 import run_task_cloud

llm_config = LLMConfig({"model": "gpt-4o", "api_key": os.getenv("OPENAI_API_KEY"), "api_type": "openai"})

assistant = AssistantAgent(
    name="Browser_Agent",
    system_message="You are a web research assistant. Use run_task_cloud to browse websites.",
    llm_config=llm_config,
)

executor = UserProxyAgent(name="Executor", human_input_mode="NEVER", code_execution_config=False)

# Register Skyvern tool
executor.register_for_execution()(run_task_cloud)
assistant.register_for_llm(description="Run a browser automation task and wait for the result")(run_task_cloud)

executor.run(
    assistant,
    message="Navigate to the Hacker News homepage and get the top 3 posts.",
).process()
```

## Quick Start — Local Mode

Uses a local Skyvern instance. Requires `skyvern init` first.

```python
from skyvern_ag2 import run_task_local

# Same pattern as cloud mode, just swap run_task_cloud → run_task_local
executor.register_for_execution()(run_task_local)
assistant.register_for_llm(description="Run a browser task locally")(run_task_local)
```

## Available Tools

| Function | Mode | Description |
|----------|------|-------------|
| `run_task_cloud` | Cloud | Run a task synchronously (blocks until done) |
| `dispatch_task_cloud` | Cloud | Dispatch a task asynchronously (returns task ID) |
| `get_task_cloud` | Cloud | Get task status and results |
| `run_task_local` | Local | Run a task synchronously on local instance |
| `dispatch_task_local` | Local | Dispatch a task asynchronously on local instance |
| `get_task_local` | Local | Get task status from local instance |

Import from `skyvern_ag2` directly, or from `skyvern_ag2.client` (cloud) / `skyvern_ag2.agent` (local).

## Multi-Agent Example

AG2 is most powerful when multiple agents collaborate. See [`examples/multi_agent_research.py`](examples/multi_agent_research.py) for a full working example where:

- **Planner** breaks down a research request into specific browsing tasks
- **Browser_Agent** calls Skyvern to visit each website and extract data
- **Analyst** synthesizes the results into a comparison report

```bash
cd integrations/ag2
uv run python -m examples.multi_agent_research
```

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `SKYVERN_API_KEY` | Skyvern cloud API key | Cloud mode only |
| `SKYVERN_BASE_URL` | Skyvern API base URL (default: `https://api.skyvern.com`) | No |
| `OPENAI_API_KEY` | OpenAI API key for AG2 agents | Yes |
