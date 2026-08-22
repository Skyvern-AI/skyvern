from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Protocol


class RateLimiter(Protocol):
    """
    Protocol for rate limiting submit run requests per organization.

    Implementations should be thread-safe and work correctly in distributed environments.
    """

    async def rate_limit_submit_run(self, organization_id: str) -> None:
        """
        Check and enforce rate limit for submitting a new run (task/workflow)
        raises RateLimitExceeded exception if rate limit is exceeded.

        Args:
            organization_id: The organization ID to rate limit

        Raises:
            Exception: If rate limit is exceeded (implementation-specific exception)
        """
        ...

    def limit_sdk_action_concurrency(self, organization_id: str) -> AbstractAsyncContextManager[None]:
        """
        Hold one in-flight SDK action slot for the organization for the duration of the block.

        Unlike the request-rate limit above, this bounds how many SDK actions one organization may
        have running at once. SDK actions execute synchronously in the API process, so a single
        organization's concurrency is what an API worker's event loop actually has to absorb.

        Entering raises when the organization is already at its cap, so a caller over the cap is
        rejected rather than queued. The slot is released when the block exits, which is what lets a
        backing-off organization resume with no operator action.

        Args:
            organization_id: The organization ID to bound

        Raises:
            Exception: If the organization is at its concurrency cap (implementation-specific)
        """
        ...


class NoopRateLimiter(RateLimiter):
    """
    No-op rate limiter.

    This implementation does not enforce any rate limits.
    """

    async def rate_limit_submit_run(self, organization_id: str) -> None:
        """No-op implementation that never rate limits."""

    def limit_sdk_action_concurrency(self, organization_id: str) -> AbstractAsyncContextManager[None]:
        """No-op implementation that never bounds concurrency."""
        return nullcontext()
