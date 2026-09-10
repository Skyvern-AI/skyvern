"""Multi-agent competitive research example.

Three AG2 agents collaborate to research and compare pricing across
multiple SaaS tools using Skyvern for browser automation:

  - Planner: breaks down the research request into specific browsing tasks
  - Browser_Agent: calls Skyvern to visit each website and extract pricing data
  - Analyst: synthesizes the collected data into a comparison report

This demonstrates the real value of AG2 + Skyvern: Skyvern handles the
hard part (visual browser interaction with dynamic pages), while AG2
handles multi-step orchestration and reasoning between agents.

Requirements:
  pip install skyvern-ag2

  export OPENAI_API_KEY="sk-..."
  export SKYVERN_API_KEY="..."   # from https://app.skyvern.com

Usage:
  cd integrations/ag2
  uv run python examples/multi_agent_research.py
"""

import os

from autogen import AssistantAgent, GroupChat, GroupChatManager, LLMConfig, UserProxyAgent
from skyvern_ag2 import run_task_cloud

# --- Configuration ---

llm_config = LLMConfig(
    {
        "model": "gpt-4o",
        "api_key": os.environ["OPENAI_API_KEY"],
        "api_type": "openai",
    }
)

# --- Agents ---

planner = AssistantAgent(
    name="Planner",
    system_message=(
        "You are a research planner. Your job is to break down the user's "
        "research request into specific, actionable browsing tasks.\n\n"
        "For each task, tell Browser_Agent exactly:\n"
        "- Which URL to visit\n"
        "- What specific data to extract (plan names, prices, features, limits)\n\n"
        "Give one task at a time. Wait for Browser_Agent to complete each task "
        "before giving the next one. After all data is collected, ask Analyst "
        "to produce the final report."
    ),
    llm_config=llm_config,
)

browser_agent = AssistantAgent(
    name="Browser_Agent",
    system_message=(
        "You execute web browsing tasks using the run_task_cloud tool. "
        "Follow the Planner's instructions precisely — pass the URL and "
        "a clear description of what data to extract.\n\n"
        "After receiving the result from Skyvern, report the extracted data "
        "back to the group in a structured format. If the task fails, report "
        "the error so the Planner can adjust."
    ),
    llm_config=llm_config,
)

analyst = AssistantAgent(
    name="Analyst",
    system_message=(
        "You are a business analyst. Wait until Browser_Agent has collected "
        "data from all websites the Planner requested.\n\n"
        "Then produce a clear comparison report:\n"
        "- Summary table of plans and prices\n"
        "- Key differences between the tools\n"
        "- Recommendation based on value for money\n\n"
        "Write TERMINATE as the very last word of your report, in the same message. "
        "Never send TERMINATE as a separate message."
    ),
    llm_config=llm_config,
)

executor = UserProxyAgent(
    name="Executor",
    human_input_mode="NEVER",
    code_execution_config=False,
    max_consecutive_auto_reply=15,
    is_termination_msg=lambda msg: "TERMINATE" in (msg.get("content") or ""),
)

# --- Tool registration ---
# Only Browser_Agent can call Skyvern, but Executor handles execution

executor.register_for_execution()(run_task_cloud)
browser_agent.register_for_llm(
    description=(
        "Run a browser automation task using Skyvern. Navigates to a website, "
        "interacts with it visually, and extracts the requested data. "
        "Blocks until the task completes."
    )
)(run_task_cloud)

# --- Group chat orchestration ---

group_chat = GroupChat(
    agents=[executor, planner, browser_agent, analyst],
    messages=[],
    max_round=20,
    speaker_selection_method="auto",
)

manager = GroupChatManager(groupchat=group_chat, llm_config=llm_config)

# --- Run ---

DEFAULT_TASK = (
    "Research the pricing pages of three project management tools: "
    "Asana (asana.com/pricing), Monday.com (monday.com/pricing), "
    "and Jira (atlassian.com/software/jira/pricing). "
    "Extract their plan names, monthly per-user prices, and key features. "
    "Then produce a comparison summary with a recommendation."
)

user_input = input("\nEnter your research task (or press Enter for the default example):\n> ").strip()
task = user_input or DEFAULT_TASK

print(f"\nStarting research: {task}\n")

response = executor.run(
    manager,
    message=task,
    max_turns=20,
    summary_method="last_msg",
)
response.process()

print("\n" + "=" * 60)
print("FINAL REPORT")
print("=" * 60)
print(response.summary)
