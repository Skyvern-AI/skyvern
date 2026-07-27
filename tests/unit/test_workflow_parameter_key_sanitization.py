"""Regression tests for parameter-key rewriting in sanitize_workflow_yaml_with_references.

Two distinct valid parameter keys can sanitize into a collision (e.g. "my-key" -> "my_key",
which already exists), so the second gets a numeric suffix. The reference-rewriting pass in
Step 4 used to run once per mapping entry and recurse into every string, so it re-applied the
mapping to each parameter's own already-final `key` field and chained a later substitution onto
an earlier one. That turned distinct keys into duplicates and pointed source_parameter_key at
the wrong parameter, silently, on the public workflow-import path.
"""

from skyvern.schemas.workflows import sanitize_workflow_yaml_with_references


def test_colliding_sanitized_keys_stay_unique() -> None:
    workflow_yaml = {
        "workflow_definition": {
            "parameters": [
                {"key": "my-key", "parameter_type": "workflow", "workflow_parameter_type": "string"},
                {"key": "my_key", "parameter_type": "workflow", "workflow_parameter_type": "string"},
            ],
            "blocks": [],
        }
    }
    sanitized = sanitize_workflow_yaml_with_references(workflow_yaml)
    keys = [param["key"] for param in sanitized["workflow_definition"]["parameters"]]
    # "my-key" -> "my_key", "my_key" -> "my_key_2"; neither should be rewritten into the other.
    assert keys == ["my_key", "my_key_2"]
    assert len(keys) == len(set(keys))


def test_source_parameter_key_follows_renamed_key_without_chaining() -> None:
    workflow_yaml = {
        "workflow_definition": {
            "parameters": [
                {"key": "my-key", "parameter_type": "workflow", "workflow_parameter_type": "string"},
                {"key": "my_key", "parameter_type": "workflow", "workflow_parameter_type": "string"},
                {"key": "ctx", "parameter_type": "context", "source_parameter_key": "my-key"},
            ],
            "blocks": [],
        }
    }
    sanitized = sanitize_workflow_yaml_with_references(workflow_yaml)
    params = sanitized["workflow_definition"]["parameters"]
    ctx = next(param for param in params if param["parameter_type"] == "context")
    # "my-key" was renamed to "my_key"; the reference must land there, not chain to "my_key_2".
    assert ctx["source_parameter_key"] == "my_key"


def test_source_parameter_key_pointing_at_suffixed_param_is_correct() -> None:
    workflow_yaml = {
        "workflow_definition": {
            "parameters": [
                {"key": "my-key", "parameter_type": "workflow", "workflow_parameter_type": "string"},
                {"key": "my_key", "parameter_type": "workflow", "workflow_parameter_type": "string"},
                {"key": "ctx", "parameter_type": "context", "source_parameter_key": "my_key"},
            ],
            "blocks": [],
        }
    }
    sanitized = sanitize_workflow_yaml_with_references(workflow_yaml)
    params = sanitized["workflow_definition"]["parameters"]
    ctx = next(param for param in params if param["parameter_type"] == "context")
    # The literal "my_key" parameter was the one suffixed to "my_key_2".
    assert ctx["source_parameter_key"] == "my_key_2"


def test_single_rename_still_updates_source_parameter_key() -> None:
    workflow_yaml = {
        "workflow_definition": {
            "parameters": [
                {"key": "bad-key", "parameter_type": "workflow", "workflow_parameter_type": "string"},
                {"key": "ctx", "parameter_type": "context", "source_parameter_key": "bad-key"},
            ],
            "blocks": [],
        }
    }
    sanitized = sanitize_workflow_yaml_with_references(workflow_yaml)
    params = sanitized["workflow_definition"]["parameters"]
    assert params[0]["key"] == "bad_key"
    ctx = next(param for param in params if param["parameter_type"] == "context")
    assert ctx["source_parameter_key"] == "bad_key"


def test_already_valid_keys_are_untouched() -> None:
    workflow_yaml = {
        "workflow_definition": {
            "parameters": [
                {"key": "alpha", "parameter_type": "workflow", "workflow_parameter_type": "string"},
                {"key": "beta", "parameter_type": "context", "source_parameter_key": "alpha"},
            ],
            "blocks": [],
        }
    }
    sanitized = sanitize_workflow_yaml_with_references(workflow_yaml)
    params = sanitized["workflow_definition"]["parameters"]
    assert [param["key"] for param in params] == ["alpha", "beta"]
    assert params[1]["source_parameter_key"] == "alpha"
