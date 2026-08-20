"""The streaming websocket rejects every failed auth the same way, but the log level has to tell
expected credential failures apart from backend faults: a bad token is a 4xx-equivalent, while an
org-lookup outage is the signal an operator is paged on."""

from typing import Any

import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from skyvern.forge.sdk.routes.streaming import auth as auth_module


class _FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.close_codes: list[int] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int) -> None:
        self.close_codes.append(code)


def _raise(exc: BaseException) -> Any:
    async def _get_current_org(**kwargs: Any) -> Any:
        raise exc

    return _get_current_org


@pytest.mark.parametrize(
    ("exc", "expected_level"),
    [
        (HTTPException(status_code=403, detail="Auth token is expired"), "warning"),
        (HTTPException(status_code=401, detail="Invalid credentials"), "warning"),
        (HTTPException(status_code=404, detail="Organization not found"), "error"),
        (TimeoutError("QueuePool limit reached"), "error"),
    ],
)
@pytest.mark.asyncio
async def test_auth_failure_log_level_separates_bad_credentials_from_backend_faults(
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
    expected_level: str,
) -> None:
    monkeypatch.setattr(auth_module, "get_current_org", _raise(exc))
    websocket = _FakeWebSocket()

    with capture_logs() as logs:
        organization_id = await auth_module.auth(apikey="bad", token=None, websocket=websocket)  # type: ignore[arg-type]

    # Rejection is unconditional; only the level moves.
    assert organization_id is None
    assert websocket.close_codes == [1002]

    levels = [entry["log_level"] for entry in logs]
    assert expected_level in levels
    # Security floor: an auth failure is never silently dropped.
    assert "debug" not in levels and "info" not in levels
