import asyncio
import os
from datetime import datetime, timezone

import httpx
import structlog

from skyvern.forge import app
from skyvern.utils.cron import cron_matches

LOG = structlog.get_logger()


class CronService:
    def __init__(self) -> None:
        self.api_base_url = os.getenv("CRON_API_BASE_URL", "http://localhost:8000")
        self.api_key = os.getenv("CRON_API_KEY")

    async def _trigger(self, workflow_id: str) -> None:
        url = f"{self.api_base_url}/api/v1/workflows/{workflow_id}/run"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                await client.post(url, json={"data": {}})
                LOG.info("Triggered workflow", workflow_id=workflow_id)
            except Exception:
                LOG.exception("Failed to trigger workflow", workflow_id=workflow_id)

    async def tick(self) -> None:
        now = datetime.now(timezone.utc)
        workflows = await app.DATABASE.get_enabled_cron_workflows()
        for workflow in workflows:
            if workflow.cron_expression and cron_matches(now, workflow.cron_expression):
                await self._trigger(workflow.workflow_id)


async def main() -> None:
    service = CronService()
    while True:
        await service.tick()
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
