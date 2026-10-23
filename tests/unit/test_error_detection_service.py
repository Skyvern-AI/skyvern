"""
Unit tests for error_detection_service module.

Tests the user-defined error detection functionality for failed tasks.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.services.error_detection_service import detect_user_defined_errors_for_task
from tests.unit.helpers import make_organization, make_task


@pytest.fixture
def mock_browser_state():
    """Create a mock browser state with a working page."""
    browser_state = MagicMock()
    page = MagicMock()
    page.url = "https://example.com/checkout"

    async def get_working_page():
        return page

    browser_state.get_working_page = get_working_page
    browser_state.cleanup_element_tree = MagicMock()

    # Mock scrape_website
    scraped_page = MagicMock()
    scraped_page.url = "https://example.com/checkout"
    scraped_page.build_element_tree = MagicMock(
        return_value='<html><body><div class="error">Payment failed</div></body></html>'
    )
    scraped_page.screenshots = [b"screenshot_data"]

    async def scrape_website(**kwargs):
        return scraped_page

    browser_state.scrape_website = scrape_website

    return browser_state


@pytest.fixture
def mock_step():
    """Create a mock step."""
    now = datetime.now()
    return Step(
        created_at=now,
        modified_at=now,
        task_id="task-123",
        step_id="step-456",
        status=StepStatus.failed,
        output=None,
        order=1,
        is_last=True,
        retry_index=0,
        organization_id="org-123",
    )


@pytest.mark.asyncio
async def test_detect_errors_with_valid_error_code_mapping(mock_browser_state, mock_step):
    """Test error detection with valid error_code_mapping and browser state."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "payment_failed": "Credit card was declined",
            "out_of_stock": "Product is not available",
        },
    )

    # Mock extract_user_defined_errors to return expected errors
    from skyvern.errors.errors import UserDefinedError

    expected_errors = [
        UserDefinedError(
            error_code="payment_failed",
            reasoning="The page shows a payment declined message",
            confidence_float=0.95,
        )
    ]

    # Mock extract_user_defined_errors from handler
    with patch(
        "skyvern.services.error_detection_service.extract_user_defined_errors", new_callable=AsyncMock
    ) as mock_extract:
        mock_extract.return_value = expected_errors

        # Call detect_user_defined_errors_for_task
        detected_errors = await detect_user_defined_errors_for_task(
            task=task, step=mock_step, browser_state=mock_browser_state
        )

        # Assertions
        assert len(detected_errors) == 1
        assert detected_errors[0].error_code == "payment_failed"
        assert detected_errors[0].reasoning == "The page shows a payment declined message"

        # Verify extract_user_defined_errors was called
        mock_extract.assert_called_once()


@pytest.mark.asyncio
async def test_detect_errors_no_error_code_mapping(mock_browser_state, mock_step):
    """Test that detection is skipped when no error_code_mapping is provided."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(now, organization, error_code_mapping=None)

    detected_errors = await detect_user_defined_errors_for_task(
        task=task, step=mock_step, browser_state=mock_browser_state
    )

    assert detected_errors == []


@pytest.mark.asyncio
async def test_detect_errors_no_browser_state(mock_step):
    """Test that detection uses context-based method when browser_state is None."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "payment_failed": "Credit card was declined",
        },
    )

    # Mock the LLM API handler for context-based detection
    with patch("skyvern.services.error_detection_service.app") as mock_app:
        mock_app.EXTRACTION_LLM_API_HANDLER = AsyncMock(
            return_value={
                "errors": [{"error_code": "payment_failed", "reasoning": "Navigation failed", "confidence_float": 0.80}]
            }
        )

        detected_errors = await detect_user_defined_errors_for_task(
            task=task, step=mock_step, browser_state=None, failure_reason="Navigation timeout"
        )

        assert len(detected_errors) == 1
        assert detected_errors[0].error_code == "payment_failed"


@pytest.mark.asyncio
async def test_detect_errors_no_working_page(mock_step):
    """Test that detection uses context-based method when there's no working page."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "payment_failed": "Credit card was declined",
        },
    )

    # Create browser state that returns None for working page
    browser_state = MagicMock()

    async def get_working_page():
        return None

    browser_state.get_working_page = get_working_page

    # Mock the LLM API handler for context-based detection
    with patch("skyvern.services.error_detection_service.app") as mock_app:
        mock_app.EXTRACTION_LLM_API_HANDLER = AsyncMock(
            return_value={
                "errors": [{"error_code": "payment_failed", "reasoning": "Page unavailable", "confidence_float": 0.75}]
            }
        )

        detected_errors = await detect_user_defined_errors_for_task(
            task=task, step=mock_step, browser_state=browser_state, failure_reason="Page load failed"
        )

        assert len(detected_errors) == 1
        assert detected_errors[0].error_code == "payment_failed"


@pytest.mark.asyncio
async def test_detect_errors_scraping_fails(mock_step):
    """Test that detection handles scraping failures gracefully."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "payment_failed": "Credit card was declined",
        },
    )

    # Create browser state that raises an exception during scraping
    browser_state = MagicMock()
    page = MagicMock()
    page.url = "https://example.com"

    async def get_working_page():
        return page

    async def scrape_website(**kwargs):
        raise Exception("Scraping failed")

    browser_state.get_working_page = get_working_page
    browser_state.scrape_website = scrape_website

    detected_errors = await detect_user_defined_errors_for_task(task=task, step=mock_step, browser_state=browser_state)

    # Should return empty list, not raise exception
    assert detected_errors == []


@pytest.mark.asyncio
async def test_detect_errors_llm_call_fails(mock_browser_state, mock_step):
    """Test that detection handles LLM call failures gracefully."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "payment_failed": "Credit card was declined",
        },
    )

    # Mock extract_user_defined_errors to raise exception
    with patch(
        "skyvern.services.error_detection_service.extract_user_defined_errors", new_callable=AsyncMock
    ) as mock_extract:
        mock_extract.side_effect = Exception("LLM call failed")

        detected_errors = await detect_user_defined_errors_for_task(
            task=task, step=mock_step, browser_state=mock_browser_state
        )

        # Should return empty list, not raise exception
        assert detected_errors == []


@pytest.mark.asyncio
async def test_detect_errors_multiple_errors(mock_browser_state, mock_step):
    """Test detection of multiple errors."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "payment_failed": "Credit card was declined",
            "address_invalid": "Shipping address is invalid",
        },
    )

    from skyvern.errors.errors import UserDefinedError

    expected_errors = [
        UserDefinedError(
            error_code="payment_failed", reasoning="Payment declined message visible", confidence_float=0.95
        ),
        UserDefinedError(
            error_code="address_invalid", reasoning="Address validation error shown", confidence_float=0.90
        ),
    ]

    # Mock extract_user_defined_errors
    with patch(
        "skyvern.services.error_detection_service.extract_user_defined_errors", new_callable=AsyncMock
    ) as mock_extract:
        mock_extract.return_value = expected_errors

        detected_errors = await detect_user_defined_errors_for_task(
            task=task, step=mock_step, browser_state=mock_browser_state
        )

        assert len(detected_errors) == 2
        assert detected_errors[0].error_code == "payment_failed"
        assert detected_errors[1].error_code == "address_invalid"


@pytest.mark.asyncio
async def test_detect_errors_invalid_error_format(mock_browser_state, mock_step):
    """Test that invalid error formats are skipped (handled by extract_user_defined_errors)."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "payment_failed": "Credit card was declined",
        },
    )

    from skyvern.errors.errors import UserDefinedError

    # Mock extract_user_defined_errors to return only valid errors
    expected_errors = [
        UserDefinedError(error_code="payment_failed", reasoning="Payment declined", confidence_float=0.90)
    ]

    with patch(
        "skyvern.services.error_detection_service.extract_user_defined_errors", new_callable=AsyncMock
    ) as mock_extract:
        mock_extract.return_value = expected_errors

        detected_errors = await detect_user_defined_errors_for_task(
            task=task, step=mock_step, browser_state=mock_browser_state
        )

        # Only valid error should be returned
        assert len(detected_errors) == 1
        assert detected_errors[0].error_code == "payment_failed"


@pytest.mark.asyncio
async def test_detect_errors_empty_llm_response(mock_browser_state, mock_step):
    """Test handling of empty LLM response."""
    now = datetime.now()
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "payment_failed": "Credit card was declined",
        },
    )

    # Mock extract_user_defined_errors to return empty list
    with patch(
        "skyvern.services.error_detection_service.extract_user_defined_errors", new_callable=AsyncMock
    ) as mock_extract:
        mock_extract.return_value = []

        detected_errors = await detect_user_defined_errors_for_task(
            task=task, step=mock_step, browser_state=mock_browser_state
        )

        assert detected_errors == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["no_page", "scrape_fails", "detector_raises", "page_lookup_raises"])
@pytest.mark.parametrize(
    ("failure_reason", "expected"),
    [
        ("The bank answered payment_failed at checkout.", ["payment_failed"]),
        # A description substring is too loose to stand on its own, and naming two codes is not concluding one.
        ("The bank said: Credit card was declined.", []),
        ("Either payment_failed or address_invalid; the page did not say.", []),
    ],
)
async def test_every_empty_detection_path_falls_back_to_a_single_code_the_reason_names(
    mock_step, path: str, failure_reason: str, expected: list[str]
) -> None:
    now = datetime.now()
    task = make_task(
        now,
        make_organization(now),
        error_code_mapping={"payment_failed": "Credit card was declined", "address_invalid": "Bad address"},
    )
    browser_state = MagicMock()
    page = MagicMock()
    page.url = "https://example.com"
    browser_state.get_working_page = AsyncMock(
        return_value=None if path == "no_page" else page,
        side_effect=Exception("browser gone") if path == "page_lookup_raises" else None,
    )
    browser_state.scrape_website = AsyncMock(
        side_effect=Exception("down") if path == "scrape_fails" else None,
        return_value=MagicMock() if path == "detector_raises" else None,
    )

    with (
        patch(
            "skyvern.services.error_detection_service._detect_errors_from_context",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "skyvern.services.error_detection_service.extract_user_defined_errors",
            new_callable=AsyncMock,
            side_effect=Exception("llm down"),
        ),
    ):
        detected = await detect_user_defined_errors_for_task(
            task=task,
            step=mock_step,
            browser_state=browser_state,
            failure_reason=failure_reason,
        )

    assert [error.error_code for error in detected] == expected
