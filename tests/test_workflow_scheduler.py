import asyncio
import datetime
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from skyvern.forge import app
from skyvern.forge.sdk.workflow.scheduler import WorkflowScheduler, workflow_scheduler
from skyvern.forge.sdk.schemas.workflows import Workflow
from skyvern.forge.sdk.schemas.organizations import Organization


@pytest.fixture
def mock_database():
    """Create a mock database for testing."""
    mock_db = AsyncMock()
    return mock_db


@pytest.fixture
def mock_workflow_service():
    """Create a mock workflow service for testing."""
    mock_service = AsyncMock()
    return mock_service


@pytest.fixture
async def scheduler(mock_database, mock_workflow_service):
    """Create a workflow scheduler instance for testing."""
    # Replace the app's database and workflow service with mocks
    app.DATABASE = mock_database
    app.WORKFLOW_SERVICE = mock_workflow_service
    
    # Create a new scheduler instance for testing
    scheduler = WorkflowScheduler()
    scheduler.scheduler = MagicMock()
    scheduler.scheduler.add_job = MagicMock(return_value=MagicMock(id="test_job_id"))
    scheduler.scheduler.remove_job = MagicMock()
    scheduler._initialized = True
    
    yield scheduler
    
    # Reset the app's database and workflow service
    app.DATABASE = None
    app.WORKFLOW_SERVICE = None


async def test_load_scheduled_workflows(scheduler, mock_database):
    """Test loading scheduled workflows from the database."""
    # Create mock workflows
    mock_workflows = [
        Workflow(
            workflow_id="workflow1",
            workflow_permanent_id="perm1",
            organization_id="org1",
            title="Test Workflow 1",
            cron_expression="0 9 * * 1-5",
            timezone="UTC",
            cron_enabled=True,
            next_run_time=datetime.datetime.now() + datetime.timedelta(days=1)
        ),
        Workflow(
            workflow_id="workflow2",
            workflow_permanent_id="perm2",
            organization_id="org1",
            title="Test Workflow 2",
            cron_expression=None,
            timezone="UTC",
            cron_enabled=True,
            next_run_time=None
        ),
    ]
    
    # Configure the mock database to return the mock workflows
    mock_database.get_workflows_with_cron_enabled.return_value = mock_workflows
    
    # Call the method under test
    await scheduler.load_scheduled_workflows()
    
    # Verify that get_workflows_with_cron_enabled was called
    mock_database.get_workflows_with_cron_enabled.assert_called_once()
    
    # Verify that schedule_workflow was called for the first workflow only (which has a cron expression)
    assert scheduler.scheduler.add_job.call_count == 1


async def test_schedule_workflow(scheduler, mock_database):
    """Test scheduling a workflow."""
    # Create a mock workflow
    mock_workflow = Workflow(
        workflow_id="workflow1",
        workflow_permanent_id="perm1",
        organization_id="org1",
        title="Test Workflow",
        cron_expression="0 9 * * 1-5",
        timezone="UTC",
        cron_enabled=True,
        next_run_time=None
    )
    
    # Configure the mock database to return the mock workflow
    mock_database.get_workflow.return_value = mock_workflow
    
    # Call the method under test
    await scheduler.schedule_workflow("workflow1")
    
    # Verify that get_workflow was called
    mock_database.get_workflow.assert_called_once_with("workflow1")
    
    # Verify that update_workflow was called to update the next_run_time
    mock_database.update_workflow.assert_called_once()
    
    # Verify that add_job was called to schedule the workflow
    scheduler.scheduler.add_job.assert_called_once()
    
    # Verify that the job ID was stored in the job map
    assert scheduler._job_map.get("workflow1") == "test_job_id"


async def test_unschedule_workflow(scheduler):
    """Test unscheduling a workflow."""
    # Set up a job in the job map
    scheduler._job_map["workflow1"] = "test_job_id"
    
    # Call the method under test
    await scheduler.unschedule_workflow("workflow1")
    
    # Verify that remove_job was called
    scheduler.scheduler.remove_job.assert_called_once_with("test_job_id")
    
    # Verify that the job ID was removed from the job map
    assert "workflow1" not in scheduler._job_map


async def test_update_workflow_schedule_enabled(scheduler, mock_database):
    """Test updating a workflow schedule when cron is enabled."""
    # Create a mock workflow with cron enabled
    mock_workflow = Workflow(
        workflow_id="workflow1",
        workflow_permanent_id="perm1",
        organization_id="org1",
        title="Test Workflow",
        cron_expression="0 9 * * 1-5",
        timezone="UTC",
        cron_enabled=True,
        next_run_time=None
    )
    
    # Configure the mock database to return the mock workflow
    mock_database.get_workflow.return_value = mock_workflow
    
    # Call the method under test
    await scheduler.update_workflow_schedule("workflow1")
    
    # Verify that get_workflow was called
    mock_database.get_workflow.assert_called_once_with("workflow1")
    
    # Verify that schedule_workflow was called
    assert scheduler.scheduler.add_job.call_count == 1


async def test_update_workflow_schedule_disabled(scheduler, mock_database):
    """Test updating a workflow schedule when cron is disabled."""
    # Create a mock workflow with cron disabled
    mock_workflow = Workflow(
        workflow_id="workflow1",
        workflow_permanent_id="perm1",
        organization_id="org1",
        title="Test Workflow",
        cron_expression="0 9 * * 1-5",
        timezone="UTC",
        cron_enabled=False,
        next_run_time=None
    )
    
    # Configure the mock database to return the mock workflow
    mock_database.get_workflow.return_value = mock_workflow
    
    # Set up a job in the job map
    scheduler._job_map["workflow1"] = "test_job_id"
    
    # Call the method under test
    await scheduler.update_workflow_schedule("workflow1")
    
    # Verify that get_workflow was called
    mock_database.get_workflow.assert_called_once_with("workflow1")
    
    # Verify that unschedule_workflow was called
    scheduler.scheduler.remove_job.assert_called_once_with("test_job_id")
    
    # Verify that update_workflow was called to clear the next_run_time
    mock_database.update_workflow.assert_called_once_with(
        workflow_id="workflow1",
        next_run_time=None
    )


async def test_execute_workflow(scheduler, mock_database, mock_workflow_service):
    """Test executing a workflow as a scheduled job."""
    # Create a mock workflow
    mock_workflow = Workflow(
        workflow_id="workflow1",
        workflow_permanent_id="perm1",
        organization_id="org1",
        title="Test Workflow",
        cron_expression="0 9 * * 1-5",
        timezone="UTC",
        cron_enabled=True,
        next_run_time=None
    )
    
    # Create a mock organization
    mock_organization = Organization(
        organization_id="org1",
        organization_name="Test Org"
    )
    
    # Configure the mock database
    mock_database.get_workflow.return_value = mock_workflow
    mock_database.get_organization.return_value = mock_organization
    
    # Mock the _create_workflow_run method
    with patch.object(scheduler, "_create_workflow_run") as mock_create_run:
        # Configure the mock to return a workflow run
        mock_workflow_run = MagicMock(workflow_run_id="run1")
        mock_create_run.return_value = mock_workflow_run
        
        # Mock the _run_workflow method
        with patch.object(scheduler, "_run_workflow") as mock_run_workflow:
            # Mock the _update_next_run_time method
            with patch.object(scheduler, "_update_next_run_time") as mock_update_next_run:
                # Call the method under test
                await scheduler._execute_workflow("workflow1", "org1")
                
                # Verify that get_organization was called
                mock_database.get_organization.assert_called_once_with("org1")
                
                # Verify that _create_workflow_run was called
                mock_create_run.assert_called_once_with("workflow1", mock_organization, triggered_by_cron=True)
                
                # Verify that _run_workflow was called
                mock_run_workflow.assert_called_once_with(mock_organization, "workflow1", "run1")
                
                # Verify that _update_next_run_time was called
                mock_update_next_run.assert_called_once_with("workflow1")
