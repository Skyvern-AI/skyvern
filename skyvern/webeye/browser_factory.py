from __future__ import annotations

import asyncio
import os
import pathlib
import platform
import re
import shutil
import socket
import subprocess
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, cast
from urllib.parse import parse_qsl, urlparse

import psutil
import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    ConsoleMessage,
    Download,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import (
    Page,
    Playwright,
    Video,
)

from skyvern.config import settings
from skyvern.constants import (
    BROWSER_DOWNLOAD_TIMEOUT,
    SKYVERN_DIR,
)
from skyvern.exceptions import (
    UnknownBrowserType,
    UnknownErrorWhileCreatingBrowserContext,
)
from skyvern.forge import app
from skyvern.forge.sdk.api.files import get_download_dir, make_temp_directory, resolve_run_download_id
from skyvern.forge.sdk.core.skyvern_context import current, ensure_context
from skyvern.schemas.runs import ProxyLocation, ProxyLocationInput, get_tzinfo_from_proxy
from skyvern.webeye.browser_artifacts import BrowserArtifacts, VideoArtifact
from skyvern.webeye.cdp_connection import (
    build_cdp_connect_headers,
)
from skyvern.webeye.cdp_connection import connect_over_cdp_with_diagnostics as _connect_over_cdp_with_diagnostics
from skyvern.webeye.cdp_connection import (
    merge_cdp_connect_headers,
    parse_default_cdp_connect_headers,
)
from skyvern.webeye.cdp_download_interceptor import CDPDownloadInterceptor, bind_download_interceptor_to_context
from skyvern.webeye.dialog_handler import set_dialog_handler
from skyvern.webeye.session_cookies import restore_session_cookies

LOG = structlog.get_logger()


BrowserCleanupFunc = Callable[[], Awaitable[None]] | None
# Header to signal fresh browser context creation (stripped before sending to websites)
# When set to "true", creates a new incognito-like context instead of reusing existing ones
FRESH_CONTEXT_HEADER = "X-Skyvern-Fresh-Context"


@dataclass
class ParsedBrowserHeaders:
    """Result of parsing extra HTTP headers for internal Skyvern headers."""

    headers: dict[str, str]  # Headers to pass to browser (internal headers stripped)
    use_fresh_context: bool  # Whether to create a fresh browser context
    enable_download: bool  # Whether download interception is enabled


def parse_extra_headers(extra_http_headers: dict[str, str] | None) -> ParsedBrowserHeaders:
    """Parse extra HTTP headers and extract internal Skyvern headers.

    Extracts internal headers (case-insensitive) and returns a copy of headers
    without them, along with the extracted values.

    Args:
        extra_http_headers: Original headers dict (not mutated)

    Returns:
        ParsedBrowserHeaders with stripped headers and extracted values
    """
    headers: dict[str, str] = {}
    use_fresh_context = False
    enable_download = False

    if extra_http_headers:
        for key, value in extra_http_headers.items():
            key_lower = key.lower()
            if key_lower == FRESH_CONTEXT_HEADER.lower():
                use_fresh_context = value.lower() == "true"
            elif key_lower == "enable_download":
                enable_download = bool(value)
            else:
                headers[key] = value

    return ParsedBrowserHeaders(
        headers=headers,
        use_fresh_context=use_fresh_context,
        enable_download=enable_download,
    )


# RFC 7230 field-name token: the only characters Chromium accepts in a header name.
_VALID_HEADER_NAME_RE = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")
# Chromium rejects the whole batch if any value carries these header-injection chars.
_INVALID_HEADER_VALUE_RE = re.compile(r"[\r\n\x00]")


def sanitize_browser_headers(headers: dict[str, str] | None) -> dict[str, str] | None:
    """Drop entries whose name or value is malformed, so one bad header can't make Chromium
    reject the whole Network.setExtraHTTPHeaders batch and fail the launch."""
    if not headers:
        return None
    sanitized: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not _VALID_HEADER_NAME_RE.fullmatch(name):
            LOG.warning("Dropping invalid extra HTTP header name before browser launch", header_name=name)
            continue
        if not isinstance(value, str) or _INVALID_HEADER_VALUE_RE.search(value):
            LOG.warning("Dropping extra HTTP header with invalid value before browser launch", header_name=name)
            continue
        sanitized[name] = value
    return sanitized or None


def set_browser_console_log(browser_context: BrowserContext, browser_artifacts: BrowserArtifacts) -> None:
    if browser_artifacts.browser_console_log_path is None:
        log_path = f"{settings.LOG_PATH}/{datetime.utcnow().strftime('%Y-%m-%d')}/{uuid.uuid4()}.log"
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            # create the empty log file
            with open(log_path, "w") as _:
                pass
        except Exception:
            LOG.warning(
                "Failed to create browser log file",
                log_path=log_path,
                exc_info=True,
            )
            return
        browser_artifacts.browser_console_log_path = log_path

    async def browser_console_log(msg: ConsoleMessage) -> None:
        current_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        key_values = " ".join([f"{key}={value}" for key, value in msg.location.items()])
        format_log = f"{current_time}[{msg.type}]{msg.text} {key_values}\n"
        await browser_artifacts.append_browser_console_log(format_log)

    LOG.info("browser console log is saved", log_path=browser_artifacts.browser_console_log_path)
    browser_context.on("console", browser_console_log)


async def resolve_video_path(video: Video, timeout_seconds: float) -> str | None:
    """Wait for video.path() without ever cancelling patchright's shared artifact future"""
    path_task = asyncio.ensure_future(video.path())
    # Consume the task's eventual outcome even when it outlives this wait (timeout / caller
    # cancelled), so a late PlaywrightError on page close doesn't log "Task exception was
    # never retrieved".
    path_task.add_done_callback(_consume_abandoned_task_result)
    try:
        raw_path = await asyncio.wait_for(asyncio.shield(path_task), timeout_seconds)
    except TimeoutError:
        # path_task keeps waiting harmlessly; the shared future stays live for other awaiters.
        return None
    except asyncio.CancelledError:
        # path_task.cancelled() is direct evidence the shared artifact future was
        # cancelled by another awaiter (shield keeps caller cancellation from reaching
        # it); current_task().cancelling() is a counter that can read stale-nonzero
        # after any caught-but-not-uncancelled CancelledError.
        if path_task.cancelled():
            return None
        raise
    return str(raw_path) if raw_path is not None else None


def _consume_abandoned_task_result(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


def set_popup_video_listener(browser_context: BrowserContext, browser_artifacts: BrowserArtifacts) -> None:
    tracked_paths: set[str] = set()

    async def _on_page(page: Page) -> None:
        try:
            video = page.video
            if not video:
                return
            video_path_or_none = await resolve_video_path(video, settings.POPUP_VIDEO_PATH_TIMEOUT_SECONDS)
            if video_path_or_none is None:
                try:
                    page_origin = urlparse(page.url).hostname or "unknown"
                except Exception:
                    page_origin = "unknown"
                LOG.warning("Popup video path resolution timed out", page_origin=page_origin)
                return
            # The await above may have raced a discard (RealBrowserState closing this page
            # before it ever became the working page) — honor it even though it landed after.
            if browser_artifacts.is_page_video_discarded(page):
                return
            video_path = video_path_or_none
            # After the await, another handler may have already registered this path
            if video_path in tracked_paths:
                return
            for va in browser_artifacts.video_artifacts:
                if va.video_path == video_path:
                    tracked_paths.add(video_path)
                    return
            tracked_paths.add(video_path)
            browser_artifacts.video_artifacts.append(VideoArtifact(video_path=video_path))
        except PlaywrightError:
            LOG.debug("Failed to register popup page video", exc_info=True)
        except Exception:
            LOG.warning("Failed to register popup page video", exc_info=True)

    browser_context.on("page", _on_page)

    # Register pages that already exist (e.g. the initial page from launch_persistent_context)
    for page in browser_context.pages:
        asyncio.ensure_future(_on_page(page))


def _redact_url_query(url: str) -> str:
    # Download URLs are often S3 presigned, carrying an X-Amz signature in the query — drop it before logging.
    try:
        return urlparse(url)._replace(query="").geturl()
    except Exception:
        return "<redacted>"


def set_download_file_listener(
    browser_context: BrowserContext, download_timeout: float | None = None, **kwargs: Any
) -> None:
    async def listen_to_download(download: Download) -> None:
        context = current()
        workflow_run_id = (context.workflow_run_id if context else None) or kwargs.get("workflow_run_id")
        task_id = (context.task_id if context else None) or kwargs.get("task_id")
        try:
            async with asyncio.timeout(download_timeout or BROWSER_DOWNLOAD_TIMEOUT):
                file_path = await download.path()
                if not file_path.exists():
                    # On an adopted persistent session the bytes live on the run connection, not
                    # this worker connection; saving is the run side's job, so skip rather than crash.
                    LOG.debug(
                        "Download artifact absent on this connection; skipping worker-side rename",
                        workflow_run_id=workflow_run_id,
                        task_id=task_id,
                        suggested_filename=download.suggested_filename,
                    )
                    return
                if file_path.suffix:
                    return

                LOG.info(
                    "No file extensions, going to add file extension automatically",
                    workflow_run_id=workflow_run_id,
                    task_id=task_id,
                    suggested_filename=download.suggested_filename,
                    url=_redact_url_query(download.url),
                )
                suffix = Path(download.suggested_filename).suffix
                if suffix:
                    LOG.info(
                        "Add extension according to suggested filename",
                        workflow_run_id=workflow_run_id,
                        task_id=task_id,
                        filepath=str(file_path) + suffix,
                    )
                    file_path.rename(str(file_path) + suffix)
                    return

                parsed_url = urlparse(download.url)
                parsed_qs = parse_qsl(parsed_url.query)
                for key, value in parsed_qs:
                    if key.lower() == "filename":
                        suffix = Path(value).suffix
                        if suffix:
                            LOG.info(
                                "Add extension according to the parsed query params of download url",
                                workflow_run_id=workflow_run_id,
                                task_id=task_id,
                                filename=value,
                            )
                            file_path.rename(str(file_path) + suffix)
                            return

                suffix = Path(parsed_url.path).suffix
                if suffix:
                    LOG.info(
                        "Add extension according to download url path",
                        workflow_run_id=workflow_run_id,
                        task_id=task_id,
                        filepath=str(file_path) + suffix,
                    )
                    file_path.rename(str(file_path) + suffix)
                    return
                # TODO: maybe should try to parse it from URL response
        except asyncio.TimeoutError:
            LOG.error(
                "timeout to download file, going to cancel the download",
                workflow_run_id=workflow_run_id,
                task_id=task_id,
            )
            await download.cancel()

        except Exception:
            LOG.exception(
                "Failed to add file extension name to downloaded file",
                workflow_run_id=workflow_run_id,
                task_id=task_id,
            )

    def listen_to_new_page(page: Page) -> None:
        page.on("download", listen_to_download)

    browser_context.on("page", listen_to_new_page)


def initialize_download_dir() -> str:
    context = ensure_context()
    return get_download_dir(resolve_run_download_id(context))


async def rebind_download_dir(browser: Browser | None, run_id: str | None, *, page: Page | None = None) -> None:
    if not run_id:
        # No run_id means no run-scoped dir to bind to, so the session keeps its current download
        # binding. Adoption callers always pass a run id, so warn on the unexpected miss.
        LOG.warning("rebind_download_dir skipped: missing run_id")
        return
    download_dir = get_download_dir(run_id)

    if browser is not None:
        rebind_contexts = list(browser.contexts)
    elif page is not None:
        rebind_contexts = [page.context]
    else:
        LOG.warning("rebind_download_dir skipped: no browser or page to bind", run_id=run_id)
        return

    rebound_interceptors = 0
    monitor_owns_binding = False
    for context in rebind_contexts:
        interceptor: CDPDownloadInterceptor | None = getattr(context, "_skyvern_cdp_download_interceptor", None)
        if interceptor is not None:
            interceptor.set_download_dir(download_dir)
            rebound_interceptors += 1
            if interceptor.is_monitoring_browser_downloads():
                monitor_owns_binding = True

    setdownloadbehavior_applied = False
    if monitor_owns_binding:
        # The download monitor holds {behavior:deny, eventsEnabled:True} and saves files over HTTP
        # (remote CDP has no valid downloadPath). Re-sending allow/downloadPath would disable it, so
        # only its run-scoped dir is rebound above.
        LOG.info(
            "setDownloadBehavior skipped: download monitor owns binding",
            download_dir=download_dir,
            run_id=run_id,
            rebound_interceptors=rebound_interceptors,
            monitor_owns_binding=monitor_owns_binding,
        )
        return

    try:
        if browser is not None:
            cdp_session = await browser.new_browser_cdp_session()
        elif page is not None:
            # launch_persistent_context browsers expose no owning Browser, so acquire the CDP session
            # through the context.
            cdp_session = await page.context.new_cdp_session(page)
        else:
            return
        await cdp_session.send(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allow",
                "downloadPath": download_dir,
            },
        )
        setdownloadbehavior_applied = True
    except Exception:
        # Fail open: a rebind/setDownloadBehavior failure must never break a browser launch or run.
        # Downloads keep their launch-time binding.
        LOG.warning(
            "setDownloadBehavior rebind failed; keeping current download binding",
            download_dir=download_dir,
            run_id=run_id,
            rebound_interceptors=rebound_interceptors,
            exc_info=True,
        )
        return

    LOG.info(
        "setDownloadBehavior applied",
        download_dir=download_dir,
        run_id=run_id,
        rebound_interceptors=rebound_interceptors,
        setdownloadbehavior_applied=setdownloadbehavior_applied,
        monitor_owns_binding=monitor_owns_binding,
    )


async def _apply_download_behaviour(browser: Browser) -> None:
    context = ensure_context()
    await rebind_download_dir(browser, resolve_run_download_id(context))


class BrowserContextCreator(Protocol):
    def __call__(
        self, playwright: Playwright, proxy_location: ProxyLocationInput = None, **kwargs: dict[str, Any]
    ) -> Awaitable[tuple[BrowserContext, BrowserArtifacts, BrowserCleanupFunc]]: ...


class BrowserContextFactory:
    _creators: dict[str, BrowserContextCreator] = {}
    _validator: Callable[[Page], Awaitable[bool]] | None = None

    @staticmethod
    def get_subdir() -> str:
        curr_context = current()
        if curr_context and curr_context.task_id:
            return curr_context.task_id
        elif curr_context and curr_context.request_id:
            return curr_context.request_id
        return str(uuid.uuid4())

    @staticmethod
    def update_chromium_browser_preferences(
        user_data_dir: str,
        download_dir: str,
        preference_template_path: str | None = None,
    ) -> None:
        preference_dst_folder = f"{user_data_dir}/Default"
        os.makedirs(preference_dst_folder, exist_ok=True)

        preference_dst_file = f"{preference_dst_folder}/Preferences"
        preference_template = preference_template_path or f"{SKYVERN_DIR}/webeye/chromium_preferences.json"

        preference_file_content = ""
        with open(preference_template) as f:
            preference_file_content = f.read()
            preference_file_content = preference_file_content.replace("MASK_SAVEFILE_DEFAULT_DIRECTORY", download_dir)
            preference_file_content = preference_file_content.replace("MASK_DOWNLOAD_DEFAULT_DIRECTORY", download_dir)
        with open(preference_dst_file, "w") as f:
            f.write(preference_file_content)

    @staticmethod
    def build_browser_args(
        proxy_location: ProxyLocationInput = None,
        cdp_port: int | None = None,
        extra_http_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        video_dir = f"{settings.VIDEO_PATH}/{datetime.utcnow().strftime('%Y-%m-%d')}"
        har_dir = (
            f"{settings.HAR_PATH}/{datetime.utcnow().strftime('%Y-%m-%d')}/{BrowserContextFactory.get_subdir()}.har"
        )

        extension_paths = []
        if settings.EXTENSIONS and settings.EXTENSIONS_BASE_PATH:
            try:
                os.makedirs(settings.EXTENSIONS_BASE_PATH, exist_ok=True)

                extension_paths = [str(Path(settings.EXTENSIONS_BASE_PATH) / ext) for ext in settings.EXTENSIONS]
                LOG.info("Extensions paths constructed", extension_paths=extension_paths)
            except Exception as e:
                LOG.error("Error constructing extension paths", error=str(e))

        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--disk-cache-size=1",
            "--start-maximized",
            "--kiosk-printing",
        ]

        if cdp_port:
            browser_args.append(f"--remote-debugging-port={cdp_port}")

        if extension_paths:
            joined_paths = ",".join(extension_paths)
            browser_args.extend([f"--disable-extensions-except={joined_paths}", f"--load-extension={joined_paths}"])
            LOG.info("Extensions added to browser args", extensions=joined_paths)

        browser_args.extend(settings.BROWSER_ADDITIONAL_ARGS)
        args = {
            "color_scheme": "no-preference",
            "args": browser_args,
            "ignore_default_args": [
                "--enable-automation",
            ],
            "record_har_path": har_dir,
            "record_video_dir": video_dir,
            "viewport": {
                "width": settings.BROWSER_WIDTH,
                "height": settings.BROWSER_HEIGHT,
            },
            "extra_http_headers": sanitize_browser_headers(extra_http_headers),
        }
        if settings.BROWSER_RECORDING_WIDTH and settings.BROWSER_RECORDING_HEIGHT:
            args["record_video_size"] = {
                "width": settings.BROWSER_RECORDING_WIDTH,
                "height": settings.BROWSER_RECORDING_HEIGHT,
            }
        if settings.BROWSER_LOCALE:
            args["locale"] = settings.BROWSER_LOCALE

        if isinstance(proxy_location, ProxyLocation):
            if tz_info := get_tzinfo_from_proxy(proxy_location=proxy_location):
                args["timezone_id"] = tz_info.key
        return args

    @staticmethod
    def build_browser_artifacts(
        video_artifacts: list[VideoArtifact] | None = None,
        har_path: str | None = None,
        traces_dir: str | None = None,
        browser_session_dir: str | None = None,
        browser_console_log_path: str | None = None,
    ) -> BrowserArtifacts:
        return BrowserArtifacts(
            video_artifacts=video_artifacts or [],
            har_path=har_path,
            traces_dir=traces_dir,
            browser_session_dir=browser_session_dir,
            browser_console_log_path=browser_console_log_path,
        )

    @classmethod
    def register_type(cls, browser_type: str, creator: BrowserContextCreator) -> None:
        cls._creators[browser_type] = creator

    @classmethod
    async def create_browser_context(
        cls, playwright: Playwright, **kwargs: Any
    ) -> tuple[BrowserContext, BrowserArtifacts, BrowserCleanupFunc]:
        browser_type = settings.BROWSER_TYPE
        browser_context: BrowserContext | None = None
        cleanup_func: BrowserCleanupFunc = None
        try:
            creator = cls._creators.get(browser_type)
            if not creator:
                raise UnknownBrowserType(browser_type)
            browser_context, browser_artifacts, cleanup_func = await creator(playwright, **kwargs)
            await restore_session_cookies(browser_context, browser_artifacts.browser_session_dir)
            if settings.BROWSER_LOGS_ENABLED:
                set_browser_console_log(browser_context=browser_context, browser_artifacts=browser_artifacts)
            set_popup_video_listener(browser_context=browser_context, browser_artifacts=browser_artifacts)
            set_download_file_listener(browser_context=browser_context, **kwargs)
            set_dialog_handler(browser_context=browser_context)
            await app.AGENT_FUNCTION.setup_browser_context_extensions(browser_context=browser_context, **kwargs)

            proxy_location: ProxyLocationInput = kwargs.get("proxy_location")
            if isinstance(proxy_location, ProxyLocation):
                context = ensure_context()
                context.tz_info = get_tzinfo_from_proxy(proxy_location)

            return browser_context, browser_artifacts, cleanup_func
        except BaseException as e:
            if browser_context is not None:
                # FIXME: sometimes it can't close the browser context?
                LOG.error("unexpected error happens after created browser context, going to close the context")
                with suppress(Exception):
                    await browser_context.close()
            if cleanup_func:
                with suppress(Exception):
                    await cleanup_func()

            if not isinstance(e, Exception) or isinstance(e, UnknownBrowserType):
                raise e

            raise UnknownErrorWhileCreatingBrowserContext(browser_type, e) from e


def _is_display_server_error(error: Exception) -> bool:
    """Return True if the worker cannot initialize the browser display/graphics stack.

    These errors appear when a headed browser is launched on a worker node
    without a usable display/graphics environment. Retrying with a fresh profile
    will not help — the fix is environment-side (display/EGL/SwiftShader support)
    rather than profile-side.
    """
    error_str = str(error).lower()
    display_indicators = [
        "missing x server",
        "xserver running",
        "no display",
        "$display",
        "the platform failed to initialize",
        "no suitable egl configs found",
        "failed to get config for surface",
        "collectgraphicsinfo failed",
        "glcontext::createoffscreenglsurface failed",
        "exiting gpu process due to errors during initialization",
    ]
    return any(indicator in error_str for indicator in display_indicators)


def _is_browser_profile_corruption_error(error: Exception) -> bool:
    """Return True if the error is consistent with a corrupted or bloated browser profile.

    These errors appear when launch_persistent_context fails to start Chrome because
    the user_data_dir is in a bad state (corrupted files, oversized cache, lock files
    from a prior crash, etc.).  The error text comes from Playwright's CDP driver.
    """
    if _is_display_server_error(error):
        return False

    error_str = str(error).lower()
    corruption_indicators = [
        "connection closed while reading from the driver",
        "target closed",
        "browser has been closed",
        "failed to launch",
        "unable to open database",
    ]
    return any(indicator in error_str for indicator in corruption_indicators)


def _get_cdp_port(kwargs: dict) -> int | None:
    raw_cdp_port = kwargs.get("cdp_port")
    if isinstance(raw_cdp_port, (int, str)):
        try:
            return int(raw_cdp_port)
        except (ValueError, TypeError):
            return None
    return None


def _is_port_in_use(port: int) -> bool:
    """Check if a port is already in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("localhost", port))
            return False
        except OSError:
            return True


def _is_chrome_running() -> bool:
    """Check if Chrome is already running."""
    chrome_process_names = ["chrome", "google-chrome", "google chrome"]
    for proc in psutil.process_iter(["name"]):
        try:
            proc_name = proc.info["name"].lower()
            if proc_name == "chrome_crashpad_handler":
                continue
            if any(chrome_name in proc_name for chrome_name in chrome_process_names):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return False


async def _create_headless_chromium(
    playwright: Playwright,
    proxy_location: ProxyLocationInput = None,
    extra_http_headers: dict[str, str] | None = None,
    cdp_connect_headers: dict[str, str] | None = None,
    **kwargs: dict,
) -> tuple[BrowserContext, BrowserArtifacts, BrowserCleanupFunc]:
    if browser_address := kwargs.get("browser_address"):
        return await _connect_to_cdp_browser(
            playwright,
            remote_browser_url=str(browser_address),
            extra_http_headers=extra_http_headers,
            cdp_connect_headers=cdp_connect_headers,
            apply_download_behaviour=True,
        )

    # Check for browser_profile_id and load from storage if available
    browser_profile_id = cast(str | None, kwargs.get("browser_profile_id"))
    organization_id_for_profile = cast(str | None, kwargs.get("organization_id"))
    user_data_dir: str | None = None
    loaded_from_saved_profile = False

    if browser_profile_id and organization_id_for_profile:
        profile_dir = await app.STORAGE.retrieve_browser_profile(
            organization_id=organization_id_for_profile,
            profile_id=browser_profile_id,
        )
        if profile_dir:
            user_data_dir = profile_dir
            loaded_from_saved_profile = True
            LOG.info(
                "Using browser profile",
                browser_profile_id=browser_profile_id,
                profile_dir=profile_dir,
            )
        else:
            LOG.warning(
                "Browser profile not found, using temp directory",
                browser_profile_id=browser_profile_id,
                organization_id=organization_id_for_profile,
            )

    if not user_data_dir:
        user_data_dir = make_temp_directory(prefix="skyvern_browser_")

    download_dir = initialize_download_dir()
    BrowserContextFactory.update_chromium_browser_preferences(
        user_data_dir=user_data_dir,
        download_dir=download_dir,
    )
    cdp_port: int | None = _get_cdp_port(kwargs)
    browser_args = BrowserContextFactory.build_browser_args(
        proxy_location=proxy_location, cdp_port=cdp_port, extra_http_headers=extra_http_headers
    )
    browser_args.update(
        {
            "user_data_dir": user_data_dir,
            "downloads_path": download_dir,
        }
    )

    browser_artifacts = BrowserContextFactory.build_browser_artifacts(
        har_path=browser_args["record_har_path"],
        browser_session_dir=user_data_dir,
    )
    try:
        browser_context = await playwright.chromium.launch_persistent_context(**browser_args)
    except Exception as launch_error:
        if loaded_from_saved_profile and _is_browser_profile_corruption_error(launch_error):
            LOG.warning(
                "Browser launch failed with saved profile — profile may be corrupted, falling back to fresh profile",
                browser_profile_id=browser_profile_id,
                organization_id=organization_id_for_profile,
                error=str(launch_error),
            )
            fallback_dir = make_temp_directory(prefix="skyvern_browser_")
            BrowserContextFactory.update_chromium_browser_preferences(
                user_data_dir=fallback_dir,
                download_dir=download_dir,
            )
            browser_args["user_data_dir"] = fallback_dir
            browser_artifacts = BrowserContextFactory.build_browser_artifacts(
                har_path=browser_args["record_har_path"],
                browser_session_dir=fallback_dir,
            )
            browser_context = await playwright.chromium.launch_persistent_context(**browser_args)
        else:
            raise
    return browser_context, browser_artifacts, None


async def _create_headful_chromium(
    playwright: Playwright,
    proxy_location: ProxyLocationInput = None,
    extra_http_headers: dict[str, str] | None = None,
    cdp_connect_headers: dict[str, str] | None = None,
    **kwargs: dict,
) -> tuple[BrowserContext, BrowserArtifacts, BrowserCleanupFunc]:
    if browser_address := kwargs.get("browser_address"):
        return await _connect_to_cdp_browser(
            playwright,
            remote_browser_url=str(browser_address),
            extra_http_headers=extra_http_headers,
            cdp_connect_headers=cdp_connect_headers,
            apply_download_behaviour=True,
        )

    # Check for browser_profile_id and load from storage if available
    browser_profile_id = cast(str | None, kwargs.get("browser_profile_id"))
    organization_id_for_profile = cast(str | None, kwargs.get("organization_id"))
    user_data_dir: str | None = None
    loaded_from_saved_profile = False

    if browser_profile_id and organization_id_for_profile:
        profile_dir = await app.STORAGE.retrieve_browser_profile(
            organization_id=organization_id_for_profile,
            profile_id=browser_profile_id,
        )
        if profile_dir:
            user_data_dir = profile_dir
            loaded_from_saved_profile = True
            LOG.info(
                "Using browser profile",
                browser_profile_id=browser_profile_id,
                profile_dir=profile_dir,
            )
        else:
            LOG.warning(
                "Browser profile not found, using temp directory",
                browser_profile_id=browser_profile_id,
                organization_id=organization_id_for_profile,
            )

    if not user_data_dir:
        user_data_dir = make_temp_directory(prefix="skyvern_browser_")

    download_dir = initialize_download_dir()
    BrowserContextFactory.update_chromium_browser_preferences(
        user_data_dir=user_data_dir,
        download_dir=download_dir,
    )
    cdp_port: int | None = _get_cdp_port(kwargs)
    browser_args = BrowserContextFactory.build_browser_args(
        proxy_location=proxy_location, cdp_port=cdp_port, extra_http_headers=extra_http_headers
    )
    browser_args.update(
        {
            "user_data_dir": user_data_dir,
            "downloads_path": download_dir,
            "headless": False,
        }
    )
    browser_artifacts = BrowserContextFactory.build_browser_artifacts(
        har_path=browser_args["record_har_path"],
        browser_session_dir=user_data_dir,
    )
    try:
        browser_context = await playwright.chromium.launch_persistent_context(**browser_args)
    except Exception as launch_error:
        if loaded_from_saved_profile and _is_browser_profile_corruption_error(launch_error):
            LOG.warning(
                "Browser launch failed with saved profile — profile may be corrupted, falling back to fresh profile",
                browser_profile_id=browser_profile_id,
                organization_id=organization_id_for_profile,
                error=str(launch_error),
            )
            fallback_dir = make_temp_directory(prefix="skyvern_browser_")
            BrowserContextFactory.update_chromium_browser_preferences(
                user_data_dir=fallback_dir,
                download_dir=download_dir,
            )
            browser_args["user_data_dir"] = fallback_dir
            browser_artifacts = BrowserContextFactory.build_browser_artifacts(
                har_path=browser_args["record_har_path"],
                browser_session_dir=fallback_dir,
            )
            browser_context = await playwright.chromium.launch_persistent_context(**browser_args)
        else:
            raise
    return browser_context, browser_artifacts, None


def default_user_data_dir() -> pathlib.Path:
    p = platform.system()
    if p == "Darwin":
        return pathlib.Path("~/Library/Application Support/Google/Chrome").expanduser()
    if p == "Windows":
        return pathlib.Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"
    # Assume Linux/Unix
    return pathlib.Path("~/.config/google-chrome").expanduser()


def is_valid_chromium_user_data_dir(directory: str) -> bool:
    """Check if a directory is a valid Chromium user data directory.

    A valid Chromium user data directory should:
    1. Exist
    2. Not be empty
    3. Contain a 'Default' directory
    4. Have a 'Preferences' file in the 'Default' directory
    """
    if not os.path.exists(directory):
        return False

    default_dir = os.path.join(directory, "Default")
    preferences_file = os.path.join(default_dir, "Preferences")

    return os.path.isdir(directory) and os.path.isdir(default_dir) and os.path.isfile(preferences_file)


async def _create_cdp_connection_browser(
    playwright: Playwright,
    proxy_location: ProxyLocationInput = None,
    extra_http_headers: dict[str, str] | None = None,
    cdp_connect_headers: dict[str, str] | None = None,
    **kwargs: dict,
) -> tuple[BrowserContext, BrowserArtifacts, BrowserCleanupFunc]:
    if browser_address := kwargs.get("browser_address"):
        return await _connect_to_cdp_browser(
            playwright,
            remote_browser_url=str(browser_address),
            extra_http_headers=extra_http_headers,
            cdp_connect_headers=cdp_connect_headers,
            apply_download_behaviour=True,
        )

    browser_type = settings.BROWSER_TYPE
    browser_path = settings.CHROME_EXECUTABLE_PATH

    if browser_type == "cdp-connect" and browser_path:
        LOG.info("Local browser path is given. Connecting to local browser with CDP", browser_path=browser_path)
        # First check if the debugging port is running and can be used
        if not _is_port_in_use(9222):
            LOG.info("Port 9222 is not in use, starting Chrome", browser_path=browser_path)
            # Check if Chrome is already running
            if _is_chrome_running():
                raise Exception(
                    "Chrome is already running. Please close all Chrome instances before starting with remote debugging."
                )
            # check if ./tmp/user_data_dir exists and if it's a valid Chromium user data directory
            try:
                if os.path.exists("./tmp/user_data_dir") and not is_valid_chromium_user_data_dir("./tmp/user_data_dir"):
                    LOG.info("Removing invalid user data directory")
                    shutil.rmtree("./tmp/user_data_dir")
                    shutil.copytree(default_user_data_dir(), "./tmp/user_data_dir")
                elif not os.path.exists("./tmp/user_data_dir"):
                    LOG.info("Copying default user data directory")
                    shutil.copytree(default_user_data_dir(), "./tmp/user_data_dir")
                else:
                    LOG.info("User data directory is valid")
            except FileExistsError:
                # If directory exists, remove it first then copy
                shutil.rmtree("./tmp/user_data_dir")
                shutil.copytree(default_user_data_dir(), "./tmp/user_data_dir")
            browser_process = subprocess.Popen(
                [
                    browser_path,
                    "--remote-debugging-port=9222",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--user-data-dir=./tmp/user_data_dir",
                    "--remote-debugging-address=0.0.0.0",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            # Add small delay to allow browser to start
            time.sleep(1)
            if browser_process.poll() is not None:
                raise Exception(f"Failed to open browser. browser_path: {browser_path}")
        else:
            LOG.info("Port 9222 is in use, using existing browser")

    return await _connect_to_cdp_browser(
        playwright,
        settings.BROWSER_REMOTE_DEBUGGING_URL,
        extra_http_headers=extra_http_headers,
        cdp_connect_headers=cdp_connect_headers,
    )


async def _connect_to_cdp_browser(
    playwright: Playwright,
    remote_browser_url: str,
    extra_http_headers: dict[str, str] | None = None,
    cdp_connect_headers: dict[str, str] | None = None,
    apply_download_behaviour: bool = False,
) -> tuple[BrowserContext, BrowserArtifacts, BrowserCleanupFunc]:
    parsed_headers = parse_extra_headers(extra_http_headers)

    browser_args = BrowserContextFactory.build_browser_args(
        extra_http_headers=parsed_headers.headers if parsed_headers.headers else None
    )

    browser_artifacts = BrowserContextFactory.build_browser_artifacts(
        har_path=browser_args["record_har_path"],
    )
    # Single chokepoint for OSS remote-CDP creation; stamp the marker so
    # RealBrowserManager attaches the CDP frame publisher.
    browser_artifacts.needs_cdp_frame_publisher = True

    LOG.info("Connecting browser CDP connection", remote_browser_url=remote_browser_url)
    cdp_headers = merge_cdp_connect_headers(
        default_headers=parse_default_cdp_connect_headers(settings.BROWSER_REMOTE_DEBUGGING_CONNECT_HEADERS),
        per_row_headers=cdp_connect_headers,
        managed_host_header=build_cdp_connect_headers(settings.BROWSER_REMOTE_DEBUGGING_HOST_HEADER) or {},
    )
    browser = await _connect_over_cdp_with_diagnostics(
        playwright,
        remote_browser_url,
        headers=cdp_headers or None,
        timeout_ms=settings.BROWSER_CDP_CONNECT_TIMEOUT_MS,
    )

    if apply_download_behaviour:
        try:
            await _apply_download_behaviour(browser)
        except Exception:
            # Fail open: a download-behaviour rebind failure must never break a browser launch.
            LOG.warning("Failed to apply download behaviour on browser launch", exc_info=True)

    # Decide whether to create fresh context or reuse existing one
    contexts = browser.contexts
    browser_context = None

    if parsed_headers.use_fresh_context or not contexts:
        LOG.info(
            "Creating new browser context",
            fresh_context_requested=parsed_headers.use_fresh_context,
            existing_contexts=len(contexts),
        )
        browser_context = await browser.new_context(
            record_video_dir=browser_args["record_video_dir"],
            record_video_size=browser_args.get("record_video_size"),
            viewport=browser_args["viewport"],
            extra_http_headers=browser_args["extra_http_headers"],
        )
    else:
        LOG.info("Reusing existing browser context", existing_contexts=len(contexts))
        browser_context = contexts[0]

    # Enable CDPDownloadInterceptor when enable_download is set.
    # This captures downloads via the Fetch domain and saves them locally.
    if parsed_headers.enable_download:
        download_dir = initialize_download_dir()
        interceptor = CDPDownloadInterceptor(output_dir=download_dir)

        # Enable interception on all existing pages
        for page in browser_context.pages:
            try:
                await interceptor.enable_for_page(page)
            except Exception:
                LOG.warning("Failed to enable CDP intercept on page", page_url=page.url, exc_info=True)

        await bind_download_interceptor_to_context(interceptor, browser_context)
        browser_context._skyvern_cdp_download_active = True  # type: ignore[attr-defined]
        LOG.info(
            "CDP download interceptor enabled",
            download_dir=download_dir,
            existing_page_count=len(browser_context.pages),
        )

    LOG.info(
        "Launched browser CDP connection",
        remote_browser_url=remote_browser_url,
    )
    return browser_context, browser_artifacts, None


BrowserContextFactory.register_type("chromium-headless", _create_headless_chromium)
BrowserContextFactory.register_type("chromium-headful", _create_headful_chromium)
BrowserContextFactory.register_type("cdp-connect", _create_cdp_connection_browser)
