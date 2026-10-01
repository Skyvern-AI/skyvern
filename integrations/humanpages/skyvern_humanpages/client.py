from __future__ import annotations

import httpx

from skyvern_humanpages.schema import (
    HumanSearchResult,
    JobCreateRequest,
    JobMessage,
    JobResponse,
)
from skyvern_humanpages.settings import settings


class HumanPagesClient:
    """Low-level async client for the Human Pages REST API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key or settings.api_key
        self.base_url = (base_url or settings.base_url).rstrip("/")
        if not self.api_key:
            raise ValueError(
                "Human Pages API key is required. "
                "Set HUMANPAGES_API_KEY or pass api_key=."
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Agent-Key": self.api_key,
        }

    async def search_humans(
        self,
        skill: str = "web task",
        available: bool = True,
    ) -> list[HumanSearchResult]:
        """Search for available humans with a given skill."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.base_url}/api/humans/search",
                headers=self._headers(),
                params={"skill": skill, "available": str(available).lower()},
            )
            resp.raise_for_status()
            return [HumanSearchResult(**h) for h in resp.json()]

    async def create_job(self, request: JobCreateRequest) -> JobResponse:
        """Create a new job for a human to complete."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/api/jobs",
                headers=self._headers(),
                json=request.model_dump(),
            )
            resp.raise_for_status()
            return JobResponse(**resp.json())

    async def get_job_status(self, job_id: str) -> JobResponse:
        """Check the current status of a job."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.base_url}/api/jobs/{job_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return JobResponse(**resp.json())

    async def get_job_messages(self, job_id: str) -> list[JobMessage]:
        """Retrieve messages exchanged on a job."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.base_url}/api/jobs/{job_id}/messages",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return [JobMessage(**m) for m in resp.json()]
