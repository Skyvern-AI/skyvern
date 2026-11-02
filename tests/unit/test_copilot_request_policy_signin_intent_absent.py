"""A stored copilot context written before the sign-in intent fields were removed must still load."""

from __future__ import annotations

import json

from skyvern.forge.sdk.copilot.context import StructuredContext


def test_stored_context_carrying_a_signin_email_still_loads() -> None:
    stored = json.dumps(
        {"user_goal": "sign in", "signin_email": "user@example.com", "signin_email_host": "example.com"}
    )

    structured = StructuredContext.from_json_str(stored)

    assert structured.user_goal == "sign in"
