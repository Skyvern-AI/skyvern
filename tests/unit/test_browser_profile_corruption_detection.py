"""Tests for browser launch error classification in browser_factory.py."""

from skyvern.webeye.browser_factory import (
    _is_browser_profile_corruption_error,
    _is_display_server_error,
)

# -- _is_display_server_error ------------------------------------------------


class TestIsDisplayServerError:
    """Detect browser launch environment errors so we skip the profile-corruption retry."""

    def test_missing_x_server_message(self) -> None:
        error = Exception(
            "BrowserType.launch_persistent_context: Target page, context or browser has been closed\n\n"
            "Browser logs:\n"
            "Looks like you launched a headed browser without having a XServer running.\n"
            "Set either 'headless: true' or use 'xvfb-run <your-playwright-app>' before running Playwright."
        )
        assert _is_display_server_error(error) is True

    def test_missing_x_server_with_display_var(self) -> None:
        error = Exception("[err] Missing X server or $DISPLAY")
        assert _is_display_server_error(error) is True

    def test_platform_failed_to_initialize(self) -> None:
        error = Exception("[err] ui/aura/env.cc: The platform failed to initialize. Exiting.")
        assert _is_display_server_error(error) is True

    def test_egl_config_failure(self) -> None:
        error = Exception(
            "[err] [297028:297028:0407/015340.854525:ERROR:ui/gl/gl_surface_egl.cc:1013] No suitable EGL configs found for initialization."
        )
        assert _is_display_server_error(error) is True

    def test_gpu_process_initialization_failure(self) -> None:
        error = Exception(
            "[err] [297028:297028:0407/015340.854713:ERROR:gpu/ipc/service/gpu_init.cc:118] CollectGraphicsInfo failed."
        )
        assert _is_display_server_error(error) is True

    def test_no_display_env(self) -> None:
        error = Exception("No display environment variable set")
        assert _is_display_server_error(error) is True

    def test_actual_corruption_is_not_display_error(self) -> None:
        error = Exception("unable to open database file")
        assert _is_display_server_error(error) is False

    def test_target_closed_without_display_context(self) -> None:
        error = Exception("Target page, context or browser has been closed")
        assert _is_display_server_error(error) is False

    def test_generic_error_is_not_display_error(self) -> None:
        error = Exception("something completely unrelated went wrong")
        assert _is_display_server_error(error) is False


# -- _is_browser_profile_corruption_error ------------------------------------


class TestIsBrowserProfileCorruptionError:
    """Profile corruption detection must NOT match display-server errors."""

    def test_xserver_error_not_classified_as_corruption(self) -> None:
        error = Exception(
            "BrowserType.launch_persistent_context: Target page, context or browser has been closed\n\n"
            "Browser logs:\n"
            "Looks like you launched a headed browser without having a XServer running.\n"
            "[err] Missing X server or $DISPLAY\n"
            "[err] ui/aura/env.cc: The platform failed to initialize. Exiting."
        )
        assert _is_browser_profile_corruption_error(error) is False

    def test_egl_error_not_classified_as_corruption(self) -> None:
        error = Exception(
            "BrowserType.launch_persistent_context: Target page, context or browser has been closed\n\n"
            "[err] [297028:297028:0407/015340.854525:ERROR:ui/gl/gl_surface_egl.cc:1013] No suitable EGL configs found for initialization.\n"
            "[err] [297028:297028:0407/015340.854713:ERROR:gpu/ipc/service/gpu_init.cc:118] CollectGraphicsInfo failed."
        )
        assert _is_browser_profile_corruption_error(error) is False

    def test_target_closed_without_xserver_is_corruption(self) -> None:
        """A plain 'target closed' without XServer context is still corruption."""
        error = Exception("target closed")
        assert _is_browser_profile_corruption_error(error) is True

    def test_connection_closed_while_reading(self) -> None:
        error = Exception("connection closed while reading from the driver")
        assert _is_browser_profile_corruption_error(error) is True

    def test_browser_has_been_closed(self) -> None:
        error = Exception("browser has been closed")
        assert _is_browser_profile_corruption_error(error) is True

    def test_failed_to_launch(self) -> None:
        error = Exception("failed to launch chromium")
        assert _is_browser_profile_corruption_error(error) is True

    def test_unable_to_open_database(self) -> None:
        error = Exception("unable to open database file")
        assert _is_browser_profile_corruption_error(error) is True

    def test_unrelated_error_is_not_corruption(self) -> None:
        error = Exception("network timeout after 30 seconds")
        assert _is_browser_profile_corruption_error(error) is False
