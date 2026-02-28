"""StreamingService integration behavior tests."""

from unittest.mock import patch

from skyvern.forge.forge_app_initializer import start_forge_app
from skyvern.services.streaming.service import StreamingService


def test_streaming_service_in_forge_app():
    """Verify StreamingService initializes on Linux/WSL."""
    with patch("skyvern.forge.forge_app.detect_os", return_value="linux"):
        forge_app = start_forge_app()

    assert forge_app.STREAMING_SERVICE is not None
    assert isinstance(forge_app.STREAMING_SERVICE, StreamingService)
    assert forge_app.api_app_startup_event is not None
    assert forge_app.api_app_shutdown_event is not None


def test_streaming_service_disabled_on_unsupported_os():
    """Ensure feature gate prevents StreamingService on non-Linux."""
    with patch("skyvern.forge.forge_app.detect_os", return_value="windows"):
        forge_app = start_forge_app()

    assert forge_app.STREAMING_SERVICE is None
    assert forge_app.api_app_startup_event is None
    assert forge_app.api_app_shutdown_event is None
