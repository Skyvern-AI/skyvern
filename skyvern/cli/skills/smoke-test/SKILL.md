---
name: smoke-test
description: "Run smoke tests against a deployed or local app based on your git diff. Each test uses Skyvern browser tools (navigate, act, validate, screenshot) and reports a pass/fail table as a PR comment."
---

# Smoke Test — CI-Oriented Validation via Skyvern Browser Tools

Read the diff, classify what changed, start the app, and run targeted smoke tests via Skyvern browser tools (`skyvern_navigate`, `skyvern_act`, `skyvern_validate`, `skyvern_screenshot`) — the same tools /qa uses, formatted for CI and PR comments.

<!-- NOTE: This content is maintained in two places — keep in sync:
     1. skyvern/cli/skills/smoke-test/SKILL.md  (bundled with pip — canonical)
     2. .claude/skills/smoke-test/SKILL.md       (project-local copy)
     Steps 1-4 are copied from skyvern/cli/skills/qa/SKILL.md.
     If you fix bugs in /qa's diff-reading, classification, or app startup,
     mirror those fixes here. -->

You changed code. This skill reads the diff, generates targeted smoke tests, and runs
each one via Skyvern browser tools — navigate, act, validate, screenshot. It is /qa's
CI companion: same diff-reading, same classification, same app startup, same browser
tools, formatted for CI output and PR comments.

## Quick Start

```text
/smoke-test                              # Diff-driven, auto-detect everything
/smoke-test https://staging.example.com  # Explicit app URL
/smoke-test -- focus on the settings page
```

## How It Works

1. Read git diff (reused from /qa)
2. Classify changes → identify testable surfaces (reused from /qa)
3. Choose validation strategy (reused from /qa)
4. Start the app if needed (reused from /qa)
5. Generate 3-8 smoke test cases as action sequences (happy paths only)
6. Run each test via browser tools: navigate → act → validate → screenshot
7. Collect results
8. Report | Flow | Result | Evidence | table
9. Post to PR if GITHUB_TOKEN available

## Step 1: Understand the Changes

### Get the diff

```bash
# What files changed?
git diff --name-only HEAD~1     # vs last commit (if changes are committed)
git diff --name-only            # vs working tree (if uncommitted)

# Full diff for context
git diff HEAD~1                 # or git diff for uncommitted
```

Pick whichever diff has content. If both are empty, there is nothing diff-driven to test.

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
| Frontend/browser | UI/routes/components/styles changed | Browser smoke tests against the dev server |
| Backend API | Route handlers, request/response schemas, or externally visible API behavior changed | Start backend locally and run smoke tests against changed endpoints |
| Backend-internal | Services/workers/business logic changed without public API surface changes | Repo-native fast checks plus targeted tests |
| Mixed | Frontend/browser and backend changed together | Backend validation first, then frontend smoke tests |

Use these rules:

- If both frontend/browser and backend changed, treat it as `Mixed`.
- If only backend internals changed, do not invent unrelated browser tests or random API calls.
- If a backend change might affect the public contract, inspect routes, schemas, and tests before choosing `backend-internal`.
- If the diff is mostly documentation or comments, keep testing lightweight and report that no behavioral validation was warranted.

## Step 3: Choose the Validation Strategy

### Frontend/browser mode

Run smoke tests via Skyvern browser tools against the dev server. Validate the specific UI
changes plus 1-2 adjacent regression checks.

### Backend API mode

Use the repo's documented local startup and auth instructions, start the backend if needed,
identify the changed endpoint(s), and run smoke tests that validate the changed contract.

### Backend-internal mode

Run the repo's fast verification commands first, then targeted unit/integration/scenario tests
for the changed logic. Only run smoke tests if the change affects exposed behavior.

### Mixed mode

Validate the backend first, then run frontend smoke tests against the flow that depends on it.
If the backend contract is broken, frontend results are not trustworthy.

## Step 4: Start the App

If the user provided a URL argument, skip startup and use that URL directly.

Otherwise, auto-detect common local ports:

```text
5173, 3000, 3001, 8080, 8000, 4200
```

If none respond, start the most direct repo-documented local command for the
changed surface. If the diff needs both frontend and backend running together
and the repo provides a combined frontend/backend dev script, prefer that.
Only ask the user to start something manually if the repo has no documented
command or startup fails.

If the repo requires background processes, start them in the background and keep
notes on how you did it.

## Step 5: Generate Smoke Test Cases

For each testable surface identified in Steps 2-3, generate a smoke test case as a
numbered action sequence using Skyvern browser tools.

### Guidelines

- 3-8 tests per PR (stay narrow — test what changed, not everything)
- Happy paths only (smoke level, not deep QA)
- Each test should answer: "does the changed thing still work?"
- For frontend: navigate to the page, interact with the changed element, verify it works
- For backend API: navigate to a page that exercises the API, verify the response
- For mixed: backend-dependent flows first, then frontend that depends on them

### Example test cases

```text
Test: Settings save button works after CSS refactor
1. skyvern_navigate(url="http://localhost:5173/settings")
2. skyvern_act(prompt="Fill Company Name with 'Test Corp', click Save")
3. skyvern_validate(prompt="Success toast visible, no error state")
4. skyvern_screenshot()

Test: Dashboard loads after route refactor
1. skyvern_navigate(url="http://localhost:5173/dashboard")
2. skyvern_validate(prompt="Dashboard content visible, no loading spinner stuck")
3. skyvern_screenshot()

Test: Login redirect works
1. skyvern_navigate(url="http://localhost:5173/protected-page")
2. skyvern_validate(prompt="Redirected to login page or auth prompt shown")
3. skyvern_screenshot()
```

## Step 6: Run Tests via Browser Tools

### Set up a browser session first

For localhost URLs, create a local browser session:

```text
skyvern_browser_session_create(local=true, timeout=15)
```

For publicly reachable URLs, create a cloud session instead:

```text
skyvern_browser_session_create(timeout=15)
```

### Execute each test

For each test case, run its action sequence. Every test starts with `skyvern_navigate`:

```text
skyvern_navigate(url="http://localhost:5173/settings")
```

**CRITICAL: Always include the `url` parameter in every `skyvern_navigate` call.** Never
omit it and rely on the current page — this prevents test-to-test state bleed. Each test
must navigate fresh.

After navigation, run the health gate to catch broken pages early:

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

If the health gate fails, mark the test as FAIL and move on. Otherwise, continue with the
test's action steps:

```text
skyvern_act(prompt="Fill Company Name with 'Test Corp', click Save")
skyvern_validate(prompt="Success toast visible, no error state")
skyvern_screenshot()
```

### Collect results

For each test, record:

- **result** — PASS or FAIL based on `skyvern_validate` outcome and health gate
- **evidence** — one-line description of what was observed (from validate result or health gate errors)

## Step 7: Report Results

```markdown
## Smoke Test Report

### Changes Tested
- <summary of what changed, from the diff>

### Results
| Flow | Result | Evidence |
|------|--------|----------|
| Settings save | PASS | Form submitted, success toast shown |
| Login redirect | PASS | Redirected to /dashboard after sign-in |
| Dashboard nav | FAIL | Sidebar link to /reports returned 404 |

### Verdict
2/3 tests passed. 1 issue found.
```

The Evidence column contains a one-line summary of what was observed during the test.

## Step 8: Post to PR

After generating the report, persist it to the pull request as a sticky comment so the
evidence survives beyond the conversation.

### Check for an open PR

```bash
PR_NUMBER=$(gh pr view --json number -q '.number' 2>/dev/null)
```

If no PR exists for the current branch:
1. Save the full report markdown to `.qa/latest-smoke-report.md` in the project root (create the directory if needed).
2. Tell the user: "No open PR found for this branch. Smoke test report saved to `.qa/latest-smoke-report.md`. Run /smoke-test again after creating a PR to post it."
3. Stop here — do not attempt to create a PR.

### Post or update the sticky comment

Use a hidden HTML marker to make the comment idempotent across multiple runs.
Write the body to a temp file to avoid shell metacharacter issues with multiline
markdown (report content may include attacker-controlled page text from health gates):

```bash
# Write the comment body to a temp file (avoids shell injection from page content)
COMMENT_FILE=$(mktemp)
# Compute dynamic header outside the heredoc (controlled values only)
REPORT_HEADER="## Smoke Test Report — $(git rev-parse --short HEAD) — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
# Write marker and header first (safe, controlled content)
printf '%s\n%s\n\n' '<!-- skyvern-smoke-test-report -->' "$REPORT_HEADER" > "$COMMENT_FILE"
# Append the report body via quoted heredoc (no shell expansion — safe for page content)
cat >> "$COMMENT_FILE" <<'REPORT_EOF'
<the full report markdown from Step 7>
REPORT_EOF

# Find an existing smoke test comment on the PR
EXISTING_COMMENT_ID=$(gh api "repos/{owner}/{repo}/issues/${PR_NUMBER}/comments" \
  --jq '.[] | select(.body | test("skyvern-smoke-test-report")) | .id' \
  2>/dev/null | head -1)

if [ -n "$EXISTING_COMMENT_ID" ]; then
  # Update the existing comment in place (read body from file, no shell expansion)
  gh api "repos/{owner}/{repo}/issues/comments/${EXISTING_COMMENT_ID}" \
    -X PATCH -F body=@"$COMMENT_FILE"
else
  # Create a new comment
  gh pr comment "$PR_NUMBER" --body-file "$COMMENT_FILE"
fi
rm -f "$COMMENT_FILE"
```

### Rules

- Always include the `<!-- skyvern-smoke-test-report -->` marker so repeated runs update the same comment instead of creating duplicates.
- Include the short commit hash and UTC timestamp in the comment header.
- Do not create a PR just to post a report — that is the user's decision.
- If `gh` is not available or not authenticated, fall back to saving the report locally and tell the user.

## Error Handling

| Problem | Action |
|---------|--------|
| No git diff found | Ask what behavior to validate, then fall back to explore mode |
| App not running and no startup command found | Start the most direct repo-documented local command; only ask user if no command exists or startup fails |
| Skyvern browser tools unavailable (no Skyvern MCP) | Report "Skyvern MCP tools not available. Install Skyvern to enable /smoke-test." |
| Health gate fails on navigation | Mark the test as FAIL with the health gate errors as evidence, continue to next test |
| `skyvern_validate` reports failure | Mark the test as FAIL with the validation result as evidence |
| No testable changes (docs-only, config-only) | Report "Changes are non-behavioral — no smoke tests generated." |

## CI Setup

To run `/smoke-test` in a GitHub Action (or any CI), the runner needs:

1. **Claude Code** in headless mode (`claude -p "/smoke-test"`)
2. **ANTHROPIC_API_KEY** — for Claude Code
3. **GITHUB_TOKEN** — for posting smoke test reports as PR comments (Step 8)
4. **Playwright browser** — `pip install skyvern && playwright install chromium` (required for `local=true` sessions that can reach localhost)
5. **Your app running** — started in a prior CI step

Cloud browser sessions (`local=false`) work for publicly reachable URLs (e.g., preview deploys) without Playwright installed, but cannot reach localhost.

## Session Cleanup

Always close the browser session when done:

```text
skyvern_browser_session_close()
```

If you started local servers or background processes, leave the user a clear note about
what is still running.
