from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from alembic.config import Config
from alembic.script import ScriptDirectory
from skyvern.forge.sdk.db.id import generate_heal_episode_id, generate_heal_proposal_id
from skyvern.forge.sdk.db.models import HealEpisodeModel, WorkflowHealProposalModel
from skyvern.schemas.self_heal import (
    HealEpisode,
    HealEpisodeDetail,
    HealEpisodeView,
    HealStatus,
    OutputObligation,
    resolve_block_outcome,
    summarize_run_heals,
)


def test_heal_episode_and_heal_proposal_id_prefixes() -> None:
    assert generate_heal_episode_id().startswith("he_")
    assert generate_heal_proposal_id().startswith("hp_")


def test_heal_episode_and_heal_proposal_model_shapes() -> None:
    assert HealEpisodeModel.__tablename__ == "heal_episodes"
    assert WorkflowHealProposalModel.__tablename__ == "workflow_heal_proposals"
    heal_id_default = HealEpisodeModel.heal_episode_id.default.arg
    heal_proposal_id_default = WorkflowHealProposalModel.heal_proposal_id.default.arg
    assert callable(heal_id_default)
    assert callable(heal_proposal_id_default)
    assert heal_id_default.__name__ == "generate_heal_episode_id"
    assert heal_proposal_id_default.__name__ == "generate_heal_proposal_id"


def test_heal_migration_chains_from_current_head() -> None:
    repo_root = Path(__file__).resolve().parents[5]
    alembic_ini_path = repo_root / "alembic.ini"
    script_location = repo_root / "alembic"
    if not alembic_ini_path.exists() or not script_location.exists():
        pytest.skip("alembic migrations are not part of this checkout")

    config = Config(str(alembic_ini_path))
    config.set_main_option("script_location", str(script_location))
    script = ScriptDirectory.from_config(config)

    heads = script.get_heads()
    assert heads == ["3b9d7a4c1e2f"]

    revision = script.get_revision("3b9d7a4c1e2f")
    assert revision is not None
    # down_revision is re-pointed to main's head on every rebase (migration-rebase policy);
    # assert the parent resolves to a real revision instead of pinning a specific hash.
    assert revision.down_revision is not None
    assert script.get_revision(revision.down_revision) is not None


def _episode(
    *,
    block_label: str,
    status: HealStatus,
    output_obligation: OutputObligation | None = None,
    workflow_run_block_id: str = "wrb_1",
) -> HealEpisode:
    now = datetime(2026, 1, 1, 0, 0, 0)
    return HealEpisode(
        heal_episode_id=f"he_{block_label}_{workflow_run_block_id}",
        organization_id="o_1",
        workflow_permanent_id="wpid_1",
        workflow_id="w_1",
        workflow_run_id="wr_1",
        workflow_run_block_id=workflow_run_block_id,
        block_label=block_label,
        engine="code",
        status=status,
        output_obligation=output_obligation,
        created_at=now,
        modified_at=now,
    )


@pytest.mark.parametrize(
    ("episodes", "expected"),
    [
        ([HealStatus.fired_completed, HealStatus.fired_failed], "healed"),
        ([HealStatus.fired_unverified, HealStatus.skipped], "unverified"),
        ([HealStatus.fired_failed, HealStatus.skipped], "failed"),
        ([HealStatus.skipped], "skipped"),
        ([], "none"),
    ],
)
def test_resolve_block_outcome_precedence(episodes: list[HealStatus], expected: str) -> None:
    assert resolve_block_outcome([_episode(block_label="block", status=status) for status in episodes]) == expected


def test_summarize_run_heals_counts_and_risk_with_obligation() -> None:
    summary = summarize_run_heals(
        [
            _episode(block_label="block_healed", status=HealStatus.fired_completed),
            _episode(
                block_label="block_unverified_risk",
                status=HealStatus.fired_unverified,
                output_obligation=OutputObligation.observed,
            ),
            _episode(
                block_label="block_failed_no_obligation",
                status=HealStatus.fired_failed,
                output_obligation=OutputObligation.none,
            ),
            _episode(
                block_label="block_failed_risk",
                status=HealStatus.fired_failed,
                output_obligation=OutputObligation.vestigial,
            ),
        ]
    )

    assert summary.blocks_healed == 1
    assert summary.blocks_with_heal_attempt == 4
    assert summary.blocks_outcome_risk == ["block_failed_risk", "block_unverified_risk"]


def test_heal_episode_view_excludes_secret_fields() -> None:
    forbidden = {"block_prompt", "block_code", "failure_message"}
    assert forbidden.isdisjoint(HealEpisodeView.model_fields.keys())


def test_heal_episode_detail_exposes_only_sanitized_free_text_fields() -> None:
    fields = HealEpisodeDetail.model_fields
    assert "sanitized_block_code" in fields
    assert "sanitized_block_prompt" in fields
    assert "sanitized_failure_message" in fields
    assert "block_code" not in fields
    assert "block_prompt" not in fields
    assert "failure_message" not in fields
