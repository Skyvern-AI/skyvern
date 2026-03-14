#!/usr/bin/env python3
"""
Test script for StreamingService.

This script tests the key functionality of the StreamingService:
- Platform compatibility check
- State file initialization
- Screenshot capture
"""

import asyncio
import os
import tempfile
from pathlib import Path

from skyvern.forge.forge_app_initializer import start_forge_app
from skyvern.services.streaming.service import StreamingService


async def test_platform_compatibility():
    """Test the platform compatibility check."""
    print("🔹 Testing platform compatibility...")
    
    # Initialize ForgeApp
    start_forge_app()
    
    service = StreamingService()
    
    try:
        # This should raise an error if not on Linux/WSL
        await service.start_monitoring()
        print("✅ Platform compatibility check passed (Linux/WSL detected)")
        return True
    except RuntimeError as e:
        if "Streaming service is only supported on Linux or WSL" in str(e):
            print(f"⚠️  Platform not supported: {e}")
            return False
        else:
            print(f"❌ Unexpected error: {e}")
            return False


async def test_state_file_initialization():
    """Test state file initialization."""
    print("\n🔹 Testing state file initialization...")
    
    # Initialize ForgeApp
    start_forge_app()
    
    service = StreamingService()
    
    try:
        # Mock the state file path for testing
        from skyvern.utils.files import get_skyvern_state_file_path
        state_file_path = get_skyvern_state_file_path()
        
        # Delete the state file if it exists
        if os.path.exists(state_file_path):
            os.remove(state_file_path)
            print(f"📝 Deleted existing state file: {state_file_path}")
        
        # Start monitoring (this should create the state file)
        await service.start_monitoring()
        
        # Check if the state file exists
        if os.path.exists(state_file_path):
            print(f"✅ State file created: {state_file_path}")
            return True
        else:
            print(f"❌ State file not found: {state_file_path}")
            return False
    except Exception as e:
        print(f"❌ Error during state file initialization: {e}")
        return False
    finally:
        # Stop monitoring
        await service.stop_monitoring()


async def test_screenshot_capture():
    """Test screenshot capture."""
    print("\n🔹 Testing screenshot capture...")
    
    # Initialize ForgeApp
    start_forge_app()
    
    service = StreamingService()
    
    try:
        # Create a temporary directory for screenshots
        temp_dir = tempfile.mkdtemp()
        screenshot_path = os.path.join(temp_dir, "test_screenshot.png")
        
        # Capture a screenshot
        success = await service._capture_screenshot(screenshot_path)
        
        if success:
            print(f"✅ Screenshot captured: {screenshot_path}")
            
            # Check if the file exists and has content
            if os.path.exists(screenshot_path) and os.path.getsize(screenshot_path) > 0:
                print(f"✅ Screenshot file exists and has content: {os.path.getsize(screenshot_path)} bytes")
                return True
            else:
                print(f"❌ Screenshot file missing or empty: {screenshot_path}")
                return False
        else:
            print("❌ Screenshot capture failed")
            return False
    except Exception as e:
        print(f"❌ Error during screenshot capture: {e}")
        return False
    finally:
        # Clean up
        if os.path.exists(screenshot_path):
            os.remove(screenshot_path)
        if os.path.exists(temp_dir):
            os.rmdir(temp_dir)


async def main():
    """Run all tests."""
    print("🚀 Testing StreamingService...")
    print("=" * 50)
    
    # Initialize ForgeApp
    start_forge_app()
    
    # Run tests
    platform_ok = await test_platform_compatibility()
    state_file_ok = await test_state_file_initialization()
    screenshot_ok = await test_screenshot_capture()
    
    print("\n" + "=" * 50)
    print("📊 Test Results:")
    print(f"  Platform Compatibility: {'✅ PASS' if platform_ok else '❌ FAIL'}")
    print(f"  State File Initialization: {'✅ PASS' if state_file_ok else '❌ FAIL'}")
    print(f"  Screenshot Capture: {'✅ PASS' if screenshot_ok else '❌ FAIL'}")
    
    if platform_ok and state_file_ok and screenshot_ok:
        print("\n🎉 All tests passed!")
        return 0
    else:
        print("\n❌ Some tests failed.")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)