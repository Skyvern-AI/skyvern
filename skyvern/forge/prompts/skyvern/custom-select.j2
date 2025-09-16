You are performing a {{ "multi-level selection" if select_history else "selection" }} action on an HTML page. Assist the user in selecting the most appropriate option(or typing some values to search if necessary) to advance toward their goal, considering the context, user details,{{" list of emerging HTML element ids" if new_elements_ids else "" }} and the HTML elements provided in the list.

You can identify the matching element based on the following guidelines:
  - Select the most suitable element based on the user goal, user details, and the context.
  - If none of the options perfectly match, and there is no search box for input, but there is a fallback option such as "Other", "Others" or "None of the above" in the HTML elements, you can consider it a match.
  - If a field is required, do not leave it blank.
  - If a field is required, do not select a placeholder value, such as "Please select", "-", or "Select...".
  - Exclude loading indicators like "loading more results" as valid options.{% if new_elements_ids %}
  - The matching element can only be in the emerging elements.{% endif %}{% if select_history %}
  - The selection history displays the previously selected values for the multi-level selection. Continue to complete the entire selection process.{% if is_date_related %}
  - Date picker might be triggered, you goal is to set the correct start date and end date.{% endif %}{% endif %}

MAKE SURE YOU OUTPUT VALID JSON. No text before or after JSON, no trailing commas, no comments (//), no unnecessary quotes, etc.
Each interactable element is tagged with an ID.

Reply in JSON format with the following keys:
{
    "page_info": str, // Think step by step. Describe the page information you parsed from the HTML elements. Your action should be based on the current page information.
    "reasoning": str, // The reasoning behind the current single action. Be specific, referencing the value and the element id in your reasoning. Mention why you chose the element id. Keep the reasoning short and to the point.
    "confidence_float": float, // The confidence of the action. Pick a number between 0.0 and 1.0. 0.0 means no confidence, 1.0 means full confidence
    "id": str, // The id of the element to take action on. The id has to be one from {{ "the emerging HTML elements list" if new_elements_ids else "the HTML elements list" }}.
    "action_type": str, // It's a string enum: "CLICK", "INPUT_TEXT"{{' ,"COMPLETE"' if support_complete_action else ""}}. "CLICK" is an option you'd like to click to choose. "INPUT_TEXT" is an element you'd like to input text into for searching, but it only should be used when there's no valid option to click.{{ ' "COMPLETE" means the user has completed user goal.' if support_complete_action else "" }}
    "value": str, // The value to select.{% if target_value %}
    "relevant": bool, // True if the value you select is relevant to the target value, otherwise False. If the value is a fallback option according to the guidelines, it's still relevant.{% endif %}
}

Context:
```
Select an option for "{{ field_information }}"{{" if user goal has not been completed" if support_complete_action else ""}}. It's {{ "a required" if required_field else "an optional" }} field.
```
{% if target_value %}
Target value:
```
{{ target_value }}
```
{% endif %}
User goal:
```
{{ navigation_goal }}
```

User details:
```
{{ navigation_payload_str }}
```
{% if new_elements_ids %}
IDs for emerging HTML elements
```
{{ new_elements_ids }}
```
{% endif %}
HTML elements:
```
{{ elements }}
```
{% if select_history %}
Select History:
```
{{ select_history }}
```
{% endif %}
Current datetime, ISO format:
```
{{ local_datetime }}
```