#!/usr/bin/env python3
"""
E2E test for browser profile persistence in OSS.

This test verifies that:
1. A persistent browser session can be created
2. Login state is preserved in the session
3. The session can be exported when closed
4. A browser profile can be created from the closed session
5. Workflows using the browser_profile_id load the saved state
6. Login state persists across workflow runs when using profiles

Usage:
    # Set required environment variables
    export SKYVERN_API_KEY="your_api_key"
    export BASE_URL="http://localhost:8000"  # Optional, defaults to localhost
    export HN_USERNAME="your_hackernews_username"
    export HN_PASSWORD="your_hackernews_password"

    # For OSS/self-hosted testing, ensure BROWSER_TYPE is set appropriately:
    # - chromium-headless (recommended for self-hosting, no UI)
    # - chromium-headful (useful for debugging, shows browser UI)
    # Do NOT use cdp-connect unless you have Chrome running with remote debugging
    # The server reads BROWSER_TYPE from .env file or environment variables

    # Run the test
    python tests/e2e/test_browser_profile_persistence.py
"""

import asyncio
import json
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("SKYVERN_API_KEY") or os.getenv("API_KEY")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
HN_USERNAME = os.getenv("HN_USERNAME", "test_user")
HN_PASSWORD = os.getenv("HN_PASSWORD", "test_password")

if not API_KEY:
    print("ERROR: SKYVERN_API_KEY or API_KEY not set in environment")
    sys.exit(1)


async def wait_for_run_completion(client: httpx.AsyncClient, run_id: str, headers: dict[str, str]) -> dict:
    """Poll run status until completion or failure."""
    max_wait = 180
    waited = 0
    while waited < max_wait:
        await asyncio.sleep(5)
        waited += 5
        resp = await client.get(f"{BASE_URL}/v1/runs/{run_id}", headers=headers)
        resp.raise_for_status()
        run_status = resp.json()
        status = run_status["status"]
        print(f"   Status: {status} (waited {waited}s)")
        if status in {"completed", "failed"}:
            return run_status
    raise RuntimeError(f"Run {run_id} did not finish within {max_wait}s")


async def create_profile_with_retry(
    client: httpx.AsyncClient,
    profile_data: dict,
    headers: dict[str, str],
    max_retries: int = 30,
    retry_delay: float = 1.0,
) -> dict:
    """
    Create a browser profile with retry logic to handle async session persistence.

    Session persistence happens asynchronously after workflow/session close, so we need
    to retry profile creation until the session is available.
    """
    for attempt in range(max_retries):
        resp = await client.post(f"{BASE_URL}/v1/browser_profiles", json=profile_data, headers=headers)

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 400:
            error_detail = resp.json().get("detail", "Unknown error")
            if (
                "no persisted session" in error_detail.lower()
                or "no persisted browser session" in error_detail.lower()
                or "does not have a persisted session" in error_detail.lower()
                or "does not have a persisted profile archive" in error_detail.lower()
            ):
                # Session not ready yet, retry
                if attempt < max_retries - 1:
                    print(f"   Session not persisted yet, retrying... (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    print(f"   Failed to create profile after {max_retries} attempts: {error_detail}")
                    raise RuntimeError(f"Session never became available: {error_detail}")
            else:
                resp.raise_for_status()
        else:
            resp.raise_for_status()

    resp.raise_for_status()
    return {}


async def get_block_output(client: httpx.AsyncClient, run_id: str, label: str, headers: dict[str, str]) -> dict | None:
    """Return the block output for a block label from the run timeline."""
    resp = await client.get(f"{BASE_URL}/v1/runs/{run_id}/timeline", headers=headers)
    resp.raise_for_status()
    timeline = resp.json()
    for item in timeline:
        if item.get("type") == "block":
            block = item.get("block")
            if block and block.get("label") == label:
                return block.get("output")
    return None


def extract_username(output: object) -> tuple[bool, str | None]:
    """Return whether the username is present (indicating successful login)."""
    if output is None:
        return False, None

    if isinstance(output, dict):
        extracted_info = output.get("extracted_information")
        if isinstance(extracted_info, dict):
            username = extracted_info.get("username")
            if username and username != "logged_out":
                return True, str(username)

        username = output.get("username")
        if username and username != "logged_out":
            return True, str(username)

        text = output.get("text", "")
        if isinstance(text, str) and HN_USERNAME.lower() in text.lower():
            return True, HN_USERNAME

    if isinstance(output, str):
        if HN_USERNAME.lower() in output.lower():
            return True, HN_USERNAME
        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict):
                extracted_info = parsed.get("extracted_information")
                if isinstance(extracted_info, dict):
                    username = extracted_info.get("username")
                    if username and username != "logged_out":
                        return True, str(username)
                username = parsed.get("username")
                if username and username != "logged_out":
                    return True, str(username)
        except json.JSONDecodeError:
            pass

    return False, None


async def test_browser_profile_e2e():
    """Run end-to-end test for browser profiles in OSS."""
    headers = {"x-api-key": API_KEY, "Content-Type": "application/json"}

    print("\n" + "=" * 80)
    print("BROWSER PROFILE END-TO-END TEST (OSS)")
    print("=" * 80)

    async with httpx.AsyncClient(timeout=300.0) as client:
        # Step 1: Create login workflow with persist_browser_session=True
        print("\n1. Creating Hacker News login workflow with persist_browser_session=True...")
        login_workflow_data = {
            "json_definition": {
                "title": "E2E Test - HN Login Profile Creator",
                "persist_browser_session": True,
                "workflow_definition": {
                    "parameters": [
                        {
                            "key": "username",
                            "parameter_type": "workflow",
                            "workflow_parameter_type": "string",
                            "description": "Hacker News username",
                            "default_value": HN_USERNAME,
                        },
                        {
                            "key": "password",
                            "parameter_type": "workflow",
                            "workflow_parameter_type": "string",
                            "description": "Hacker News password",
                            "default_value": HN_PASSWORD,
                        },
                    ],
                    "blocks": [
                        {
                            "block_type": "login",
                            "label": "HN Login",
                            "url": "https://news.ycombinator.com/login",
                            "navigation_goal": (
                                f"Log in to Hacker News with username '{HN_USERNAME}' and the provided password. "
                                "Look for the username field (with name 'acct') and password field. "
                                "After successful login, you should see your username displayed in the top navigation bar."
                            ),
                            "parameter_keys": ["username", "password"],
                            "cache_actions": False,
                            "max_steps_per_run": 10,
                            "complete_criterion": f"Username '{HN_USERNAME}' is visible in the page or logout link is present",
                            "engine": "skyvern-1.0",
                        }
                    ],
                },
            }
        }

        resp = await client.post(f"{BASE_URL}/v1/workflows", json=login_workflow_data, headers=headers)
        resp.raise_for_status()
        login_workflow = resp.json()
        login_workflow_permanent_id = login_workflow["workflow_permanent_id"]
        print(f"   Created login workflow: {login_workflow_permanent_id}")

        # Step 1b: Create verification workflow
        print("\n1b. Creating verification workflow that checks for logged-in state...")
        verify_workflow_data = {
            "json_definition": {
                "title": "E2E Test - HN Login Verifier",
                "persist_browser_session": False,
                "workflow_definition": {
                    "parameters": [],
                    "blocks": [
                        {
                            "block_type": "navigation",
                            "label": "Open HN Homepage",
                            "url": "https://news.ycombinator.com",
                            "navigation_goal": "Navigate to the Hacker News homepage.",
                            "cache_actions": False,
                            "engine": "skyvern-1.0",
                        },
                        {
                            "block_type": "extraction",
                            "label": "Extract Username",
                            "url": "https://news.ycombinator.com",
                            "data_extraction_goal": (
                                f"Extract the username from the page. If logged in, you should see '{HN_USERNAME}' "
                                "in the top navigation bar. Return an object with a 'username' property "
                                "containing the username if found, or 'logged_out' if not logged in."
                            ),
                            "data_schema": {
                                "type": "object",
                                "properties": {
                                    "username": {
                                        "type": "string",
                                        "description": "The logged-in username, or 'logged_out'",
                                    }
                                },
                            },
                            "engine": "skyvern-1.0",
                        },
                    ],
                },
            }
        }
        resp = await client.post(f"{BASE_URL}/v1/workflows", json=verify_workflow_data, headers=headers)
        resp.raise_for_status()
        verify_workflow = resp.json()
        verify_workflow_permanent_id = verify_workflow["workflow_permanent_id"]
        print(f"   Created verification workflow: {verify_workflow_permanent_id}")

        # Step 2: Run login workflow
        print("\n2. Running login workflow to establish authenticated session...")
        run_data = {
            "workflow_id": login_workflow_permanent_id,
            "proxy_location": "NONE",
            "parameter_values": {"username": HN_USERNAME, "password": HN_PASSWORD},
        }
        resp = await client.post(f"{BASE_URL}/v1/run/workflows", json=run_data, headers=headers)
        if resp.status_code != 200:
            error_detail = resp.text
            try:
                error_json = resp.json()
                error_detail = error_json.get("detail", error_detail)
            except Exception:
                pass
            print(f"   ERROR: Server returned {resp.status_code}")
            print(f"   Error detail: {error_detail}")
        resp.raise_for_status()
        run_response = resp.json()
        workflow_run_id = run_response["run_id"]
        print(f"   Started workflow run: {workflow_run_id}")

        # Step 3: Wait for workflow completion
        print("\n3. Waiting for workflow to complete...")
        run_status = await wait_for_run_completion(client, workflow_run_id, headers)
        if run_status["status"] != "completed":
            print(f"   Login workflow failed: {run_status.get('failure_reason')}")
            return False
        print("   Login workflow completed")

        # Step 4: Create browser profile from workflow run
        print("\n4. Creating browser profile from workflow run...")
        profile_name = f"E2E Test Profile {int(time.time())}"
        profile_data = {
            "name": profile_name,
            "description": "Created from workflow run test",
            "workflow_run_id": workflow_run_id,
        }
        profile = await create_profile_with_retry(client, profile_data, headers)
        profile_id = profile["browser_profile_id"]
        print(f"   Created profile: {profile_id}")
        print(f"   Name: {profile['name']}")

        # Step 5: Run verification workflow with browser profile
        print("\n5. Running verification workflow with browser profile (should detect logged-in state)...")
        run_data2 = {
            "workflow_id": verify_workflow_permanent_id,
            "browser_profile_id": profile_id,
            "proxy_location": "NONE",
        }
        resp = await client.post(f"{BASE_URL}/v1/run/workflows", json=run_data2, headers=headers)
        resp.raise_for_status()
        run_response2 = resp.json()
        workflow_run_id2 = run_response2["run_id"]
        print(f"   Started workflow run with profile: {workflow_run_id2}")
        run_status2 = await wait_for_run_completion(client, workflow_run_id2, headers)
        if run_status2["status"] != "completed":
            print(f"   Verification workflow with profile failed: {run_status2.get('failure_reason')}")
            return False
        output_with_profile = await get_block_output(client, workflow_run_id2, "Extract Username", headers)
        print(f"   Output with profile: {output_with_profile}")
        is_logged_in, username = extract_username(output_with_profile)
        if not is_logged_in or not username or username.lower() != HN_USERNAME.lower():
            print(
                f"   Expected username '{HN_USERNAME}' not found in profile-enabled run. "
                f"Found: {username}, Raw output: {output_with_profile}"
            )
            return False
        print(f"   Profile run detected logged-in username: {username}")

        # Step 6: Control run without profile
        print("\n6. Running verification workflow without profile (should not be logged in)...")
        resp = await client.post(
            f"{BASE_URL}/v1/run/workflows",
            json={"workflow_id": verify_workflow_permanent_id, "proxy_location": "NONE"},
            headers=headers,
        )
        resp.raise_for_status()
        run_response3 = resp.json()
        workflow_run_id3 = run_response3["run_id"]
        print(f"   Started control workflow run: {workflow_run_id3}")
        run_status3 = await wait_for_run_completion(client, workflow_run_id3, headers)
        if run_status3["status"] != "completed":
            print(f"   Control workflow did not complete (status={run_status3['status']})")
        output_without_profile = await get_block_output(client, workflow_run_id3, "Extract Username", headers)
        print(f"   Output without profile: {output_without_profile}")
        is_logged_in_control, username_control = extract_username(output_without_profile)
        if is_logged_in_control and username_control and username_control.lower() == HN_USERNAME.lower():
            print(f"   Control run unexpectedly detected login. Found username: {username_control}")
            return False
        print("   Control run did not detect logged-in state (as expected)")

        print("\n" + "=" * 80)
        print("ALL TESTS PASSED!")
        print("=" * 80)
        print(f"\nProfile ID: {profile_id}")
        print(f"Workflow Run 1 (login setup): {workflow_run_id}")
        print(f"Workflow Run 2 (with profile): {workflow_run_id2}")
        print(f"Workflow Run 3 (control, no profile): {workflow_run_id3}")
        print("\nBrowser profile reuse verified:")
        print("   Run 1 logged into Hacker News and saved the session")
        print(f"   Profile {profile_id} captured that authenticated session")
        print(f"   Run 2, using the profile, remained logged in with username '{username}'")
        print("   Run 3, without the profile, was not logged in (as expected)")

        return True


if __name__ == "__main__":
    try:
        success = asyncio.run(test_browser_profile_e2e())
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\nTEST FAILED WITH EXCEPTION: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
