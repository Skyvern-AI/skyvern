from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import (
    BrowserProfileModel,
    CredentialModel,
    WorkflowModel,
    WorkflowRunModel,
)

ORG = "o_usage_test"


@pytest_asyncio.fixture
async def db(sqlite_engine: AsyncEngine) -> AgentDB:
    return AgentDB("sqlite+aiosqlite://", db_engine=sqlite_engine)


async def _add(db: AgentDB, *rows: object) -> None:
    async with db.Session() as session:
        session.add_all(list(rows))
        await session.commit()


def _profile(profile_id: str = "bp_target") -> BrowserProfileModel:
    return BrowserProfileModel(browser_profile_id=profile_id, organization_id=ORG, name="Target profile")


def _workflow(*, wpid: str, version: int, title: str, browser_profile_id: str | None) -> WorkflowModel:
    return WorkflowModel(
        workflow_id=f"w_{wpid}_v{version}",
        organization_id=ORG,
        title=title,
        workflow_definition={},
        workflow_permanent_id=wpid,
        version=version,
        browser_profile_id=browser_profile_id,
    )


def _run(*, wpid: str, browser_profile_id: str, created_at: datetime) -> WorkflowRunModel:
    return WorkflowRunModel(
        workflow_id=f"w_{wpid}_v1",
        workflow_permanent_id=wpid,
        organization_id=ORG,
        status="completed",
        browser_profile_id=browser_profile_id,
        created_at=created_at,
    )


def _credential(*, credential_id: str, name: str, browser_profile_id: str | None) -> CredentialModel:
    return CredentialModel(
        credential_id=credential_id,
        organization_id=ORG,
        item_id=f"it_{credential_id}",
        name=name,
        credential_type="password",
        browser_profile_id=browser_profile_id,
    )


@pytest.mark.asyncio
async def test_usage_reports_pinned_workflows_credentials_and_recent_runs(db: AgentDB) -> None:
    now = datetime.utcnow()
    await _add(
        db,
        _profile(),
        _workflow(wpid="wpid_checkout", version=1, title="Checkout", browser_profile_id="bp_target"),
        _credential(credential_id="cred_bank", name="Bank Login", browser_profile_id="bp_target"),
        _run(wpid="wpid_checkout", browser_profile_id="bp_target", created_at=now),
        _run(wpid="wpid_checkout", browser_profile_id="bp_target", created_at=now - timedelta(days=40)),
    )

    usage = await db.browser_sessions.get_browser_profile_usage("bp_target", ORG)

    assert [(w.workflow_permanent_id, w.title, w.via) for w in usage.workflows] == [
        ("wpid_checkout", "Checkout", "browser_profile_id")
    ]
    assert [(c.credential_id, c.name) for c in usage.credentials] == [("cred_bank", "Bank Login")]
    assert usage.recent_seeded_run_count == 1  # the 40-day-old run is outside the window


@pytest.mark.asyncio
async def test_usage_only_counts_latest_workflow_version(db: AgentDB) -> None:
    await _add(
        db,
        _profile(),
        # latest version UNPINS the profile -> workflow should not be reported
        _workflow(wpid="wpid_unpinned", version=1, title="v1", browser_profile_id="bp_target"),
        _workflow(wpid="wpid_unpinned", version=2, title="v2", browser_profile_id=None),
        # latest version pins it, with the newest title
        _workflow(wpid="wpid_pinned", version=1, title="old title", browser_profile_id=None),
        _workflow(wpid="wpid_pinned", version=2, title="new title", browser_profile_id="bp_target"),
    )

    usage = await db.browser_sessions.get_browser_profile_usage("bp_target", ORG)

    assert [(w.workflow_permanent_id, w.title) for w in usage.workflows] == [("wpid_pinned", "new title")]


@pytest.mark.asyncio
async def test_usage_empty_for_unreferenced_profile(db: AgentDB) -> None:
    await _add(db, _profile())

    usage = await db.browser_sessions.get_browser_profile_usage("bp_target", ORG)

    assert usage.workflows == []
    assert usage.credentials == []
    assert usage.recent_seeded_run_count == 0


@pytest.mark.asyncio
async def test_delete_detaches_linked_credentials_atomically(db: AgentDB) -> None:
    await _add(
        db,
        _profile(),
        _credential(credential_id="cred_a", name="A", browser_profile_id="bp_target"),
        _credential(credential_id="cred_b", name="B", browser_profile_id="bp_target"),
        _credential(credential_id="cred_other", name="Other", browser_profile_id="bp_other"),
    )

    cleared = await db.browser_sessions.delete_browser_profile("bp_target", ORG)

    assert sorted(cleared) == ["cred_a", "cred_b"]
    # Same transaction as the soft-delete, so no dangling link can survive a mid-failure.
    assert await db.browser_sessions.get_browser_profile("bp_target", ORG) is None
    assert (await db.credentials.get_credential("cred_a", ORG)).browser_profile_id is None
    assert (await db.credentials.get_credential("cred_other", ORG)).browser_profile_id == "bp_other"


@pytest.mark.asyncio
async def test_list_batch_populates_linked_credential_name(db: AgentDB) -> None:
    # The list endpoint enriches rows with the linking credential name in one batched query, so the UI
    # can render the credential-login role without a per-row usage fetch.
    await _add(
        db,
        BrowserProfileModel(browser_profile_id="bp_cred", organization_id=ORG, name="Credential profile"),
        BrowserProfileModel(browser_profile_id="bp_plain", organization_id=ORG, name="Plain profile"),
        _credential(credential_id="cred_x", name="Bank portal", browser_profile_id="bp_cred"),
    )

    profiles = await db.browser_sessions.list_browser_profiles(organization_id=ORG, page_size=50)
    by_id = {p.browser_profile_id: p for p in profiles}

    assert by_id["bp_cred"].linked_credential_name == "Bank portal"
    assert by_id["bp_plain"].linked_credential_name is None
