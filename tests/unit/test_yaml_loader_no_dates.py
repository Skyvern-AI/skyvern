"""Regression tests for SKY-8799.

PyYAML's default ``SafeLoader`` resolves ISO 8601 strings into Python
``datetime`` objects. When users embed such strings inside the
``default_value`` of a JSON workflow parameter, those values become
``datetime`` instances on the way in and then crash ``json.dumps`` on
the way back out, producing::

    TypeError: Object of type datetime is not JSON serializable

``skyvern.utils.yaml_loader.safe_load_no_dates`` removes the timestamp
implicit resolver so the values stay as plain strings.
"""

import json

import yaml

from skyvern.forge.sdk.routes.workflow_copilot import _process_workflow_yaml
from skyvern.utils.yaml_loader import safe_load_no_dates

ISO_BLOB = """
parameters:
  - parameter_type: workflow
    key: payload
    workflow_parameter_type: json
    default_value:
      id: "12345"
      metadata:
        # Unquoted ISO 8601 strings are what trip the default SafeLoader.
        created_at: 2023-10-27T10:00:00Z
        updated_at: 2023-10-28T14:30:00Z
        tags: ["primary", "test-data"]
"""


def test_default_safe_load_does_parse_datetimes() -> None:
    # Sanity check: documents the behavior we are working around.
    parsed = yaml.safe_load(ISO_BLOB)
    default_value = parsed["parameters"][0]["default_value"]
    # The default loader turns the unquoted-looking timestamps into datetimes,
    # which is exactly what breaks downstream JSON serialization.
    assert not isinstance(default_value["metadata"]["created_at"], str)


def test_safe_load_no_dates_keeps_iso_strings_as_strings() -> None:
    parsed = safe_load_no_dates(ISO_BLOB)
    metadata = parsed["parameters"][0]["default_value"]["metadata"]

    assert metadata["created_at"] == "2023-10-27T10:00:00Z"
    assert metadata["updated_at"] == "2023-10-28T14:30:00Z"
    assert isinstance(metadata["created_at"], str)
    assert isinstance(metadata["updated_at"], str)


def test_safe_load_no_dates_round_trips_through_json() -> None:
    parsed = safe_load_no_dates(ISO_BLOB)
    # The whole point: the parsed structure must be JSON-serializable
    # without a custom encoder.
    serialized = json.dumps(parsed)
    assert "2023-10-27T10:00:00Z" in serialized


def test_safe_load_no_dates_preserves_other_implicit_types() -> None:
    parsed = safe_load_no_dates(
        """
        an_int: 42
        a_float: 3.14
        a_bool: true
        a_null: null
        a_list: [1, 2, 3]
        """
    )
    assert parsed["an_int"] == 42
    assert parsed["a_float"] == 3.14
    assert parsed["a_bool"] is True
    assert parsed["a_null"] is None
    assert parsed["a_list"] == [1, 2, 3]


def test_process_workflow_yaml_keeps_json_parameter_iso_strings() -> None:
    workflow = _process_workflow_yaml(
        workflow_id="wf-123",
        workflow_permanent_id="wfp-123",
        organization_id="org-123",
        workflow_yaml="""
title: Test
workflow_definition:
  parameters:
    - parameter_type: workflow
      key: payload
      workflow_parameter_type: json
      default_value:
        id: "12345"
        metadata:
          created_at: 2023-10-27T10:00:00Z
          updated_at: 2023-10-28T14:30:00Z
  blocks:
    - block_type: navigation
      label: step1
      url: https://example.com
      title: Step 1
      navigation_goal: Open the page
""",
    )

    parameter = workflow.get_parameter("payload")
    assert parameter is not None
    assert parameter.default_value is not None

    metadata = parameter.default_value["metadata"]
    assert metadata["created_at"] == "2023-10-27T10:00:00Z"
    assert metadata["updated_at"] == "2023-10-28T14:30:00Z"
    assert isinstance(metadata["created_at"], str)
    assert isinstance(metadata["updated_at"], str)
