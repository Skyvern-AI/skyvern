---
name: qa
description: "QA test your code changes by reading your git diff, choosing the right validation path for frontend/browser and backend changes, and reporting pass/fail with evidence."
---

# QA — Validate Frontend and Backend Changes

Read the diff, classify what changed, and run the right validation path: browser QA for frontend/browser changes, API validation for backend surface changes, repo-native validation for backend-internal changes, and both for mixed changes.

<!-- NOTE: This content is maintained in three places — keep all in sync:
     1. skyvern/cli/skills/qa/SKILL.md         (bundled with pip package — canonical)
     2. .claude/skills/qa/SKILL.md              (project-local copy for this repo)
     3. skyvern/cli/mcp_tools/prompts.py        (QA_TEST_CONTENT for the MCP prompt) -->

You changed code. This skill is diff-driven first: it reads what changed, understands the
affected behavior, and validates that behavior with the right tools. It is not a generic
website crawler, and it should not invent random API checks that are unrelated to the diff.

## Quick Start

```text
/qa                              # Diff-based: choose the right validation path automatically
/qa http://localhost:3000        # Same, explicit frontend URL
/qa -- validate the workflow filters API
```

## How It Works

1. Read the code changes from `git diff`
2. Read the changed files to understand behavior, routes, schemas, and UI
3. Classify the diff as `frontend/browser`, `backend API`, `backend-internal`, or `mixed`
4. Run the right validation flow
5. Report pass/fail with concrete evidence

## Step 1: Understand the Changes

### Get the diff

```bash
# What files changed?
git diff --name-only HEAD~1     # vs last commit (if changes are committed)
git diff --name-only            # vs working tree (if uncommitted)

# Full diff for context
git diff HEAD~1                 # or git diff for uncommitted
```

Pick whichever diff has content. If both are empty, there is nothing diff-driven to QA.

### Read the changed files

Read the full contents of every changed file that affects behavior:

- Frontend files: `.tsx`, `.jsx`, `.ts`, `.js`, `.css`, `.html`
- Backend/API files: routes, controllers, request/response schemas, serializers, handlers
- Backend-internal files: services, workers, business logic, validators, data-layer code
- Tests that changed alongside the implementation

Look for:

- route paths and page entry points
- component names, visible text, forms, buttons, error states
- API endpoints, request params, response fields, auth requirements
- validation logic, branching behavior, feature flags, empty states
- tests that describe the expected behavior

## Step 2: Classify the Diff

| Mode | Trigger | Primary validation |
|------|---------|--------------------|
| Frontend/browser | UI/routes/components/styles changed | Browser QA against the dev server |
| Backend API | Route handlers, request/response schemas, or externally visible API behavior changed | Start backend locally and run targeted API requests |
| Backend-internal | Services/workers/business logic changed without public API surface changes | Repo-native fast checks plus targeted tests |
| Mixed | Frontend/browser and backend changed together | Backend validation first, then frontend/browser QA |

Use these rules:

- If both frontend/browser and backend changed, treat it as `Mixed`.
- If only backend internals changed, do not invent unrelated browser tests or random API calls.
- If a backend change might affect the public contract, inspect routes, schemas, and tests before choosing `backend-internal`.
- If the diff is mostly documentation or comments, keep QA lightweight and report that no behavioral validation was warranted.

## Step 3: Choose the Validation Strategy

### Frontend/browser mode

Use browser automation against the dev server. Validate the specific UI changes plus 1-2
adjacent regression checks.

### Backend API mode

Use the repo's documented local startup and auth instructions, start the backend if needed,
identify the changed endpoint(s), and run targeted HTTP requests to validate the changed contract.

### Backend-internal mode

Run the repo's fast verification commands first, then targeted unit/integration/scenario tests
for the changed logic. Only start the backend and do live API calls if the change affects exposed behavior.

### Mixed mode

Validate the backend first, then run frontend/browser QA against the flow that depends on it.
If the backend contract is broken, frontend results are not trustworthy.

## Step 4A: Frontend/Browser QA

### Find the dev server

If the user provided a URL, use it. Otherwise auto-detect common local ports:

```text
5173, 3000, 3001, 8080, 8000, 4200
```

If none respond, start the most direct repo-documented local command for the
changed surface. If the diff needs both frontend and backend running together
and the repo provides a combined frontend/backend dev script, prefer that.
Only ask the user to start something manually if the repo has no documented
command or startup fails.

### Connect to a browser

Try these in order:

#### Option A: Local browser (fastest)

```text
skyvern_browser_session_create(local=true, headless=false, timeout=15)
```

Use `local=true` so the browser can reach `localhost`.

#### Option B: Local browser via tunnel

If local session creation fails because the MCP server is remote, the cloud browser cannot reach
`localhost`. Tell the user to run:

```bash
# Terminal 1: Launch a local browser with CDP exposed
skyvern browser serve --port 9222

# Terminal 2: Tunnel it to the internet
ngrok http 9222
```

Then connect:

```text
skyvern_browser_session_connect(cdp_url="wss://<ngrok-subdomain>.ngrok-free.app/devtools/browser/<id>")
```

The user can get the browser ID from the `skyvern browser serve` output or by calling the
ngrok URL's `/json` endpoint.

#### Option C: Cloud browser

```text
skyvern_browser_session_create(timeout=15)
```

Only works for publicly reachable URLs. `localhost` URLs will not work here.

### Generate frontend/browser test cases

For each changed frontend file, create targeted checks. Examples:

```text
Test 1: Settings page renders the new "Retry failed run" button
  - Navigate to /settings/runs
  - Assert: button with text "Retry failed run" exists
  - Click it
  - Assert: success toast appears

Test 2: Adjacent regression
  - Verify the existing "Delete run" action still works or is still visible
```

Be specific. Do not write "verify the page works."

### Run the frontend/browser tests

For each test case:

```text
skyvern_navigate(url="http://localhost:<port>/<route>")
```

Health gate after navigation:

```text
skyvern_evaluate(expression="(() => {
  const errors = [];
  const body = document.body?.innerText || '';
  if (body.includes('Something went wrong')) errors.push('error_message');
  if (body.includes('Cannot read properties')) errors.push('js_error_in_ui');
  if (/\\bundefined\\b/.test(body) && !/\\bif\\b|\\btypeof\\b|\\bdocument|tutorial|example/i.test(body) && body.length < 5000) errors.push('undefined_text');
  if (body.includes('connection refused')) errors.push('connection_refused');
  if (/sign.?in|log.?in|auth/i.test(window.location.pathname)) errors.push('auth_redirect');
  if (document.querySelector('[role=\"alert\"]')) errors.push('alert_element');
  if (!document.querySelector('main, [role=\"main\"], nav, header, h1, h2, [class*=\"layout\" i], [class*=\"page\" i], [class*=\"app\" i]'))
    errors.push('blank_page');
  return JSON.stringify({ pass: errors.length === 0, errors });
})()")
```

Prefer deterministic DOM assertions:

```text
skyvern_evaluate(expression="!!document.querySelector('button')")
skyvern_evaluate(expression="document.querySelector('h1')?.textContent?.trim()")
skyvern_evaluate(expression="window.location.pathname")
```

Use interaction tools when needed:

```text
skyvern_act(prompt="Click the 'Retry failed run' button")
skyvern_act(prompt="Fill the email field with 'test@example.com' and click Submit")
skyvern_validate(prompt="The page shows the success toast and the form is no longer loading")
skyvern_screenshot()
```

Also check for failed network requests once per page:

```text
skyvern_evaluate(expression="(() => {
  const entries = performance.getEntriesByType('resource').filter(e => e.responseStatus >= 400);
  return JSON.stringify({ failed: entries.map(e => ({ url: e.name, status: e.responseStatus })).slice(0, 5) });
})()")
```

## Step 4B: Backend API QA

### Gather repo-local context first

Before starting the server or sending requests, read the repo's local instructions:

- `README`, `AGENTS.md`, `CLAUDE.md`, `Makefile`, `package.json`, `pyproject.toml`
- existing test files for the changed endpoints
- any docs that describe auth, local ports, or startup commands

Do not guess the startup command if the repo already documents one.

### Start the backend if needed

If the backend is not already responding on the expected local port:

1. Start it with the most direct repo-documented local command for the changed
   surface. If the validation needs both frontend and backend and the repo
   documents a combined dev environment command, prefer that over inventing
   separate startup steps.
2. Wait for readiness
3. Confirm the API is reachable before sending validation requests

If the repo requires background processes, start them in the background and keep notes on how you did it.

### Identify the changed API surface

Use the diff to answer:

- Which endpoints changed?
- Which request parameters, headers, or bodies changed?
- Which response fields or status codes changed?
- Did auth requirements change?
- Is there a create/update/delete side effect that needs follow-up verification?

Do not stop at the route file. Read the full handler, schema, and any changed tests.

### Generate backend API test cases

For each changed endpoint, create targeted checks:

- Happy path with valid parameters
- Empty result or not-found path where applicable
- Invalid input or validation error path
- Combined filters or sorting behavior when relevant
- Follow-up read after mutation endpoints

Examples:

```text
Test 1: GET /api/runs returns the new field in the response body
Test 2: GET /api/runs?status=missing returns an empty list, not a 500
Test 3: POST /api/runs rejects invalid payload with a 4xx validation error
Test 4: PATCH /api/runs/:id updates the record and a follow-up GET shows the change
```

### Execute the API requests

Use the repo's documented auth scheme and local base URL. Use `curl`, the repo SDK, or a small
one-off client if that is clearer than shell quoting. Prefer simple, inspectable commands.

Examples:

```bash
curl -sS -H "Authorization: Bearer <token>" \
  "http://localhost:<port>/api/..."

curl -sS -X POST \
  -H "Content-Type: application/json" \
  -H "<auth-header>: <token>" \
  -d '{"example":"value"}' \
  "http://localhost:<port>/api/..."
```

Capture:

- request being tested
- status code
- response body snippet or parsed result
- whether the changed field/behavior is present

If the endpoint is authenticated and you cannot obtain local credentials from repo docs, say so clearly and stop rather than faking coverage.

## Step 4C: Backend-Internal QA

If the diff is backend-only but does not change an exposed endpoint or UI flow:

1. Run the repo's fastest compile/type/lint checks for the changed files
2. Run targeted unit/integration/scenario tests that cover the changed logic
3. Add live API calls only if the internal change affects exposed behavior

Examples of appropriate checks:

- compile/type-check the changed files
- targeted `pytest`, `npm test`, `go test`, or equivalent
- scenario/integration tests around changed services or workflows

Examples of inappropriate checks:

- hitting an unrelated health endpoint and calling it "validated"
- browsing the UI when no frontend behavior changed
- calling random APIs just because the backend changed

## Step 5: Report Results

```markdown
## QA Report

### Validation Mode
- Mode: Backend API
- Scope: `routes/runs.py`, `schemas/run_response.py`

### Changes Tested
- Added `retryable` field to run responses
- Updated `status` filter handling

### Results
| # | Test | Result | Evidence |
|---|------|--------|----------|
| 1 | GET /api/runs returns `retryable` for valid runs | PASS | HTTP 200, field present in response |
| 2 | GET /api/runs?status=missing returns empty list | PASS | HTTP 200, `[]` |
| 3 | GET /api/runs?status=invalid returns validation error | PASS | HTTP 422 |
| 4 | Frontend runs page still renders filter state | PASS | screenshot_3 |

### Issues Found
1. `retryable` is missing from one branch of the response serializer.

### Verdict
3/4 tests passed. 1 issue found.
```

Report the evidence that actually matters:

- screenshots for frontend/browser results
- status codes and response snippets for backend API results
- command + failing assertion for unit/integration tests

## Step 6: Post Evidence to PR

After generating the QA report, persist it to the pull request as a sticky comment so the
evidence survives beyond the conversation.

### Check for an open PR

```bash
PR_NUMBER=$(gh pr view --json number -q '.number' 2>/dev/null)
```

If no PR exists for the current branch:
1. Save the full report markdown to `.qa/latest-report.md` in the project root (create the directory if needed).
2. Tell the user: "No open PR found for this branch. QA report saved to `.qa/latest-report.md`. Run /qa again after creating a PR to post it."
3. Stop here — do not attempt to create a PR.

### Post or update the sticky comment

Use a hidden HTML marker to make the comment idempotent across multiple runs:

```bash
# Prepare the comment body with the hidden marker
COMMENT_BODY="<!-- skyvern-qa-report -->
## QA Report — $(git rev-parse --short HEAD) — $(date -u +%Y-%m-%dT%H:%M:%SZ)

<the full report markdown from Step 5>
"

# Find an existing QA comment on the PR
EXISTING_COMMENT_ID=$(gh api "repos/{owner}/{repo}/issues/${PR_NUMBER}/comments" \
  --jq '.[] | select(.body | test("skyvern-qa-report")) | .id' \
  2>/dev/null | head -1)

if [ -n "$EXISTING_COMMENT_ID" ]; then
  # Update the existing comment in place
  gh api "repos/{owner}/{repo}/issues/comments/${EXISTING_COMMENT_ID}" \
    -X PATCH -f body="$COMMENT_BODY"
else
  # Create a new comment
  gh pr comment "$PR_NUMBER" --body "$COMMENT_BODY"
fi
```

### Screenshot handling

Screenshots taken during QA (via `skyvern_screenshot()`) are saved locally for the agent's
verification. They are not uploaded to the PR comment because GitHub's API does not support
image uploads in issue comments. The text report describes what was observed.

If the user asks to preserve screenshots, save them to `.qa/screenshots/` and tell the user
the local path. Do not include local file paths in the PR comment — they are meaningless to
other reviewers.

### Rules

- Always include the `<!-- skyvern-qa-report -->` marker so repeated runs update the same comment instead of creating duplicates.
- Include the short commit hash and UTC timestamp in the comment header.
- Do not create a PR just to post a QA report — that is the user's decision.
- If `gh` is not available or not authenticated, fall back to saving the report locally and tell the user.

## Error Handling

| Problem | Action |
|---------|--------|
| No git diff found | Ask what behavior to validate, then fall back to explore mode |
| Frontend dev server not running | Start the most direct repo-documented local command for the changed surface; prefer a combined dev command only when the validation needs both frontend and backend; only ask the user if no documented command exists or startup fails |
| Backend server not running | Start the most direct repo-documented local command for the changed surface; prefer a combined dev environment command only when the validation needs both sides |
| Cannot identify changed endpoint | Read changed routes, schemas, and tests before proceeding |
| Auth required but no local creds available | Report the blocker clearly; do not fake coverage |
| Component does not render | Capture screenshot and specific UI error |
| API returns unexpected 5xx | Save request/response evidence and report the regression |

## Session Cleanup

Always close browser sessions when done:

```text
skyvern_browser_session_close()
```

If you started local servers or background processes, leave the user a clear note about what is still running.

## Fallback: Explore Mode

If there is no useful diff, fall back to explicit exploration:

1. Ask what behavior should be validated
2. If it is a frontend flow, use browser QA
3. If it is a backend/API flow, run targeted local API checks
4. Report findings with the same evidence standard

The primary mode is still **diff-driven**. Always try to understand the code changes first.
