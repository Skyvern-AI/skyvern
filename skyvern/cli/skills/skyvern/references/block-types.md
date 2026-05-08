# Block Types: Practical Use

## `navigation`

The primary block for page-level actions described in natural language. Accepts a URL and a `navigation_goal`.

```json
{"block_type": "navigation", "label": "fill_form", "url": "https://example.com", "navigation_goal": "Fill first name, last name, and email from parameters, then click Continue."}
```

## `extraction`

Use to convert visible page state into structured output. Pair with a `data_extraction_goal` and `data_schema`.

```json
{"block_type": "extraction", "label": "get_order", "url": "https://example.com/orders", "data_extraction_goal": "Extract order number, status, and estimated delivery date."}
```

## `login`

Handles credential-based authentication flows. Pairs with a `credential_id` workflow parameter to securely log in before downstream blocks execute. Use a `complete_criterion` to confirm login success.

```json
{"block_type": "login", "label": "login", "url": "{{portal_url}}", "parameter_keys": ["login_credential"], "complete_criterion": "The dashboard is visible."}
```

## `wait`

Use when page transitions are asynchronous.

Use conditions like:
- spinner disappears
- success banner appears
- table row count is non-zero

## `conditional`

Use for known branching states (e.g., optional MFA prompt).

Keep conditions narrow and testable.

## `for_loop`

Use when the workflow already has a concrete list to iterate over, such as extracted rows, item cards, uploaded files, or user-provided URLs.

Avoid nested loops unless absolutely necessary; they increase run variance.

## `while_loop`

Use when the workflow should repeat until a condition changes. Good fits include pagination with an enabled Next button, polling until a status is complete, and retry-until flows where each pass can make progress.

Prefer a Jinja2 condition when a prior block extracts a boolean such as `has_next_page`. Use a prompt condition only when the decision depends on visual page state that is hard to express from structured outputs.

Keep a bounded exit path in mind. The runtime enforces a maximum iteration limit, but workflows are more reliable when the condition is expected to become false through normal page progress.
