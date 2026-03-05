---
name: qa
description: "QA test your frontend changes in a real browser. Reads your git diff, generates targeted browser tests, runs them against your local dev server, and reports pass/fail with screenshots."
---

# QA — Test Your Frontend Changes in a Real Browser

<!-- NOTE: This content is maintained in three places — keep all in sync:
     1. skyvern/cli/skills/qa/SKILL.md         (bundled with pip package — canonical)
     2. .claude/skills/qa/SKILL.md              (project-local copy for this repo)
     3. skyvern/cli/mcp_tools/prompts.py        (QA_TEST_CONTENT for the MCP prompt) -->

You changed some code. This skill reads your diff, understands what UI was affected, opens
a real browser against your running dev server, and tests that your changes actually work.

## Quick Start

```
/qa                              # Diff-based: test what you changed
/qa http://localhost:3000        # Same, explicit URL
/qa -- test the checkout flow    # Targeted: test specific behavior
```

## How It Works

1. **Read your code changes** (`git diff`) to understand what you modified
2. **Read the changed files** to understand the UI: routes, components, props, text
3. **Generate targeted test cases** based on what the code actually does
4. **Open a browser** against your running dev server
5. **Run the tests** — navigate, interact, assert, screenshot
6. **Report** pass/fail with evidence

This is NOT a generic website crawler. It tests YOUR changes specifically.

---

## Step 1: Understand the Changes

### Get the diff

```bash
# What files changed?
git diff --name-only HEAD~1     # vs last commit (if changes are committed)
git diff --name-only             # vs working tree (if uncommitted)

# Full diff for context
git diff HEAD~1                  # or git diff for uncommitted
```

Pick whichever diff has content. If both are empty, there's nothing to QA.

### Read the changed files

For every changed frontend file (`.tsx`, `.jsx`, `.ts`, `.js`, `.css`, `.html`):
- Read the FULL file (not just the diff) to understand the component
- Look for: route paths, component names, text labels, form fields, button labels,
  API endpoints called, conditional rendering, error states

### Classify the changes

| Change Type | What to Test |
|-------------|-------------|
| New component/page | Navigate to it, verify it renders, interact with its elements |
| Modified component | Navigate to it, verify the specific change works (new button, new text, new behavior) |
| Styling changes | Navigate, screenshot, verify layout isn't broken |
| API integration | Navigate, trigger the action, verify the API call works (check network, verify UI updates) |
| Form changes | Fill the form, submit, verify validation and success states |
| Route changes | Navigate to old and new routes, verify routing works |
| Shared component (used in many places) | Test 2-3 pages that use it |
| Bug fix | Reproduce the original bug scenario, verify it's fixed |

### Generate test cases

For each changed file, write specific test cases. Example:

If the diff shows changes to `LoginForm.tsx` adding a "Forgot password" link:
```
Test 1: Login page renders the new "Forgot password" link
  - Navigate to /login
  - Assert: link with text "Forgot password" exists
  - Click it
  - Assert: navigated to /forgot-password (or modal appeared)

Test 2: Login form still works (regression)
  - Navigate to /login
  - Verify email and password inputs exist
  - Submit empty form, verify validation errors appear
```

**Be specific.** Don't write "verify the page works." Write "verify the 'Forgot password'
link navigates to /forgot-password."

---

## Step 2: Find the Dev Server

If the user provided a URL, use it. Otherwise, auto-detect:

```
# Try common dev server ports
# 5173 (Vite), 3000 (Next/CRA), 3001, 8080, 8000, 4200 (Angular)
```

Navigate to each until one responds. If none respond, tell the user:
"Start your dev server first, then run `/qa` again."

---

## Step 3: Connect to a Browser

```
skyvern_browser_session_create(local=true, headless=false, timeout=15)
```

Use `local=true` so it can reach `localhost`. Use `headless=false` so the user can watch.

If local fails, fall back to cloud (warn that URL must be publicly accessible):
```
skyvern_browser_session_create(timeout=15)
```

---

## Step 4: Run the Tests

For each test case generated in Step 1:

### Navigate
```
skyvern_navigate(url="http://localhost:<port>/<route>")
```

### Health gate (after every navigate, ~10ms)
```
skyvern_evaluate(expression="(() => {
  const errors = [];
  const body = document.body?.innerText || '';
  if (body.includes('Something went wrong')) errors.push('error_message');
  if (body.includes('Cannot read properties')) errors.push('js_error_in_ui');
  if (/\bundefined\b/.test(body) && !/\bif\b|\btypeof\b|\bdocument|tutorial|example/i.test(body) && body.length < 5000) errors.push('undefined_text');
  if (body.includes('connection refused')) errors.push('connection_refused');
  if (/sign.?in|log.?in|auth/i.test(window.location.pathname)) errors.push('auth_redirect');
  if (document.querySelector('[role=\"alert\"]')) errors.push('alert_element');
  if (!document.querySelector('main, [role=\"main\"], nav, header, h1, h2, [class*=\"layout\" i], [class*=\"page\" i], [class*=\"app\" i]'))
    errors.push('blank_page');
  return JSON.stringify({ pass: errors.length === 0, errors });
})()")
```

### Assert with DOM queries (prefer `skyvern_evaluate` — fast, deterministic)
```
# Element exists
skyvern_evaluate(expression="!!document.querySelector('a[href=\"/forgot-password\"]')")

# Text content
skyvern_evaluate(expression="document.querySelector('h1')?.textContent?.trim()")

# Element count
skyvern_evaluate(expression="document.querySelectorAll('.card').length")

# URL after navigation
skyvern_evaluate(expression="window.location.pathname")
```

### Interact (use `skyvern_act` for natural language actions)
```
skyvern_act(prompt="Click the 'Forgot password' link")
skyvern_act(prompt="Fill the email field with 'test@example.com' and click Submit")
skyvern_act(prompt="Open the dropdown menu and select 'Settings'")
```

### Visual checks (use `skyvern_validate` only when DOM queries aren't enough)
```
skyvern_validate(prompt="The login form shows email and password fields with a blue Submit button")
```

### Screenshot (after every significant action)
```
skyvern_screenshot()
```

### Failed network requests (once per page)
```
skyvern_evaluate(expression="(() => {
  const entries = performance.getEntriesByType('resource').filter(e => e.responseStatus >= 400);
  return JSON.stringify({ failed: entries.map(e => ({ url: e.name, status: e.responseStatus })).slice(0, 5) });
})()")
```

---

## Step 5: Report Results

```markdown
## QA Report

### Changes Tested
Files: `LoginForm.tsx`, `ForgotPassword.tsx`
Diff summary: Added "Forgot password" link to login form, new /forgot-password page

### Results
| # | Test | Result | Screenshot |
|---|------|--------|------------|
| 1 | Login page renders "Forgot password" link | PASS | screenshot_1 |
| 2 | Clicking link navigates to /forgot-password | PASS | screenshot_2 |
| 3 | Forgot password page renders form | PASS | screenshot_3 |
| 4 | Login form still works (regression) | PASS | screenshot_4 |
| 5 | Empty form shows validation errors | FAIL | screenshot_5 |

### Issues Found
1. **Empty login form submits without validation** — Submitting with no email/password
   doesn't show error messages. The form submits and the page reloads.
   Expected: validation errors. Screenshot: screenshot_5

### Network
- No failed requests detected

### Verdict
4/5 tests passed. 1 issue found: missing form validation on empty submit.
```

---

## Tool Selection

| What you need | Tool | Speed |
|---------------|------|-------|
| Check element exists, text, count, URL | `skyvern_evaluate` | ~10ms |
| Click, type, fill forms, multi-step interaction | `skyvern_act` | 5-30s |
| "Does this look right?" visual check | `skyvern_validate` | 15-50s |
| Get structured data from a page | `skyvern_extract` | 15-50s |
| Screenshot | `skyvern_screenshot` | ~1s |
| Wait for async content | `skyvern_wait` | varies |

**Default to `skyvern_evaluate` for assertions.** Only use `skyvern_validate` when you
can't express the check as a DOM query (visual layout, "does this look like a dashboard").

---

## Error Handling

| Problem | Action |
|---------|--------|
| No git diff found | Ask user what they want to test, fall back to explore mode |
| Dev server not running | Tell user to start it. Suggest common commands (npm run dev, etc.) |
| Auth redirect on page | Report it. Ask if they want to provide credentials or skip that route. |
| Component doesn't render | Screenshot + check console. Report with the specific error. |
| Session create fails | Try cloud fallback. Warn about URL accessibility. |

## Session Cleanup

ALWAYS close the session when done, even if errors occurred:
```
skyvern_browser_session_close()
```

---

## Fallback: Explore Mode

If there's no git diff (user just wants a general QA pass), fall back to exploring:

1. Navigate to the root URL
2. Extract nav links and page structure with `skyvern_extract`
3. Visit each major route, health gate + screenshot
4. Test interactive elements (forms, buttons, links)
5. Report findings

But the primary mode is **diff-driven**. The agent should always try to read the code
changes first.
