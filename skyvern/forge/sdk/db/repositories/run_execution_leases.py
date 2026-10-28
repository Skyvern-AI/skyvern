from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now
from skyvern.forge.sdk.db.models import RunExecutionLeaseModel


@dataclass(frozen=True)
class RunExecutionLease:
    run_id: str
    run_type: str
    owner_id: str
    attempt_number: int | None
    heartbeat_at: datetime


class RunExecutionLeasesRepository(BaseRepository):
    @db_operation("acquire_run_execution_leases")
    async def acquire(
        self, owner_id: str, runs: Mapping[str, tuple[str, int | None]], stale_before: datetime
    ) -> set[str]:
        if not runs:
            return set()
        now = naive_utc_now()
        acquired: set[str] = set()
        async with self.Session() as session:
            insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
            # One row per statement also stays below SQLite's bind parameter limit for large registries.
            for run_id, (run_type, attempt_number) in runs.items():
                stmt = insert(RunExecutionLeaseModel).values(
                    run_id=run_id,
                    run_type=run_type,
                    owner_id=owner_id,
                    attempt_number=attempt_number,
                    heartbeat_at=now,
                )
                result = await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[RunExecutionLeaseModel.run_id],
                        set_={
                            key: stmt.excluded[key]
                            for key in ("run_type", "owner_id", "attempt_number", "heartbeat_at")
                        },
                        where=or_(
                            RunExecutionLeaseModel.owner_id == owner_id,
                            RunExecutionLeaseModel.heartbeat_at < stale_before,
                        ),
                    ).returning(RunExecutionLeaseModel.run_id)
                )
                acquired.update(result.scalars().all())
            await session.commit()
            return acquired

    @db_operation("renew_run_execution_leases")
    async def renew(self, owner_id: str, run_ids: Collection[str]) -> set[str]:
        if not run_ids:
            return set()
        async with self.Session() as session:
            result = await session.execute(
                update(RunExecutionLeaseModel)
                .where(RunExecutionLeaseModel.owner_id == owner_id, RunExecutionLeaseModel.run_id.in_(run_ids))
                .values(heartbeat_at=naive_utc_now())
                .returning(RunExecutionLeaseModel.run_id)
            )
            renewed = set(result.scalars().all())
            await session.commit()
            return renewed

    @db_operation("release_run_execution_leases")
    async def release(self, owner_id: str, run_ids: Collection[str]) -> None:
        if not run_ids:
            return
        async with self.Session() as session:
            await session.execute(
                delete(RunExecutionLeaseModel).where(
                    RunExecutionLeaseModel.owner_id == owner_id,
                    RunExecutionLeaseModel.run_id.in_(run_ids),
                )
            )
            await session.commit()

    @db_operation("get_expired_run_execution_leases")
    async def get_expired(
        self, heartbeat_before: datetime, limit: int = 500, after: tuple[datetime, str] | None = None
    ) -> list[RunExecutionLease]:
        query = select(RunExecutionLeaseModel).where(RunExecutionLeaseModel.heartbeat_at < heartbeat_before)
        if after is not None:
            query = query.where(tuple_(RunExecutionLeaseModel.heartbeat_at, RunExecutionLeaseModel.run_id) > after)
        async with self.Session() as session:
            rows = (
                await session.scalars(
                    query.order_by(RunExecutionLeaseModel.heartbeat_at, RunExecutionLeaseModel.run_id).limit(limit)
                )
            ).all()
            return [
                RunExecutionLease(row.run_id, row.run_type, row.owner_id, row.attempt_number, row.heartbeat_at)
                for row in rows
            ]

    @db_operation("claim_run_execution_lease")
    async def claim(
        self,
        run_id: str,
        observed_owner_id: str,
        observed_heartbeat_at: datetime,
        reaper_owner_id: str,
    ) -> bool:
        async with self.Session() as session:
            result = await session.execute(
                update(RunExecutionLeaseModel)
                .where(
                    RunExecutionLeaseModel.run_id == run_id,
                    RunExecutionLeaseModel.owner_id == observed_owner_id,
                    RunExecutionLeaseModel.heartbeat_at == observed_heartbeat_at,
                )
                .values(owner_id=reaper_owner_id, heartbeat_at=naive_utc_now())
                .returning(RunExecutionLeaseModel.run_id)
            )
            won = result.scalar_one_or_none() is not None
            await session.commit()
            return won

    @db_operation("unclaim_run_execution_lease")
    async def unclaim(self, run_id: str, reaper_owner_id: str, observed_heartbeat_at: datetime) -> bool:
        async with self.Session() as session:
            result = await session.execute(
                update(RunExecutionLeaseModel)
                .where(
                    RunExecutionLeaseModel.run_id == run_id,
                    RunExecutionLeaseModel.owner_id == reaper_owner_id,
                )
                .values(heartbeat_at=observed_heartbeat_at)
                .returning(RunExecutionLeaseModel.run_id)
            )
            unclaimed = result.scalar_one_or_none() is not None
            await session.commit()
            return unclaimed
