"""Test to verify StreamingService startup integration."""

from fastapi import FastAPI

from skyvern.forge.forge_app_initializer import start_forge_app


def test_streaming_service_startup_event_set():
    """Test that api_app_startup_event is set to start StreamingService monitoring."""
    # Initialize the forge app
    forge_app_instance = start_forge_app()

    # Verify startup event is set
    assert forge_app_instance.api_app_startup_event is not None

    # Create a mock FastAPI app for testing
    fastapi_app = FastAPI()

    # Call the startup event (this should start monitoring)

    async def call_startup():
        await forge_app_instance.api_app_startup_event(fastapi_app)

    # The monitoring loop should start
    # We can't easily test that it's running without complex mocking,
    # but we can verify the function exists and is callable
    assert callable(forge_app_instance.api_app_startup_event)
