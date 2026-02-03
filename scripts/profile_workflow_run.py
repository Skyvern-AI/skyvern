"""
Script to profile a workflow run by collecting and displaying all key timestamps.

Usage:
    python scripts/profile_workflow_run.py <workflow_run_id>
    python scripts/profile_workflow_run.py <workflow_run_id> --include-actions
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

import typer
from sqlalchemy import select

from skyvern.forge import app
from skyvern.forge.forge_app_initializer import start_forge_app
from skyvern.forge.sdk.db.models import (
    ActionModel,
    StepModel,
    TaskModel,
    WorkflowRunBlockModel,
    WorkflowRunModel,
)


@dataclass
class TimestampEntry:
    """Represents a single timestamp entry for profiling."""

    timestamp: datetime
    entity_type: str  # "workflow_run", "workflow_run_block", "task", "step", "action"
    entity_id: str
    field_name: str  # "created_at", "started_at", "finished_at", etc.
    label: str | None = None  # For blocks with labels
    status: str | None = None

    def __str__(self) -> str:
        label_str = f" [{self.label}]" if self.label else ""
        status_str = f" ({self.status})" if self.status else ""
        return f"{self.timestamp.isoformat()} | {self.entity_type:20}{label_str} | {self.field_name:12} | {self.entity_id}{status_str}"


async def collect_timestamps(workflow_run_id: str, include_actions: bool = False) -> list[TimestampEntry]:
    """Collect all timestamps from the workflow run and its children."""
    entries: list[TimestampEntry] = []

    async with app.DATABASE.Session() as session:
        # 1. Fetch the workflow run
        workflow_run = (
            await session.scalars(select(WorkflowRunModel).filter_by(workflow_run_id=workflow_run_id))
        ).first()

        if not workflow_run:
            raise ValueError(f"Workflow run not found: {workflow_run_id}")

        # Add workflow run timestamps
        for field in ["created_at", "queued_at", "started_at", "finished_at"]:
            ts = getattr(workflow_run, field, None)
            if ts:
                entries.append(
                    TimestampEntry(
                        timestamp=ts,
                        entity_type="workflow_run",
                        entity_id=workflow_run_id,
                        field_name=field,
                        status=workflow_run.status,
                    )
                )

        # 2. Fetch all workflow run blocks
        workflow_run_blocks = (
            await session.scalars(
                select(WorkflowRunBlockModel)
                .filter_by(workflow_run_id=workflow_run_id)
                .order_by(WorkflowRunBlockModel.created_at)
            )
        ).all()

        for block in workflow_run_blocks:
            for field in ["created_at", "queued_at", "started_at", "finished_at", "modified_at"]:
                ts = getattr(block, field, None)
                if ts:
                    entries.append(
                        TimestampEntry(
                            timestamp=ts,
                            entity_type="workflow_run_block",
                            entity_id=block.workflow_run_block_id,
                            field_name=field,
                            label=block.label,
                            status=block.status,
                        )
                    )

        # 3. Fetch all tasks for this workflow run
        tasks = (
            await session.scalars(
                select(TaskModel).filter_by(workflow_run_id=workflow_run_id).order_by(TaskModel.created_at)
            )
        ).all()

        task_ids = []
        for task in tasks:
            task_ids.append(task.task_id)
            for field in ["created_at", "queued_at", "started_at", "finished_at"]:
                ts = getattr(task, field, None)
                if ts:
                    entries.append(
                        TimestampEntry(
                            timestamp=ts,
                            entity_type="task",
                            entity_id=task.task_id,
                            field_name=field,
                            status=task.status,
                        )
                    )

        # 4. Fetch all steps for all tasks
        if task_ids:
            steps = (
                await session.scalars(
                    select(StepModel).filter(StepModel.task_id.in_(task_ids)).order_by(StepModel.created_at)
                )
            ).all()

            for step in steps:
                for field in ["created_at", "finished_at"]:
                    ts = getattr(step, field, None)
                    if ts:
                        entries.append(
                            TimestampEntry(
                                timestamp=ts,
                                entity_type="step",
                                entity_id=step.step_id,
                                field_name=field,
                                status=step.status,
                            )
                        )

        # 5. Fetch all actions for all tasks (optional)
        if include_actions and task_ids:
            actions = (
                await session.scalars(
                    select(ActionModel).filter(ActionModel.task_id.in_(task_ids)).order_by(ActionModel.created_at)
                )
            ).all()

            for action in actions:
                for field in ["modified_at"]:
                    ts = getattr(action, field, None)
                    if ts:
                        entries.append(
                            TimestampEntry(
                                timestamp=ts,
                                entity_type="action",
                                entity_id=action.action_id,
                                field_name=field,
                                label=action.action_type,
                                status=action.status,
                            )
                        )

    return entries


def print_profile(entries: list[TimestampEntry]) -> None:
    """Print the profiling results sorted by timestamp."""
    # Sort by timestamp
    sorted_entries = sorted(entries, key=lambda e: e.timestamp)

    if not sorted_entries:
        print("No timestamps found.")
        return

    print("\n" + "=" * 120)
    print("WORKFLOW RUN PROFILE")
    print("=" * 120)
    print(f"{'Timestamp':<30} | {'Entity Type':<25} | {'Field':<12} | {'Entity ID'}")
    print("-" * 120)

    first_ts = sorted_entries[0].timestamp
    for entry in sorted_entries:
        # Calculate relative time from first timestamp
        delta = entry.timestamp - first_ts
        delta_str = f"+{delta.total_seconds():>10.3f}s"

        label_str = f" [{entry.label}]" if entry.label else ""
        status_str = f" ({entry.status})" if entry.status else ""

        print(
            f"{entry.timestamp.isoformat():<30} {delta_str} | {entry.entity_type:<25}{label_str:<15} | {entry.field_name:<12} | {entry.entity_id[:36]}{status_str}"
        )

    print("-" * 120)

    # Print summary
    total_duration = sorted_entries[-1].timestamp - sorted_entries[0].timestamp
    print(f"\nTotal Duration: {total_duration.total_seconds():.3f} seconds")

    # Count entities
    entity_counts: dict[str, int] = {}
    for entry in sorted_entries:
        if entry.field_name == "created_at":
            entity_counts[entry.entity_type] = entity_counts.get(entry.entity_type, 0) + 1

    print("\nEntity Counts:")
    for entity_type, count in entity_counts.items():
        print(f"  {entity_type}: {count}")

    print("=" * 120 + "\n")


async def profile_workflow_run(workflow_run_id: str, include_actions: bool = False) -> None:
    """Main function to profile a workflow run."""
    print(f"Profiling workflow run: {workflow_run_id}")
    if include_actions:
        print("(including actions)")

    entries = await collect_timestamps(workflow_run_id, include_actions=include_actions)
    print_profile(entries)


def main(
    workflow_run_id: Annotated[str, typer.Argument(help="The workflow run ID to profile")],
    include_actions: Annotated[
        bool, typer.Option("--include-actions", "-a", help="Include action timestamps (can be noisy)")
    ] = False,
) -> None:
    """Profile a workflow run by collecting and displaying all key timestamps."""
    start_forge_app()
    asyncio.run(profile_workflow_run(workflow_run_id, include_actions=include_actions))


if __name__ == "__main__":
    typer.run(main)
