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
    ReliabilityState,
    RunHealGroup,
    compute_workflow_reliability,
    reliability_state_transition,
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
    assert len(heads) == 1

    # Revision hashes differ between this repo and the OSS mirror (migrations are
    # regenerated on sync), so locate the migration by its docstring title instead.
    heal_revisions = [
        rev for rev in script.walk_revisions() if rev.doc == "add heal episode workflow block label index"
    ]
    assert len(heal_revisions) == 1
    revision = heal_revisions[0]
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


def _run(workflow_run_id: str, episodes: list[HealEpisode]) -> RunHealGroup:
    return RunHealGroup(workflow_run_id=workflow_run_id, episodes=episodes)


def _episode_for_run(
    *,
    workflow_run_id: str,
    block_label: str,
    status: HealStatus,
    output_obligation: OutputObligation | None = None,
    engine: str = "code",
) -> HealEpisode:
    now = datetime(2026, 1, 1, 0, 0, 0)
    return HealEpisode(
        heal_episode_id=f"he_{workflow_run_id}_{block_label}",
        organization_id="o_1",
        workflow_permanent_id="wpid_1",
        workflow_id="w_1",
        workflow_run_id=workflow_run_id,
        workflow_run_block_id=f"wrb_{workflow_run_id}_{block_label}",
        block_label=block_label,
        engine=engine,
        status=status,
        output_obligation=output_obligation,
        created_at=now,
        modified_at=now,
    )


@pytest.mark.parametrize(
    ("episodes", "expected"),
    [
        ([HealStatus.fired_completed, HealStatus.fired_failed], "healed"),
        ([HealStatus.fired_unverified, HealStatus.fired_failed], "failed"),
        ([HealStatus.fired_unverified], "unverified"),
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


def test_compute_workflow_reliability_below_min_runs_is_unscored_healthy() -> None:
    runs = [_run(f"wr_{idx}", []) for idx in range(9)]

    reliability = compute_workflow_reliability(runs)

    assert reliability.scored is False
    assert reliability.state == ReliabilityState.healthy
    assert reliability.window_runs == 9
    assert reliability.healed_runs == 0
    assert reliability.heal_rate == 0.0
    assert reliability.consecutive_healed_runs == 0
    assert reliability.floor_runs == 0
    assert reliability.outcome_risk is False
    assert reliability.outcome_risk_runs == 0


def test_compute_workflow_reliability_watch_when_two_healed_in_window() -> None:
    runs = [_run(f"wr_{idx}", []) for idx in range(20)]
    runs[5] = _run(
        "wr_5",
        [_episode_for_run(workflow_run_id="wr_5", block_label="block_5", status=HealStatus.fired_failed)],
    )
    runs[12] = _run(
        "wr_12",
        [_episode_for_run(workflow_run_id="wr_12", block_label="block_12", status=HealStatus.fired_completed)],
    )

    reliability = compute_workflow_reliability(runs)

    assert reliability.scored is True
    assert reliability.state == ReliabilityState.watch
    assert reliability.healed_runs == 2
    assert reliability.consecutive_healed_runs == 0
    assert reliability.heal_rate == 0.1


def test_compute_workflow_reliability_watch_when_two_consecutive_recent_healed() -> None:
    runs = [_run(f"wr_{idx}", []) for idx in range(20)]
    runs[0] = _run(
        "wr_0",
        [_episode_for_run(workflow_run_id="wr_0", block_label="block_0", status=HealStatus.fired_unverified)],
    )
    runs[1] = _run(
        "wr_1",
        [_episode_for_run(workflow_run_id="wr_1", block_label="block_1", status=HealStatus.fired_failed)],
    )

    reliability = compute_workflow_reliability(runs)

    assert reliability.state == ReliabilityState.watch
    assert reliability.healed_runs == 2
    assert reliability.consecutive_healed_runs == 2


def test_compute_workflow_reliability_action_needed_when_three_recent_healed() -> None:
    runs = [_run(f"wr_{idx}", []) for idx in range(20)]
    runs[0] = _run(
        "wr_0",
        [_episode_for_run(workflow_run_id="wr_0", block_label="block_0", status=HealStatus.fired_failed)],
    )
    runs[3] = _run(
        "wr_3",
        [_episode_for_run(workflow_run_id="wr_3", block_label="block_3", status=HealStatus.fired_completed)],
    )
    runs[7] = _run(
        "wr_7",
        [_episode_for_run(workflow_run_id="wr_7", block_label="block_7", status=HealStatus.fired_unverified)],
    )

    reliability = compute_workflow_reliability(runs)

    assert reliability.state == ReliabilityState.action_needed
    assert reliability.healed_runs == 3
    assert reliability.heal_rate == 0.15


def test_compute_workflow_reliability_action_needed_when_heal_rate_reaches_threshold() -> None:
    runs = [_run(f"wr_{idx}", []) for idx in range(20)]
    for idx in (0, 5, 12, 18):
        runs[idx] = _run(
            f"wr_{idx}",
            [_episode_for_run(workflow_run_id=f"wr_{idx}", block_label=f"block_{idx}", status=HealStatus.fired_failed)],
        )

    reliability = compute_workflow_reliability(runs)

    assert reliability.state == ReliabilityState.action_needed
    assert reliability.healed_runs == 4
    assert reliability.heal_rate == 0.2


def test_compute_workflow_reliability_action_needed_when_floor_runs_reach_threshold() -> None:
    runs = [_run(f"wr_{idx}", []) for idx in range(20)]
    runs[2] = _run(
        "wr_2",
        [
            _episode_for_run(
                workflow_run_id="wr_2",
                block_label="block_2",
                status=HealStatus.skipped,
                engine="floor",
            )
        ],
    )
    runs[13] = _run(
        "wr_13",
        [
            _episode_for_run(
                workflow_run_id="wr_13",
                block_label="block_13",
                status=HealStatus.skipped,
                engine="floor",
            )
        ],
    )

    reliability = compute_workflow_reliability(runs)

    assert reliability.state == ReliabilityState.action_needed
    assert reliability.floor_runs == 2
    assert reliability.healed_runs == 0


def test_compute_workflow_reliability_sets_outcome_risk_from_recent_runs() -> None:
    runs = [_run(f"wr_{idx}", []) for idx in range(10)]
    runs[0] = _run(
        "wr_0",
        [
            _episode_for_run(
                workflow_run_id="wr_0",
                block_label="invoice_submit",
                status=HealStatus.fired_unverified,
                output_obligation=OutputObligation.observed,
            )
        ],
    )

    reliability = compute_workflow_reliability(runs)

    assert reliability.outcome_risk is True
    assert reliability.outcome_risk_runs == 1
    assert reliability.state == ReliabilityState.healthy


def test_compute_workflow_reliability_consecutive_streak_stops_at_first_unhealed() -> None:
    runs = [_run(f"wr_{idx}", []) for idx in range(20)]
    runs[0] = _run(
        "wr_0",
        [_episode_for_run(workflow_run_id="wr_0", block_label="block_0", status=HealStatus.fired_completed)],
    )
    runs[1] = _run(
        "wr_1",
        [_episode_for_run(workflow_run_id="wr_1", block_label="block_1", status=HealStatus.fired_failed)],
    )
    runs[3] = _run(
        "wr_3",
        [_episode_for_run(workflow_run_id="wr_3", block_label="block_3", status=HealStatus.fired_failed)],
    )

    reliability = compute_workflow_reliability(runs)

    assert reliability.consecutive_healed_runs == 2
    assert reliability.healed_runs == 3
    assert reliability.state == ReliabilityState.action_needed


def test_compute_workflow_reliability_heal_rate_math() -> None:
    runs = [
        _run("wr_0", [_episode_for_run(workflow_run_id="wr_0", block_label="block_0", status=HealStatus.fired_failed)]),
        _run("wr_1", []),
        _run("wr_2", []),
    ]

    reliability = compute_workflow_reliability(runs)

    assert reliability.healed_runs == 1
    assert reliability.window_runs == 3
    assert reliability.heal_rate == pytest.approx(1 / 3)


def test_reliability_state_transition() -> None:
    assert reliability_state_transition(None, ReliabilityState.healthy) is True
    assert reliability_state_transition(ReliabilityState.watch, ReliabilityState.watch) is False
    assert reliability_state_transition(ReliabilityState.watch, ReliabilityState.action_needed) is True
