I am going to give you an original prompt for a browser agent, and a block type.

Can you improve this original prompt using the following prompting rules for a browser agent:

Depending on the block type, you will need to structure the prompt differently.

Most deterministic: Action Block
single action like click, input text, etc

Happy medium: Task Block
single goal, like your goal is to fill out the form

Most flexible: Task V2 Block
multi-goal, like your goal is to login, then fill the form, etc.

Examples by block type:

Example Action Block prompt:
```
Click the continue button
```

Example Task Block prompt:
```
Your goal is to fill out the form. Only fill out required fields that you have information for. To add address information, you will need to select the Add button and input the information in a popup modal.

Here is the information you need to get through the form: {{complete_payload}}

Your goal is complete when you have filled out the form and submitted. You will know your goal is complete when you are viewing a confirmation message that it was submitted.
```

Example Task V2 Block prompt:
```
Your first goal is to search for an image using Google Lens. To do so, select the Google Lens camera icon. You will know you're looking at google lens when you see the option to drag and drop an image. Then search using the provided image link: {{image_URL}}

Now that you've searched google for the image, your goal is to identify the location. If you're unsure about the location, default to the location that is most represented in the search results.

Now that you have the location, your goal is to go to google maps and extract the address. Provide the address in the following format: {{schema}}
```

For Browser Task blocks the structure that we format it in is
  1. [Required] Main goal
  2. Guardrails/details
  3. Payload, or information that Skyvern uses in input fields
  4. [Required] Completion/Termination criteria

Use the user's prompt to customize each of these four items. If they don’t mention something you might need to infer (for instance, if they are talking about an SS4 filing — the goal is to fill the form)
The main goal can be general like "fill the form" or granular like "fill x page of the form".

Guardrails include context like sequence of steps if specified by the user, conditionals if/else, weird quirks about the website, edge cases, etc.

If it’s a sensitive workflow, usually indicated by the user, make sure the guardrails reflect these important, emphasized parts of the user prompt.

Payload is the content skyvern needs to use in the automation for instance, login credentials or variables. Completion criteria

Completion criteria marks when the agent stops and recognizes the goal is complete. Termination criteria is if the agent encounters a specified edge case, it stops the workflow

Respond ONLY with valid JSON in this format with no additional text before or after it:
```json
{
 "improved_prompt": str, // The improved version of the prompt
}
```

Ensure that the "improved_prompt" contains liberal whitespace tokens for formatting, clarity, and legibility.

Here is the original prompt from the user:

```
{{ prompt }}
```
{% if context %}

Additional context about the user's needs:
```
{{ context }}
```
{% endif %}

The block type is a {{context.block_type}} (default to Task V2 Block if not specified).
