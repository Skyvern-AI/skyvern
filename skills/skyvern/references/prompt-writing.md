# Prompt Writing for Running Tasks

## Outcome-first template

```text
Goal: <business outcome>
Site: <url>
Constraints: <what must or must not happen>
Success criteria: <verifiable completion state>
Output: <exact fields to return>
```

## Good prompts

- "Open the pricing page, extract plan name and monthly price for each visible tier, return JSON array."
- "Submit the lead form with provided fields and confirm success toast text is visible."

## Weak prompts

- "Click around and get data." (no outcome)
- "Find the button with selector #submit" (overly brittle unless required)

## Reliability guardrails

- Add explicit navigation scope when pages can redirect.
- Ask for evidence in output (`page title`, confirmation text, extracted row count).
- Keep schema small for first pass; expand only after stable execution.
