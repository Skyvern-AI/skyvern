import asyncio
import pytest
import yaml
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime

from skyvern.forge import app
from skyvern.forge.sdk.api.aws import AsyncAWSClient
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody, WorkflowRunStatus, WorkflowDefinition
from skyvern.forge.sdk.workflow.models.yaml import WorkflowCreateYAMLRequest, WorkflowDefinitionYAML, ParameterYAML, CodeBlockYAML
from skyvern.forge.sdk.workflow.models.parameter import ParameterType
from skyvern.forge.sdk.workflow.models.parameter import OnePasswordLoginCredentialParameter as SDKOnePasswordLoginCredentialParameter
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.forge.sdk.services.onepassword import OnePasswordService, OnePasswordItemNotFoundError
from skyvern.forge.sdk.db.models import WorkflowModel
from skyvern.client.types import OnePasswordLoginCredentialParameter as ClientOnePasswordLoginCredentialParameter
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.exceptions import SkyvernException
from skyvern.forge.sdk.workflow.models.block import BlockStatus

# Mark all tests in this module as asyncio
pytestmark = pytest.mark.asyncio

@pytest.fixture
def test_organization():
    return Organization(organization_id="org_test_123", organization_name="Test Org", domain="test.org")

@pytest.fixture
def workflow_service():
    return WorkflowService()

ONEPASSWORD_WORKFLOW_YAML_STR = """
title: Test 1Password Credential Workflow
description: A simple workflow to test 1Password credential resolution.
parameters:
  - key: op_creds
    parameter_type: onepassword_login_credential
    description: 1Password login item details.
    onepassword_access_token_aws_secret_key: "op_service_token_key_in_aws"
    onepassword_item_id: "test_item_uuid_or_name"
    onepassword_vault_id: "test_vault_uuid_or_name"
blocks:
  - label: use_creds_block
    block_type: code
    code: |
      username = skyvern.context.get_value("op_creds.username")
      password = skyvern.context.get_value("op_creds.password")
      totp_code = skyvern.context.get_value("op_creds.totp_code")
      skyvern.block_output = {
          "retrieved_username": username,
          "retrieved_password": password,
          "retrieved_totp": totp_code
      }
    parameters:
      - op_creds
"""

def _get_workflow_create_request_from_yaml_str(yaml_str: str) -> WorkflowCreateYAMLRequest:
    yaml_data = yaml.safe_load(yaml_str)
    parsed_parameters = []
    for p_data in yaml_data.get('parameters', []):
        param_fields = {
            'key': p_data['key'],
            'description': p_data.get('description'),
            'parameter_type': p_data['parameter_type']
        }
        if p_data['parameter_type'] == 'onepassword_login_credential':
            param_fields.update({
                'onepassword_access_token_aws_secret_key': p_data.get('onepassword_access_token_aws_secret_key'),
                'onepassword_item_id': p_data.get('onepassword_item_id'),
                'onepassword_vault_id': p_data.get('onepassword_vault_id'),
            })
        parsed_parameters.append(ParameterYAML(**param_fields))

    parsed_blocks = []
    for b_data in yaml_data.get('blocks', []):
        block_yaml = CodeBlockYAML(
            label=b_data['label'],
            block_type=b_data['block_type'],
            code=b_data['code'],
            parameters=[p for p in b_data.get('parameters', [])]
        )
        parsed_blocks.append(block_yaml)

    return WorkflowCreateYAMLRequest(
        title=yaml_data['title'],
        description=yaml_data.get('description'),
        workflow_definition=WorkflowDefinitionYAML(
            parameters=parsed_parameters,
            blocks=parsed_blocks
        )
    )

@patch('skyvern.forge.app.WORKFLOW_CONTEXT_MANAGER', new_callable=MagicMock)
@patch('skyvern.forge.app.DATABASE', new_callable=AsyncMock)
@patch('skyvern.forge.sdk.api.aws.AsyncAWSClient', new_callable=AsyncMock)
@patch('skyvern.forge.sdk.services.onepassword.OnePasswordService.get_login_credentials', new_callable=AsyncMock)
async def test_onepassword_workflow_success(
    mock_op_get_creds: AsyncMock,
    mock_aws_client_cls: AsyncMock,
    mock_db: AsyncMock,
    mock_context_manager_cls: MagicMock,
    workflow_service: WorkflowService,
    test_organization: Organization
):
    mock_aws_instance = mock_aws_client_cls.return_value
    mock_aws_instance.get_secret.return_value = "dummy_op_access_token"

    expected_username = "user_from_op"
    expected_password = "password_from_op"
    expected_totp = "123456"
    mock_op_get_creds.return_value = {
        "username": expected_username,
        "password": expected_password,
        "totp": expected_totp,
    }
    workflow_create_request = _get_workflow_create_request_from_yaml_str(ONEPASSWORD_WORKFLOW_YAML_STR)

    converted_workflow_mock = MagicMock()
    converted_workflow_mock.workflow_id = "wf_test_123"
    converted_workflow_mock.workflow_permanent_id = "wfp_test_123"
    # ... (other attributes)
    converted_workflow_mock.workflow_definition = WorkflowDefinition(parameters=[], blocks=[])


    mock_db.create_workflow.return_value = converted_workflow_mock
    mock_db.convert_to_workflow.return_value = converted_workflow_mock

    mock_op_param_client = ClientOnePasswordLoginCredentialParameter(
        key="op_creds",
        description="1Password login item details.",
        onepassword_login_credential_parameter_id="op_param_id_123",
        workflow_id="wf_test_123",
        onepassword_access_token_aws_secret_key="op_service_token_key_in_aws",
        onepassword_item_id="test_item_uuid_or_name",
        onepassword_vault_id="test_vault_uuid_or_name",
        created_at=datetime.utcnow(),
        modified_at=datetime.utcnow()
    )
    mock_db.create_onepassword_login_credential_parameter.return_value = mock_op_param_client
    mock_db.get_workflow_parameters.return_value = []
    mock_db.create_output_parameter.return_value = MagicMock(output_parameter_id="out_param_123", key="use_creds_block_output")

    created_workflow = await workflow_service.create_workflow_from_request(
        organization=test_organization,
        request=workflow_create_request
    )
    if created_workflow and hasattr(created_workflow, 'workflow_definition'):
        converted_workflow_mock.workflow_definition = created_workflow.workflow_definition

    workflow_request_body = WorkflowRequestBody(data={})
    mock_db.get_workflow_by_permanent_id.return_value = converted_workflow_mock

    mock_workflow_run_model = MagicMock()
    mock_workflow_run_model.workflow_run_id = "wfr_test_123"
    mock_workflow_run_model.workflow_id = converted_workflow_mock.workflow_id
    # ... (set other necessary attributes on mock_workflow_run_model as in previous step)
    mock_workflow_run_model.workflow_permanent_id = converted_workflow_mock.workflow_permanent_id
    mock_workflow_run_model.organization_id = test_organization.organization_id
    mock_workflow_run_model.status = WorkflowRunStatus.created
    mock_workflow_run_model.created_at = datetime.utcnow()
    mock_workflow_run_model.modified_at = datetime.utcnow()
    mock_workflow_run_model.proxy_location = None
    mock_workflow_run_model.webhook_callback_url = None
    mock_workflow_run_model.totp_verification_url = None
    mock_workflow_run_model.totp_identifier = None
    mock_workflow_run_model.parent_workflow_run_id = None
    mock_workflow_run_model.failure_reason = None

    mock_db.create_workflow_run.return_value = app.DATABASE.convert_to_workflow_run(mock_workflow_run_model)

    def update_workflow_run_side_effect(workflow_run_id, status, failure_reason=None):
        mock_workflow_run_model.status = status
        mock_workflow_run_model.failure_reason = failure_reason
        return app.DATABASE.convert_to_workflow_run(mock_workflow_run_model)

    mock_db.update_workflow_run.side_effect = update_workflow_run_side_effect
    mock_db.get_workflow_run_parameter_tuples.return_value = []
    mock_db.get_workflow_output_parameters.return_value = []

    mock_actual_context_manager_instance = WorkflowRunContext()
    app.WORKFLOW_CONTEXT_MANAGER.initialize_workflow_run_context = AsyncMock(return_value=mock_actual_context_manager_instance)

    mock_db.get_workflow_run_block.return_value = MagicMock(status=BlockStatus.completed, output={
        "retrieved_username": expected_username,
        "retrieved_password": expected_password,
        "retrieved_totp": expected_totp
    })
    mock_db.get_workflow_run.return_value = app.DATABASE.convert_to_workflow_run(mock_workflow_run_model)

    app.STORAGE = AsyncMock()
    app.ARTIFACT_MANAGER = AsyncMock()
    app.BROWSER_MANAGER = AsyncMock()
    app.BROWSER_MANAGER.cleanup_for_workflow_run.return_value = None

    workflow_run_result = await workflow_service.execute_workflow(
        workflow_run_id="wfr_test_123",
        api_key="dummy_api_key",
        organization=test_organization
    )

    assert workflow_run_result.status == WorkflowRunStatus.completed
    mock_op_get_creds.assert_called_once_with(
        item_id_or_name="test_item_uuid_or_name",
        vault_id_or_name="test_vault_uuid_or_name",
        additional_env={"OP_SERVICE_ACCOUNT_TOKEN": "dummy_op_access_token"}
    )
    mock_aws_instance.get_secret.assert_called_once_with("op_service_token_key_in_aws")

    final_context_values = mock_actual_context_manager_instance.values.get("op_creds", {})
    assert mock_actual_context_manager_instance.secrets.get(final_context_values.get("username")) == expected_username
    assert mock_actual_context_manager_instance.secrets.get(final_context_values.get("password")) == expected_password
    assert mock_actual_context_manager_instance.secrets.get(final_context_values.get("totp_code")) == expected_totp


@patch('skyvern.forge.app.WORKFLOW_CONTEXT_MANAGER', new_callable=MagicMock)
@patch('skyvern.forge.app.DATABASE', new_callable=AsyncMock)
@patch('skyvern.forge.sdk.api.aws.AsyncAWSClient', new_callable=AsyncMock)
async def test_onepassword_workflow_missing_aws_token(
    mock_aws_client_cls: AsyncMock,
    mock_db: AsyncMock,
    mock_context_manager_cls: MagicMock,
    workflow_service: WorkflowService,
    test_organization: Organization
):
    mock_aws_instance = mock_aws_client_cls.return_value
    mock_aws_instance.get_secret.return_value = None

    workflow_create_request = _get_workflow_create_request_from_yaml_str(ONEPASSWORD_WORKFLOW_YAML_STR)

    mock_workflow_obj = MagicMock()
    mock_workflow_obj.workflow_id = "wf_fail_aws_123"
    # ... (other attributes as in previous test)
    mock_workflow_obj.workflow_permanent_id = "wfp_fail_aws_123"
    mock_workflow_obj.organization_id = test_organization.organization_id
    mock_workflow_obj.workflow_definition = WorkflowDefinition(
        parameters=[
            SDKOnePasswordLoginCredentialParameter(
                key="op_creds",
                parameter_type=ParameterType.ONEPASSWORD_LOGIN_CREDENTIAL,
                onepassword_login_credential_parameter_id="op_param_id_123",
                workflow_id="wf_fail_aws_123",
                onepassword_access_token_aws_secret_key="op_service_token_key_in_aws",
                onepassword_item_id="test_item_uuid_or_name",
                onepassword_vault_id="test_vault_uuid_or_name",
                created_at=datetime.utcnow(), modified_at=datetime.utcnow()
            )
        ],
        blocks=[]
    ) # This definition is key for context init
    mock_db.get_workflow_by_permanent_id.return_value = mock_workflow_obj

    mock_workflow_run_model = MagicMock()
    mock_workflow_run_model.workflow_run_id="wfr_fail_aws_123"
    # ... (other attributes)
    mock_workflow_run_model.workflow_id = mock_workflow_obj.workflow_id
    mock_workflow_run_model.workflow_permanent_id = mock_workflow_obj.workflow_permanent_id
    mock_workflow_run_model.organization_id = test_organization.organization_id
    mock_workflow_run_model.status = WorkflowRunStatus.created
    mock_workflow_run_model.created_at = datetime.utcnow()
    mock_workflow_run_model.modified_at = datetime.utcnow()
    mock_workflow_run_model.proxy_location = None # Added
    mock_workflow_run_model.webhook_callback_url = None # Added
    mock_workflow_run_model.totp_verification_url = None # Added
    mock_workflow_run_model.totp_identifier = None # Added
    mock_workflow_run_model.parent_workflow_run_id = None # Added
    mock_workflow_run_model.failure_reason = None # Added


    mock_db.create_workflow_run.return_value = app.DATABASE.convert_to_workflow_run(mock_workflow_run_model)

    def update_workflow_run_side_effect(workflow_run_id, status, failure_reason=None):
        mock_workflow_run_model.status = status
        mock_workflow_run_model.failure_reason = failure_reason
        return app.DATABASE.convert_to_workflow_run(mock_workflow_run_model)
    mock_db.update_workflow_run.side_effect = update_workflow_run_side_effect

    mock_db.get_workflow_run_parameter_tuples.return_value = []
    mock_db.get_workflow_output_parameters.return_value = []
    mock_db.get_workflow_run.return_value = app.DATABASE.convert_to_workflow_run(mock_workflow_run_model)

    app.STORAGE = AsyncMock()
    app.ARTIFACT_MANAGER = AsyncMock()
    app.BROWSER_MANAGER = AsyncMock()
    app.BROWSER_MANAGER.cleanup_for_workflow_run.return_value = None

    with pytest.raises(SkyvernException, match="1Password service account token not found"):
        await workflow_service.execute_workflow(
            workflow_run_id="wfr_fail_aws_123",
            api_key="dummy_api_key",
            organization=test_organization
        )

    assert mock_workflow_run_model.status == WorkflowRunStatus.failed
    assert "1Password service account token not found" in mock_workflow_run_model.failure_reason
    mock_aws_instance.get_secret.assert_called_once_with("op_service_token_key_in_aws")


@patch('skyvern.forge.app.WORKFLOW_CONTEXT_MANAGER', new_callable=MagicMock)
@patch('skyvern.forge.app.DATABASE', new_callable=AsyncMock)
@patch('skyvern.forge.sdk.api.aws.AsyncAWSClient', new_callable=AsyncMock)
@patch('skyvern.forge.sdk.services.onepassword.OnePasswordService.get_login_credentials', new_callable=AsyncMock)
async def test_onepassword_workflow_op_service_failure(
    mock_op_get_creds: AsyncMock,
    mock_aws_client_cls: AsyncMock,
    mock_db: AsyncMock,
    mock_context_manager_cls: MagicMock,
    workflow_service: WorkflowService,
    test_organization: Organization
):
    mock_aws_instance = mock_aws_client_cls.return_value
    mock_aws_instance.get_secret.return_value = "dummy_op_access_token" # AWS token is fine

    # Simulate OnePasswordService failing to get credentials
    mock_op_get_creds.side_effect = OnePasswordItemNotFoundError(
        item_id_or_name="test_item_uuid_or_name",
        vault_id_or_name="test_vault_uuid_or_name",
        message="Mocked 1Password item not found"
    )

    workflow_create_request = _get_workflow_create_request_from_yaml_str(ONEPASSWORD_WORKFLOW_YAML_STR)

    mock_workflow_obj = MagicMock() # As used in previous test
    mock_workflow_obj.workflow_id = "wf_fail_op_123"
    mock_workflow_obj.workflow_permanent_id = "wfp_fail_op_123"
    mock_workflow_obj.organization_id = test_organization.organization_id
    mock_workflow_obj.workflow_definition = WorkflowDefinition(
        parameters=[
             SDKOnePasswordLoginCredentialParameter(
                key="op_creds",
                parameter_type=ParameterType.ONEPASSWORD_LOGIN_CREDENTIAL,
                onepassword_login_credential_parameter_id="op_param_id_456",
                workflow_id="wf_fail_op_123",
                onepassword_access_token_aws_secret_key="op_service_token_key_in_aws",
                onepassword_item_id="test_item_uuid_or_name",
                onepassword_vault_id="test_vault_uuid_or_name",
                created_at=datetime.utcnow(), modified_at=datetime.utcnow()
            )
        ],
        blocks=[]
    )
    mock_db.get_workflow_by_permanent_id.return_value = mock_workflow_obj

    mock_workflow_run_model = MagicMock()
    mock_workflow_run_model.workflow_run_id="wfr_fail_op_123"
    # ... (other attributes)
    mock_workflow_run_model.workflow_id = mock_workflow_obj.workflow_id
    mock_workflow_run_model.workflow_permanent_id = mock_workflow_obj.workflow_permanent_id
    mock_workflow_run_model.organization_id = test_organization.organization_id
    mock_workflow_run_model.status = WorkflowRunStatus.created
    mock_workflow_run_model.created_at = datetime.utcnow()
    mock_workflow_run_model.modified_at = datetime.utcnow()
    mock_workflow_run_model.proxy_location = None
    mock_workflow_run_model.webhook_callback_url = None
    mock_workflow_run_model.totp_verification_url = None
    mock_workflow_run_model.totp_identifier = None
    mock_workflow_run_model.parent_workflow_run_id = None
    mock_workflow_run_model.failure_reason = None

    mock_db.create_workflow_run.return_value = app.DATABASE.convert_to_workflow_run(mock_workflow_run_model)

    def update_workflow_run_side_effect(workflow_run_id, status, failure_reason=None):
        mock_workflow_run_model.status = status
        mock_workflow_run_model.failure_reason = failure_reason
        return app.DATABASE.convert_to_workflow_run(mock_workflow_run_model)
    mock_db.update_workflow_run.side_effect = update_workflow_run_side_effect

    mock_db.get_workflow_run_parameter_tuples.return_value = []
    mock_db.get_workflow_output_parameters.return_value = []
    mock_db.get_workflow_run.return_value = app.DATABASE.convert_to_workflow_run(mock_workflow_run_model)

    app.STORAGE = AsyncMock()
    app.ARTIFACT_MANAGER = AsyncMock()
    app.BROWSER_MANAGER = AsyncMock()
    app.BROWSER_MANAGER.cleanup_for_workflow_run.return_value = None

    # Expect execute_workflow to fail because context initialization will fail due to op_service error
    with pytest.raises(OnePasswordItemNotFoundError, match="Mocked 1Password item not found"):
        await workflow_service.execute_workflow(
            workflow_run_id="wfr_fail_op_123",
            api_key="dummy_api_key",
            organization=test_organization
        )

    assert mock_workflow_run_model.status == WorkflowRunStatus.failed
    assert "Mocked 1Password item not found" in mock_workflow_run_model.failure_reason
    mock_aws_instance.get_secret.assert_called_once_with("op_service_token_key_in_aws")
    mock_op_get_creds.assert_called_once()
```
