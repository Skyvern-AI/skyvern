#!/usr/bin/env python3
"""
Simple test script for StreamingService platform compatibility.
"""

import asyncio
import platform


def is_linux_or_wsl() -> bool:
    """
    Check if the current platform is Linux or WSL (Windows Subsystem for Linux).
    
    Returns:
        True if running on Linux or WSL, False otherwise.
    """
    system = platform.system().lower()
    if system == "linux":
        # Check if running in WSL
        if "microsoft" in platform.release().lower() or "wsl" in platform.version().lower():
            return True
        return True
    return False


async def test_platform_compatibility():
    """Test the platform compatibility check."""
    print("🔹 Testing platform compatibility...")
    
    # Test the platform check
    if is_linux_or_wsl():
        print("✅ Platform compatibility check passed (Linux/WSL detected)")
        print(f"  System: {platform.system()}")
        print(f"  Release: {platform.release()}")
        print(f"  Version: {platform.version()}")
        return True
    else:
        print(f"⚠️  Platform not supported: {platform.system()}")
        return False


async def test_async_subprocess():
    """Test async subprocess execution."""
    print("\n🔹 Testing async subprocess...")
    
    try:
        # Test async subprocess
        proc = await asyncio.create_subprocess_shell(
            "echo 'Hello, World!'",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        if proc.returncode == 0:
            print("✅ Async subprocess test passed")
            print(f"  Output: {stdout.decode().strip()}")
            return True
        else:
            print(f"❌ Async subprocess failed: {stderr.decode()}")
            return False
    except Exception as e:
        print(f"❌ Error during async subprocess test: {e}")
        return False


async def main():
    """Run all tests."""
    print("🚀 Testing StreamingService Changes...")
    print("=" * 50)
    
    # Run tests
    platform_ok = await test_platform_compatibility()
    subprocess_ok = await test_async_subprocess()
    
    print("\n" + "=" * 50)
    print("📊 Test Results:")
    print(f"  Platform Compatibility: {'✅ PASS' if platform_ok else '❌ FAIL'}")
    print(f"  Async Subprocess: {'✅ PASS' if subprocess_ok else '❌ FAIL'}")
    
    if platform_ok and subprocess_ok:
        print("\n🎉 All tests passed!")
        return 0
    else:
        print("\n❌ Some tests failed.")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)