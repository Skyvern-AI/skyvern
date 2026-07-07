"""enable_self_healing YAML semantics: omitted means inherit-on-update (None), never an
implicit disable — older clients that don't send the field must not clobber the setting."""

from skyvern.schemas.workflows import WorkflowCreateYAMLRequest, WorkflowDefinitionYAML


def _request(**kwargs: object) -> WorkflowCreateYAMLRequest:
    return WorkflowCreateYAMLRequest(
        title="t",
        workflow_definition=WorkflowDefinitionYAML(parameters=[], blocks=[]),
        **kwargs,
    )


def test_omitted_enable_self_healing_is_none() -> None:
    assert _request().enable_self_healing is None


def test_explicit_false_survives() -> None:
    assert _request(enable_self_healing=False).enable_self_healing is False


def test_explicit_true_survives() -> None:
    assert _request(enable_self_healing=True).enable_self_healing is True
