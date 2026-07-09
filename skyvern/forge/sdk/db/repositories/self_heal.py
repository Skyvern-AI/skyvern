from __future__ import annotations

from typing import Literal

from sqlalchemy import select, update

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now
from skyvern.forge.sdk.db.models import HealEpisodeModel, WorkflowHealProposalModel
from skyvern.forge.sdk.utils.sanitization import sanitize_postgres_text
from skyvern.schemas.self_heal import HealEpisode, HealSkipReason, HealStatus, OutputObligation, WorkflowHealProposal


class SelfHealRepository(BaseRepository):
    @db_operation("create_heal_episode")
    async def create_heal_episode(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        workflow_id: str,
        workflow_run_id: str,
        workflow_run_block_id: str,
        block_label: str,
        engine: str,
        status: HealStatus,
        skip_reason: HealSkipReason | None = None,
        block_prompt: str | None = None,
        block_code: str | None = None,
        block_steps: list | dict | None = None,
        snapshot_available: bool = False,
        convergence_eligible: bool = False,
        parameter_binding_keys: list[str] | dict | None = None,
        exception_class: str | None = None,
        failing_line: int | None = None,
        matched_step_index: int | None = None,
        failure_message: str | None = None,
        escalation_task_id: str | None = None,
        wall_clock_ms: int | None = None,
        action_count: int | None = None,
        output_obligation: OutputObligation | None = None,
        dom_snapshot_artifact_id: str | None = None,
        scout_transcript_artifact_id: str | None = None,
        screenshot_artifact_id: str | None = None,
    ) -> HealEpisode:
        async with self.Session() as session:
            episode = HealEpisodeModel(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                block_label=block_label,
                engine=engine,
                status=status,
                skip_reason=skip_reason,
                block_prompt=sanitize_postgres_text(block_prompt) if block_prompt else None,
                block_code=sanitize_postgres_text(block_code) if block_code else None,
                block_steps=block_steps,
                snapshot_available=snapshot_available,
                convergence_eligible=convergence_eligible,
                parameter_binding_keys=parameter_binding_keys,
                exception_class=exception_class,
                failing_line=failing_line,
                matched_step_index=matched_step_index,
                failure_message=sanitize_postgres_text(failure_message) if failure_message else None,
                escalation_task_id=escalation_task_id,
                wall_clock_ms=wall_clock_ms,
                action_count=action_count,
                output_obligation=output_obligation,
                dom_snapshot_artifact_id=dom_snapshot_artifact_id,
                scout_transcript_artifact_id=scout_transcript_artifact_id,
                screenshot_artifact_id=screenshot_artifact_id,
            )
            session.add(episode)
            await session.commit()
            await session.refresh(episode)
            return HealEpisode.model_validate(episode)

    @db_operation("get_heal_episodes")
    async def get_heal_episodes(
        self,
        organization_id: str,
        workflow_permanent_id: str | None = None,
        block_label: str | None = None,
        workflow_run_id: str | None = None,
        limit: int = 100,
        ascending: bool = False,
    ) -> list[HealEpisode]:
        async with self.Session() as session:
            query = select(HealEpisodeModel).where(HealEpisodeModel.organization_id == organization_id)
            if workflow_permanent_id is not None:
                query = query.where(HealEpisodeModel.workflow_permanent_id == workflow_permanent_id)
            if block_label is not None:
                query = query.where(HealEpisodeModel.block_label == block_label)
            if workflow_run_id is not None:
                query = query.where(HealEpisodeModel.workflow_run_id == workflow_run_id)

            order = HealEpisodeModel.created_at.asc() if ascending else HealEpisodeModel.created_at.desc()
            episodes = (await session.scalars(query.order_by(order).limit(limit))).all()
            return [HealEpisode.model_validate(episode) for episode in episodes]

    @db_operation("create_heal_proposal")
    async def create_heal_proposal(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        block_label: str,
        candidate_definition: dict | list,
        episode_ids: list[str],
        base_version: int,
        base_definition_hash: str,
        provenance: dict | list | None = None,
        rendered_diff: str | None = None,
        status: Literal["proposed", "adopted", "rejected", "stale"] = "proposed",
        adopted_workflow_id: str | None = None,
        episode_window: str | None = None,
    ) -> WorkflowHealProposal:
        async with self.Session() as session:
            proposal = WorkflowHealProposalModel(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                block_label=block_label,
                candidate_definition=candidate_definition,
                provenance=provenance,
                episode_ids=episode_ids,
                rendered_diff=sanitize_postgres_text(rendered_diff) if rendered_diff else None,
                base_version=base_version,
                base_definition_hash=base_definition_hash,
                status=status,
                adopted_workflow_id=adopted_workflow_id,
                episode_window=episode_window,
            )
            session.add(proposal)
            await session.commit()
            await session.refresh(proposal)
            return WorkflowHealProposal.model_validate(proposal)

    @db_operation("get_heal_proposals")
    async def get_heal_proposals(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        status: Literal["proposed", "adopted", "rejected", "stale"] | None = None,
        limit: int = 100,
    ) -> list[WorkflowHealProposal]:
        async with self.Session() as session:
            query = select(WorkflowHealProposalModel).where(
                WorkflowHealProposalModel.organization_id == organization_id,
                WorkflowHealProposalModel.workflow_permanent_id == workflow_permanent_id,
            )
            if status is not None:
                query = query.where(WorkflowHealProposalModel.status == status)

            proposals = (
                await session.scalars(query.order_by(WorkflowHealProposalModel.created_at.desc()).limit(limit))
            ).all()
            return [WorkflowHealProposal.model_validate(proposal) for proposal in proposals]

    @db_operation("update_heal_proposal_status")
    async def update_heal_proposal_status(
        self,
        heal_proposal_id: str,
        organization_id: str,
        status: Literal["proposed", "adopted", "rejected", "stale"],
        adopted_workflow_id: str | None = None,
        episode_window: str | None = None,
        expected_current_status: Literal["proposed", "adopted", "rejected", "stale"] | None = None,
    ) -> WorkflowHealProposal | None:
        async with self.Session() as session:
            values: dict[str, object] = {
                "status": status,
                "modified_at": naive_utc_now(),
            }
            if adopted_workflow_id is not None:
                values["adopted_workflow_id"] = adopted_workflow_id
            if episode_window is not None:
                values["episode_window"] = episode_window

            # Optional compare-and-set: adoption callers pass the status they read so a
            # concurrent transition (e.g. proposed -> stale) can't be silently overwritten.
            statement = (
                update(WorkflowHealProposalModel)
                .where(WorkflowHealProposalModel.heal_proposal_id == heal_proposal_id)
                .where(WorkflowHealProposalModel.organization_id == organization_id)
            )
            if expected_current_status is not None:
                statement = statement.where(WorkflowHealProposalModel.status == expected_current_status)
            result = await session.execute(statement.values(**values))
            await session.commit()

            if expected_current_status is not None and result.rowcount == 0:
                return None

            proposal = await session.scalar(
                select(WorkflowHealProposalModel).where(
                    WorkflowHealProposalModel.heal_proposal_id == heal_proposal_id,
                    WorkflowHealProposalModel.organization_id == organization_id,
                )
            )
            if proposal is None:
                return None
            return WorkflowHealProposal.model_validate(proposal)
