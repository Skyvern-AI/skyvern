import json
import os
from typing import Any, Optional, cast

import pytest
import requests
from dotenv import load_dotenv

from skyvern.forge import app
from skyvern.forge.sdk.schemas.tasks import TaskRequest

# Skip tests if network access is not available
pytest.skip("requires network access", allow_module_level=True)

# Load environment variables and set up configuration
load_dotenv("./skyvern-frontend/.env")
API_KEY = os.getenv("VITE_SKYVERN_API_KEY")

API_BASE_URL = "http://localhost:8000/api/v1"
HEADERS = {"x-api-key": API_KEY, "Content-Type": "application/json"}


def make_request(method: str, endpoint: str, data: Optional[dict[str, Any]] = None) -> requests.Response:
    """Helper function to make API requests"""
    url = f"{API_BASE_URL}{endpoint}"
    try:
        response = requests.request(method=method, url=url, headers=HEADERS, json=data)
        response.raise_for_status()
        return response
    except requests.exceptions.RequestException as e:
        print(f"\nRequest failed: {method} {url}")
        if hasattr(e, "response") and e.response is not None:
            print(f"Status code: {e.response.status_code}")
            try:
                error_detail = e.response.json() if e.response is not None else str(e)
                print(f"Error details: {json.dumps(error_detail, indent=2)}")
            except json.JSONDecodeError:
                print(f"Raw error response: {e.response.text}")
        else:
            print("Status code: N/A")
            print(f"Error details: {str(e)}")
        raise


def list_sessions() -> None:
    """List all active browser sessions"""
    try:
        response = make_request("GET", "/browser_sessions")
        sessions = response.json()
        print("\nActive browser sessions:")
        if not sessions:
            print("  No active sessions found")
            return
        for session in sessions:
            try:
                print(json.dumps(session, indent=2))
                print("  ---")
            except Exception as e:
                print(f"  Error parsing session data: {session}")
                print(f"  Error: {str(e)}")
    except Exception as e:
        print(f"Error listing sessions: {str(e)}")


def create_browser_session() -> Optional[str]:
    """Create a new browser session"""
    try:
        response = make_request("POST", "/browser_sessions")
        session = response.json()
        print("\nCreated new browser session:")
        try:
            print(f"  ID: {session.get('browser_session_id', 'N/A')}")
            print(f"  Status: {session.get('status', 'N/A')}")
            print(f"Full response: {json.dumps(session, indent=2)}")
            return session.get("browser_session_id")
        except Exception as e:
            print(f"Error parsing response: {session}")
            print(f"Error: {str(e)}")
            return None
    except Exception as e:
        print(f"Error creating session: {str(e)}")
        return None


def get_session(session_id: str) -> None:
    """Get details of a specific browser session"""
    try:
        response = make_request("GET", f"/browser_sessions/{session_id}")
        session = response.json()
        print("\nBrowser session details:")
        print(json.dumps(session, indent=2))
    except Exception as e:
        print(f"Error getting session: {str(e)}")


def close_all_sessions() -> None:
    """Close all active browser sessions"""
    try:
        response = make_request("POST", "/browser_sessions/close")
        print("\nClosed all browser sessions")
        print(f"Response: {response.json()}")
    except Exception as e:
        print(f"Error closing sessions: {str(e)}")


async def direct_list_sessions(organization_id: str) -> None:
    """List sessions directly from PersistentSessionsManager"""
    try:
        manager = app.PERSISTENT_SESSIONS_MANAGER
        sessions = await manager.get_active_sessions(organization_id)
        print("\nActive browser sessions (direct):")
        if not sessions:
            print("  No active sessions found")
            return
        for session in sessions:
            print(json.dumps(session.model_dump(), indent=2))
            print("  ---")
    except Exception as e:
        print(f"Error listing sessions directly: {str(e)}")


def print_direct_help() -> None:
    """Print available direct commands"""
    print("\nAvailable direct commands:")
    print("  direct_list <org_id> - List all active browser sessions directly")
    print("  direct_network <session_id> - Get network info directly")
    print("  help_direct - Show this help message")


async def handle_direct_command(cmd: str, args: list[str]) -> None:
    """Handle direct method calls"""
    if cmd == "help_direct":
        print_direct_help()
    elif cmd == "direct_list":
        if not args:
            print("Error: organization_id required")
            return
        await direct_list_sessions(args[0])
    else:
        print(f"Unknown direct command: {cmd}")
        print("Type 'help_direct' for available direct commands")


def close_session(session_id: str) -> None:
    """Close a specific browser session"""
    try:
        response = make_request("POST", f"/browser_sessions/{session_id}/close")
        print("\nClosed browser session:")
        print(f"Response: {response.json()}")
    except Exception as e:
        print(f"Error closing session: {str(e)}")


def create_task(
    url: str | None = None,
    goal: str | None = None,
    browser_session_id: str | None = None,
) -> Optional[str]:
    """Create a new task

    Args:
        url: URL to navigate to (default: https://news.ycombinator.com)
        goal: Task goal/instructions (default: Extract top HN post)
        browser_session_id: Optional browser session ID to use
    """
    try:
        default_url = "https://news.ycombinator.com"
        default_goal = "Navigate to the Hacker News homepage and identify the top post. COMPLETE when the title and URL of the top post are extracted. Ensure that the top post is the first post listed on the page."
        data = TaskRequest(
            url=url or default_url,
            goal=goal or default_goal,
            browser_session_id=browser_session_id,
        )
        response = make_request("POST", "/tasks", data=data.model_dump())
        task = cast(dict[str, Any], response.json())
        print("\nCreated new task:")
        try:
            print(f"  ID: {task.get('task_id', 'N/A')}")
            print(f"Full response: {json.dumps(task, indent=2)}")
            return task.get("task_id")
        except Exception as e:
            print(f"Error parsing response: {task}")
            print(f"Error: {str(e)}")
            return None
    except Exception as e:
        print(f"Error creating task: {str(e)}")
        return None


def create_workflow_run(
    workflow_permanent_id: str = "wpid_346464432851787586",
    browser_session_id: str | None = None,
) -> Optional[str]:
    """Create a new workflow run

    Args:
        workflow_permanent_id: Workflow permanent ID (default: wpid_346464432851787586)
        browser_session_id: Optional browser session ID to use
    """
    try:
        data: dict[str, Any] = {
            "parameters": {},  # Add parameters if needed
            "browser_session_id": browser_session_id,
        }
        response = make_request("POST", f"/workflows/{workflow_permanent_id}/run", data=data)
        workflow_run = response.json()
        print("\nCreated new workflow run:")
        try:
            print(f"  Workflow Run ID: {workflow_run.get('workflow_run_id', 'N/A')}")
            print(f"  Workflow ID: {workflow_run.get('workflow_id', 'N/A')}")
            print(f"Full response: {json.dumps(workflow_run, indent=2)}")
            return workflow_run.get("workflow_run_id")
        except Exception as e:
            print(f"Error parsing response: {workflow_run}")
            print(f"Error: {str(e)}")
            return None
    except Exception as e:
        print(f"Error creating workflow run: {str(e)}")
        return None


def create_cruise(
    prompt: str | None = None,
    url: str | None = None,
    browser_session_id: str | None = None,
) -> Optional[str]:
    """Create a new observer cruise

    Args:
        prompt: Task prompt/instructions (default: Extract top HN post)
        url: URL to navigate to (default: None)
        browser_session_id: Optional browser session ID to use
    """
    try:
        default_prompt = "Navigate to the Hacker News homepage and identify the top post. COMPLETE when the title and URL of the top post are extracted. Ensure that the top post is the first post listed on the page."
        data = {"user_prompt": prompt or default_prompt, "url": url, "browser_session_id": browser_session_id}
        response = make_request("POST", "/cruise", data=data)
        cruise = response.json()
        print("\nCreated new observer cruise:")
        try:
            print(f"  Cruise ID: {cruise.get('observer_cruise_id', 'N/A')}")
            print(f"  URL: {cruise.get('url', 'N/A')}")
            print(f"Full response: {json.dumps(cruise, indent=2)}")
            return cruise.get("observer_cruise_id")
        except Exception as e:
            print(f"Error parsing response: {cruise}")
            print(f"Error: {str(e)}")
            return None
    except Exception as e:
        print(f"Error creating cruise: {str(e)}")
        return None


def print_help() -> None:
    """Print available commands"""
    print("\nHTTP API Commands:")
    print("  list_sessions - List all active browser sessions")
    print("  create_browser_session - Create a new browser session")
    print("  get_session <session_id> - Get details of a specific session")
    print("  close_session <session_id> - Close a specific session")
    print("  close_all_sessions - Close all active browser sessions")
    print("  create_task [args] - Create a new task")
    print("    Optional args:")
    print("      --url <url> - URL to navigate to")
    print("      --goal <goal> - Task goal/instructions")
    print("      --browser_session_id <id> - Browser session ID to use")
    print("  create_workflow_run [args] - Create a new workflow run")
    print("    Optional args:")
    print("      --workflow_id <id> - Workflow permanent ID")
    print("      --browser_session_id <id> - Browser session ID to use")
    print("  create_cruise [args] - Create a new observer cruise")
    print("    Optional args:")
    print("      --prompt <prompt> - Task prompt/instructions")
    print("      --url <url> - URL to navigate to")
    print("      --browser_session_id <id> - Browser session ID to use")
    print("  help - Show this help message")
    print("\nDirect Method Commands:")
    print("  direct_list <org_id> - List sessions directly")
    print("  direct_network <session_id> - Get network info directly")
    print("  help_direct - Show direct command help")
    print("\nOther Commands:")
    print("  exit - Exit the program")


async def main() -> None:
    print("Browser Sessions Testing CLI")
    print("Type 'help' for available commands")

    while True:
        try:
            command = input("\n> ").strip()

            if command == "":
                continue

            parts = command.split()
            cmd = parts[0]
            args = parts[1:]

            if cmd == "exit":
                break
            elif cmd.startswith("direct_") or cmd == "help_direct":
                await handle_direct_command(cmd, args)
            elif cmd == "help":
                print_help()
            elif cmd == "list_sessions":
                list_sessions()
            elif cmd == "create_browser_session":
                create_browser_session()
            elif cmd == "create_task":
                # Parse optional args
                url = None
                goal = None
                browser_session_id = None
                i = 0
                while i < len(args):
                    if args[i] == "--url" and i + 1 < len(args):
                        url = args[i + 1]
                        i += 2
                    elif args[i] == "--goal" and i + 1 < len(args):
                        goal = args[i + 1]
                        i += 2
                    elif args[i] == "--browser_session_id" and i + 1 < len(args):
                        browser_session_id = args[i + 1]
                        i += 2
                    else:
                        i += 1
                create_task(url=url, goal=goal, browser_session_id=browser_session_id)
            elif cmd == "get_session":
                if not args:
                    print("Error: session_id required")
                    continue
                get_session(args[0])
            elif cmd == "close_session":
                if not args:
                    print("Error: session_id required")
                    continue
                close_session(args[0])
            elif cmd == "close_all_sessions":
                close_all_sessions()
            elif cmd == "create_workflow_run":
                # Parse optional args
                workflow_id = "wpid_346464432851787586"  # Default workflow ID
                browser_session_id = None
                i = 0
                while i < len(args):
                    if args[i] == "--workflow_id" and i + 1 < len(args):
                        workflow_id = args[i + 1]
                        i += 2
                    elif args[i] == "--browser_session_id" and i + 1 < len(args):
                        browser_session_id = args[i + 1]
                        i += 2
                    else:
                        i += 1
                create_workflow_run(workflow_permanent_id=workflow_id, browser_session_id=browser_session_id)
            elif cmd == "create_cruise":
                # Parse optional args
                prompt = None
                url = None
                browser_session_id = None
                i = 0
                while i < len(args):
                    if args[i] == "--prompt" and i + 1 < len(args):
                        prompt = args[i + 1]
                        i += 2
                    elif args[i] == "--url" and i + 1 < len(args):
                        url = args[i + 1]
                        i += 2
                    elif args[i] == "--browser_session_id" and i + 1 < len(args):
                        browser_session_id = args[i + 1]
                        i += 2
                    else:
                        i += 1
                create_cruise(prompt=prompt, url=url, browser_session_id=browser_session_id)
            else:
                print(f"Unknown command: {cmd}")
                print("Type 'help' for available commands")

        except KeyboardInterrupt:
            print("\nUse 'exit' to quit")
        except Exception as e:
            print(f"Error: {str(e)}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
