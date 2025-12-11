from __future__ import annotations

import asyncio
import os
import pathlib
import platform
import random
import re
import shutil
import socket
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, cast
from urllib.parse import parse_qsl, urlparse

import psutil
import structlog
from playwright.async_api import Browser, BrowserContext, ConsoleMessage, Download, Page, Playwright

from skyvern.config import settings
from skyvern.constants import (
    BROWSER_DOWNLOAD_TIMEOUT,
    SKYVERN_DIR,
)
from skyvern.exceptions import UnknownBrowserType, UnknownErrorWhileCreatingBrowserContext
from skyvern.forge import app
from skyvern.forge.sdk.api.files import get_download_dir, make_temp_directory
from skyvern.forge.sdk.core.skyvern_context import current, ensure_context
from skyvern.schemas.runs import ProxyLocation, get_tzinfo_from_proxy
from skyvern.webeye.browser_artifacts import BrowserArtifacts, VideoArtifact

LOG = structlog.get_logger()

BrowserCleanupFunc = Callable[[], None] | None


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


def set_download_file_listener(
    browser_context: BrowserContext, download_timeout: float | None = None, **kwargs: Any
) -> None:
    async def listen_to_download(download: Download) -> None:
        workflow_run_id = kwargs.get("workflow_run_id")
        task_id = kwargs.get("task_id")
        try:
            async with asyncio.timeout(download_timeout or BROWSER_DOWNLOAD_TIMEOUT):
                file_path = await download.path()
                if file_path.suffix:
                    return

                LOG.info(
                    "No file extensions, going to add file extension automatically",
                    workflow_run_id=workflow_run_id,
                    task_id=task_id,
                    suggested_filename=download.suggested_filename,
                    url=download.url,
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
    return get_download_dir(
        context.run_id if context and context.run_id else context.workflow_run_id or context.task_id
    )


async def _apply_download_behaviour(browser: Browser) -> None:
    context = ensure_context()
    download_dir = get_download_dir(
        context.run_id if context and context.run_id else context.workflow_run_id or context.task_id
    )
    cdp_session = await browser.new_browser_cdp_session()
    await cdp_session.send(
        "Browser.setDownloadBehavior",
        {
            "behavior": "allow",
            "downloadPath": download_dir,
        },
    )

    LOG.info("setDownloadBehavior applied", download_dir=download_dir)


class BrowserContextCreator(Protocol):
    def __call__(
        self, playwright: Playwright, proxy_location: ProxyLocation | None = None, **kwargs: dict[str, Any]
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
    def update_chromium_browser_preferences(user_data_dir: str, download_dir: str) -> None:
        preference_dst_folder = f"{user_data_dir}/Default"
        os.makedirs(preference_dst_folder, exist_ok=True)

        preference_dst_file = f"{preference_dst_folder}/Preferences"
        preference_template = f"{SKYVERN_DIR}/webeye/chromium_preferences.json"

        preference_file_content = ""
        with open(preference_template) as f:
            preference_file_content = f.read()
            preference_file_content = preference_file_content.replace("MASK_SAVEFILE_DEFAULT_DIRECTORY", download_dir)
            preference_file_content = preference_file_content.replace("MASK_DOWNLOAD_DEFAULT_DIRECTORY", download_dir)
        with open(preference_dst_file, "w") as f:
            f.write(preference_file_content)

    @staticmethod
    def build_browser_args(
        proxy_location: ProxyLocation | None = None,
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

        args = {
            "locale": settings.BROWSER_LOCALE,
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
            "extra_http_headers": extra_http_headers,
        }

        if settings.ENABLE_PROXY:
            proxy_config = setup_proxy()
            if proxy_config:
                args["proxy"] = proxy_config

        if proxy_location:
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
        try:
            creator = cls._creators.get(browser_type)
            if not creator:
                raise UnknownBrowserType(browser_type)
            browser_context, browser_artifacts, cleanup_func = await creator(playwright, **kwargs)
            if settings.BROWSER_LOGS_ENABLED:
                set_browser_console_log(browser_context=browser_context, browser_artifacts=browser_artifacts)
            set_download_file_listener(browser_context=browser_context, **kwargs)

            proxy_location: ProxyLocation | None = kwargs.get("proxy_location")
            if proxy_location is not None:
                context = ensure_context()
                context.tz_info = get_tzinfo_from_proxy(proxy_location)

            return browser_context, browser_artifacts, cleanup_func
        except Exception as e:
            if browser_context is not None:
                # FIXME: sometimes it can't close the browser context?
                LOG.error("unexpected error happens after created browser context, going to close the context")
                await browser_context.close()

            if isinstance(e, UnknownBrowserType):
                raise e

            raise UnknownErrorWhileCreatingBrowserContext(browser_type, e) from e


def setup_proxy() -> dict | None:
    if not settings.HOSTED_PROXY_POOL or settings.HOSTED_PROXY_POOL.strip() == "":
        LOG.warning("No proxy server value found. Continuing without using proxy...")
        return None

    proxy_servers = [server.strip() for server in settings.HOSTED_PROXY_POOL.split(",") if server.strip()]

    if not proxy_servers:
        LOG.warning("Proxy pool contains only empty values. Continuing without proxy...")
        return None

    valid_proxies = []
    for proxy in proxy_servers:
        if _is_valid_proxy_url(proxy):
            valid_proxies.append(proxy)
        else:
            LOG.warning(f"Invalid proxy URL format: {proxy}")

    if not valid_proxies:
        LOG.warning("No valid proxy URLs found. Continuing without proxy...")
        return None

    try:
        proxy_server = random.choice(valid_proxies)
        proxy_creds = _get_proxy_server_creds(proxy_server)

        LOG.info("Found proxy server creds, using them...")

        return {
            "server": proxy_server,
            "username": proxy_creds.get("username", ""),
            "password": proxy_creds.get("password", ""),
        }
    except Exception as e:
        LOG.warning(f"Error setting up proxy: {e}. Continuing without proxy...")
        return None


def _is_valid_proxy_url(url: str) -> bool:
    PROXY_PATTERN = re.compile(r"^(http|https|socks5):\/\/([^:@]+(:[^@]*)?@)?[^\s:\/]+(:\d+)?$")
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return False
        return bool(PROXY_PATTERN.match(url))
    except Exception:
        return False


def _get_proxy_server_creds(proxy: str) -> dict:
    parsed_url = urlparse(proxy)
    if parsed_url.username and parsed_url.password:
        return {"username": parsed_url.username, "password": parsed_url.password}
    LOG.warning("No credentials found in the proxy URL.")
    return {}


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
    proxy_location: ProxyLocation | None = None,
    extra_http_headers: dict[str, str] | None = None,
    **kwargs: dict,
) -> tuple[BrowserContext, BrowserArtifacts, BrowserCleanupFunc]:
    if browser_address := kwargs.get("browser_address"):
        return await _connect_to_cdp_browser(
            playwright,
            remote_browser_url=str(browser_address),
            extra_http_headers=extra_http_headers,
            apply_download_behaviour=True,
        )

    # Check for browser_profile_id and load from storage if available
    browser_profile_id = cast(str | None, kwargs.get("browser_profile_id"))
    organization_id_for_profile = cast(str | None, kwargs.get("organization_id"))
    user_data_dir: str | None = None

    if browser_profile_id and organization_id_for_profile:
        profile_dir = await app.STORAGE.retrieve_browser_profile(
            organization_id=organization_id_for_profile,
            profile_id=browser_profile_id,
        )
        if profile_dir:
            user_data_dir = profile_dir
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
    browser_context = await playwright.chromium.launch_persistent_context(**browser_args)
    return browser_context, browser_artifacts, None


async def _create_headful_chromium(
    playwright: Playwright,
    proxy_location: ProxyLocation | None = None,
    extra_http_headers: dict[str, str] | None = None,
    **kwargs: dict,
) -> tuple[BrowserContext, BrowserArtifacts, BrowserCleanupFunc]:
    if browser_address := kwargs.get("browser_address"):
        return await _connect_to_cdp_browser(
            playwright,
            remote_browser_url=str(browser_address),
            extra_http_headers=extra_http_headers,
            apply_download_behaviour=True,
        )

    # Check for browser_profile_id and load from storage if available
    browser_profile_id = cast(str | None, kwargs.get("browser_profile_id"))
    organization_id_for_profile = cast(str | None, kwargs.get("organization_id"))
    user_data_dir: str | None = None

    if browser_profile_id and organization_id_for_profile:
        profile_dir = await app.STORAGE.retrieve_browser_profile(
            organization_id=organization_id_for_profile,
            profile_id=browser_profile_id,
        )
        if profile_dir:
            user_data_dir = profile_dir
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
    browser_context = await playwright.chromium.launch_persistent_context(**browser_args)
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
    proxy_location: ProxyLocation | None = None,
    extra_http_headers: dict[str, str] | None = None,
    **kwargs: dict,
) -> tuple[BrowserContext, BrowserArtifacts, BrowserCleanupFunc]:
    if browser_address := kwargs.get("browser_address"):
        return await _connect_to_cdp_browser(
            playwright,
            remote_browser_url=str(browser_address),
            extra_http_headers=extra_http_headers,
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
                    "--remote-debugging-address=0.0.0.0",
                    "--user-data-dir=./tmp/user_data_dir",
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

    return await _connect_to_cdp_browser(playwright, settings.BROWSER_REMOTE_DEBUGGING_URL, extra_http_headers)


async def _connect_to_cdp_browser(
    playwright: Playwright,
    remote_browser_url: str,
    extra_http_headers: dict[str, str] | None = None,
    apply_download_behaviour: bool = False,
) -> tuple[BrowserContext, BrowserArtifacts, BrowserCleanupFunc]:
    browser_args = BrowserContextFactory.build_browser_args(extra_http_headers=extra_http_headers)

    browser_artifacts = BrowserContextFactory.build_browser_artifacts(
        har_path=browser_args["record_har_path"],
    )

    LOG.info("Connecting browser CDP connection", remote_browser_url=remote_browser_url)
    browser = await playwright.chromium.connect_over_cdp(remote_browser_url)

    if apply_download_behaviour:
        await _apply_download_behaviour(browser)

    contexts = browser.contexts
    browser_context = None

    if contexts:
        # Use the first existing context if available
        LOG.info("Using existing browser context")
        browser_context = contexts[0]
    else:
        browser_context = await browser.new_context(
            record_video_dir=browser_args["record_video_dir"],
            viewport=browser_args["viewport"],
            extra_http_headers=browser_args["extra_http_headers"],
        )
    LOG.info(
        "Launched browser CDP connection",
        remote_browser_url=remote_browser_url,
    )
    return browser_context, browser_artifacts, None


BrowserContextFactory.register_type("chromium-headless", _create_headless_chromium)
BrowserContextFactory.register_type("chromium-headful", _create_headful_chromium)
BrowserContextFactory.register_type("cdp-connect", _create_cdp_connection_browser)
