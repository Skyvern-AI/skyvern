User is doing the task step by step on a web page. You are here to help the user summarize the main reason why the user goal has not been achieved within the limit of {{ step_count }} steps. This summary should be based on the provided screenshot, navigation goals, user details, and the results of actions taken step by step.

Make sure to ONLY return the JSON object in this format with no additional text before or after it:
```json
{
  "page_info": str, // Think step by step. Describe all the useful information in the page related to the user goal.
  "reasoning": str, // Think step by step. Describe the reason you summarized based on 'page_info', screenshots, user goal, user detail and result of actions. Keep the reasoning short and to the point.{% if error_code_mapping_str %}
  "errors": array // A list of errors. This is used to surface any errors that matches the current situation. If no error description suits the current situation on the screenshots or the action history, return an empty list. You are allowed to return multiple errors if there are multiple errors on the page.
    [{
        "error_code": str, // The error code from the user's error code list
        "reasoning": str, // The reasoning behind the error. Be specific, referencing any user information and their fields in your reasoning. Keep the reasoning short and to the point.
        "confidence_float": float // The confidence of the error. Pick a number between 0.0 and 1.0. 0.0 means no confidence, 1.0 means full confidence
    }]{% endif %}
}

User Goal:
{{ navigation_goal }}
{% if error_code_mapping_str %}
Use the error codes and their descriptions to surface user-defined errors. Do not return any error that's not defined by the user. User defined errors:
{{ error_code_mapping_str }}
{% endif %}

User Details:
{{ navigation_payload }}

Actions Taken In Each Step:
{% for step in steps %}Step {{ step.order }} -- {{ step.actions_result }}
{% endfor %}

Current datetime, ISO format:
```
{{ local_datetime }}
```