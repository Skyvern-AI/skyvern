"""SKY-12344: classify an environmental renderer/target crash so callers can
downgrade its log. The crash is already caught and the run continues (degraded)
at every get_content caller, so it is noise in error-tracking, not a failure --
but a genuine (non-crash) get_content bug must still be classified loud."""

import pytest
from playwright._impl._errors import Error as PlaywrightError
from playwright._impl._errors import TargetClosedError
from playwright._impl._errors import TimeoutError as PlaywrightTimeoutError

from skyvern.webeye.utils.page import is_browser_crashed_error


@pytest.mark.parametrize(
    "exc",
    [
        PlaywrightError("Page.content: Target crashed"),
        PlaywrightError("Page.content: Target closed"),
        TargetClosedError("Target page, context or browser has been closed"),
        PlaywrightError("Page crashed"),
    ],
)
def test_browser_crashed_error_true_for_crash_and_closed(exc: BaseException) -> None:
    assert is_browser_crashed_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        PlaywrightTimeoutError("Page.content: Timeout 300000ms exceeded."),
        PlaywrightError("Some other content extraction bug"),
        ValueError("unrelated"),
    ],
)
def test_browser_crashed_error_false_for_real_failures(exc: BaseException) -> None:
    assert is_browser_crashed_error(exc) is False
