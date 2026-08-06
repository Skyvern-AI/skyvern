from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from skyvern.forge.sdk.copilot.agent_naming import derive_agent_title, maybe_name_agent
from skyvern.forge.sdk.copilot.request_policy import (
    RequestPolicy,
)
from skyvern.forge.sdk.copilot.turn_intent import (
    TurnIntent,
    TurnIntentClassification,
    TurnIntentClassifierResult,
    TurnIntentMode,
)
from skyvern.forge.sdk.copilot.workflow_yaml import with_workflow_yaml_title, workflow_yaml_title
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
)

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)

NAMED_YAML = "title: Quarterly AP\nworkflow_definition:\n  blocks: []\n"
DEFAULT_YAML = "title: New Agent\nworkflow_definition:\n  blocks: []\n"


def _history(*messages: tuple[str, str]) -> list[WorkflowCopilotChatHistoryMessage]:
    return [
        WorkflowCopilotChatHistoryMessage(
            sender=WorkflowCopilotChatSender.USER if sender == "user" else WorkflowCopilotChatSender.AI,
            content=content,
            created_at=_TS,
        )
        for sender, content in messages
    ]


def _classifier_result(title: str | None) -> TurnIntentClassifierResult:
    return TurnIntentClassifierResult.success(TurnIntentClassification(mode=TurnIntentMode.BUILD, workflow_title=title))


def _ctx(
    mode: TurnIntentMode = TurnIntentMode.BUILD,
    workflow_yaml: str = DEFAULT_YAML,
    raw_secret_detected: bool = False,
) -> MagicMock:
    ctx = MagicMock()
    ctx.turn_intent = TurnIntent(mode=mode)
    ctx.request_policy = RequestPolicy(raw_secret_detected=raw_secret_detected)
    ctx.workflow_yaml = workflow_yaml
    ctx.workflow_id = "w_1"
    ctx.workflow_permanent_id = "wpid_1"
    ctx.organization_id = "o_1"
    return ctx


def test_derive_uses_the_classifier_candidate() -> None:
    title = derive_agent_title(_classifier_result("Invoice Bot"))

    assert title == "Invoice Bot"


@pytest.mark.asyncio
async def test_naming_skips_non_authoring_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    rename = AsyncMock(return_value=True)
    monkeypatch.setattr("skyvern.forge.app.DATABASE.workflows.rename_workflow_if_still_default", rename)

    assert (
        await maybe_name_agent(_ctx(mode=TurnIntentMode.DIAGNOSE), classifier_result=_classifier_result("Invoice Bot"))
        is None
    )
    rename.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [TurnIntentMode.BUILD, TurnIntentMode.EDIT, TurnIntentMode.DRAFT_ONLY])
async def test_naming_runs_for_every_authoring_mode(mode: TurnIntentMode, monkeypatch: pytest.MonkeyPatch) -> None:
    rename = AsyncMock(return_value=True)
    monkeypatch.setattr("skyvern.forge.app.DATABASE.workflows.rename_workflow_if_still_default", rename)

    title = await maybe_name_agent(_ctx(mode=mode), classifier_result=_classifier_result("Invoice Bot"))

    assert title == "Invoice Bot"
    assert rename.await_args.kwargs["title"] == "Invoice Bot"


@pytest.mark.asyncio
async def test_naming_yields_to_an_unsaved_editor_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    """Canonical can still read default while the user has typed a name they have not saved."""
    rename = AsyncMock(return_value=True)
    monkeypatch.setattr("skyvern.forge.app.DATABASE.workflows.rename_workflow_if_still_default", rename)

    assert (
        await maybe_name_agent(_ctx(workflow_yaml=NAMED_YAML), classifier_result=_classifier_result("Invoice Bot"))
        is None
    )
    rename.assert_not_awaited()


@pytest.mark.asyncio
async def test_naming_reports_nothing_when_the_compare_and_set_loses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lost race must not emit a title the database does not hold."""
    monkeypatch.setattr(
        "skyvern.forge.app.DATABASE.workflows.rename_workflow_if_still_default",
        AsyncMock(return_value=False),
    )

    assert await maybe_name_agent(_ctx(), classifier_result=_classifier_result("Invoice Bot")) is None


@pytest.mark.asyncio
async def test_naming_fails_open_when_the_write_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "skyvern.forge.app.DATABASE.workflows.rename_workflow_if_still_default",
        AsyncMock(side_effect=RuntimeError("db down")),
    )

    assert await maybe_name_agent(_ctx(), classifier_result=_classifier_result("Invoice Bot")) is None


def test_with_workflow_yaml_title_replaces_in_place_and_preserves_the_rest() -> None:
    original = 'title: New Agent\nworkflow_definition:\n  blocks:\n    - code: |\n        x = "  keep   me  "\n'

    updated = with_workflow_yaml_title(original, "Invoice Bot")

    assert workflow_yaml_title(updated) == "Invoice Bot"
    assert 'x = "  keep   me  "' in updated
    assert "New Agent" not in updated


def test_with_workflow_yaml_title_adds_a_title_when_absent() -> None:
    updated = with_workflow_yaml_title("workflow_definition:\n  blocks: []\n", "Invoice Bot")

    assert workflow_yaml_title(updated) == "Invoice Bot"
    assert "workflow_definition:" in updated


@pytest.mark.asyncio
async def test_naming_withholds_when_the_policy_layer_flagged_a_raw_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    rename = AsyncMock(return_value=True)
    monkeypatch.setattr("skyvern.forge.app.DATABASE.workflows.rename_workflow_if_still_default", rename)

    assert (
        await maybe_name_agent(_ctx(raw_secret_detected=True), classifier_result=_classifier_result("Invoice Bot"))
        is None
    )
    rename.assert_not_awaited()


def test_with_workflow_yaml_title_replaces_a_block_scalar_title_whole() -> None:
    """The old value's continuation lines must go with it, or they fold into the new title."""
    original = "title: >-\n  A wrapped\n  name\nworkflow_definition:\n  blocks: []\n"

    updated = with_workflow_yaml_title(original, "Invoice Bot")

    assert workflow_yaml_title(updated) == "Invoice Bot"
    assert "A wrapped" not in updated


def test_with_workflow_yaml_title_does_not_compound_a_folded_title() -> None:
    """PyYAML folds a long title at width=80; a partial replacement grows it every turn."""
    long_title = "Download all invoices from the vendor portal and upload them to the accounting sheet"
    document = yaml.safe_dump({"title": long_title, "workflow_definition": {"blocks": []}}, sort_keys=False)

    for _ in range(3):
        document = with_workflow_yaml_title(document, workflow_yaml_title(document) or long_title)
        document = yaml.safe_dump(yaml.safe_load(document), sort_keys=False)

    assert workflow_yaml_title(document) == long_title


def test_with_workflow_yaml_title_matches_a_quoted_root_key() -> None:
    updated = with_workflow_yaml_title('"title": Old Name\nworkflow_definition:\n  blocks: []\n', "Invoice Bot")

    assert workflow_yaml_title(updated) == "Invoice Bot"


def test_with_workflow_yaml_title_inserts_after_a_document_marker() -> None:
    """Prepending ahead of --- would make two documents, which safe_load rejects."""
    updated = with_workflow_yaml_title("---\nworkflow_definition:\n  blocks: []\n", "Invoice Bot")

    assert workflow_yaml_title(updated) == "Invoice Bot"


def test_derive_never_falls_back_to_user_text() -> None:
    """The only filter available here is the pattern scrub the policy layer already ran."""
    assert derive_agent_title(_classifier_result(None)) is None
    assert derive_agent_title(None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_secret_detected", "raw_secret_handling"),
    [(True, "none"), (False, "redacted_draft"), (False, "block")],
)
async def test_naming_withholds_on_either_credential_verdict(
    raw_secret_detected: bool, raw_secret_handling: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """raw_secret_detected is the pattern verdict; raw_secret_handling is the classifier's."""
    rename = AsyncMock(return_value=True)
    monkeypatch.setattr("skyvern.forge.app.DATABASE.workflows.rename_workflow_if_still_default", rename)
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(raw_secret_detected=raw_secret_detected, raw_secret_handling=raw_secret_handling)

    assert await maybe_name_agent(ctx, classifier_result=_classifier_result("Invoice Bot")) is None
    rename.assert_not_awaited()
