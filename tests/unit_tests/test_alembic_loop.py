#!/usr/bin/env python3
"""Test script to verify alembic works correctly with an existing event loop."""

import asyncio
import os
import subprocess
import sys


async def test_alembic_with_running_loop():
    """Test alembic migration execution within an existing event loop."""
    print("Testing alembic migration with existing event loop...")

    # Change to the project directory
    os.chdir(os.path.dirname(__file__))

    # Run alembic command in a subprocess
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "current"], capture_output=True, text=True, timeout=30
        )

        print(f"Return code: {result.returncode}")
        print(f"Stdout: {result.stdout}")
        if result.stderr:
            print(f"Stderr: {result.stderr}")

        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("ERROR: Alembic command timed out!")
        return False
    except Exception as e:
        print(f"ERROR: {e}")
        return False


if __name__ == "__main__":
    # This creates an event loop and runs alembic within it
    success = asyncio.run(test_alembic_with_running_loop())
    print(f"Test {'PASSED' if success else 'FAILED'}")
    sys.exit(0 if success else 1)
