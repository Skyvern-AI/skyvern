# Quick Start Patterns

Examples for each tool classification. See the MCP server instructions for the classification table.

## Quick check (yes/no)
```
skyvern_validate(prompt="Is the user logged in?")
```

## Quick inspection (structured data)
```
skyvern_extract(prompt="Extract all prices", schema='{"type":"object","properties":{...}}')
```

## Single action (known selector)
```
skyvern_click(selector="#submit") or skyvern_type(selector="#email", text="user@co.com")
```

## Single action (unknown target)
```
skyvern_act(prompt="Click the Sign In button")
```

## Multi-step (simple, PREFERRED for forms)
1. `skyvern_observe()` returns element refs (`e0`, `e1`, ...)
2. Your LLM decides which refs to interact with
3. Run `skyvern_execute(...)` with those refs, for example:

```python
skyvern_execute(
    steps=[
        {"tool": "click", "params": {"ref": "e0"}},
        {"tool": "type", "params": {"ref": "e1", "text": "hello"}},
    ]
)
```

## Throwaway autonomous trial
```
skyvern_run_task(prompt="Try the checkout flow once and tell me whether it succeeds")
```

## Multi-step (complex, workflow)
```
skyvern_workflow_create(
  definition='{"title":"Checkout","workflow_definition":{"blocks":[
    {"block_type":"navigation","label":"shipping","navigation_goal":"Fill shipping info"},
    {"block_type":"navigation","label":"payment","navigation_goal":"Select payment and submit"},
    {"block_type":"extraction","label":"confirm","data_extraction_goal":"Extract order number"}
  ]}}',
  format="json"
) -> skyvern_workflow_run(workflow_id="wpid_...") -> skyvern_workflow_status(run_id="wr_...")
```

## QA testing
Use the qa_test prompt to test frontend changes — reads git diff, generates + runs browser tests.

## Common Patterns

### Logging in securely
1. `skyvern_credential_list` -- find the credential
2. `skyvern_browser_session_create` -- start session
3. `skyvern_navigate(url="https://login.example.com")` -- go to login page
4. `skyvern_login(credential_id="cred_...")` -- AI handles the full login flow
5. `skyvern_screenshot` -- verify login succeeded

### Debugging browser issues
`skyvern_browser_session_create` -> `skyvern_navigate` -> perform actions ->
`skyvern_console_messages(level="error")` for JS errors, `skyvern_network_requests` for API calls

### Testing feasibility before building a workflow
Walk through the site interactively — use `skyvern_act` on each page and `skyvern_screenshot` to verify.
Once confirmed, compose steps into a workflow with `skyvern_workflow_create`.

## Writing Scripts

Use the Skyvern Python SDK: `from skyvern import Skyvern`.
NEVER import from `skyvern.cli.mcp_tools` — those are internal server modules.
In verbose mode (`--verbose`), every tool response includes an `sdk_equivalent` field for script conversion.

### Hybrid xpath+prompt pattern (recommended for production scripts)
```python
await page.click("xpath=//button[@id='submit']", prompt="the Submit button")
await page.fill("xpath=//input[@name='email']", "user@example.com", prompt="email input field")
```

## Workflow Example (multi-block form application)
```json
{
  "title": "Multi-Step Form Application",
  "workflow_definition": {
    "parameters": [
      {"parameter_type": "workflow", "key": "business_name", "workflow_parameter_type": "string"},
      {"parameter_type": "workflow", "key": "owner_name", "workflow_parameter_type": "string"},
      {"parameter_type": "workflow", "key": "owner_id", "workflow_parameter_type": "string"}
    ],
    "blocks": [
      {"block_type": "navigation", "label": "select_entity_type",
       "url": "https://example.com/form/step1",
       "title": "Select Entity Type",
       "navigation_goal": "Select 'Sole Proprietor' as the entity type and click Continue."},
      {"block_type": "navigation", "label": "enter_business_info",
       "title": "Enter Business Info",
       "navigation_goal": "Fill in the business name as '{{business_name}}' and click Continue.",
       "parameter_keys": ["business_name"]},
      {"block_type": "navigation", "label": "enter_owner_info",
       "title": "Enter Owner Info",
       "navigation_goal": "Enter the responsible party name '{{owner_name}}' and ID '{{owner_id}}'. Click Continue.",
       "parameter_keys": ["owner_name", "owner_id"]},
      {"block_type": "extraction", "label": "extract_confirmation",
       "title": "Extract Confirmation",
       "data_extraction_goal": "Extract the confirmation number from the success page",
       "data_schema": {"type": "object", "properties": {"confirmation_number": {"type": "string"}}}}
    ]
  }
}
```
Use `{{parameter_key}}` to reference workflow input parameters in any block field.
Blocks in the same run share the same browser session automatically.
