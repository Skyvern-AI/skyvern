from __future__ import annotations

import asyncio
import logging

from skyvern_humanpages.client import HumanPagesClient
from skyvern_humanpages.schema import (
    BlockerType,
    FallbackRequest,
    FallbackResult,
    JobCreateRequest,
    JobStatus,
)
from skyvern_humanpages.settings import settings

logger = logging.getLogger(__name__)

# Terminal statuses that stop polling
_TERMINAL = {JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.EXPIRED, JobStatus.DISPUTED}


class HumanFallback:
    """High-level helper that delegates a blocked Skyvern step to a real human
    via Human Pages, then returns control once the human finishes."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        price_usdc: float | None = None,
        deadline_hours: int | None = None,
        poll_interval: int | None = None,
        poll_timeout: int | None = None,
    ) -> None:
        self.client = HumanPagesClient(api_key=api_key, base_url=base_url)
        self.price_usdc = price_usdc or settings.default_price_usdc
        self.deadline_hours = deadline_hours or settings.default_deadline_hours
        self.poll_interval = poll_interval or settings.poll_interval_seconds
        self.poll_timeout = poll_timeout or settings.poll_timeout_seconds

    def _build_description(self, request: FallbackRequest) -> str:
        """Build a human-readable job description from the fallback request."""
        parts = [
            f"A browser automation task hit a blocker: {request.blocker_type.value}",
            f"URL: {request.url}",
            "",
            "What the human needs to do:",
            request.description,
        ]
        if request.screenshot_url:
            parts.append(f"\nScreenshot of the blocked page: {request.screenshot_url}")
        if request.additional_context:
            parts.append("\nAdditional context:")
            for key, value in request.additional_context.items():
                parts.append(f"  {key}: {value}")
        return "\n".join(parts)

    def _build_title(self, blocker_type: BlockerType, url: str) -> str:
        labels = {
            BlockerType.CAPTCHA: "Solve CAPTCHA",
            BlockerType.IDENTITY_VERIFICATION: "Complete identity verification",
            BlockerType.PHONE_VERIFICATION: "Complete phone verification",
            BlockerType.MANUAL_REVIEW: "Manual review needed",
            BlockerType.LOGIN_REQUIRED: "Log in to website",
            BlockerType.OTHER: "Complete blocked browser step",
        }
        # Keep the domain for context, truncate if needed
        domain = url.split("//")[-1].split("/")[0]
        return f"{labels[blocker_type]} on {domain}"

    async def handle_blocker(self, request: FallbackRequest) -> FallbackResult:
        """Search for a human, create a job, poll until done, and return the result.

        This is the main entry point. Call it when Skyvern encounters a step it
        cannot automate (CAPTCHA, identity check, etc.).
        """
        logger.info(
            "Human fallback triggered: blocker=%s url=%s",
            request.blocker_type.value,
            request.url,
        )

        # 1. Find an available human
        humans = await self.client.search_humans(skill="web task", available=True)
        if not humans:
            raise RuntimeError("No humans available on Human Pages for this task")

        human = humans[0]  # pick the first available
        logger.info("Selected human %s (id=%s)", human.name, human.id)

        # 2. Create the job
        job = await self.client.create_job(
            JobCreateRequest(
                humanId=human.id,
                title=self._build_title(request.blocker_type, request.url),
                description=self._build_description(request),
                priceUsdc=self.price_usdc,
                deadlineHours=self.deadline_hours,
            )
        )
        logger.info("Created job %s (status=%s)", job.id, job.status.value)

        # 3. Poll until the job reaches a terminal status
        elapsed = 0
        while elapsed < self.poll_timeout:
            await asyncio.sleep(self.poll_interval)
            elapsed += self.poll_interval

            job = await self.client.get_job_status(job.id)
            logger.debug("Job %s status: %s (elapsed %ds)", job.id, job.status.value, elapsed)

            if job.status in _TERMINAL:
                break

        # 4. Gather messages and return
        messages = await self.client.get_job_messages(job.id)

        if job.status != JobStatus.COMPLETED:
            logger.warning(
                "Job %s ended with status %s (not COMPLETED)", job.id, job.status.value
            )

        return FallbackResult(
            job_id=job.id,
            status=job.status,
            result=job.result,
            messages=messages,
        )
