# OSS Browser Profile Persistence

This document describes how browser profiles work in OSS Skyvern and the changes made to achieve parity with cloud behavior.

## Overview

Browser profiles allow you to capture and reuse browser state (cookies, localStorage, session data) across workflow runs. This is useful for:
- Persisting login sessions across multiple workflow executions
- Avoiding repeated authentication flows
- Maintaining user preferences and settings

## How Browser Profiles Work

### Creating a Profile

1. **Run a login workflow** with `persist_browser_session: true`
2. **Close the session** - the user_data_dir is automatically exported to storage
3. **Create a browser profile** from the workflow run via the `/v1/browser_profiles` API

### Using a Profile

When running a workflow with `browser_profile_id`:
1. The browser loads the saved user_data_dir from storage
2. Cookies, localStorage, and session data are restored
3. The browser starts in the authenticated state

## API Usage

### Create Profile from Workflow Run

```python
# After running a login workflow with persist_browser_session=True
response = client.post("/v1/browser_profiles", json={
    "name": "My Login Profile",
    "description": "Profile with authenticated session",
    "workflow_run_id": "<workflow_run_id>"
})
profile_id = response.json()["browser_profile_id"]
```

### Run Workflow with Profile

```python
response = client.post("/v1/run/workflows", json={
    "workflow_id": "<workflow_permanent_id>",
    "browser_profile_id": profile_id,
    "proxy_location": "NONE"
})
```

## Implementation Details

### Files Modified

| File | Change |
|------|--------|
| `skyvern/webeye/browser_factory.py` | Load profile from storage when `browser_profile_id` is set; track `user_data_dir` in browser_artifacts |
| `skyvern/webeye/persistent_sessions_manager.py` | Export user_data_dir to storage before closing browser sessions |
| `skyvern/forge/sdk/workflow/service.py` | Skip workflow-scoped session save when using browser_profile_id |

### Storage Keys

- **Session profiles**: Stored at `profiles/{browser_session_id}/`
- **Browser profiles**: Stored at `profiles/{browser_profile_id}/`

When creating a profile from a session, the session data is copied to the profile's storage location.

### Profile Behavior

Profiles are **read-only snapshots**:
- The profile is loaded when the browser starts
- Changes made during the workflow run are NOT saved back to the profile
- To capture new state, create a new profile from the completed run

This design avoids concurrency issues when multiple workflows use the same profile.

## Timing Considerations

Session persistence happens asynchronously after workflow/session close. When creating a profile immediately after closing a session, the API will retry for up to 30 seconds waiting for the session data to be available.

If you encounter "no persisted session" errors:
1. Ensure the workflow had `persist_browser_session: true`
2. Wait a few seconds after closing the session before creating the profile
3. The profile creation API will automatically retry

## Testing

Run the E2E test to verify browser profile persistence:

```bash
# Set environment variables
export SKYVERN_API_KEY="your_api_key"
export HN_USERNAME="your_hackernews_username"
export HN_PASSWORD="your_hackernews_password"

# IMPORTANT: For OSS/self-hosted testing, ensure BROWSER_TYPE is set appropriately
# in your .env file or environment:
# - chromium-headless (recommended for self-hosting, no UI)
# - chromium-headful (useful for debugging, shows browser UI)
# Do NOT use cdp-connect unless you have Chrome running with remote debugging enabled
# 
# Check your server's .env file or set it before starting the server:
# export BROWSER_TYPE="chromium-headless"

# Run the test
python tests/e2e/test_browser_profile_persistence.py
```

The test verifies:
1. Login workflow saves session state
2. Profile is created from the session
3. Subsequent workflow with profile loads logged-in state
4. Control workflow without profile starts logged out

## Differences from Cloud

OSS uses the same storage interfaces (`store_browser_profile`, `retrieve_browser_profile`) as cloud, but with different infrastructure:

| Aspect | Cloud | OSS |
|--------|-------|-----|
| Browser orchestration | Separate `browser_controller/` microservice on ECS | In-process `RealBrowserManager` |
| Session export | `browser_activity.py` calls export before container terminates | `close_session()` calls export before browser closes |
| Profile loading | `special_browsers.py` with custom browser types | `browser_factory.py` with standard chromium types |

The behavior is equivalent - profiles are exported on shutdown and loaded on startup.
