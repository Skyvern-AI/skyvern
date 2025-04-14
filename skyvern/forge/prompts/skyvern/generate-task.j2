We are building an AI agent that can automate browser tasks. The task creation schema is a JSON object with the following fields:

url: str. This is a required field. It is the starting URL for the task. This will be the first page the agent visits in order to achieve the goal. Use HTTPS URLs only.
suggested_title: str. This is a required field. It is the title of the task. It should be a few words long. It should describe what the task is doing. Example: "Find top post on hackernews", "Job search on LinkedIn", "Get Apple stock price".
navigation_goal_reasoning: str. This is a required field. The reason why navigation goal is needed to achieve the goal. Navigation goal is needed when the agent needs to take actions on the website to achieve the goal such as clicking a button, typing in a search bar, or navigating to another URL.
is_navigation_goal_required: bool. This is a required field. Based on the navigation_goal_reasoning, whether the navigation goal is required to achieve the task.
navigation_goal: str. This is an optional field. If is_navigation_goal_required is true, then this field should be provided. Otherwise, provide the value null. The value should be a string that we can use as an input to a Large Language Modal. It needs to tell the agent what actions need to be taken to achieve the task. It needs to define a single goal. If this field is provided, you must include explicit completion criteria. Provide completion criteria by completing the sentence: "COMPLETE when...". You can define guardrails that could help the agent from taking certain actions or getting derailed.
data_extraction_reasoning: str. This is a required field. Think step by step. Is the user trying to retrieve any information from the website? Should any information be extracted given the user goal? Is data extraction required to achieve the goal? If the user is searching for something, looking for information or specifically trying to extract information along side the goal, consider it an intention to extract information. Phrases like "find something", "show me something", "search something", "return something" and so on indicate the intention to extract information.
require_extraction: bool. This is a required field. Based on data_extraction_reasoning, is data extraction required to achieve the goal?
data_extraction_goal: str. This is an optional field. The value should be a string that we can use as an input to a Large Language Modal. It needs to tell the agent the goal in terms of extracting data. It needs to be a single goal.
navigation_payload: json. This is an optional field. The value should be JSON. Use this field if there is any information for the agent to be able to complete the task such as values that can help fill a form, parameters for queries and so on.

At least one of navigation_goal or data_extraction_goal should be provided. The agent can't proceed without any goals. They can both be provided. Provide data_extraction_goal if the user has an intention to retrieve any information from the website.

If a field is not required to achieve a task, provide the value `null`.

Respond with only JSON output that follows the task creation schema for the following prompt:
```
{{ user_prompt }}
```