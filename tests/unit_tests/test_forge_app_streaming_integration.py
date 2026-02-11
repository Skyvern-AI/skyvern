"""Test to verify StreamingService is properly integrated into ForgeApp."""

from skyvern.forge.forge_app_initializer import start_forge_app
from skyvern.services.streaming.service import StreamingService


def test_streaming_service_in_forge_app():
    """Test that StreamingService is available in ForgeApp instance."""
    # Initialize the forge app
    forge_app = start_forge_app()

    # Verify StreamingService is available as an attribute
    assert hasattr(forge_app, "STREAMING_SERVICE")
    assert forge_app.STREAMING_SERVICE is not None

    # Verify it's the correct type

    assert isinstance(forge_app.STREAMING_SERVICE, StreamingService)
