import os
import requests
from typing import Any, Optional
from dotenv import load_dotenv
import json

load_dotenv("./skyvern-frontend/.env")
API_KEY = os.getenv("VITE_SKYVERN_API_KEY")

API_BASE_URL = "http://localhost:8000/api/v1"  # Adjust if needed
HEADERS = {
    "x-api-key": API_KEY,
    "Content-Type": "application/json"
}

def make_request(
    method: str,
    endpoint: str,
    data: Optional[dict[str, Any]] = None
) -> requests.Response:
    """Helper function to make API requests"""
    url = f"{API_BASE_URL}{endpoint}"
    try:
        response = requests.request(
            method=method,
            url=url,
            headers=HEADERS,
            json=data
        )
        response.raise_for_status()
        return response
    except requests.exceptions.RequestException as e:
        print(f"\nRequest failed: {method} {url}")
        print(f"Status code: {e.response.status_code if hasattr(e, 'response') else 'N/A'}")
        try:
            error_detail = e.response.json() if hasattr(e, 'response') else str(e)
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
            return session.get('browser_session_id')
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

def print_help():
    """Print available commands"""
    print("\nAvailable commands:")
    print("  list - List all active browser sessions")
    print("  create - Create a new browser session")
    print("  get <session_id> - Get details of a specific session")
    print("  close_all - Close all active browser sessions")
    print("  help - Show this help message")
    print("  exit - Exit the program")

def main():
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
    main()
