from __future__ import annotations

import textwrap

from skyvern.forge.sdk.copilot.request_policy import _workflow_credential_inputs_unbound


def _yaml(body: str) -> str:
    return textwrap.dedent(body).strip() + "\n"


def test_detects_workflow_level_credential_referencing_empty_workflow_param() -> None:
    yaml = _yaml(
        """
        title: example
        workflow_definition:
          parameters:
          - key: username_key_vault_id
            parameter_type: workflow
            workflow_parameter_type: string
            default_value: ''
          - key: password_key_vault_id
            parameter_type: workflow
            workflow_parameter_type: string
            default_value: null
          - key: azure_credentials
            parameter_type: azure_vault_credential
            azure_vault_credential_parameter_id: azcp_528000000000000000
            vault_name: skyvern-secret-store
            username_key: '{{username_key_vault_id}}'
            password_key: '{{password_key_vault_id}}'
          blocks:
          - block_type: login
            label: login
            parameters: []
        """
    )

    findings = _workflow_credential_inputs_unbound(yaml)
    kinds = {(f["location"], f["field"], f["kind"]) for f in findings}

    assert ("workflow", "username_key", "credential_template_unbound") in kinds
    assert ("workflow", "password_key", "credential_template_unbound") in kinds


def test_resolves_when_workflow_param_has_non_empty_default_value() -> None:
    yaml = _yaml(
        """
        title: example
        workflow_definition:
          parameters:
          - key: username_key_vault_id
            parameter_type: workflow
            workflow_parameter_type: string
            default_value: user@example.com
          - key: password_key_vault_id
            parameter_type: workflow
            workflow_parameter_type: string
            default_value: stored
          - key: azure_credentials
            parameter_type: azure_vault_credential
            azure_vault_credential_parameter_id: azcp_real
            vault_name: skyvern-secret-store
            username_key: '{{username_key_vault_id}}'
            password_key: '{{password_key_vault_id}}'
          blocks: []
        """
    )

    assert _workflow_credential_inputs_unbound(yaml) == []


def test_jinja_reference_to_undefined_workflow_param_is_flagged() -> None:
    yaml = _yaml(
        """
        title: example
        workflow_definition:
          parameters:
          - key: azure_credentials
            parameter_type: azure_vault_credential
            azure_vault_credential_parameter_id: azcp_real
            vault_name: skyvern-secret-store
            username_key: '{{not_a_parameter}}'
            password_key: literal
          blocks: []
        """
    )

    findings = _workflow_credential_inputs_unbound(yaml)

    assert any(f["kind"] == "credential_template_undefined" and f["missing"] == "not_a_parameter" for f in findings)


def test_empty_literal_credential_key_flagged() -> None:
    yaml = _yaml(
        """
        title: example
        workflow_definition:
          parameters:
          - key: azure_credentials
            parameter_type: azure_vault_credential
            azure_vault_credential_parameter_id: azcp_real
            vault_name: skyvern-secret-store
            username_key: ''
            password_key: '   '
          blocks: []
        """
    )

    findings = _workflow_credential_inputs_unbound(yaml)

    assert {(f["field"], f["kind"]) for f in findings} == {
        ("username_key", "credential_empty"),
        ("password_key", "credential_empty"),
    }


def test_mock_eval_keys_with_placeholder_suffix_are_not_false_flagged() -> None:
    yaml = _yaml(
        """
        title: example
        workflow_definition:
          parameters:
          - key: username_key_placeholder
            parameter_type: workflow
            workflow_parameter_type: string
            default_value: mock-user
          - key: password_key_placeholder
            parameter_type: workflow
            workflow_parameter_type: string
            default_value: mock-pass
          - key: azure_credentials
            parameter_type: azure_vault_credential
            azure_vault_credential_parameter_id: azcp_placeholder
            vault_name: skyvern-secret-store
            username_key: '{{ username_key_placeholder }}'
            password_key: '{{ password_key_placeholder }}'
          blocks: []
        """
    )

    assert _workflow_credential_inputs_unbound(yaml) == []


def test_block_level_credential_inside_loop_blocks_is_walked() -> None:
    yaml = _yaml(
        """
        title: example
        workflow_definition:
          parameters: []
          blocks:
          - block_type: for_loop
            label: outer
            loop_blocks:
            - block_type: login
              label: inner_login
              parameters:
              - parameter_type: azure_vault_credential
                key: azure_credentials
                azure_vault_credential_parameter_id: azcp_real
                vault_name: skyvern-secret-store
                username_key: '{{missing_key}}'
                password_key: '{{missing_key}}'
        """
    )

    findings = _workflow_credential_inputs_unbound(yaml)

    assert any(
        f["location"] == "inner_login" and f["field"] == "username_key" and f["kind"] == "credential_template_undefined"
        for f in findings
    )


def test_non_login_credential_types_are_out_of_scope() -> None:
    """Only login-credential types have username/password key fields; secret-only
    types (AWS_SECRET, AZURE_SECRET, Bitwarden Sensitive/CreditCard) use different
    schemas and fall outside this guardrail."""
    yaml = _yaml(
        """
        title: example
        workflow_definition:
          parameters:
          - key: my_secret
            parameter_type: aws_secret
            aws_key: '{{ some_unbound_key }}'
          blocks: []
        """
    )

    assert _workflow_credential_inputs_unbound(yaml) == []


def test_workflow_without_credentials_returns_empty() -> None:
    yaml = _yaml(
        """
        title: example
        workflow_definition:
          parameters: []
          blocks:
          - block_type: navigation
            label: visit
            url: https://example.com/
            navigation_goal: open
        """
    )

    assert _workflow_credential_inputs_unbound(yaml) == []


def test_malformed_or_empty_yaml_is_inert() -> None:
    assert _workflow_credential_inputs_unbound("") == []
    assert _workflow_credential_inputs_unbound("- not a workflow yaml\n") == []
    assert _workflow_credential_inputs_unbound(":: broken yaml ::") == []
