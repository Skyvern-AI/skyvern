"""
Tests for workflow schema field name preservation (SKY-7434).

When a workflow is regenerated (e.g., after adding a new block), the LLM should
preserve field names for unchanged blocks to prevent schema mismatches with
cached block code.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest
from dotenv import load_dotenv

from skyvern.core.script_generations.generate_script import (
    ScriptBlockSource,
    _build_existing_field_assignments,
)
from skyvern.core.script_generations.generate_workflow_parameters import (
    generate_workflow_parameters_schema,
)
from tests.unit.force_stub_app import start_forge_stub_app

# Load environment variables for real LLM tests
load_dotenv()

# Check if real LLM tests should run (set RUN_LLM_TESTS=1 to enable)
SKIP_LLM_TESTS = os.environ.get("RUN_LLM_TESTS", "0") != "1"


class TestBuildExistingFieldAssignments:
    """Test the helper function that builds existing field assignments from cached blocks."""

    def test_returns_empty_dict_when_no_cached_blocks(self):
        """When there are no cached blocks, should return empty dict."""
        blocks = [
            {"block_type": "login", "label": "login_block", "task_id": "task_1"},
        ]
        actions_by_task = {
            "task_1": [
                {"action_type": "input_text", "text": "john", "intention": "Enter username"},
            ]
        }
        cached_blocks: dict[str, ScriptBlockSource] = {}
        updated_block_labels: set[str] = set()

        result = _build_existing_field_assignments(blocks, actions_by_task, cached_blocks, updated_block_labels)

        assert result == {}

    def test_returns_empty_dict_when_all_blocks_updated(self):
        """When all blocks are in updated_block_labels, should return empty dict."""
        blocks = [
            {"block_type": "login", "label": "login_block", "task_id": "task_1"},
        ]
        actions_by_task = {
            "task_1": [
                {"action_type": "input_text", "text": "john", "intention": "Enter username"},
            ]
        }
        cached_blocks = {
            "login_block": ScriptBlockSource(
                label="login_block",
                code="async def login_block(): ...",
                run_signature=None,
                workflow_run_id=None,
                workflow_run_block_id=None,
                input_fields=["username"],
            )
        }
        updated_block_labels = {"login_block"}  # Block is updated, should not preserve

        result = _build_existing_field_assignments(blocks, actions_by_task, cached_blocks, updated_block_labels)

        assert result == {}

    def test_preserves_field_names_for_unchanged_blocks(self):
        """Unchanged blocks with input_fields should have their field names preserved."""
        blocks = [
            {"block_type": "login", "label": "login_block", "task_id": "task_1"},
        ]
        actions_by_task = {
            "task_1": [
                {"action_type": "input_text", "text": "john", "intention": "Enter username"},
                {"action_type": "input_text", "text": "pass123", "intention": "Enter password"},
            ]
        }
        cached_blocks = {
            "login_block": ScriptBlockSource(
                label="login_block",
                code="async def login_block(): ...",
                run_signature=None,
                workflow_run_id=None,
                workflow_run_block_id=None,
                input_fields=["user_full_name", "user_password"],
            )
        }
        updated_block_labels: set[str] = set()  # No blocks updated

        result = _build_existing_field_assignments(blocks, actions_by_task, cached_blocks, updated_block_labels)

        # Action 1 -> user_full_name, Action 2 -> user_password
        assert result == {1: "user_full_name", 2: "user_password"}

    def test_preserves_fields_for_multiple_unchanged_blocks(self):
        """Multiple unchanged blocks should each have their fields preserved."""
        blocks = [
            {"block_type": "login", "label": "login_block", "task_id": "task_1"},
            {"block_type": "task", "label": "form_block", "task_id": "task_2"},
        ]
        actions_by_task = {
            "task_1": [
                {"action_type": "input_text", "text": "john", "intention": "Enter username"},
            ],
            "task_2": [
                {"action_type": "input_text", "text": "Acme Inc", "intention": "Enter company"},
            ],
        }
        cached_blocks = {
            "login_block": ScriptBlockSource(
                label="login_block",
                code="...",
                run_signature=None,
                workflow_run_id=None,
                workflow_run_block_id=None,
                input_fields=["username"],
            ),
            "form_block": ScriptBlockSource(
                label="form_block",
                code="...",
                run_signature=None,
                workflow_run_id=None,
                workflow_run_block_id=None,
                input_fields=["company_name"],
            ),
        }
        updated_block_labels: set[str] = set()

        result = _build_existing_field_assignments(blocks, actions_by_task, cached_blocks, updated_block_labels)

        # Action 1 (task_1) -> username, Action 2 (task_2) -> company_name
        assert result == {1: "username", 2: "company_name"}

    def test_mixed_updated_and_unchanged_blocks(self):
        """Only unchanged blocks should have their fields preserved."""
        blocks = [
            {"block_type": "login", "label": "login_block", "task_id": "task_1"},
            {"block_type": "task", "label": "new_block", "task_id": "task_2"},
        ]
        actions_by_task = {
            "task_1": [
                {"action_type": "input_text", "text": "john", "intention": "Enter username"},
            ],
            "task_2": [
                {"action_type": "input_text", "text": "new value", "intention": "Enter something"},
            ],
        }
        cached_blocks = {
            "login_block": ScriptBlockSource(
                label="login_block",
                code="...",
                run_signature=None,
                workflow_run_id=None,
                workflow_run_block_id=None,
                input_fields=["username"],
            ),
            # new_block is not in cached_blocks (it's new)
        }
        updated_block_labels: set[str] = set()

        result = _build_existing_field_assignments(blocks, actions_by_task, cached_blocks, updated_block_labels)

        # Only action 1 should be preserved, action 2 is from a new block
        assert result == {1: "username"}

    def test_skips_non_custom_field_actions(self):
        """Actions that aren't INPUT_TEXT, UPLOAD_FILE, or SELECT_OPTION should be skipped."""
        blocks = [
            {"block_type": "login", "label": "login_block", "task_id": "task_1"},
        ]
        actions_by_task = {
            "task_1": [
                {"action_type": "click", "intention": "Click button"},  # Not a custom field action
                {"action_type": "input_text", "text": "john", "intention": "Enter username"},
            ]
        }
        cached_blocks = {
            "login_block": ScriptBlockSource(
                label="login_block",
                code="...",
                run_signature=None,
                workflow_run_id=None,
                workflow_run_block_id=None,
                input_fields=["username"],  # Only one input field
            )
        }
        updated_block_labels: set[str] = set()

        result = _build_existing_field_assignments(blocks, actions_by_task, cached_blocks, updated_block_labels)

        # The click action is skipped, so input_text is action 1
        assert result == {1: "username"}


class TestGenerateWorkflowParametersSchemaWithExistingFields:
    """Test that the LLM receives existing field names when generating schema."""

    @pytest.fixture(autouse=True)
    def setup_stub_app(self):
        """Set up stub app for all tests in this class."""
        self.stub_app = start_forge_stub_app()

    @pytest.mark.asyncio
    async def test_llm_receives_existing_field_names_in_prompt(self):
        """The LLM should receive existing field names to preserve in the prompt."""
        actions_by_task = {
            "task_1": [
                {"action_type": "input_text", "text": "john", "intention": "Enter username", "action_id": "act_1"},
                {"action_type": "input_text", "text": "pass", "intention": "Enter password", "action_id": "act_2"},
            ],
            "task_2": [
                {"action_type": "input_text", "text": "new", "intention": "Enter new field", "action_id": "act_3"},
            ],
        }
        existing_field_assignments = {
            1: "preserved_username",
            2: "preserved_password",
            # Action 3 has no existing field - needs new name
        }

        # Mock the LLM response
        mock_llm_response = {
            "field_mappings": {
                "action_index_1": "preserved_username",
                "action_index_2": "preserved_password",
                "action_index_3": "new_field_name",
            },
            "schema_fields": {
                "preserved_username": {"type": "str", "description": "Username"},
                "preserved_password": {"type": "str", "description": "Password"},
                "new_field_name": {"type": "str", "description": "New field"},
            },
        }

        captured_prompt = {}

        async def mock_llm_handler(prompt, prompt_name):
            captured_prompt["prompt"] = prompt
            captured_prompt["prompt_name"] = prompt_name
            return mock_llm_response

        self.stub_app.SCRIPT_GENERATION_LLM_API_HANDLER = AsyncMock(side_effect=mock_llm_handler)

        schema_code, field_mappings = await generate_workflow_parameters_schema(
            actions_by_task, existing_field_assignments
        )

        # Verify the prompt contains the existing field names
        prompt = captured_prompt["prompt"]
        assert "preserved_username" in prompt
        assert "preserved_password" in prompt
        assert "MUST PRESERVE" in prompt or "EXISTING FIELD NAME" in prompt

        # Verify the returned field mappings include preserved names
        assert field_mappings["task_1:act_1"] == "preserved_username"
        assert field_mappings["task_1:act_2"] == "preserved_password"
        assert field_mappings["task_2:act_3"] == "new_field_name"

    @pytest.mark.asyncio
    async def test_no_existing_fields_works_normally(self):
        """When there are no existing fields, schema generation should work normally."""
        actions_by_task = {
            "task_1": [
                {"action_type": "input_text", "text": "john", "intention": "Enter username", "action_id": "act_1"},
            ],
        }
        existing_field_assignments: dict[int, str] = {}  # No existing fields

        mock_llm_response = {
            "field_mappings": {
                "action_index_1": "username",
            },
            "schema_fields": {
                "username": {"type": "str", "description": "Username field"},
            },
        }

        captured_prompt = {}

        async def mock_llm_handler(prompt, prompt_name):
            captured_prompt["prompt"] = prompt
            return mock_llm_response

        self.stub_app.SCRIPT_GENERATION_LLM_API_HANDLER = AsyncMock(side_effect=mock_llm_handler)

        schema_code, field_mappings = await generate_workflow_parameters_schema(
            actions_by_task, existing_field_assignments
        )

        # Should not contain preservation instructions when no existing fields
        prompt = captured_prompt["prompt"]
        # The CRITICAL rule only appears when has_existing_fields is True
        assert "CRITICAL" not in prompt

        # Should still return valid mappings
        assert field_mappings["task_1:act_1"] == "username"

    @pytest.mark.asyncio
    async def test_schema_code_includes_preserved_field_names(self):
        """The generated schema code should include the preserved field names."""
        actions_by_task = {
            "task_1": [
                {"action_type": "input_text", "text": "john", "intention": "Enter username", "action_id": "act_1"},
            ],
        }
        existing_field_assignments = {1: "user_full_name"}

        mock_llm_response = {
            "field_mappings": {
                "action_index_1": "user_full_name",
            },
            "schema_fields": {
                "user_full_name": {"type": "str", "description": "The user's full name"},
            },
        }

        async def mock_llm_handler(prompt, prompt_name):
            return mock_llm_response

        self.stub_app.SCRIPT_GENERATION_LLM_API_HANDLER = AsyncMock(side_effect=mock_llm_handler)

        schema_code, field_mappings = await generate_workflow_parameters_schema(
            actions_by_task, existing_field_assignments
        )

        # Schema code should include the preserved field name
        assert "user_full_name" in schema_code
        assert "str" in schema_code


class TestEndToEndFieldPreservation:
    """
    End-to-end test simulating the real scenario:
    1. Workflow has a login block with cached code using field names
    2. User adds a new block
    3. Schema is regenerated
    4. Login block's field names should be preserved
    """

    @pytest.fixture(autouse=True)
    def setup_stub_app(self):
        """Set up stub app for all tests in this class."""
        self.stub_app = start_forge_stub_app()

    @pytest.mark.asyncio
    async def test_adding_new_block_preserves_existing_block_field_names(self):
        """
        Simulates: User has workflow with login block, adds a new block.
        The login block's field names should be preserved in the regenerated schema.
        """
        # Existing blocks (login was already there)
        blocks = [
            {"block_type": "login", "label": "login_block", "task_id": "task_1"},
            {"block_type": "task", "label": "new_block", "task_id": "task_2"},  # Newly added
        ]

        # Actions from both blocks
        actions_by_task = {
            "task_1": [
                {
                    "action_type": "input_text",
                    "text": "john@example.com",
                    "intention": "Enter email",
                    "action_id": "act_1",
                },
                {"action_type": "input_text", "text": "secret123", "intention": "Enter password", "action_id": "act_2"},
            ],
            "task_2": [
                {
                    "action_type": "input_text",
                    "text": "Acme Inc",
                    "intention": "Enter company name",
                    "action_id": "act_3",
                },
            ],
        }

        # Cached blocks - login_block has existing field names
        cached_blocks = {
            "login_block": ScriptBlockSource(
                label="login_block",
                code="""
@skyvern.cached(cache_key='login_block')
async def login_block(page: SkyvernPage, context: RunContext):
    await page.fill(
        selector='xpath=//input[@id="email"]',
        value=context.parameters['user_email'],
    )
    await page.fill(
        selector='xpath=//input[@id="password"]',
        value=context.parameters['user_password'],
    )
""",
                run_signature="await skyvern.login(...)",
                workflow_run_id="wr_123",
                workflow_run_block_id="wrb_123",
                input_fields=["user_email", "user_password"],  # These must be preserved!
            ),
            # new_block is not in cached_blocks - it's brand new
        }

        # Only the new block is "updated" (actually new)
        updated_block_labels: set[str] = set()  # login_block is NOT updated

        # Step 1: Build existing field assignments
        existing_field_assignments = _build_existing_field_assignments(
            blocks, actions_by_task, cached_blocks, updated_block_labels
        )

        # Verify login block fields are identified for preservation
        assert existing_field_assignments == {
            1: "user_email",
            2: "user_password",
            # Action 3 has no existing field (new block)
        }

        # Step 2: Mock LLM that respects the preservation instructions
        mock_llm_response = {
            "field_mappings": {
                "action_index_1": "user_email",  # Preserved
                "action_index_2": "user_password",  # Preserved
                "action_index_3": "company_name",  # New field for new block
            },
            "schema_fields": {
                "user_email": {"type": "str", "description": "User's email address"},
                "user_password": {"type": "str", "description": "User's password"},
                "company_name": {"type": "str", "description": "Company name"},
            },
        }

        captured_prompt = {}

        async def mock_llm_handler(prompt, prompt_name):
            captured_prompt["prompt"] = prompt
            return mock_llm_response

        self.stub_app.SCRIPT_GENERATION_LLM_API_HANDLER = AsyncMock(side_effect=mock_llm_handler)

        schema_code, field_mappings = await generate_workflow_parameters_schema(
            actions_by_task, existing_field_assignments
        )

        # Verify the prompt contains preservation instructions
        prompt = captured_prompt["prompt"]
        assert "user_email" in prompt, "Prompt should contain existing field name 'user_email'"
        assert "user_password" in prompt, "Prompt should contain existing field name 'user_password'"
        assert "MUST PRESERVE" in prompt or "EXISTING FIELD NAME" in prompt

        # Verify field mappings preserve the original names
        assert field_mappings["task_1:act_1"] == "user_email", "Login block email field should be preserved"
        assert field_mappings["task_1:act_2"] == "user_password", "Login block password field should be preserved"
        assert field_mappings["task_2:act_3"] == "company_name", "New block should get new field name"

        # Verify schema code contains preserved field names
        assert "user_email" in schema_code
        assert "user_password" in schema_code
        assert "company_name" in schema_code

        # The cached login block code references context.parameters['user_email']
        # and context.parameters['user_password'], which now match the schema!
        cached_code = cached_blocks["login_block"].code
        assert "user_email" in cached_code
        assert "user_password" in cached_code


@pytest.mark.skipif(SKIP_LLM_TESTS, reason="Real LLM test - set RUN_LLM_TESTS=1 to enable")
class TestRealLLMFieldPreservation:
    """
    Integration tests that make actual LLM calls to verify field preservation.

    These tests require environment variables to be set (via .env file):
    - SCRIPT_GENERATION_LLM_KEY or SECONDARY_LLM_KEY
    - Appropriate API keys for the LLM provider

    Run these tests with:
        RUN_LLM_TESTS=1 pytest tests/unit/test_workflow_schema_field_preservation.py::TestRealLLMFieldPreservation -v -s

    Note: Skipped by default since they make real LLM calls (costs money).
    """

    @pytest.fixture(scope="class", autouse=True)
    def setup_real_app(self):
        """Set up the real Forge app for LLM calls."""
        from skyvern.forge.forge_app_initializer import start_forge_app

        start_forge_app()
        yield

    @pytest.mark.asyncio
    async def test_llm_preserves_existing_field_names(self):
        """
        Test that a real LLM preserves field names when instructed to.

        This test sends a prompt with existing field names marked as "MUST PRESERVE"
        and verifies the LLM returns those exact names in the response.
        """
        actions_by_task = {
            "task_1": [
                {
                    "action_type": "input_text",
                    "text": "john.doe@example.com",
                    "intention": "Enter the user's email address for login",
                    "action_id": "act_1",
                },
                {
                    "action_type": "input_text",
                    "text": "secretpassword123",
                    "intention": "Enter the user's password",
                    "action_id": "act_2",
                },
            ],
            "task_2": [
                {
                    "action_type": "input_text",
                    "text": "Acme Corporation",
                    "intention": "Enter the company name",
                    "action_id": "act_3",
                },
            ],
        }

        # These are the existing field names that MUST be preserved
        # Using unique names to ensure the LLM doesn't accidentally match them
        existing_field_assignments = {
            1: "preserved_login_email_xyz",
            2: "preserved_login_password_abc",
            # Action 3 has no existing field - LLM should generate a new name
        }

        schema_code, field_mappings = await generate_workflow_parameters_schema(
            actions_by_task, existing_field_assignments
        )

        # Verify the LLM preserved the exact field names we specified
        assert field_mappings["task_1:act_1"] == "preserved_login_email_xyz", (
            f"LLM should have preserved 'preserved_login_email_xyz' but got '{field_mappings.get('task_1:act_1')}'"
        )
        assert field_mappings["task_1:act_2"] == "preserved_login_password_abc", (
            f"LLM should have preserved 'preserved_login_password_abc' but got '{field_mappings.get('task_1:act_2')}'"
        )

        # Verify action 3 got a new field name (not one of the preserved ones)
        action_3_field = field_mappings.get("task_2:act_3")
        assert action_3_field is not None, "LLM should have generated a field name for action 3"
        assert action_3_field not in ["preserved_login_email_xyz", "preserved_login_password_abc"], (
            f"Action 3 should have a new field name, not a preserved one. Got: {action_3_field}"
        )

        # Verify the schema code contains the preserved field names
        assert "preserved_login_email_xyz" in schema_code, "Schema should contain preserved email field"
        assert "preserved_login_password_abc" in schema_code, "Schema should contain preserved password field"
        assert action_3_field in schema_code, f"Schema should contain new field '{action_3_field}'"

        print("\n✅ LLM preserved field names correctly!")
        print("   - Action 1: preserved_login_email_xyz ✓")
        print("   - Action 2: preserved_login_password_abc ✓")
        print(f"   - Action 3: {action_3_field} (newly generated) ✓")

    @pytest.mark.asyncio
    async def test_llm_generates_all_new_names_when_no_existing_fields(self):
        """
        Test that when there are no existing fields, the LLM generates appropriate new names.
        This is a baseline test to ensure the LLM call works correctly.
        """
        actions_by_task = {
            "task_1": [
                {
                    "action_type": "input_text",
                    "text": "test@example.com",
                    "intention": "Enter email address",
                    "action_id": "act_1",
                },
            ],
        }

        # No existing field assignments
        existing_field_assignments: dict[int, str] = {}

        schema_code, field_mappings = await generate_workflow_parameters_schema(
            actions_by_task, existing_field_assignments
        )

        # Verify we got a field mapping
        assert "task_1:act_1" in field_mappings, "Should have a field mapping for the action"
        field_name = field_mappings["task_1:act_1"]
        assert field_name, "Field name should not be empty"
        assert field_name in schema_code, f"Schema should contain the generated field name '{field_name}'"

        print(f"\n✅ LLM generated new field name: {field_name}")
