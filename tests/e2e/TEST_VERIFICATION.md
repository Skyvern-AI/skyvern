# Test Verification: Browser Profile Persistence E2E Test

## ✅ Test Flow Matches Customer's Exact Flow

The test in `test_browser_profile_persistence.py` **exactly matches** what a self-hosting OSS customer would do:

### Customer's Flow:
1. ✅ Run login workflow with `persist_browser_session: true`
2. ✅ Wait for the run to complete
3. ✅ Create a browser profile from the `workflow_run_id`
4. ✅ Run another workflow using only the `browser_profile_id`

### Test Implementation:
- **Step 1** (lines 182-230): Creates login workflow with `persist_browser_session: True`
- **Step 2** (lines 274-302): Runs login workflow and waits for completion
- **Step 3** (lines 304-315): Creates browser profile from `workflow_run_id` with retry logic
- **Step 4** (lines 317-342): Runs verification workflow with `browser_profile_id` only

## Implementation Flow Verification

### Profile Creation Flow:
1. Workflow run completes → `persistent_sessions_manager.close_session()` exports `user_data_dir` to storage
2. Profile creation API (`/v1/browser_profiles`) copies session data to profile storage location
3. Profile is stored at: `profiles/{browser_profile_id}/` (local) or S3 (cloud)

### Profile Loading Flow:
1. Workflow run request includes `browser_profile_id` → stored in `workflow_run.browser_profile_id`
2. `RealBrowserManager.get_or_create_for_workflow_run()` called with `browser_profile_id` and `organization_id`
3. `_create_browser_state()` passes both to `BrowserContextFactory.create_browser_context()`
4. Browser factory checks for `browser_profile_id` and `organization_id` in kwargs
5. Calls `app.STORAGE.retrieve_browser_profile(organization_id, profile_id)` 
6. If found, uses returned directory as `user_data_dir` for `launch_persistent_context()`

## Potential Issues to Check

If the browser loads in a logged-out state despite the profile being created, check:

### 1. Profile Storage/Retrieval
- ✅ Verify profile is actually stored: Check logs for "Storing browser profile" 
- ✅ Verify profile is retrieved: Check logs for "Using browser profile" (not "Browser profile not found")
- ✅ Check storage path: For local storage, verify `{BROWSER_SESSION_BASE_PATH}/{org_id}/profiles/{profile_id}/` exists

### 2. User Data Directory Structure
- ✅ Verify the retrieved directory contains valid Chromium user data:
  - Should have `Default/` subdirectory
  - Should contain `Cookies`, `Local Storage`, `Session Storage` files
- ✅ Check if `update_chromium_browser_preferences()` is overwriting profile data

### 3. Browser Context Creation
- ✅ Verify `organization_id` is passed correctly (comes from `workflow_run.organization_id`)
- ✅ Verify `browser_profile_id` is passed correctly (comes from request or `workflow_run.browser_profile_id`)
- ✅ Check if `launch_persistent_context()` is actually using the `user_data_dir`

### 4. Profile Copy vs Reference
- ⚠️ **IMPORTANT**: The profile directory might be a temporary directory that gets cleaned up
- Check if `retrieve_browser_profile()` returns a temp directory that needs to persist
- Verify the profile data isn't being cleared between runs

### 5. Timing Issues
- ✅ Test already handles async persistence with retry logic (30 retries, 1s delay)
- Check if profile needs more time to be fully written to storage

## Debugging Steps

1. **Add logging** to verify profile is loaded:
   ```python
   # In browser_factory.py, after retrieving profile:
   LOG.info("Profile retrieved", profile_dir=profile_dir, files=os.listdir(profile_dir))
   ```

2. **Check server logs** when running workflow with profile:
   - Look for "Using browser profile" log message
   - Check if profile_dir path is correct
   - Verify organization_id matches

3. **Verify profile directory contents**:
   ```bash
   ls -la {BROWSER_SESSION_BASE_PATH}/{org_id}/profiles/{profile_id}/
   # Should see Default/ directory with cookies, localStorage, etc.
   ```

4. **Test profile loading directly**:
   - Manually retrieve profile using storage API
   - Verify directory structure matches what Playwright expects

## Test Accuracy

✅ **The test accurately mimics the self-hosting OSS customer flow**

The test uses:
- Same API endpoints (`/v1/workflows`, `/v1/run/workflows`, `/v1/browser_profiles`)
- Same request format (JSON with `browser_profile_id`)
- Same browser types (`chromium-headless` or `chromium-headful` for OSS)
- Same storage mechanism (local storage for OSS)

If the test passes but customer reports issues, the problem is likely:
1. Environment-specific (different storage paths, permissions)
2. Profile data corruption during storage/retrieval
3. Browser context not properly loading the user_data_dir
4. Profile directory structure mismatch
