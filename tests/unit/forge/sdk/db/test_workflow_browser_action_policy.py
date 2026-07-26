"""Policy persistence rules on the workflows repository (SKY-12873).

Only the control-plane setter may write policy; every ordinary definition write carries the stored
value forward unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from scripts.backfill_encrypt_file_block_secrets import encrypt_file_block_secrets
from skyvern.forge.sdk.browser_action_policy import declare_policy
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import WorkflowModel
from skyvern.forge.sdk.workflow.browser_action_policy_enrollment import POLICY_KEY, serialize_policy
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition

EMPTY_DEFINITION: dict[str, Any] = {"parameters": [], "blocks": []}


@pytest_asyncio.fixture
async def org_id(agent_db: AgentDB) -> str:
    org = await agent_db.organizations.create_organization(
        organization_name="Policy Org",
        domain="policy.test",
    )
    return org.organization_id


async def _stored_definition(agent_db: AgentDB, workflow_id: str) -> dict[str, Any]:
    """Read the raw JSON column directly — the policy is deliberately invisible to the Workflow
    model, and the repository exposes no unscoped reader for it."""
    async with agent_db.workflows.Session() as session:
        stored = await session.scalar(
            select(WorkflowModel.workflow_definition).where(WorkflowModel.workflow_id == workflow_id)
        )
    assert isinstance(stored, dict)
    return stored


async def _enroll(agent_db: AgentDB, workflow_id: str, org_id: str, *origins: str) -> Any:
    return await agent_db.workflows.set_browser_action_policy(
        workflow_id=workflow_id,
        organization_id=org_id,
        allowed_origin_urls=list(origins) or ["https://example.com"],
    )


@pytest.mark.asyncio
async def test_a_new_workflow_starts_unenrolled(agent_db: AgentDB, org_id: str) -> None:
    workflow = await agent_db.workflows.create_workflow(
        title="fresh", workflow_definition=EMPTY_DEFINITION, organization_id=org_id
    )
    assert POLICY_KEY not in await _stored_definition(agent_db, workflow.workflow_id)
    assert (
        await agent_db.workflows.get_browser_action_policy(workflow_id=workflow.workflow_id, organization_id=org_id)
        is None
    )


@pytest.mark.asyncio
async def test_control_plane_enrollment_round_trips(agent_db: AgentDB, org_id: str) -> None:
    workflow = await agent_db.workflows.create_workflow(
        title="enrolled", workflow_definition=EMPTY_DEFINITION, organization_id=org_id
    )
    written = await _enroll(agent_db, workflow.workflow_id, org_id, "https://example.com", "https://app.example.com")

    read_back = await agent_db.workflows.get_browser_action_policy(
        workflow_id=workflow.workflow_id, organization_id=org_id
    )
    assert read_back == written
    assert read_back is not None
    assert read_back.owner_id == org_id
    assert {origin.canonical for origin in read_back.allowed_origins} == {
        "https://example.com",
        "https://app.example.com",
    }


@pytest.mark.asyncio
async def test_each_replacement_advances_the_pinned_policy_version(agent_db: AgentDB, org_id: str) -> None:
    workflow = await agent_db.workflows.create_workflow(
        title="versions", workflow_definition=EMPTY_DEFINITION, organization_id=org_id
    )
    first = await _enroll(agent_db, workflow.workflow_id, org_id, "https://example.com")
    second = await _enroll(agent_db, workflow.workflow_id, org_id, "https://other.example.com")
    assert first is not None and second is not None
    assert (first.version, second.version) == (1, 2)


@pytest.mark.asyncio
async def test_clearing_removes_the_policy_and_restarts_the_version(agent_db: AgentDB, org_id: str) -> None:
    workflow = await agent_db.workflows.create_workflow(
        title="cleared", workflow_definition=EMPTY_DEFINITION, organization_id=org_id
    )
    await _enroll(agent_db, workflow.workflow_id, org_id, "https://example.com")

    cleared = await agent_db.workflows.set_browser_action_policy(
        workflow_id=workflow.workflow_id, organization_id=org_id, allowed_origin_urls=None
    )
    assert cleared is None
    assert POLICY_KEY not in await _stored_definition(agent_db, workflow.workflow_id)

    # A clear de-enrolls the version outright, so the next enrollment is a new regime from v1.
    reenrolled = await _enroll(agent_db, workflow.workflow_id, org_id, "https://example.com")
    assert reenrolled is not None and reenrolled.version == 1


@pytest.mark.asyncio
async def test_enrollment_is_scoped_to_the_owning_organization(agent_db: AgentDB, org_id: str) -> None:
    other = await agent_db.organizations.create_organization(organization_name="Other", domain="other.test")
    workflow = await agent_db.workflows.create_workflow(
        title="scoped", workflow_definition=EMPTY_DEFINITION, organization_id=org_id
    )
    with pytest.raises(NotFoundError):
        await agent_db.workflows.set_browser_action_policy(
            workflow_id=workflow.workflow_id,
            organization_id=other.organization_id,
            allowed_origin_urls=["https://example.com"],
        )
    assert POLICY_KEY not in await _stored_definition(agent_db, workflow.workflow_id)


@pytest.mark.asyncio
async def test_an_undeclarable_origin_is_refused_at_enrollment(agent_db: AgentDB, org_id: str) -> None:
    workflow = await agent_db.workflows.create_workflow(
        title="bad-origin", workflow_definition=EMPTY_DEFINITION, organization_id=org_id
    )
    with pytest.raises(ValueError):
        await _enroll(agent_db, workflow.workflow_id, org_id, "file:///etc/passwd")
    assert POLICY_KEY not in await _stored_definition(agent_db, workflow.workflow_id)


@pytest.mark.asyncio
async def test_reading_a_corrupt_stored_policy_raises(agent_db: AgentDB, org_id: str) -> None:
    # Written past the repository — no supported write path can produce this, but a hand-edited or
    # partially-migrated row must fail the run rather than resolve to "unenrolled".
    workflow = await agent_db.workflows.create_workflow(
        title="corrupt", workflow_definition=EMPTY_DEFINITION, organization_id=org_id
    )
    async with agent_db.workflows.Session() as session:
        await session.execute(
            update(WorkflowModel)
            .where(WorkflowModel.workflow_id == workflow.workflow_id)
            .values(workflow_definition={**EMPTY_DEFINITION, POLICY_KEY: {"owner_id": org_id}})
        )
        await session.commit()

    with pytest.raises(ValueError):
        await agent_db.workflows.get_browser_action_policy(workflow_id=workflow.workflow_id, organization_id=org_id)


@pytest.mark.asyncio
async def test_an_ordinary_create_cannot_add_policy(agent_db: AgentDB, org_id: str) -> None:
    # A caller that smuggles the key into the definition dict must not get an enrolled workflow.
    workflow = await agent_db.workflows.create_workflow(
        title="smuggled",
        workflow_definition={
            **EMPTY_DEFINITION,
            POLICY_KEY: serialize_policy(declare_policy(owner_id=org_id, origin_urls=["https://evil.example.com"])),
        },
        organization_id=org_id,
    )
    assert POLICY_KEY not in await _stored_definition(agent_db, workflow.workflow_id)


@pytest.mark.asyncio
async def test_a_new_version_carries_the_policy_forward(agent_db: AgentDB, org_id: str) -> None:
    first = await agent_db.workflows.create_workflow(
        title="v1", workflow_definition=EMPTY_DEFINITION, organization_id=org_id
    )
    policy = await _enroll(agent_db, first.workflow_id, org_id, "https://example.com")

    second = await agent_db.workflows.create_workflow(
        title="v2",
        workflow_definition=EMPTY_DEFINITION,
        organization_id=org_id,
        workflow_permanent_id=first.workflow_permanent_id,
        version=first.version + 1,
    )
    assert (
        await agent_db.workflows.get_browser_action_policy(workflow_id=second.workflow_id, organization_id=org_id)
        == policy
    )


@pytest.mark.asyncio
async def test_a_new_version_cannot_widen_the_carried_policy(agent_db: AgentDB, org_id: str) -> None:
    first = await agent_db.workflows.create_workflow(
        title="v1", workflow_definition=EMPTY_DEFINITION, organization_id=org_id
    )
    await _enroll(agent_db, first.workflow_id, org_id, "https://example.com")

    widened = serialize_policy(
        declare_policy(owner_id=org_id, origin_urls=["https://example.com", "https://evil.example.com"], version=99)
    )
    second = await agent_db.workflows.create_workflow(
        title="v2",
        workflow_definition={**EMPTY_DEFINITION, POLICY_KEY: widened},
        organization_id=org_id,
        workflow_permanent_id=first.workflow_permanent_id,
        version=first.version + 1,
    )
    carried = await agent_db.workflows.get_browser_action_policy(workflow_id=second.workflow_id, organization_id=org_id)
    assert carried is not None
    assert {origin.canonical for origin in carried.allowed_origins} == {"https://example.com"}
    assert carried.version == 1


@pytest.mark.asyncio
async def test_a_new_version_of_an_unenrolled_workflow_stays_unenrolled(agent_db: AgentDB, org_id: str) -> None:
    first = await agent_db.workflows.create_workflow(
        title="v1", workflow_definition=EMPTY_DEFINITION, organization_id=org_id
    )
    second = await agent_db.workflows.create_workflow(
        title="v2",
        workflow_definition=EMPTY_DEFINITION,
        organization_id=org_id,
        workflow_permanent_id=first.workflow_permanent_id,
        version=first.version + 1,
    )
    assert POLICY_KEY not in await _stored_definition(agent_db, second.workflow_id)


@pytest.mark.asyncio
async def test_updating_the_definition_preserves_the_policy(agent_db: AgentDB, org_id: str) -> None:
    workflow = await agent_db.workflows.create_workflow(
        title="edited", workflow_definition=EMPTY_DEFINITION, organization_id=org_id
    )
    policy = await _enroll(agent_db, workflow.workflow_id, org_id, "https://example.com")

    await agent_db.workflows.update_workflow(
        workflow_id=workflow.workflow_id,
        organization_id=org_id,
        workflow_definition={"parameters": [], "blocks": [], "workflow_system_prompt": "edited"},
    )
    stored = await _stored_definition(agent_db, workflow.workflow_id)
    assert stored["workflow_system_prompt"] == "edited"
    assert (
        await agent_db.workflows.get_browser_action_policy(workflow_id=workflow.workflow_id, organization_id=org_id)
        == policy
    )


@pytest.mark.asyncio
async def test_updating_the_definition_cannot_clear_the_policy(agent_db: AgentDB, org_id: str) -> None:
    workflow = await agent_db.workflows.create_workflow(
        title="cleared-by-edit", workflow_definition=EMPTY_DEFINITION, organization_id=org_id
    )
    policy = await _enroll(agent_db, workflow.workflow_id, org_id, "https://example.com")

    await agent_db.workflows.update_workflow(
        workflow_id=workflow.workflow_id, organization_id=org_id, workflow_definition=EMPTY_DEFINITION
    )
    assert (
        await agent_db.workflows.get_browser_action_policy(workflow_id=workflow.workflow_id, organization_id=org_id)
        == policy
    )


@pytest.mark.asyncio
async def test_updating_the_definition_cannot_add_policy(agent_db: AgentDB, org_id: str) -> None:
    workflow = await agent_db.workflows.create_workflow(
        title="added-by-edit", workflow_definition=EMPTY_DEFINITION, organization_id=org_id
    )
    smuggled = serialize_policy(declare_policy(owner_id=org_id, origin_urls=["https://evil.example.com"]))
    await agent_db.workflows.update_workflow(
        workflow_id=workflow.workflow_id,
        organization_id=org_id,
        workflow_definition={**EMPTY_DEFINITION, POLICY_KEY: smuggled},
    )
    assert POLICY_KEY not in await _stored_definition(agent_db, workflow.workflow_id)


@pytest.mark.asyncio
async def test_the_reconciling_definition_write_preserves_the_policy(agent_db: AgentDB, org_id: str) -> None:
    workflow = await agent_db.workflows.create_workflow(
        title="reconciled", workflow_definition=EMPTY_DEFINITION, organization_id=org_id
    )
    policy = await _enroll(agent_db, workflow.workflow_id, org_id, "https://example.com")

    await agent_db.workflows.update_workflow_and_reconcile_definition_params(
        workflow_id=workflow.workflow_id,
        organization_id=org_id,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=[], workflow_system_prompt="reconciled"),
    )
    stored = await _stored_definition(agent_db, workflow.workflow_id)
    assert stored["workflow_system_prompt"] == "reconciled"
    assert (
        await agent_db.workflows.get_browser_action_policy(workflow_id=workflow.workflow_id, organization_id=org_id)
        == policy
    )


@pytest.mark.asyncio
async def test_the_policy_is_invisible_to_the_workflow_model(agent_db: AgentDB, org_id: str) -> None:
    # Keeping the key out of WorkflowDefinition is what keeps it off the generated public client.
    workflow = await agent_db.workflows.create_workflow(
        title="hidden", workflow_definition=EMPTY_DEFINITION, organization_id=org_id
    )
    await _enroll(agent_db, workflow.workflow_id, org_id, "https://example.com")

    reloaded = await agent_db.workflows.get_workflow(workflow_id=workflow.workflow_id, organization_id=org_id)
    assert reloaded is not None
    assert POLICY_KEY not in reloaded.workflow_definition.model_dump(mode="json")
    assert POLICY_KEY not in reloaded.model_dump(mode="json")


@pytest.mark.asyncio
async def test_a_definition_round_trip_through_the_pydantic_model_cannot_erase_the_policy(
    agent_db: AgentDB, org_id: str
) -> None:
    """The reserved key is invisible to WorkflowDefinition, which is what keeps it off the public
    client — and would silently erase it on any read-modify-write if the repository did not restore
    it. This is the erasure half of "cannot add, clear or widen"."""
    workflow = await agent_db.workflows.create_workflow(
        title="round-trip", workflow_definition=EMPTY_DEFINITION, organization_id=org_id
    )
    policy = await _enroll(agent_db, workflow.workflow_id, org_id, "https://example.com")

    reloaded = await agent_db.workflows.get_workflow(workflow_id=workflow.workflow_id, organization_id=org_id)
    assert reloaded is not None
    # The model genuinely drops the key — this is the erasure the repository has to undo.
    round_tripped = WorkflowDefinition.model_validate(reloaded.workflow_definition.model_dump(mode="json"))
    assert POLICY_KEY not in round_tripped.model_dump(mode="json")

    await agent_db.workflows.update_workflow_and_reconcile_definition_params(
        workflow_id=workflow.workflow_id, organization_id=org_id, workflow_definition=round_tripped
    )
    assert (
        await agent_db.workflows.get_browser_action_policy(workflow_id=workflow.workflow_id, organization_id=org_id)
        == policy
    )


@pytest.mark.asyncio
async def test_the_secret_backfill_transform_preserves_the_policy(agent_db: AgentDB, org_id: str) -> None:
    """The one definition writer outside this repository. It mutates a deepcopy of the raw dict
    rather than rebuilding it, so the reserved key survives; this pins that."""
    stored = serialize_policy(declare_policy(owner_id=org_id, origin_urls=["https://example.com"]))
    definition = {
        "parameters": [],
        "blocks": [{"block_type": "file_upload", "label": "upload", "path": "/tmp/x"}],
        POLICY_KEY: stored,
    }
    transformed, _ = await encrypt_file_block_secrets(definition, org_id)
    assert transformed[POLICY_KEY] == stored


@pytest.mark.asyncio
async def test_a_new_version_inherits_from_the_most_recent_version_even_if_it_was_deleted(
    agent_db: AgentDB, org_id: str
) -> None:
    """Carry-forward reads the highest version including soft-deleted rows, on purpose.

    `create_workflow_from_request` numbers the new version off the same row set
    (`filter_deleted=False`), so skipping deleted rows here would let deleting the enrolled version
    silently unenroll the next save — a clear, which no ordinary save is allowed to perform.
    """
    first = await agent_db.workflows.create_workflow(
        title="v1", workflow_definition=EMPTY_DEFINITION, organization_id=org_id
    )
    second = await agent_db.workflows.create_workflow(
        title="v2",
        workflow_definition=EMPTY_DEFINITION,
        organization_id=org_id,
        workflow_permanent_id=first.workflow_permanent_id,
        version=first.version + 1,
    )
    policy = await _enroll(agent_db, second.workflow_id, org_id, "https://example.com")
    await agent_db.workflows.soft_delete_workflow_by_id(workflow_id=second.workflow_id, organization_id=org_id)

    third = await agent_db.workflows.create_workflow(
        title="v3",
        workflow_definition=EMPTY_DEFINITION,
        organization_id=org_id,
        workflow_permanent_id=first.workflow_permanent_id,
        version=second.version + 1,
    )
    assert (
        await agent_db.workflows.get_browser_action_policy(workflow_id=third.workflow_id, organization_id=org_id)
        == policy
    )
