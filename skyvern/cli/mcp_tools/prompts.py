"""MCP prompt skills for Skyvern workflow design, debugging, and data extraction.

These prompts are registered with @mcp.prompt() and injected into LLM conversations
to guide Claude through Skyvern automation tasks. Each prompt teaches Claude to act
as the USER — designing workflows and interpreting results — while Skyvern's AI
handles the actual browser navigation.

Milestone context:
  M3: Expose MCP functionality as Claude Skills (this module)
  M4: Expose Skyvern Workflow Copilot via MCP + Skills (these prompts power it)
"""

from typing import Annotated

from pydantic import Field

# ---------------------------------------------------------------------------
# build_workflow
# ---------------------------------------------------------------------------

BUILD_WORKFLOW_CONTENT = """\
# Build a Skyvern Workflow

## How Skyvern Workflows Work

You create a workflow definition (blocks + parameters). Skyvern executes it in a cloud browser.
Each block runs in order, sharing the same browser session.  The key differentiator is
**navigation blocks**: you describe a goal in natural language and Skyvern's AI navigates the site
autonomously — clicking, filling forms, handling popups, and retrying on failure. You do NOT
need to specify selectors, XPaths, or step-by-step instructions.

Workflows are versioned, parameterized, and reusable.  Once a workflow works, you can re-run it
with different inputs forever. Think of each workflow as a saved automation skill.

Tools you will use:  skyvern_workflow_create, skyvern_workflow_run, skyvern_workflow_status,
skyvern_workflow_update, skyvern_block_schema, skyvern_block_validate.

---

## Design the Workflow

### Default to navigation blocks

navigation is the right choice for most steps. You give it a URL and a navigation_goal.
Skyvern figures out the navigation at runtime.

GOOD navigation_goal — describes the GOAL and what "done" looks like:
  "Search for '{{product_name}}' in the search bar, click the first result, and add it to cart.
   Done when the cart icon shows 1 item."

BAD navigation_goal — describes HOW to do it (Skyvern already knows):
  "Find the input element with id='search', type the product name, press Enter, wait for
   results to load, find the first anchor tag in the results div, click it..."

### Block type decision tree

1. **navigation** (default) — AI-powered browser actions.  Use when the step involves browsing,
   clicking, filling forms, or any multi-action sequence.  Skyvern handles element finding,
   popup dismissal, scrolling, and retries.
2. **extraction** — structured data extraction.  Use when you need JSON output from a page
   (prices, tables, lists).  Requires data_extraction_goal and data_schema.
3. **for_loop** — iterate over a list.  Use when you need to repeat blocks for each item
   in a parameter list (e.g., process each URL, each product).
4. **conditional** — branch based on conditions.  Use when workflow logic should diverge
   based on data from a previous block or a Jinja2 expression.
5. **goto_url** — simple navigation without any actions.  Use to jump to a known URL.
6. **login** — authenticate with stored credentials.  Use for sites that require login.
7. **code** — run Python for data transformation between blocks.
8. **action** — single focused action on the current page (e.g., click one button).

Use skyvern_block_schema() to see full schemas and examples for any block type.

### Engine selection for workflow blocks

Task-based blocks (navigation, extraction, action, login, file_download) default to engine 1.0 (`skyvern-1.0`).
Omit the `engine` field unless you need 2.0.  Non-task blocks (for_loop, conditional, code, wait, etc.)
do not have an engine field — do not set one.

Use engine 2.0 (`"engine": "skyvern-2.0"`) on a **navigation** block when:
- The block's goal requires dynamic planning — discovering what to do at runtime, conditional
  branching, or looping over unknown items on the page.
- Example: "Navigate through a multi-step insurance quote wizard, handling dynamic questions
  based on previous answers, then extract the final quote."

Keep engine 1.0 (default, omit field) when:
- The path is known upfront — all fields, values, and actions are specified in the prompt.
- A long prompt with many form fields is still 1.0.  Complexity means dynamic planning, not field count.
- Example: "Fill in SSN, first name, last name, select 'Sole Proprietor', click Continue."

When in doubt, split into multiple 1.0 blocks rather than using one 2.0 block — it's cheaper and
gives you per-block observability.  Only navigation blocks support engine 2.0.

### One block per logical step

Split workflows into small, focused blocks.  Each block should do ONE thing.

GOOD (3 blocks):
  Block 1 (navigation): "Go to the search page and search for '{{query}}'. Done when results load."
  Block 2 (navigation): "Click the first result and add it to cart. Done when cart shows 1 item."
  Block 3 (extraction): "Extract the product name, price, and availability from the cart page."

BAD (1 block):
  Block 1 (navigation): "Search for the product, click the first result, add to cart, go to checkout,
   fill in shipping, enter payment, and submit the order."

### Common workflow shapes

**Search + Extract**: goto_url -> navigation (search) -> extraction (results)
**Multi-page form**: navigation (page 1) -> navigation (page 2) -> navigation (page 3) -> extraction (confirmation)
**Login + Action**: login (authenticate) -> navigation (do work) -> extraction (results)
**Batch processing**: for_loop over URLs -> navigation (process each) -> extraction (gather data)

---

## Parameterize for Reuse

Every workflow should accept parameters so it can be re-run with different inputs.

### Declaring parameters

Parameters are declared in the workflow_definition.parameters array:
  {"parameter_type": "workflow", "key": "company_name", "workflow_parameter_type": "string"}

Supported types: string, integer, float, boolean, json, file_url.

### Referencing parameters

Use {{parameter_key}} in any block field — prompts, URLs, data schemas, goal descriptions.
Skyvern substitutes values at runtime.

### What to parameterize

- Input data: names, addresses, search queries, product IDs
- URLs: if the target URL varies between runs
- Credentials: use the login block + credential_id parameter

### What NOT to parameterize

- Navigation instructions: these are the block prompts themselves
- Block structure: if you need different flows, create separate workflows
- Static site URLs: if the URL is always the same, hardcode it

---

## Test via Skyvern's Feedback Loop

Do NOT try to get the workflow perfect on the first attempt. Use this iteration loop:

### Step 1: Create and run

Call skyvern_workflow_create with your definition, then skyvern_workflow_run with test parameters.

### Step 2: Check status

Poll with skyvern_workflow_status using the run_id.  The response tells you:
- Which block succeeded or failed
- The failure_reason with what Skyvern saw on the page
- Step count and timing

### Step 3: Fix and re-run

Based on the error feedback:
- **"Prompt too vague"** — add specificity about what "done" looks like.  Example: change
  "Fill in the form" to "Fill in the form with company name '{{name}}'. Done when the
  confirmation page shows 'Application Submitted'."
- **"Element not found"** — add a navigation hint.  Example: "Look for the search bar in the
  top navigation area" or "The form is inside an iframe."
- **"Wrong page"** — add a URL check or split into smaller blocks so each one starts on the
  right page.
- **"Timeout"** — the page may be slow.  Increase max_retries on the block or add a wait block.

Call skyvern_workflow_update with the fixed definition, then skyvern_workflow_run again.

### Step 4: Verify output

When the run succeeds, check the output field in skyvern_workflow_status.  For extraction blocks,
verify the JSON matches your data_schema.

---

## Quick Feasibility Check (Optional)

If you are unsure whether Skyvern can handle a particular site, use skyvern_run_task as a probe
BEFORE building a full workflow.

skyvern_run_task is a one-shot autonomous agent.  Give it a URL and a prompt — it navigates
the site, takes actions, and reports results.  No workflow definition needed.

If it succeeds: turn the approach into a multi-block workflow for reuse.
If it struggles: read the failure_reason, refine the prompt, and try again.  Two or three
iterations usually reveal whether the site is automatable and what prompt phrasing works.

Do NOT open a manual browser session (skyvern_browser_session_create) to explore the site before
building a workflow.  That approach bypasses Skyvern's AI and wastes time.  skyvern_run_task
gives you the same insight faster because Skyvern navigates the site for you.

---

## Prompt Refinement Tips

When a navigation block fails, refine the navigation_goal:
1. Use skyvern_run_task first to identify what Skyvern sees on the page.
2. Add specificity: reference exact labels visible on the page.
3. Describe what "done" looks like so Skyvern knows when to stop.

Example workflow with clear goals:
  Block 1 (login): Authenticate with stored credentials.
  Block 2 (navigation): "Navigate to the settings page and open 'Notification Preferences'.
   Uncheck 'Marketing Emails' and check 'Security Alerts'. Click 'Save Changes'.
   Done when a success banner appears."
  Block 3 (extraction): "Extract the confirmation message."

---

## Pre-Flight Checklist

Before calling skyvern_workflow_create, verify:
1. Each block has a clear, single-responsibility goal (not a multi-page mega-prompt).
2. Navigation block goals describe WHAT to achieve, not HOW to click.
3. Every variable input uses {{parameter_key}} and is declared in parameters.
4. Extraction blocks include a data_schema with the expected JSON structure.
5. The block order matches the actual site flow (blocks share one browser session).
6. Login is handled by a login block (not embedded in a navigation goal).
7. You have test parameter values ready for the first skyvern_workflow_run call.
8. Validate blocks with skyvern_block_validate before submitting the full definition.
"""


# ---------------------------------------------------------------------------
# debug_automation
# ---------------------------------------------------------------------------

DEBUG_AUTOMATION_CONTENT = """\
# Debugging Skyvern Automations

When a workflow run or task fails, follow this structured process: read the error, diagnose the pattern,
fix and re-run. Do NOT open a manual browser session to explore — Skyvern already tells you what went wrong.

## Step 1: Read the Error

Call skyvern_workflow_status with the workflow_run_id to get structured failure info.

Key fields to examine:
- **status**: "failed", "terminated", or "timed_out"
- **failure_reason**: which block failed and why
- **screenshot**: what the page looked like when the failure occurred
- **extracted_information**: any partial data the AI did extract before failing

If this was an interactive session failure (not a workflow), call skyvern_screenshot to see the current page state,
then check the last tool response for error details.

Record three things before proceeding:
1. Which block (by label) failed
2. The error type (timeout, element not found, wrong page, extraction empty, auth required)
3. What the AI reported seeing on the page

## Step 2: Diagnose the Pattern

Skyvern failures fall into predictable categories. Match the error to a pattern and apply the standard fix.

### Timeout (block exceeded max_steps or wall time)
- Cause: prompt is too vague, so the AI explores without converging.
- Fix: add specificity about what "done" looks like. Instead of "fill out the form", write "fill out the form and
  click the blue Submit button. Done when you see a confirmation message containing a reference number."
- Also check: is max_steps too low? Default is reasonable, but complex forms may need more.

### Element Not Found (AI could not locate the target element)
- Cause: label mismatch (the button says "Continue" but the prompt says "Next"), or element loads asynchronously.
- Fix: update the prompt to use the exact label visible on the page. If the element loads after a delay, add
  "wait for the page to fully load before acting" to the prompt.
- If you know the exact label: switch to an action block for a single precise interaction.

### Wrong Page (block started on an unexpected page)
- Cause: the previous block did not complete its page transition. The current block assumed it would land on page B
  but it is still on page A.
- Fix: update the previous block's prompt to explicitly include the page transition. Add "click Continue and wait
  until the next page loads" instead of just "click Continue". Alternatively, add a goto_url block between them.

### Extraction Empty (extraction returned null or empty object)
- Cause: data loads dynamically (AJAX, infinite scroll) and was not present when the AI read the page. Or the
  extraction prompt does not match the page structure.
- Fix: add "wait for the data table to fully load" to the prompt. If data requires scrolling, add a navigation
  block that scrolls first. If the prompt is wrong, update it to describe the data using labels visible on the page.

### Auth Failure (redirected to login page)
- Cause: workflow does not handle authentication, or session cookies expired.
- Fix: add a login block at the start of the workflow, or use a browser_profile that has saved credentials.

### Stuck / Hanging (run stays "running" indefinitely)
- Action: call skyvern_workflow_cancel to stop the run. Then investigate: is the page showing a CAPTCHA, a
  modal dialog, or an unexpected redirect? Check the last screenshot from skyvern_workflow_status.

### Rate Limited or Blocked (403, CAPTCHA, "unusual traffic" message)
- Cause: the target site detected automation.
- Fix: add a proxy (residential or ISP) to the workflow's proxy_location parameter. Reduce request frequency
  by adding wait blocks between actions. If CAPTCHA persists, report to the user — this may require manual
  intervention or a CAPTCHA-solving integration.

## Step 3: Fix and Re-run

Use skyvern_workflow_update to modify the failing block. Do NOT delete the workflow and recreate it.

Fixing playbook:
1. Update the failing block's prompt to address the diagnosed issue. Be specific: add exact labels, describe
   what "done" looks like, mention elements to wait for.
2. If the navigation_goal is too vague for a complex form, make it more explicit — reference exact field labels,
   describe the form layout, and specify what "done" looks like.
3. Re-run with skyvern_workflow_run using the same parameters as the failed run.
4. Poll skyvern_workflow_status until the run completes. Check whether the previously failing block now passes.
5. If it still fails with the same error: refine the prompt further. If it fails with a NEW error, restart
   diagnosis from Step 1.

## Step 4: Escalation

Not every failure can be fixed by prompt refinement. Know when to escalate.

**Use action blocks for single-step precision** when:
- A navigation block does too much — you only need one specific click or input
- The page has multiple similar-looking elements and the AI picks the wrong one
- The step involves a single focused action (e.g., click one button, toggle one checkbox)

**Open a manual session (last resort)** when:
- You cannot determine from error output what the page looks like
- The site has unusual UI patterns not described in any error message
- Use: skyvern_browser_session_create, skyvern_navigate to the failing URL, skyvern_screenshot to see the page

**Report to the user** when:
- CAPTCHA blocks persist even with proxy rotation
- The site requires 2FA or hardware authentication
- Rate limiting cannot be avoided with proxies and delays
- The site's terms of service explicitly prohibit automation
"""


# ---------------------------------------------------------------------------
# extract_data
# ---------------------------------------------------------------------------

EXTRACT_DATA_CONTENT = """\
# Data Extraction with Skyvern

You design the data schema and describe what to extract. Skyvern's AI finds the elements, \
parses the page, and returns structured JSON. You never write selectors or scraping code.

## Schema Design

Always provide a `data_extraction_schema` (JSON Schema) so Skyvern returns typed, validated output.

- Use `"type": "object"` for a single record (profile, summary, confirmation).
- Use `"type": "array"` with `"items": { "type": "object", ... }` for lists (search results, table rows).
- Mark critical fields as `"required"` so missing data surfaces as an error rather than null.
- Choose descriptive property names that reflect the data, not the page layout \
(`order_date` not `col_3`, `company_name` not `first_bold_text`).
- Nest objects when the data is naturally hierarchical:
  `{ "seller": { "name": "...", "rating": 4.5 }, "price": { "amount": 29.99, "currency": "USD" } }`

## Writing Extraction Prompts

Describe WHAT to extract, not WHERE it is on the page. Skyvern's AI locates the data.

Good: "Extract all product names, prices, and star ratings from the search results"
Bad: "Get the text from each div.product-card > span.price"

When the page has multiple similar sections, specify which one:
"Extract order details from the table under 'Recent Orders', not 'Recommended Products'"

For simple extractions on a page you already navigated to, use `skyvern_extract` with a schema. \
For extraction combined with navigation (log in, then go to dashboard, then extract), use \
`skyvern_run_task` with a `data_extraction_schema` -- it handles the full flow in one call.

## Multi-Page Extraction

For paginated results, build a workflow with a `for_loop` block that iterates over page numbers \
or "next" clicks. Each iteration uses an extraction block to pull that page's data.

For infinite-scroll pages, use `skyvern_run_task` with a prompt like \
"scroll to load all results, then extract every item" -- Skyvern handles the scrolling.

For detail-page drilling (list page -> click each item -> extract details), build a workflow: \
extraction block to get the list of links, then a `for_loop` block that visits each link and \
extracts the detail fields.

## Validate Results

After extraction, check the returned data before using it:
- Verify record count matches expectations (e.g., "I expected ~50 results but got 3").
- Check for null or empty fields that should have values.
- If the data looks wrong, refine the extraction prompt (be more specific about which section \
or what the data looks like), not the schema.
- Use `skyvern_validate` for page-level assertions before extracting \
("Is this the search results page?" / "Are there at least 10 results visible?").
"""


# ---------------------------------------------------------------------------
# Prompt functions
# ---------------------------------------------------------------------------


def build_workflow(
    task_description: Annotated[
        str,
        Field(description="What the workflow should automate, e.g. 'Fill out a tax form on irs.gov'"),
    ] = "",
) -> str:
    """Guide for building a Skyvern workflow. Invoke this prompt when a user asks to create,
    design, or build a browser automation workflow.  The guide covers block selection, prompt
    writing, parameterization, testing, and iteration."""
    if task_description:
        return f"{BUILD_WORKFLOW_CONTENT}\n---\n\nUser's automation goal:\n```\n{task_description}\n```\n"
    return BUILD_WORKFLOW_CONTENT


def debug_automation(
    error_or_symptom: Annotated[
        str,
        Field(description="The error message or symptom to diagnose, e.g. 'Timeout after 30s on login page'"),
    ] = "",
) -> str:
    """Diagnose and fix a failing Skyvern workflow or task.

    Guides you through reading Skyvern's structured error output, matching the failure to a known pattern,
    and fixing the workflow by updating block prompts — without manually exploring in a browser.
    """
    parts = [DEBUG_AUTOMATION_CONTENT]
    if error_or_symptom:
        parts.append(
            f"\n---\n\nThe user reports this error or symptom:\n```\n{error_or_symptom}\n```\n\n"
            "Start at Step 1: call skyvern_workflow_status (or check the last tool response) to get the full error "
            "details. Then match it to a pattern in Step 2 and apply the fix from Step 3."
        )
    return "\n".join(parts)


def extract_data(
    target_description: Annotated[
        str,
        Field(description="What data to extract, e.g. 'Product prices and ratings from Amazon search results'"),
    ] = "",
) -> str:
    """Guide for extracting structured data from websites using Skyvern.

    Covers schema design, writing extraction prompts, multi-page extraction patterns, and result
    validation. Call this prompt before building an extraction workflow or writing extraction calls.
    """
    suffix = ""
    if target_description:
        suffix = (
            f"\n\nApply the above methodology to extract:\n```\n{target_description}\n```\n"
            "Design a JSON Schema for the output, choose the right tool "
            "(skyvern_extract for current page, skyvern_run_task for navigate-then-extract), "
            "and validate the results."
        )
    return EXTRACT_DATA_CONTENT + suffix
