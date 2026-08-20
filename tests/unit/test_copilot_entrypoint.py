from __future__ import annotations

import pytest

from skyvern.forge.sdk.copilot.entrypoint import (
    anchor_recovers_entrypoint,
    extract_anchor_entry_url,
    extract_in_turn_entry_url,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("open https://example.com/login", "https://example.com/login"),
        ("open *https://example.com/login*", "https://example.com/login"),
        ("open http://localhost:8080/login", "http://localhost:8080/login"),
        ("no url here", None),
        ("truncated https://exam…ple.com/login", None),
    ],
)
def test_extract_anchor_entry_url(text: str, expected: str | None) -> None:
    assert extract_anchor_entry_url(text) == expected


def test_extract_in_turn_entry_url_prefers_latest_message() -> None:
    workflow_yaml = """
workflow_definition:
  blocks:
    - block_type: goto_url
      label: open_site
      url: https://workflow.example/start
"""

    assert (
        extract_in_turn_entry_url("use https://message.example/start", "", workflow_yaml)
        == "https://message.example/start"
    )


def test_extract_in_turn_entry_url_falls_back_to_workflow() -> None:
    workflow_yaml = """
workflow_definition:
  blocks:
    - block_type: goto_url
      label: open_site
      url: https://workflow.example/start
"""

    assert extract_in_turn_entry_url("continue", "", workflow_yaml) == "https://workflow.example/start"


def test_anchor_recovery_does_not_override_a_current_url() -> None:
    assert (
        anchor_recovers_entrypoint(
            "open https://current.example/start",
            "",
            "",
            "earlier https://anchor.example/start",
        )
        is None
    )


def test_anchor_recovery_supplies_missing_current_url() -> None:
    assert (
        anchor_recovers_entrypoint("continue", "", "", "earlier https://anchor.example/start")
        == "https://anchor.example/start"
    )
