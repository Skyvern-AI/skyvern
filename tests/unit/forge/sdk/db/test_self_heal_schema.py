from __future__ import annotations

import ast
from pathlib import Path

import pytest

from skyvern.forge.sdk.db.id import generate_heal_episode_id, generate_heal_proposal_id
from skyvern.forge.sdk.db.models import HealEpisodeModel, WorkflowHealProposalModel


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
    # alembic/ does not sync to the OSS repo; skip there instead of failing on the missing file.
    migration_path = (
        Path(__file__).resolve().parents[5]
        / "alembic"
        / "versions"
        / "2026_07_08_0935-a1c4d9e7b2f1_add_heal_episodes_and_proposals.py"
    )
    if not migration_path.exists():
        pytest.skip("alembic migrations are not part of this checkout")
    module = ast.parse(migration_path.read_text())
    down_revisions = [
        node.value.value
        for node in module.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "down_revision"
        and isinstance(node.value, ast.Constant)
    ]

    assert down_revisions == ["5a7c9d1e2f34"]
