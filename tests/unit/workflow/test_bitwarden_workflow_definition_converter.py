import pytest

from skyvern.forge.sdk.workflow.exceptions import WorkflowParameterMissingRequiredValue
from skyvern.forge.sdk.workflow.models.parameter import BitwardenLoginCredentialParameter
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.workflows import BitwardenLoginCredentialParameterYAML, WorkflowDefinitionYAML


def _bitwarden_login_parameter(
    *,
    bitwarden_collection_id: str | None,
    bitwarden_item_id: str | None,
    url_parameter_key: str | None,
) -> BitwardenLoginCredentialParameterYAML:
    return BitwardenLoginCredentialParameterYAML(
        key="login",
        bitwarden_client_id_aws_secret_key="client_id_secret",
        bitwarden_client_secret_aws_secret_key="client_secret_secret",
        bitwarden_master_password_aws_secret_key="master_password_secret",
        bitwarden_collection_id=bitwarden_collection_id,
        bitwarden_item_id=bitwarden_item_id,
        url_parameter_key=url_parameter_key,
    )


def test_bitwarden_login_item_id_with_collection_id_does_not_require_url_parameter_key() -> None:
    workflow_definition = WorkflowDefinitionYAML(
        parameters=[
            _bitwarden_login_parameter(
                bitwarden_collection_id="collection-id",
                bitwarden_item_id="item-id",
                url_parameter_key=None,
            )
        ],
        blocks=[],
    )

    converted = convert_workflow_definition(workflow_definition_yaml=workflow_definition, workflow_id="workflow-id")

    parameter = converted.parameters[0]
    assert isinstance(parameter, BitwardenLoginCredentialParameter)
    assert parameter.bitwarden_collection_id == "collection-id"
    assert parameter.bitwarden_item_id == "item-id"
    assert parameter.url_parameter_key is None


def test_bitwarden_login_collection_only_still_requires_url_parameter_key() -> None:
    workflow_definition = WorkflowDefinitionYAML(
        parameters=[
            _bitwarden_login_parameter(
                bitwarden_collection_id="collection-id",
                bitwarden_item_id=None,
                url_parameter_key=None,
            )
        ],
        blocks=[],
    )

    with pytest.raises(WorkflowParameterMissingRequiredValue) as exc_info:
        convert_workflow_definition(workflow_definition_yaml=workflow_definition, workflow_id="workflow-id")

    assert "workflow_parameter_key: login" in str(exc_info.value)
    assert "Required value: url_parameter_key" in str(exc_info.value)
