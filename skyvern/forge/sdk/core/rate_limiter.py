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


class NoopRateLimiter(RateLimiter):
    """
    No-op rate limiter.

    This implementation does not enforce any rate limits.
    """

    async def rate_limit_submit_run(self, organization_id: str) -> None:
        """No-op implementation that never rate limits."""
