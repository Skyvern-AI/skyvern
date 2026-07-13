"""Regression tests for the cache-invalidation batching on workflow save.

Saving a large workflow timed out because cache invalidation walked every cached
``WorkflowScript`` for the wpid issuing two sequential DB queries per candidate
(an N+1). These tests pin three guarantees:

- the partitioning result (published vs draft buckets, and which blocks get
  cleared) is unchanged,
- the number of DB round-trips is constant, independent of candidate count, and
- the dedup/chunking helper behind the batch repository queries splits inputs
  correctly so no single ``IN (...)`` clause grows unbounded.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from skyvern.forge import app
from skyvern.schemas.scripts import Script, ScriptBlock, ScriptStatus, WorkflowScript

ORG_ID = "o_test_partition"
WPID = "wpid_test_partition"


class FakeScriptsDB:
    """Stand-in for ``app.DATABASE.scripts`` that records every round-trip.

    Implements both the legacy per-item methods and the batch methods so the
    same fixture works against the N+1 code path and the batched fix.
    """

    def __init__(self, scripts_by_id: dict[str, Script], blocks_by_revision: dict[str, list[ScriptBlock]]) -> None:
        self._scripts_by_id = scripts_by_id
        self._blocks_by_revision = blocks_by_revision
        self.call_log: list[str] = []
        self.cleared_script_block_ids: list[str] = []
        self.update_script_block_calls: list[str] = []

    async def get_script(self, script_id: str, organization_id: str, version: int | None = None) -> Script | None:
        self.call_log.append("get_script")
        return self._scripts_by_id.get(script_id)

    async def get_script_blocks_by_script_revision_id(
        self, script_revision_id: str, organization_id: str
    ) -> list[ScriptBlock]:
        self.call_log.append("get_script_blocks_by_script_revision_id")
        return self._blocks_by_revision.get(script_revision_id, [])

    async def get_latest_scripts_by_ids(self, organization_id: str, script_ids: list[str]) -> dict[str, Script]:
        self.call_log.append("get_latest_scripts_by_ids")
        return {sid: self._scripts_by_id[sid] for sid in script_ids if sid in self._scripts_by_id}

    async def get_script_blocks_by_script_revision_ids(
        self, organization_id: str, script_revision_ids: list[str]
    ) -> dict[str, list[ScriptBlock]]:
        self.call_log.append("get_script_blocks_by_script_revision_ids")
        # Mirror the real contract: revisions with no blocks are absent from the result.
        return {
            rid: self._blocks_by_revision[rid]
            for rid in dict.fromkeys(script_revision_ids)
            if self._blocks_by_revision.get(rid)
        }

    async def update_script_block(
        self,
        script_block_id: str,
        organization_id: str,
        clear_run_signature: bool = False,
    ) -> ScriptBlock | None:
        self.call_log.append("update_script_block")
        self.update_script_block_calls.append(script_block_id)
        return None

    async def clear_script_block_run_signatures(
        self,
        *,
        organization_id: str,
        script_block_ids: list[str],
    ) -> int:
        self.call_log.append("clear_script_block_run_signatures")
        self.cleared_script_block_ids.extend(script_block_ids)
        return len(script_block_ids)


def _now() -> datetime:
    return datetime(2026, 6, 18, 0, 0, 0)


def _candidate(script_id: str, status: ScriptStatus) -> WorkflowScript:
    return WorkflowScript(
        workflow_script_id=f"ws_{script_id}",
        organization_id=ORG_ID,
        script_id=script_id,
        workflow_permanent_id=WPID,
        cache_key="default",
        cache_key_value=f"default-{script_id}",
        status=status,
        created_at=_now(),
        modified_at=_now(),
    )


def _script(script_id: str, revision_id: str) -> Script:
    return Script(
        script_revision_id=revision_id,
        script_id=script_id,
        organization_id=ORG_ID,
        version=1,
        created_at=_now(),
        modified_at=_now(),
    )


def _block(revision_id: str, label: str, run_signature: str | None) -> ScriptBlock:
    return ScriptBlock(
        script_block_id=f"sb_{revision_id}_{label}",
        organization_id=ORG_ID,
        script_id=f"s_{revision_id}",
        script_revision_id=revision_id,
        script_block_label=label,
        run_signature=run_signature,
        created_at=_now(),
        modified_at=_now(),
    )


def _build_fixture() -> tuple[list[WorkflowScript], FakeScriptsDB]:
    # c1: published, has a target block with a run_signature -> cleared (published bucket)
    # c2: pending, target block but no run_signature -> nothing to clear, skipped
    # c3: pending, has a different target block -> cleared (draft bucket)
    # c4: published, no blocks -> skipped
    # c5: published, script row missing entirely -> skipped
    candidates = [
        _candidate("s1", ScriptStatus.published),
        _candidate("s2", ScriptStatus.pending),
        _candidate("s3", ScriptStatus.pending),
        _candidate("s4", ScriptStatus.published),
        _candidate("s5", ScriptStatus.published),
    ]
    scripts_by_id = {
        "s1": _script("s1", "r1"),
        "s2": _script("s2", "r2"),
        "s3": _script("s3", "r3"),
        "s4": _script("s4", "r4"),
        # s5 intentionally absent
    }
    blocks_by_revision = {
        "r1": [
            _block("r1", "block_a", "sig_a"),  # target + signature -> clear
            _block("r1", "block_x", "sig_x"),  # not a target -> keep
        ],
        "r2": [_block("r2", "block_a", None)],  # target but no signature -> not cleared
        "r3": [_block("r3", "block_b", "sig_b")],  # target + signature -> clear
        "r4": [],
    }
    return candidates, FakeScriptsDB(scripts_by_id, blocks_by_revision)


@pytest.mark.asyncio
async def test_partition_cached_blocks_preserves_partitioning(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.workflow.service import WorkflowService

    candidates, fake = _build_fixture()
    monkeypatch.setattr(app.DATABASE, "scripts", fake)

    svc = WorkflowService()
    cached_groups, published_groups = await svc._partition_cached_blocks(
        organization_id=ORG_ID,
        candidates=candidates,
        block_labels_to_disable=["block_a", "block_b"],
    )

    published_ids = {g.workflow_script.script_id for g in published_groups}
    cached_ids = {g.workflow_script.script_id for g in cached_groups}
    assert published_ids == {"s1"}
    assert cached_ids == {"s3"}

    s1_group = next(g for g in published_groups if g.workflow_script.script_id == "s1")
    assert [b.script_block_label for b in s1_group.blocks_to_clear] == ["block_a"]

    s3_group = next(g for g in cached_groups if g.workflow_script.script_id == "s3")
    assert [b.script_block_label for b in s3_group.blocks_to_clear] == ["block_b"]


@pytest.mark.asyncio
async def test_partition_cached_blocks_uses_constant_query_count(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.workflow.service import WorkflowService

    candidates, fake = _build_fixture()
    monkeypatch.setattr(app.DATABASE, "scripts", fake)

    svc = WorkflowService()
    await svc._partition_cached_blocks(
        organization_id=ORG_ID,
        candidates=candidates,
        block_labels_to_disable=["block_a", "block_b"],
    )

    # Five candidates must not produce a per-candidate fan-out of queries.
    # The batched implementation makes at most one scripts query + one blocks query.
    assert len(fake.call_log) <= 2, f"expected constant query budget, got {fake.call_log}"


@pytest.mark.asyncio
async def test_partition_cached_blocks_dedupes_duplicate_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.workflow.service import WorkflowService

    _, fake = _build_fixture()
    candidates = [
        _candidate("s1", ScriptStatus.published),
        _candidate("s1", ScriptStatus.published),
        _candidate("s3", ScriptStatus.pending),
        _candidate("s3", ScriptStatus.pending),
    ]
    monkeypatch.setattr(app.DATABASE, "scripts", fake)

    svc = WorkflowService()
    cached_groups, published_groups = await svc._partition_cached_blocks(
        organization_id=ORG_ID,
        candidates=candidates,
        block_labels_to_disable=["block_a", "block_b"],
    )

    assert [group.workflow_script.script_id for group in published_groups] == ["s1"]
    assert [group.workflow_script.script_id for group in cached_groups] == ["s3"]
    assert len(fake.call_log) <= 2, f"expected constant query budget, got {fake.call_log}"


@pytest.mark.asyncio
async def test_clear_cached_block_groups_bulk_clears_deduped_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.workflow.service import CachedScriptBlocks, CacheInvalidationPlan, WorkflowService

    _, fake = _build_fixture()
    monkeypatch.setattr(app.DATABASE, "scripts", fake)

    script = fake._scripts_by_id["s1"]
    block = fake._blocks_by_revision["r1"][0]
    groups = [
        CachedScriptBlocks(
            workflow_script=_candidate("s1", ScriptStatus.published),
            script=script,
            blocks_to_clear=[block, block],
        ),
        CachedScriptBlocks(
            workflow_script=_candidate("s1", ScriptStatus.published),
            script=script,
            blocks_to_clear=[block],
        ),
    ]

    svc = WorkflowService()
    await svc._clear_cached_block_groups(
        organization_id=ORG_ID,
        workflow=SimpleNamespace(
            workflow_id="wf_new",
            workflow_permanent_id=WPID,
            organization_id=ORG_ID,
            version=2,
        ),
        previous_workflow=SimpleNamespace(
            workflow_id="wf_previous",
            workflow_permanent_id=WPID,
            organization_id=ORG_ID,
            version=1,
        ),
        plan=CacheInvalidationPlan(
            reason="updated_block",
            label="block_a",
            previous_index=0,
            new_index=0,
            block_labels_to_disable=["block_a"],
        ),
        groups=groups,
    )

    assert fake.call_log == ["clear_script_block_run_signatures"]
    assert fake.cleared_script_block_ids == [block.script_block_id]
    assert fake.update_script_block_calls == []


def test_dedup_into_chunks_preserves_order_and_dedups() -> None:
    from skyvern.forge.sdk.db.repositories.scripts import _dedup_into_chunks

    assert _dedup_into_chunks(["b", "a", "b", "c", "a"]) == [["b", "a", "c"]]


def test_dedup_into_chunks_splits_at_chunk_size() -> None:
    from skyvern.forge.sdk.db.repositories.scripts import _dedup_into_chunks

    ids = [f"id_{i}" for i in range(1001)]
    chunks = _dedup_into_chunks(ids, chunk_size=500)

    assert [len(chunk) for chunk in chunks] == [500, 500, 1]
    assert [item for chunk in chunks for item in chunk] == ids


def test_dedup_into_chunks_dedups_across_chunk_boundary() -> None:
    from skyvern.forge.sdk.db.repositories.scripts import _dedup_into_chunks

    ids = [f"id_{i}" for i in range(600)] * 2  # full duplicate set spanning the 500 boundary
    chunks = _dedup_into_chunks(ids, chunk_size=500)

    assert [len(chunk) for chunk in chunks] == [500, 100]


def test_dedup_into_chunks_empty() -> None:
    from skyvern.forge.sdk.db.repositories.scripts import _dedup_into_chunks

    assert _dedup_into_chunks([]) == []
