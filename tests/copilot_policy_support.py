"""Copilot request-policy helpers shared by the copilot test modules.

Deliberately not a conftest: the repo-root conftest is imported by every pytest
process, and these helpers pull in the copilot agent, which costs seconds of
import time for suites that never touch copilot.
"""

from typing import Any
from unittest.mock import AsyncMock

from skyvern.forge.sdk.copilot.agent import RequestPolicyGuardrailInputs, _apply_raw_secret_turn_transition
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import (
    RequestPolicy,
    _classifier_fallback_policy,
    _raw_secret_detected,
    build_request_policy_trust_floor,
)

SCREEN_INTERRUPTED_DRAFT_YAML = """
title: Synthetic three-block draft
workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: sign_in_with_saved_credential
      code: "return {'signed_in': True}"
    - block_type: code
      label: read_reliability_metric
      code: "return {'metric': 0.0}"
    - block_type: code
      label: write_destination_cell
      code: "return {'written': True}"
"""
SCREEN_INTERRUPTED_LABELS = [
    "sign_in_with_saved_credential",
    "read_reliability_metric",
    "write_destination_cell",
]


def screen_interrupted_proposal() -> dict[str, Any]:
    """Synthetic stand-in for a chat's retained ``proposed_workflow`` row."""
    return {
        "title": "Synthetic three-block draft",
        "workflow_definition": {"parameters": [], "blocks": []},
        "_copilot_yaml": SCREEN_INTERRUPTED_DRAFT_YAML,
    }


SYNTHETIC_SECRET_TURN = "password: Xk9mQ2vLp8ZtRw3Nb"
NO_CHANGE_FOLLOW_UP = "no-change follow-up on a turn that already proposed a workflow"


async def authoring_barred_policy(
    kind: str,
    *,
    ctx: CopilotContext,
    organization_id: str,
    workflow_yaml: str = "",
) -> RequestPolicy:
    """Build the RequestPolicy a safety block produces, via the product constructors."""
    if kind == "screen_unavailable":
        return await build_request_policy_trust_floor(
            user_message=NO_CHANGE_FOLLOW_UP,
            workflow_yaml=workflow_yaml,
            chat_history=[],
            global_llm_context="",
            organization_id=organization_id,
            handler=None,
        )
    policy = _classifier_fallback_policy(
        [],
        raw_secret_present=_raw_secret_detected(SYNTHETIC_SECRET_TURN),
        failure_kind="provider_error",
        user_message=SYNTHETIC_SECRET_TURN,
    )
    _apply_raw_secret_turn_transition(
        ctx,
        policy,
        RequestPolicyGuardrailInputs(
            user_message=SYNTHETIC_SECRET_TURN,
            workflow_yaml=workflow_yaml,
            chat_history_text="",
            chat_history_messages=[],
            global_llm_context="",
            organization_id=organization_id,
            request_policy_handler=None,
        ),
    )
    return policy


CLEAN_FOLLOW_UP = "rename the second block to read_reliability_metric"


async def authorized_clarification_policy(
    *,
    organization_id: str,
    workflow_yaml: str = "",
) -> RequestPolicy:
    """Build the RequestPolicy a turn that screened clean produces.

    The authoring bar is derived from this turn's own message, so a clean turn
    following a barred one is authorized again; hand-setting
    ``allow_update_workflow`` here would assert against a policy no production
    path can produce.
    """
    handler = AsyncMock(return_value={"version": "1", "state": "clean", "citations": []})
    return await build_request_policy_trust_floor(
        user_message=CLEAN_FOLLOW_UP,
        workflow_yaml=workflow_yaml,
        chat_history=[],
        global_llm_context="",
        organization_id=organization_id,
        handler=handler,
    )
