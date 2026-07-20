from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from skyvern.exceptions import InvalidUrl
from skyvern.forge import app
from skyvern.services import webhook_delivery


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_url", ["example.com/webhook", "ftp://example.com", "", "//example.com/x"])
async def test_scheme_less_url_rejected_before_delivery(monkeypatch: pytest.MonkeyPatch, bad_url: str) -> None:
    deliver = AsyncMock()
    monkeypatch.setattr(app, "AGENT_FUNCTION", MagicMock(deliver_webhook=deliver), raising=False)

    with pytest.raises(InvalidUrl):
        await webhook_delivery.deliver_webhook_with_retries(
            url=bad_url,
            payload="{}",
            headers={},
            timeout_seconds=1.0,
            organization_id="o_1",
            run_id="wr_1",
        )
    deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_https_url_reaches_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = httpx.Response(200, request=httpx.Request("POST", "https://example.com/webhook"))
    deliver = AsyncMock(return_value=resp)
    monkeypatch.setattr(app, "AGENT_FUNCTION", MagicMock(deliver_webhook=deliver), raising=False)

    out = await webhook_delivery.deliver_webhook_with_retries(
        url="https://example.com/webhook",
        payload="{}",
        headers={},
        timeout_seconds=1.0,
        organization_id="o_1",
        run_id="wr_1",
    )
    assert out.status_code == 200
    deliver.assert_awaited_once()
