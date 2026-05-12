from __future__ import annotations

from datetime import datetime, timezone

from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.workflow.models.block import _should_skip_retry_on_anti_bot_detection


def _make_failed_task(
    *,
    failure_reason: str | None = None,
    failure_category: list[dict] | None = None,
) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        task_id="tsk_test",
        organization_id="o_test",
        url="https://example.com/login",
        created_at=now,
        modified_at=now,
        status=TaskStatus.failed,
        failure_reason=failure_reason,
        failure_category=failure_category,
    )


class TestPersistedCategory:
    def test_primary_anti_bot_category_skips_retry(self) -> None:
        task = _make_failed_task(
            failure_category=[
                {"category": "ANTI_BOT_DETECTION", "confidence_float": 0.9, "reasoning": "captcha"},
            ],
        )
        assert _should_skip_retry_on_anti_bot_detection(task) is True

    def test_anti_bot_secondary_position_still_skips_retry(self) -> None:
        task = _make_failed_task(
            failure_category=[
                {"category": "MAX_STEPS_EXCEEDED", "confidence_float": 0.9, "reasoning": "max"},
                {"category": "ANTI_BOT_DETECTION", "confidence_float": 0.7, "reasoning": "captcha"},
            ],
        )
        assert _should_skip_retry_on_anti_bot_detection(task) is True

    def test_auth_failure_does_not_skip_retry(self) -> None:
        task = _make_failed_task(
            failure_category=[
                {"category": "AUTH_FAILURE", "confidence_float": 0.9, "reasoning": "bad password"},
            ],
        )
        assert _should_skip_retry_on_anti_bot_detection(task) is False

    def test_persisted_primary_wins_over_reason_keywords(self) -> None:
        """A persisted non-anti-bot primary must NOT be overridden by reason text.

        Example: persisted AUTH_FAILURE whose reason happens to mention a captcha;
        retry should still proceed because the persisted classification is the
        authoritative signal.
        """
        task = _make_failed_task(
            failure_category=[
                {"category": "AUTH_FAILURE", "confidence_float": 0.9, "reasoning": "bad password"},
            ],
            failure_reason="Login failed; the page also showed a captcha challenge after submit",
        )
        assert _should_skip_retry_on_anti_bot_detection(task) is False


class TestFallbackFromReason:
    def test_keyword_reason_skips_retry_when_no_category(self) -> None:
        task = _make_failed_task(failure_reason="Page blocked by captcha challenge")
        assert _should_skip_retry_on_anti_bot_detection(task) is True

    def test_managed_challenge_service_reason_skips_retry(self) -> None:
        task = _make_failed_task(failure_reason="Bot detection service blocked access")
        assert _should_skip_retry_on_anti_bot_detection(task) is True

    def test_max_steps_with_captcha_in_reason_skips_retry(self) -> None:
        """Exact ticket scenario for tasks without a persisted category list.

        The agent stores max_steps failures as 'Reached the maximum steps (...)'.
        That phrase ranks higher in the classifier than the captcha keyword,
        so [0] is MAX_STEPS_EXCEEDED and [1] is ANTI_BOT_DETECTION. The fallback
        must still recognize the anti-bot signal in any position.
        """
        task = _make_failed_task(
            failure_reason=(
                "Reached the maximum steps (10). Possible failure reasons: "
                "the page presented a captcha challenge after the login form submit"
            ),
        )
        assert _should_skip_retry_on_anti_bot_detection(task) is True

    def test_benign_reason_does_not_skip_retry(self) -> None:
        task = _make_failed_task(failure_reason="Could not find the submit button on the page")
        assert _should_skip_retry_on_anti_bot_detection(task) is False

    def test_no_reason_and_no_category_does_not_skip_retry(self) -> None:
        task = _make_failed_task()
        assert _should_skip_retry_on_anti_bot_detection(task) is False

    def test_access_denied_with_auth_context_does_not_skip(self) -> None:
        """Auth-context 'access denied' classifies as AUTH_FAILURE, not bot detection."""
        task = _make_failed_task(
            failure_reason="Access denied after login - user does not have permission",
        )
        assert _should_skip_retry_on_anti_bot_detection(task) is False
