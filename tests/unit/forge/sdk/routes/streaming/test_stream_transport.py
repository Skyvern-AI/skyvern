"""Tests for per-session stream-transport resolution (SKY-13291)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.routes.streaming import verify


class TestStreamTransport:
    @pytest.mark.asyncio
    async def test_delegates_to_agent_function(self) -> None:
        with patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock:
            app_mock.AGENT_FUNCTION.resolve_stream_transport = AsyncMock(return_value="cdp")

            assert await verify.stream_transport("pbs_1", "o_1") == "cdp"

            app_mock.AGENT_FUNCTION.resolve_stream_transport.assert_awaited_once_with(
                browser_session_id="pbs_1", organization_id="o_1"
            )

    @pytest.mark.asyncio
    async def test_falls_back_to_setting_on_error(self) -> None:
        with (
            patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock,
            patch("skyvern.forge.sdk.routes.streaming.verify.settings") as settings_mock,
        ):
            app_mock.AGENT_FUNCTION.resolve_stream_transport = AsyncMock(side_effect=RuntimeError("db down"))
            settings_mock.BROWSER_STREAMING_MODE = "vnc"

            assert await verify.stream_transport("pbs_1", "o_1") == "vnc"

    @pytest.mark.asyncio
    async def test_base_agent_function_returns_deployment_setting(self) -> None:
        from skyvern.forge.agent_functions import AgentFunction

        with patch("skyvern.forge.agent_functions.settings") as settings_mock:
            settings_mock.BROWSER_STREAMING_MODE = "cdp"

            transport = await AgentFunction().resolve_stream_transport(browser_session_id=None, organization_id=None)

        assert transport == "cdp"

    @pytest.mark.asyncio
    async def test_base_agent_function_ignores_the_pod_address(self) -> None:
        from skyvern.forge.agent_functions import AgentFunction

        with patch("skyvern.forge.agent_functions.settings") as settings_mock:
            settings_mock.BROWSER_STREAMING_MODE = "cdp"

            transport = await AgentFunction().resolve_stream_transport(
                browser_session_id="pbs_1", organization_id="o_1", ip_address="10.0.0.1"
            )

        assert transport == "cdp"
