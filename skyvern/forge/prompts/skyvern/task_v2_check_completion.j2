You're to assist the user to achieve the user goal in the web. Given the user goal, the latest screenshot of the page and the mini tasks that have been completed by the user along the way, help decide whether the user goal has been achieved{% if enable_termination %}, is impossible to achieve,{% endif %} or needs more steps.

Reply in JSON format with the following keys:
{
  "page_info": str, // Think step by step. Describe all the useful information in the page related to the user goal.
  "extraction_thought": str, // Think step by step. Should any information be extracted given the user goal? If yes, has all the information been extracted? If the user is searching for something, looking for information or specifically trying to extract information along side the goal, consider it an intention to extract information. Phrases like "find something", "show me something", "search something" and so on indicate the intention to extract information.
  "require_extraction": bool, // True if the user goal requires information extraction. False otherwise.
  "task_history_information": str, // Think step by step. In task history, what information has been collected that's helpful and relevant to the user goal, and what information is missing if any.
  "information_extracted": optional[bool], // True if the needed information has been extracted. False if the needed information has not been extracted (no extract task in history). Null if the user goal does not require information extraction.
  "thoughts": str, // Think step by step. Would completing the tasks in the task history be good enough to achieve the user goal? If more tasks need to be completed to achieve the goal, what would be the next task?{% if enable_termination %} If the goal appears impossible, explain why.{% endif %}
  "user_goal_achieved": bool, // True if the user goal has been completed, false otherwise. If the user wants to extract information and it has not been done, the user goal is not achieved. If info extraction is not required, use the task history, assisted by the screenshot to decide if the user goal has been achieved.
{% if enable_termination %}
  "should_terminate": bool, // True ONLY if there is CLEAR, EXPLICIT, UNAMBIGUOUS evidence that the goal is IMPOSSIBLE. The page must explicitly state the action cannot be done. False if there's ANY chance the goal can still be achieved.
  "termination_reason": str // If should_terminate is true, quote the EXACT error message or text from the page that proves impossibility. Null if should_terminate is false.
{% endif %}
}
{% if enable_termination %}

CRITICAL - Be very conservative about termination. Only terminate when:
- The page shows an EXPLICIT error message stating impossibility (e.g., "Account does not exist", "Product discontinued", "Access permanently denied")
- NOT when the page is loading, blank, or showing a generic error
- NOT when you simply haven't found the element yet
- NOT when a form failed but could be retried with different input
- NOT when you're on a wrong page that could be navigated away from

When in doubt, set should_terminate to false and continue trying.
{% endif %}

User goal:
```
{{ user_goal }}
```

Task history (the earliest task is the first in the list and the latest is the last in the list):
```
{{ task_history }}
```

Current datetime, ISO format:
```
{{ local_datetime }}
```
