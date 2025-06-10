#!/usr/bin/env python3
"""
Comprehensive test suite for all UI-TARS action types.
Tests conversion from UI-TARS responses to valid Skyvern actions.
"""

import asyncio
import sys
import uuid
from io import BytesIO
from unittest.mock import Mock, patch

from PIL import Image

# Add the project root to the path so we can import Skyvern modules
sys.path.append('.')

from skyvern.forge.sdk.api.llm.ui_tars_client import UITarsClient
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.webeye.actions.actions import (
    ClickAction,
    CompleteAction,
    DragAction,
    InputTextAction,
    KeypressAction,
    ScrollAction,
    WaitAction,
)
from skyvern.webeye.scraper.scraper import ScrapedPage


def create_mock_task():
    """Create a mock task for testing."""
    from datetime import datetime
    
    return Task(
        task_id=str(uuid.uuid4()),
        organization_id=str(uuid.uuid4()),
        workflow_run_id=str(uuid.uuid4()),
        url="https://example.com",
        navigation_goal="Test UI-TARS actions",
        data_extraction_goal="Extract test data",
        navigation_payload={},
        status=TaskStatus.running,
        created_at=datetime.utcnow(),
        modified_at=datetime.utcnow()
    )


def create_mock_step():
    """Create a mock step for testing."""
    from datetime import datetime
    
    return Step(
        step_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        order=1,
        retry_index=0,
        status=StepStatus.running,
        organization_id=str(uuid.uuid4()),
        is_last=False,
        created_at=datetime.utcnow(),
        modified_at=datetime.utcnow()
    )


def create_mock_scraped_page():
    """Create a mock scraped page with a test screenshot."""
    # Create a simple test image (800x600 pixels)
    image = Image.new('RGB', (800, 600), color='white')
    
    # Convert to bytes
    img_byte_arr = BytesIO()
    image.save(img_byte_arr, format='PNG')
    screenshot_bytes = img_byte_arr.getvalue()
    
    # Create a mock ScrapedPage using direct attribute assignment
    scraped_page = Mock(spec=ScrapedPage)
    scraped_page.url = "https://example.com"
    scraped_page.html = "<html><body>Test page</body></html>"
    scraped_page.screenshots = [screenshot_bytes]
    scraped_page.elements = []
    scraped_page.id_to_element_dict = {}
    scraped_page.id_to_css_dict = {}
    scraped_page.id_to_element_hash = {}
    scraped_page.hash_to_element_ids = {}
    scraped_page.element_tree = []
    scraped_page.element_tree_trimmed = []
    scraped_page.extracted_text = None
    
    return scraped_page


class UITarsActionTestSuite:
    """Test suite for all UI-TARS action types."""
    
    def __init__(self):
        self.ui_tars_client = UITarsClient(
            api_key="test-api-key",
            task_id=str(uuid.uuid4())
        )
        self.mock_task = create_mock_task()
        self.mock_step = create_mock_step()
        self.mock_scraped_page = create_mock_scraped_page()
        self.passed_tests = 0
        self.total_tests = 0
    
    async def test_click_action(self):
        """Test click action conversion."""
        print("🧪 Testing Click Action...")
        self.total_tests += 1
        
        ui_tars_response = """Thought: I need to click on the login button to proceed with authentication.
Action: click(point='<point>450 320</point>')"""
        
        with patch.object(self.ui_tars_client, '_call_api', return_value=ui_tars_response):
            actions = await self.ui_tars_client.generate_actions(self.mock_task, self.mock_step, self.mock_scraped_page)
            
            assert len(actions) == 1, f"Expected 1 action, got {len(actions)}"
            action = actions[0]
            assert isinstance(action, ClickAction), f"Expected ClickAction, got {type(action)}"
            assert action.button == "left"
            assert action.repeat == 1
            assert "login button" in action.reasoning.lower()
            
        print("✅ Click action test passed")
        self.passed_tests += 1
    
    async def test_left_double_action(self):
        """Test left double click action conversion."""
        print("🧪 Testing Left Double Click Action...")
        self.total_tests += 1
        
        ui_tars_response = """Thought: I need to double-click on the file icon to open it.
Action: left_double(point='<point>200 150</point>')"""
        
        with patch.object(self.ui_tars_client, '_call_api', return_value=ui_tars_response):
            actions = await self.ui_tars_client.generate_actions(self.mock_task, self.mock_step, self.mock_scraped_page)
            
            assert len(actions) == 1, f"Expected 1 action, got {len(actions)}"
            action = actions[0]
            assert isinstance(action, ClickAction), f"Expected ClickAction, got {type(action)}"
            assert action.button == "left"
            assert action.repeat == 2
            assert "double-click" in action.reasoning.lower()
            
        print("✅ Left double click action test passed")
        self.passed_tests += 1
    
    async def test_right_single_action(self):
        """Test right single click action conversion."""
        print("🧪 Testing Right Single Click Action...")
        self.total_tests += 1
        
        ui_tars_response = """Thought: I need to right-click on the text to open the context menu.
Action: right_single(point='<point>400 300</point>')"""
        
        with patch.object(self.ui_tars_client, '_call_api', return_value=ui_tars_response):
            actions = await self.ui_tars_client.generate_actions(self.mock_task, self.mock_step, self.mock_scraped_page)
            
            assert len(actions) == 1, f"Expected 1 action, got {len(actions)}"
            action = actions[0]
            assert isinstance(action, ClickAction), f"Expected ClickAction, got {type(action)}"
            assert action.button == "right"
            assert "right-click" in action.reasoning.lower()
            
        print("✅ Right single click action test passed")
        self.passed_tests += 1
    
    async def test_drag_action(self):
        """Test drag action conversion."""
        print("🧪 Testing Drag Action...")
        self.total_tests += 1
        
        ui_tars_response = """Thought: I need to drag the file from the source folder to the destination folder.
Action: drag(start_point='<point>300 200</point>', end_point='<point>500 400</point>')"""
        
        with patch.object(self.ui_tars_client, '_call_api', return_value=ui_tars_response):
            actions = await self.ui_tars_client.generate_actions(self.mock_task, self.mock_step, self.mock_scraped_page)
            
            assert len(actions) == 1, f"Expected 1 action, got {len(actions)}"
            action = actions[0]
            assert isinstance(action, DragAction), f"Expected DragAction, got {type(action)}"
            assert hasattr(action, 'start_x') and hasattr(action, 'start_y')
            assert hasattr(action, 'path') and len(action.path) > 0
            assert "drag" in action.reasoning.lower()
            
        print("✅ Drag action test passed")
        self.passed_tests += 1
    
    async def test_type_action(self):
        """Test type action conversion."""
        print("🧪 Testing Type Action...")
        self.total_tests += 1
        
        ui_tars_response = """Thought: I need to enter my username in the login field.
Action: type(content='john.doe@example.com')"""
        
        with patch.object(self.ui_tars_client, '_call_api', return_value=ui_tars_response):
            actions = await self.ui_tars_client.generate_actions(self.mock_task, self.mock_step, self.mock_scraped_page)
            
            assert len(actions) == 1, f"Expected 1 action, got {len(actions)}"
            action = actions[0]
            assert isinstance(action, InputTextAction), f"Expected InputTextAction, got {type(action)}"
            assert action.text == "john.doe@example.com"
            assert "username" in action.reasoning.lower()
            
        print("✅ Type action test passed")
        self.passed_tests += 1
    
    async def test_type_action_with_special_chars(self):
        """Test type action with special characters and newline."""
        print("🧪 Testing Type Action with Special Characters...")
        self.total_tests += 1
        
        ui_tars_response = """Thought: I need to enter a password and submit the form.
Action: type(content='MyP@ssw0rd123\\n')"""
        
        with patch.object(self.ui_tars_client, '_call_api', return_value=ui_tars_response):
            actions = await self.ui_tars_client.generate_actions(self.mock_task, self.mock_step, self.mock_scraped_page)
            
            assert len(actions) == 1, f"Expected 1 action, got {len(actions)}"
            action = actions[0]
            assert isinstance(action, InputTextAction), f"Expected InputTextAction, got {type(action)}"
            assert "MyP@ssw0rd123" in action.text
            assert "password" in action.reasoning.lower()
            
        print("✅ Type action with special characters test passed")
        self.passed_tests += 1
    
    async def test_hotkey_action(self):
        """Test hotkey action conversion."""
        print("🧪 Testing Hotkey Action...")
        self.total_tests += 1
        
        ui_tars_response = """Thought: I need to copy the selected text using the keyboard shortcut.
Action: hotkey(key='ctrl c')"""
        
        with patch.object(self.ui_tars_client, '_call_api', return_value=ui_tars_response):
            actions = await self.ui_tars_client.generate_actions(self.mock_task, self.mock_step, self.mock_scraped_page)
            
            assert len(actions) == 1, f"Expected 1 action, got {len(actions)}"
            action = actions[0]
            assert isinstance(action, KeypressAction), f"Expected KeypressAction, got {type(action)}"
            assert action.keys == ["ctrl", "c"]
            assert "copy" in action.reasoning.lower()
            
        print("✅ Hotkey action test passed")
        self.passed_tests += 1
    
    async def test_hotkey_action_complex(self):
        """Test complex hotkey action conversion."""
        print("🧪 Testing Complex Hotkey Action...")
        self.total_tests += 1
        
        ui_tars_response = """Thought: I need to select all text using Ctrl+Shift+A.
Action: hotkey(key='ctrl shift a')"""
        
        with patch.object(self.ui_tars_client, '_call_api', return_value=ui_tars_response):
            actions = await self.ui_tars_client.generate_actions(self.mock_task, self.mock_step, self.mock_scraped_page)
            
            assert len(actions) == 1, f"Expected 1 action, got {len(actions)}"
            action = actions[0]
            assert isinstance(action, KeypressAction), f"Expected KeypressAction, got {type(action)}"
            assert action.keys == ["ctrl", "shift", "a"]
            assert "select" in action.reasoning.lower()
            
        print("✅ Complex hotkey action test passed")
        self.passed_tests += 1
    
    async def test_scroll_down_action(self):
        """Test scroll down action conversion."""
        print("🧪 Testing Scroll Down Action...")
        self.total_tests += 1
        
        ui_tars_response = """Thought: I need to scroll down to see more content on the page.
Action: scroll(point='<point>640 360</point>', direction='down')"""
        
        with patch.object(self.ui_tars_client, '_call_api', return_value=ui_tars_response):
            actions = await self.ui_tars_client.generate_actions(self.mock_task, self.mock_step, self.mock_scraped_page)
            
            assert len(actions) == 1, f"Expected 1 action, got {len(actions)}"
            action = actions[0]
            assert isinstance(action, ScrollAction), f"Expected ScrollAction, got {type(action)}"
            assert action.scroll_x == 0
            assert action.scroll_y > 0  # Positive for down
            assert "scroll down" in action.reasoning.lower()
            
        print("✅ Scroll down action test passed")
        self.passed_tests += 1
    
    async def test_scroll_up_action(self):
        """Test scroll up action conversion."""
        print("🧪 Testing Scroll Up Action...")
        self.total_tests += 1
        
        ui_tars_response = """Thought: I need to scroll up to see the previous content.
Action: scroll(point='<point>640 360</point>', direction='up')"""
        
        with patch.object(self.ui_tars_client, '_call_api', return_value=ui_tars_response):
            actions = await self.ui_tars_client.generate_actions(self.mock_task, self.mock_step, self.mock_scraped_page)
            
            assert len(actions) == 1, f"Expected 1 action, got {len(actions)}"
            action = actions[0]
            assert isinstance(action, ScrollAction), f"Expected ScrollAction, got {type(action)}"
            assert action.scroll_x == 0
            assert action.scroll_y < 0  # Negative for up
            assert "scroll up" in action.reasoning.lower()
            
        print("✅ Scroll up action test passed")
        self.passed_tests += 1
    
    async def test_scroll_right_action(self):
        """Test scroll right action conversion."""
        print("🧪 Testing Scroll Right Action...")
        self.total_tests += 1
        
        ui_tars_response = """Thought: I need to scroll right to see more columns in the spreadsheet.
Action: scroll(point='<point>640 360</point>', direction='right')"""
        
        with patch.object(self.ui_tars_client, '_call_api', return_value=ui_tars_response):
            actions = await self.ui_tars_client.generate_actions(self.mock_task, self.mock_step, self.mock_scraped_page)
            
            assert len(actions) == 1, f"Expected 1 action, got {len(actions)}"
            action = actions[0]
            assert isinstance(action, ScrollAction), f"Expected ScrollAction, got {type(action)}"
            assert action.scroll_x > 0  # Positive for right
            assert action.scroll_y == 0
            assert "scroll right" in action.reasoning.lower()
            
        print("✅ Scroll right action test passed")
        self.passed_tests += 1
    
    async def test_scroll_left_action(self):
        """Test scroll left action conversion."""
        print("🧪 Testing Scroll Left Action...")
        self.total_tests += 1
        
        ui_tars_response = """Thought: I need to scroll left to see the beginning columns.
Action: scroll(point='<point>640 360</point>', direction='left')"""
        
        with patch.object(self.ui_tars_client, '_call_api', return_value=ui_tars_response):
            actions = await self.ui_tars_client.generate_actions(self.mock_task, self.mock_step, self.mock_scraped_page)
            
            assert len(actions) == 1, f"Expected 1 action, got {len(actions)}"
            action = actions[0]
            assert isinstance(action, ScrollAction), f"Expected ScrollAction, got {type(action)}"
            assert action.scroll_x < 0  # Negative for left
            assert action.scroll_y == 0
            assert "scroll left" in action.reasoning.lower()
            
        print("✅ Scroll left action test passed")
        self.passed_tests += 1
    
    async def test_wait_action(self):
        """Test wait action conversion."""
        print("🧪 Testing Wait Action...")
        self.total_tests += 1
        
        ui_tars_response = """Thought: The page is loading and I need to wait for it to complete before taking the next action.
Action: wait()"""
        
        with patch.object(self.ui_tars_client, '_call_api', return_value=ui_tars_response):
            actions = await self.ui_tars_client.generate_actions(self.mock_task, self.mock_step, self.mock_scraped_page)
            
            assert len(actions) == 1, f"Expected 1 action, got {len(actions)}"
            action = actions[0]
            assert isinstance(action, WaitAction), f"Expected WaitAction, got {type(action)}"
            assert action.seconds == 5  # Default wait time
            assert "wait" in action.reasoning.lower()
            
        print("✅ Wait action test passed")
        self.passed_tests += 1
    
    async def test_finished_action(self):
        """Test finished action conversion."""
        print("🧪 Testing Finished Action...")
        self.total_tests += 1
        
        ui_tars_response = """Thought: I have successfully completed the login process and reached the dashboard.
Action: finished(content='Successfully logged in and navigated to the user dashboard')"""
        
        with patch.object(self.ui_tars_client, '_call_api', return_value=ui_tars_response):
            actions = await self.ui_tars_client.generate_actions(self.mock_task, self.mock_step, self.mock_scraped_page)
            
            assert len(actions) == 1, f"Expected 1 action, got {len(actions)}"
            action = actions[0]
            assert isinstance(action, CompleteAction), f"Expected CompleteAction, got {type(action)}"
            assert "completed" in action.reasoning.lower()
            
        print("✅ Finished action test passed")
        self.passed_tests += 1
    
    async def test_finished_action_with_special_chars(self):
        """Test finished action with special characters."""
        print("🧪 Testing Finished Action with Special Characters...")
        self.total_tests += 1
        
        ui_tars_response = """Thought: I have extracted all the required data from the form.
Action: finished(content='Data extracted: Name: \\'John Doe\\', Email: \\'john@example.com\\', Status: \\'Active\\'')"""
        
        with patch.object(self.ui_tars_client, '_call_api', return_value=ui_tars_response):
            actions = await self.ui_tars_client.generate_actions(self.mock_task, self.mock_step, self.mock_scraped_page)
            
            assert len(actions) == 1, f"Expected 1 action, got {len(actions)}"
            action = actions[0]
            assert isinstance(action, CompleteAction), f"Expected CompleteAction, got {type(action)}"
            assert "extracted" in action.reasoning.lower()
            
        print("✅ Finished action with special characters test passed")
        self.passed_tests += 1
    
    async def run_all_tests(self):
        """Run all test cases."""
        print("🚀 Starting UI-TARS Action Test Suite...")
        print("=" * 60)
        
        test_methods = [
            self.test_click_action,
            self.test_left_double_action,
            self.test_right_single_action,
            self.test_drag_action,
            self.test_type_action,
            self.test_type_action_with_special_chars,
            self.test_hotkey_action,
            self.test_hotkey_action_complex,
            self.test_scroll_down_action,
            self.test_scroll_up_action,
            self.test_scroll_right_action,
            self.test_scroll_left_action,
            self.test_wait_action,
            self.test_finished_action,
            self.test_finished_action_with_special_chars,
        ]
        
        for test_method in test_methods:
            try:
                await test_method()
            except Exception as e:
                print(f"❌ {test_method.__name__} failed: {e}")
                import traceback
                traceback.print_exc()
        
        print("=" * 60)
        print(f"🎯 Test Results: {self.passed_tests}/{self.total_tests} tests passed")
        
        if self.passed_tests == self.total_tests:
            print("🎉 All UI-TARS action types successfully convert to valid Skyvern actions!")
            return True
        else:
            print(f"⚠️  {self.total_tests - self.passed_tests} tests failed")
            return False


async def main():
    """Main test runner."""
    test_suite = UITarsActionTestSuite()
    success = await test_suite.run_all_tests()
    
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main()) 