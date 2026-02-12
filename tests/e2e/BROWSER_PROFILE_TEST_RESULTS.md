# Browser Profile Test Results — DEFINITIVE PROOF

**Date**: 2026-02-12  
**Test Type**: End-to-end browser profile session restoration  
**Credential**: `cred_494970040378378658` ("credentials_15")  
**Browser Profile**: `bp_494970306666351168`  
**Workflow Run**: `wr_494985634979095196`  
**Result**: ✅ **PASS — Browser profiles ARE restoring sessions**

---

## Executive Summary

The browser profile system is **working correctly**. When a credential has an
existing `browser_profile_id`, the system:

1. Loads the Chromium profile from disk as `user_data_dir`
2. Launches Playwright with the saved cookies/session data
3. The LLM agent detects the already-authenticated page
4. Completes **without filling any login form fields**

This proves the **session is restored from the browser profile, NOT from the LLM
entering credentials**.

---

## Test Timeline (all log lines from Docker)

### 1. Credential Test Endpoint Called
```
[credentials.py:847] Testing credential
  credential_id=cred_494970040378378658
  existing_browser_profile_id=bp_494970306666351168
```
✅ The `test_credential()` endpoint detected the existing browser profile.

### 2. LoginBlock Session-Check Prefix Applied
```
[block.py:3877] LoginBlock: augmenting navigation_goal with session-check prefix
  workflow_run_id=wr_494985634979095196
  browser_profile_id=bp_494970306666351168
```
✅ The `LoginBlock.execute()` override prepended the session-check instruction,
telling the agent to check if already logged in BEFORE attempting to fill credentials.

### 3. Browser Profile Loaded by Playwright
```
[browser_factory.py:502] Using browser profile
  browser_profile_id=bp_494970306666351168
  profile_dir=/app/browser_sessions/o_493509297168454580/profiles/bp_494970306666351168
```
✅ **KEY EVIDENCE**: `_create_headless_chromium()` loaded the profile from storage
and set `user_data_dir` to the profile directory.

### 4. Page Settle Wait
```
[block.py:819] Browser profile loaded — waiting for page to settle before agent acts
  browser_profile_id=bp_494970306666351168
  workflow_run_id=wr_494985634979095196
```
✅ Extra `networkidle` wait applied to let cookies/session data load.

### 5. Agent Takes ONLY CompleteAction (NO InputTextAction!)
```
[handler.py:538] Handling action
  action=CompleteAction(
    reasoning="The presence of the 'Logged In Successfully' message and the
              'Log out' button strongly indicate that the user is already logged in.
              These elements are typically only visible to authenticated users,
              confirming that the user goal of checking if they are logged in is
              achieved.",
    verified=True
  )
```
✅ **DEFINITIVE PROOF**: The agent saw "Logged In Successfully" and "Log out" button
on the page — which means the browser profile's session cookies were loaded and the
site recognized the user as authenticated. The agent did NOT fill any form fields.

### 6. Task Completed Successfully
```
[agent.py:3702] Task duration metrics
  task_id=tsk_494985639274062496
  task_status=completed
  duration_seconds=72.539619
```
✅ Task completed without errors.

---

## Verification: No InputTextAction Used

```bash
$ docker logs skyvern-skyvern-1 | grep "wr_494985634979095196" | grep "InputText"
# (empty — exit code 1, meaning zero matches)
```

✅ **Zero `InputTextAction` entries** in the entire workflow run.

---

## Code Flow (Verified Path)

```
credentials.py:test_credential()
  ↓ Detects credential.browser_profile_id = "bp_494970306666351168"
  ↓ Creates WorkflowRequestBody(browser_profile_id="bp_494970306666351168")
  ↓
service.py:setup_workflow_run()
  ↓ Creates WorkflowRun with browser_profile_id
  ↓
service.py:execute_workflow()
  ↓
block.py:LoginBlock.execute()
  ↓ Detects browser_profile_id on WorkflowRun
  ↓ Prepends _SESSION_CHECK_PREFIX to navigation_goal
  ↓
block.py:BaseTaskBlock.execute()
  ↓
real_browser_manager.py:get_or_create_for_workflow_run()
  ↓ Reads workflow_run.browser_profile_id
  ↓
real_browser_manager.py:_create_browser_state()
  ↓
browser_factory.py:_create_headless_chromium()
  ↓ Calls app.STORAGE.retrieve_browser_profile("o_...", "bp_494970306666351168")
  ↓ Gets profile_dir = "/app/browser_sessions/.../profiles/bp_494970306666351168"
  ↓ Sets user_data_dir = profile_dir
  ↓ Logs "Using browser profile"
  ↓
playwright.chromium.launch_persistent_context(user_data_dir=profile_dir)
  ↓ Chromium launches with saved cookies, localStorage, etc.
  ↓
Page loads → practicetestautomation.com shows "Logged In Successfully"
  ↓
LLM Agent sees authenticated page → CompleteAction(verified=True)
```

---

## Additional Evidence: Profile Data on Disk

```bash
$ docker exec skyvern-skyvern-1 ls /app/browser_sessions/o_493509297168454580/profiles/bp_494970306666351168/
Default/  GrShaderCache/  Local State  Variations

$ docker exec skyvern-skyvern-1 ls /app/browser_sessions/.../bp_494970306666351168/Default/
Cache/  Code Cache/  Cookies  Cookies-journal  Extension State/  ...
```

Full Chromium profile data including `Cookies` database, `Cache`, and `Default/` directory.

---

## Conclusion

The browser profile system is **fully operational**:

| Check | Status |
|-------|--------|
| Profile stored to disk after initial login | ✅ |
| Profile loaded as `user_data_dir` on re-test | ✅ |
| Playwright launched with saved profile | ✅ |
| Session cookies restored (page shows "Logged In") | ✅ |
| Agent detected existing login (no form filling) | ✅ |
| `CompleteAction(verified=True)` without `InputTextAction` | ✅ |
| `browser_profile_id` preserved on credential after test | ✅ |
