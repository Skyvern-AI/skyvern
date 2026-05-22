"""Tests for TaskRequest input validation (SKY-9857)."""

from __future__ import annotations

import pytest


def test_data_extraction_goal_none_passes() -> None:
    from skyvern.forge.sdk.schemas.tasks import TaskRequest

    req = TaskRequest(url="https://example.com", data_extraction_goal=None)
    assert req.data_extraction_goal is None


def test_data_extraction_goal_short_passes() -> None:
    from skyvern.forge.sdk.schemas.tasks import TaskRequest

    req = TaskRequest(url="https://example.com", data_extraction_goal="Extract the total price")
    assert req.data_extraction_goal == "Extract the total price"


def test_data_extraction_goal_over_limit_raises() -> None:
    from skyvern.exceptions import SkyvernHTTPException
    from skyvern.utils.prompt_truncation import EXTRACTION_GOAL_MAX_TOKENS

    # Build a goal that is clearly over the token limit.
    # 200_000 repetitions of "extract " * ~7 chars → ~1.4M chars well above 600k fast-exit.
    oversized_goal = "extract " * 200_000

    from skyvern.forge.sdk.schemas.tasks import TaskRequest

    with pytest.raises(SkyvernHTTPException) as exc_info:
        TaskRequest(url="https://example.com", data_extraction_goal=oversized_goal)

    assert f"{EXTRACTION_GOAL_MAX_TOKENS:,}" in exc_info.value.message


def test_extraction_goal_max_tokens_constant() -> None:
    from skyvern.utils.prompt_truncation import EXTRACTION_GOAL_MAX_TOKENS

    assert EXTRACTION_GOAL_MAX_TOKENS == 150_000
