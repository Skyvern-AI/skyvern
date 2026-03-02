---
name: testing
description: Verify a Skyvern deployment is working correctly by smoke-testing the backend API, frontend rendering, browser session provisioning, and workflow execution. Use when the user says 'is Skyvern working', 'test my deployment', 'verify the installation', 'smoke test', or needs to check that a self-hosted or local Skyvern instance is healthy.
---

# Testing

Smoke-test a Skyvern deployment to verify the backend API responds, the frontend renders
correctly, browser sessions can be provisioned, and workflows can execute end to end.

## Checks

Run these three checks sequentially. Stop on the first failure — later checks depend on
earlier ones passing.

### 1. Backend API Health + Frontend Renders

Verify the API server is reachable and the frontend renders correctly.

```
skyvern_browser_session_create(timeout=5)
skyvern_navigate(url="{{base_url}}")
skyvern_evaluate(expression="fetch('/api/v1/workflows?page=1&page_size=1', {credentials: 'include'}).then(r => ({status: r.status, ok: r.ok, reachable: r.status > 0})).catch(e => ({status: 0, ok: false, reachable: false, error: e.message}))")
```

**Pass**: fetch returns a response (any HTTP status confirms the backend is reachable).
A 2xx means fully healthy; 401/403 means the backend is running but requires authentication.
Only `status: 0` or a network error means the backend is actually down.

```
skyvern_navigate(url="{{base_url}}/discover")
skyvern_validate(prompt="The page does NOT show any error messages, error toasts, 'Something went wrong', a persistent loading spinner, a blank white screen, or a connection refused message")
skyvern_validate(prompt="The page shows 'What task would you like to accomplish?' as a heading, a text input area with 'Enter your prompt...' placeholder, an engine version selector, and a send/submit button icon")
skyvern_screenshot()
skyvern_browser_session_close()
```

**Pass**: backend returned an HTTP response (not a network error) AND both frontend validations return `valid: true`.

### 2. Browser Session Provisioning

Verify the system can create, use, and close browser sessions end to end. This tests the
critical path — Skyvern's ability to provision cloud browsers.

```
skyvern_browser_session_create(timeout=5)
skyvern_navigate(url="https://example.com")
skyvern_validate(prompt="The page shows 'Example Domain' as a heading and contains a link to 'More information'")
skyvern_browser_session_close()
```

**Pass**: session creation succeeds, navigation works, and the external page loads correctly.
If this fails, browser provisioning infrastructure is broken.

### 3. Workflow Execution (Smoke)

Verify a minimal workflow can be created and executed to completion.

```
skyvern_workflow_create(definition='{"title":"Deployment Smoke Test","workflow_definition":{"parameters":[],"blocks":[{"block_type":"goto_url","label":"visit","url":"https://example.com"}]}}', format="json")
skyvern_workflow_run(workflow_id="<id from above>", wait=true, timeout_seconds=60)
skyvern_workflow_status(run_id="<run_id from above>")
```

**Pass**: workflow run completes with status `completed`. If this fails, the execution
pipeline (Temporal workers, browser provisioning, or task orchestration) is broken.

**Always clean up the smoke test workflow**, regardless of pass or fail:
```
skyvern_workflow_delete(workflow_id="<id>", force=true)
```

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `base_url` | `http://localhost:8080` | Frontend URL to test (Skyvern default is 8080) |

## Pass Criteria

All three checks pass in order. If any check fails:
1. Capture a screenshot with `skyvern_screenshot()`
2. Report which check failed and why
3. Skip remaining checks (they depend on earlier ones)

## Retry Protocol

When a validation returns `valid: false`:
1. Wait 3 seconds: `skyvern_wait(time_ms=3000)`
2. Take a screenshot for evidence: `skyvern_screenshot()`
3. Retry the same validation once
4. If still false, mark as FAILED with both screenshots attached

## Session Cleanup

ALWAYS close the session, even if earlier steps fail. If any step errors out:
1. Capture a failure screenshot: `skyvern_screenshot()`
2. Record the failure reason
3. Call `skyvern_browser_session_close()` before moving to the next check

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Connection refused | Backend not running | `./run_skyvern.sh` or `skyvern run server` |
| Auth redirect to /sign-in | Running cloud build (Clerk auth) | Use the OSS entry point (`src/main.tsx`) instead of the cloud entry (`cloud/index.tsx`) |
| Blank page | Frontend not built/running | `cd skyvern-frontend && npm run dev` |
| API returns 401/403 | API key invalid or expired | Check `VITE_SKYVERN_API_KEY`. Note: 401/403 still confirms the backend is running. |
| Port 5173 instead of 8080 | Using Vite default, not Skyvern's | Skyvern runs on 8080 by default. Use `/testing http://localhost:8080` |
| Session create fails | Browser infra down | Check Docker/cloud browser service |
| Workflow stuck | Workers not running | Check Temporal workers with `./run_worker.sh` |
| API check 404 on fetch | Non-Vite server without proxy | The API health check uses `fetch('/api/v1/...')` which relies on the Vite dev server proxy. For production builds served by another web server, ensure the server proxies `/api/` to the backend. |
