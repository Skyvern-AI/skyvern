import json
import os
from typing import Any, Optional

import requests
from dotenv import load_dotenv

from skyvern.forge import app

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
        print(f"Status code: {e.response.status_code if hasattr(e, 'response') else 'N/A'}")
        try:
            error_detail = e.response.json() if hasattr(e, "response") else str(e)
            print(f"Error details: {json.dumps(error_detail, indent=2)}")
        except:
            print(f"Raw error response: {e.response.text if hasattr(e, 'response') else str(e)}")
        raise


def list_sessions():
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
                # print all fields
                print(json.dumps(session, indent=2))
                print("  ---")
            except Exception as e:
                print(f"  Error parsing session data: {session}")
                print(f"  Error: {str(e)}")
    except Exception as e:
        print(f"Error listing sessions: {str(e)}")


def create_session():
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
    except Exception as e:
        print(f"Error creating session: {str(e)}")


def get_session(session_id: str):
    """Get details of a specific browser session"""
    try:
        response = make_request("GET", f"/browser_sessions/{session_id}")
        session = response.json()
        print("\nBrowser session details:")
        print(json.dumps(session, indent=2))
    except Exception as e:
        print(f"Error getting session: {str(e)}")


def close_all_sessions():
    """Close all active browser sessions"""
    try:
        response = make_request("POST", "/browser_sessions/close")
        print("\nClosed all browser sessions")
        print(f"Response: {response.json()}")
    except Exception as e:
        print(f"Error closing sessions: {str(e)}")


async def direct_get_network_info(session_id: str):
    """Get network info directly from PersistentSessionsManager"""
    try:
        manager = app.PERSISTENT_SESSIONS_MANAGER
        cdp_port, ip_address = await manager.get_network_info(session_id)
        print("\nNetwork info:")
        print(f"  CDP Port: {cdp_port}")
        print(f"  IP Address: {ip_address}")
    except Exception as e:
        print(f"Error getting network info: {str(e)}")


async def direct_list_sessions(organization_id: str):
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


def print_direct_help():
    """Print available direct commands"""
    print("\nAvailable direct commands:")
    print("  direct_list <org_id> - List all active browser sessions directly")
    print("  direct_network <session_id> - Get network info directly")
    print("  help_direct - Show this help message")


async def handle_direct_command(cmd: str, args: list[str]):
    """Handle direct method calls"""
    if cmd == "help_direct":
        print_direct_help()
    elif cmd == "direct_network":
        if not args:
            print("Error: session_id required")
            return
        await direct_get_network_info(args[0])
    elif cmd == "direct_list":
        if not args:
            print("Error: organization_id required")
            return
        await direct_list_sessions(args[0])
    else:
        print(f"Unknown direct command: {cmd}")
        print("Type 'help_direct' for available direct commands")


def print_help():
    """Print available commands"""
    print("\nHTTP API Commands:")
    print("  list - List all active browser sessions")
    print("  create - Create a new browser session")
    print("  get <session_id> - Get details of a specific session")
    print("  close_all - Close all active browser sessions")
    print("  help - Show this help message")
    print("\nDirect Method Commands:")
    print("  direct_list <org_id> - List sessions directly")
    print("  direct_network <session_id> - Get network info directly")
    print("  help_direct - Show direct command help")
    print("\nOther Commands:")
    print("  exit - Exit the program")


async def main():
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
            elif cmd == "list":
                list_sessions()
            elif cmd == "create":
                create_session()
            elif cmd == "get":
                if not args:
                    print("Error: session_id required")
                    continue
                get_session(args[0])
            elif cmd == "close_all":
                close_all_sessions()
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
