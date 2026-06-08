---
name: smoke-test
description: "Run smoke tests against a deployed or local app based on your git diff. Each test uses Skyvern browser tools (navigate, act, validate, screenshot) with Chrome DevTools MCP as fallback. Posts screenshot evidence as PR comments."
---

# Smoke Test — CI-Oriented Validation via Skyvern Browser Tools

Read the diff, classify changes, start the app, and run targeted smoke tests via Skyvern browser tools with Chrome DevTools MCP as fallback.

<!-- NOTE: This content is maintained in two places — keep in sync:
     1. skyvern/cli/skills/smoke-test/SKILL.md  (bundled with pip — canonical)
     2. .claude/skills/smoke-test/SKILL.md       (project-local copy)
     Steps 1-4 are copied from skyvern/cli/skills/qa/SKILL.md.
     If you fix bugs in /qa's diff-reading, classification, or app startup,
     mirror those fixes here. -->

You changed code. This skill reads the diff, generates targeted smoke tests, and runs
each one via Skyvern browser tools - navigate, act, validate, screenshot. If Skyvern
tools are unavailable (no Skyvern MCP), fall back to Chrome DevTools MCP, which connects
to a real Chrome instance and supports authenticated sessions. It is /qa's CI companion:
same diff-reading, same classification, same app startup, formatted for CI output and
PR comments with screenshot evidence on every test.

## Quick Start

```text
/smoke-test                              # Diff-driven, auto-detect everything
/smoke-test https://staging.example.com  # Explicit app URL
/smoke-test -- focus on the settings page
/smoke-test --pr 11488                   # Target a specific PR
```

## How It Works

1. Read git diff (reused from /qa)
2. Classify changes → identify testable surfaces (reused from /qa)
3. Choose validation strategy (reused from /qa)
4. Pick browser backend (Chrome DevTools MCP or Skyvern)
5. Handle auth (connect to authenticated session or bypass)
6. Start the app if needed (reused from /qa)
7. Generate 3-8 smoke test cases as action sequences (happy paths only)
8. Run each test via browser tools: navigate → act → validate → screenshot
9. Collect results with screenshot evidence
10. Report with embedded screenshots
11. Post to PR with images

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

## Step 3b: Pick Browser Backend

Try Skyvern browser tools first. Fall back to Chrome DevTools MCP if Skyvern MCP is
unavailable.

### Skyvern browser tools (primary)

Use `skyvern_browser_session_create`, `skyvern_navigate`, `skyvern_act`,
`skyvern_validate`, `skyvern_screenshot`. Works in CI and locally. Create a `local=true`
session to reach localhost.

### Chrome DevTools MCP (fallback)

If Skyvern MCP tools are not available, invoke the `/chrome-devtools` skill to learn the
tool API. Key tools:

- `list_pages` / `new_page` / `navigate_page` - page management
- `take_screenshot` - capture viewport (save to a path within workspace roots)
- `take_snapshot` - get page structure with element `uid`s for interaction
- `click` / `fill` / `press_key` - interact with elements by `uid`
- `evaluate_script` - run JS for health gates and data extraction
- `wait_for` - wait for content to load

Chrome DevTools MCP connects to a real Chrome instance with the user's profile, so
**authenticated sessions carry over** - no separate auth step needed if the user is
already logged in. Useful when the app requires auth that Skyvern sessions can't provide.

## Step 3c: Handle Auth

Check whether the app requires authentication:

1. **Chrome DevTools MCP with existing session**: Call `list_pages`. If the user already
   has the app open and authenticated, `navigate_page` inherits their session. No auth
   step needed.
2. **User needs to log in**: Call `new_page` or `navigate_page` to the app URL. If a
   login page appears, tell the user: "Chrome is open to the login page. Please
   authenticate, then tell me when you're done." Wait for confirmation before proceeding.
3. **Auth bypass**: If the repo has a local auth-bypass mechanism (env var or dev
   branch), use it. Check the project's dev setup docs or CLAUDE.md for details.
4. **CI / headless**: Use Skyvern browser tools with `local=true` against an
   unauthenticated dev server, or test only public-facing pages.

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

### Option A: Skyvern browser tools (primary)

For localhost URLs, create a local browser session:

```text
skyvern_browser_session_create(local=true, timeout=15)
```

For publicly reachable URLs, create a cloud session instead:

```text
skyvern_browser_session_create(timeout=15)
```

For each test case, run its action sequence. Every test starts with `skyvern_navigate`:

```text
skyvern_navigate(url="http://localhost:5173/settings")
```

**CRITICAL: Always include the `url` parameter in every `skyvern_navigate` call.**
Never omit it and rely on the current page - this prevents test-to-test state bleed.

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

If the health gate fails, mark the test as FAIL and move on. Otherwise, continue with
the test's action steps:

```text
skyvern_act(prompt="Fill Company Name with 'Test Corp', click Save")
skyvern_validate(prompt="Success toast visible, no error state")
skyvern_screenshot()
```

### Option B: Chrome DevTools MCP (fallback)

If Skyvern MCP tools are unavailable, use Chrome DevTools MCP. Invoke the
`/chrome-devtools` skill first.

No session setup needed - the browser launches automatically on first tool call.
For each test:

```text
1. navigate_page(url="http://localhost:8080/workflows")
2. wait_for(text="Workflows", timeout=5000)
3. take_snapshot()                                             # get element UIDs
4. click(uid="<target-element>")                               # interact
5. take_screenshot(filePath="/path/within/workspace/test-name.png")
```

Run the health gate via `evaluate_script`:

```text
evaluate_script(function="() => {
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
}")
```

**Screenshot paths** must be within the workspace roots reported by the MCP server.

Chrome DevTools MCP connects to a real Chrome instance with the user's profile, so
authenticated sessions carry over. If the user is already logged in, no auth step needed.
If a login page appears, tell the user to authenticate in the browser window and wait.

### Collect results

For each test, record:

- **result** - PASS or FAIL based on validation outcome and health gate
- **evidence** - one-line description of what was observed
- **screenshot** - file path to the captured screenshot (required for every test)

## Step 7: Report Results

The report must include embedded screenshots. Host them at a **PR-accessible URL** and reference
them as markdown images. **Do not upload evidence to a GitHub release, and do not commit
screenshots into the repository.**

### Host the screenshots

Embed each screenshot from somewhere the PR can render it:

- An image host or your issue tracker's attachment upload that returns a public URL, or
- Drag-and-drop the file into the PR or comment composer on github.com, which uploads it to
  `user-attachments` and yields a ready markdown embed.

**Privacy gate:** the embedded URL is public and the PR may be public, so before uploading scrub
each capture of names, PII, secrets, tokens, and internal URLs — otherwise re-capture against
non-sensitive data, crop/mask the sensitive regions, or describe the result instead of embedding it.

Look up the PR number (Step 8 reuses it):

```bash
PR_NUMBER=$(gh pr view --json number -q '.number' 2>/dev/null || echo "draft")
```

If you have no PR-accessible host, save the captures under `.qa/screenshots/` and tell the user
where they are and that a host is needed — do not post unreachable local paths as evidence.

### Report format

```markdown
## Smoke Test Report

### Changes Tested
- <summary of what changed, from the diff>

### Results
| Flow | Result | Screenshot |
|------|--------|------------|
| Workflows page | PASS | ![workflows](https://<image-host>/workflows.png) |
| Task detail | PASS | ![task-detail](https://<image-host>/task-detail.png) |
| Settings save | FAIL | ![settings-error](https://<image-host>/settings.png) |

### Verdict
2/3 tests passed. 1 issue found.
```

Every test row must have a screenshot. The screenshot is the primary evidence.

## Step 8: Post to PR

After generating the report, persist it to the pull request as a sticky comment so the
evidence survives beyond the conversation.

### Check for an open PR

`PR_NUMBER` was set in Step 7. If it is `"draft"` (no PR found):
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
| Chrome DevTools MCP browser profile locked | Kill stale Chrome processes, retry. If still locked, fall back to Skyvern |
| Auth redirect detected | Tell user to authenticate in the Chrome DevTools browser, wait for confirmation |
| Skyvern browser tools also unavailable | Use Playwright directly (`npx playwright`) as last resort |
| Health gate fails on navigation | Mark the test as FAIL, take a screenshot anyway for evidence, continue |
| Screenshot upload fails | Save the captures under `.qa/screenshots/` for the record, then stop and ask the user for a PR-accessible host — do not post unreachable local paths as evidence |
| No testable changes (docs-only, config-only) | Report "Changes are non-behavioral - no smoke tests generated." |

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
