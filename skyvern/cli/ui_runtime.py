from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata, resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from skyvern.utils.env_paths import resolve_frontend_env_path

UI_PORT = 8080
ARTIFACT_PORT = 9090
UI_PACKAGE_NAME = "skyvern-ui"
UI_PACKAGE_MODULE = "skyvern_ui"
UI_CACHE_ENV_VAR = "SKYVERN_UI_CACHE_DIR"
ARTIFACT_PATH_ROOTS_ENV_VAR = "SKYVERN_ARTIFACT_PATH_ROOTS"
UI_BIND_HOST = "127.0.0.1"


@dataclass(frozen=True)
class InstalledUiConfig:
    api_base_url: str
    wss_base_url: str
    artifact_api_base_url: str
    skyvern_api_key: str
    browser_streaming_mode: str


def installed_ui_dist() -> Traversable | None:
    """Return the installed prebuilt UI dist resource, if skyvern-ui is installed."""
    try:
        dist = resources.files(UI_PACKAGE_MODULE).joinpath("dist")
    except ModuleNotFoundError:
        return None

    if not dist.is_dir() or not dist.joinpath("index.html").is_file():
        return None
    return dist


def installed_ui_dist_available() -> bool:
    return installed_ui_dist() is not None


def has_frontend_runtime() -> bool:
    """Return whether `skyvern run ui` can start either source or packaged UI."""
    return resolve_frontend_env_path() is not None or installed_ui_dist_available()


def installed_ui_version() -> str:
    try:
        return metadata.version(UI_PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        try:
            module = __import__(UI_PACKAGE_MODULE)
        except ModuleNotFoundError:
            return "unknown"
        return str(getattr(module, "__version__", "unknown"))


def _copy_resource_tree(source: Traversable, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        child_destination = destination / child.name
        if child.is_dir():
            _copy_resource_tree(child, child_destination)
        else:
            child_destination.parent.mkdir(parents=True, exist_ok=True)
            child_destination.write_bytes(child.read_bytes())


def _ui_cache_root() -> Path:
    configured = os.getenv(UI_CACHE_ENV_VAR)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache" / "skyvern" / "ui"


def _replace_placeholders(dist_dir: Path, replacements: dict[str, str]) -> None:
    for asset in dist_dir.rglob("*"):
        if not asset.is_file() or asset.suffix not in {".html", ".js", ".css"}:
            continue
        try:
            contents = asset.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        updated = contents
        for placeholder, value in replacements.items():
            updated = updated.replace(placeholder, value)
        if updated != contents:
            asset.write_text(updated, encoding="utf-8")


def prepare_installed_ui_dist(config: InstalledUiConfig) -> Path:
    """Copy installed UI assets to a writable cache and inject runtime values."""
    source_dist = installed_ui_dist()
    if source_dist is None:
        raise FileNotFoundError('Prebuilt Skyvern UI assets are not installed. Run `pip install "skyvern[ui]"`.')

    runtime_dist = _ui_cache_root() / installed_ui_version() / "runtime"
    if runtime_dist.exists():
        shutil.rmtree(runtime_dist)
    _copy_resource_tree(source_dist, runtime_dist)
    _replace_placeholders(
        runtime_dist,
        {
            "__VITE_API_BASE_URL_PLACEHOLDER__": config.api_base_url,
            "__VITE_WSS_BASE_URL_PLACEHOLDER__": config.wss_base_url,
            "__VITE_ARTIFACT_API_BASE_URL_PLACEHOLDER__": config.artifact_api_base_url,
            "__SKYVERN_API_KEY_PLACEHOLDER__": config.skyvern_api_key,
            "__VITE_BROWSER_STREAMING_MODE_PLACEHOLDER__": config.browser_streaming_mode,
        },
    )
    return runtime_dist


def artifact_api_base_url_with_token(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/{token}"


def _candidate_artifact_roots() -> list[Path]:
    configured_roots = os.getenv(ARTIFACT_PATH_ROOTS_ENV_VAR)
    if configured_roots:
        return [Path(raw).expanduser() for raw in configured_roots.split(os.pathsep) if raw]

    roots: list[Path] = []
    for env_var in ("ARTIFACT_STORAGE_PATH", "TEMP_PATH"):
        configured = os.getenv(env_var)
        if configured:
            roots.append(Path(configured).expanduser())

    try:
        from skyvern.config import settings  # noqa: PLC0415
    except Exception:
        settings = None
    if settings is not None:
        roots.extend(
            [
                Path(settings.ARTIFACT_STORAGE_PATH).expanduser(),
                Path(settings.TEMP_PATH).expanduser(),
            ]
        )

    roots.extend(
        [
            Path.cwd() / "artifacts",
            Path.cwd() / "temp",
            Path.home() / ".skyvern" / "artifacts",
            Path.home() / ".skyvern" / "temp",
        ]
    )
    return roots


def _configured_artifact_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for root in _candidate_artifact_roots():
        resolved = root.resolve(strict=False)
        if resolved in seen:
            continue
        roots.append(resolved)
        seen.add(resolved)
    return tuple(roots)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class _SinglePageAppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, directory: str, **kwargs: Any) -> None:
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def send_head(self) -> Any:
        parsed = urllib.parse.urlparse(self.path)
        requested_path = Path(self.directory) / parsed.path.lstrip("/")
        if self.command in {"GET", "HEAD"} and not requested_path.exists():
            self.path = "/index.html"
        return super().send_head()


class _ArtifactHandler(BaseHTTPRequestHandler):
    server_version = "SkyvernArtifactServer/1.0"
    artifact_token: str | None = None
    artifact_roots: tuple[Path, ...] = ()
    allowed_origins: tuple[str, ...] = ()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def end_headers(self) -> None:
        cors_origin = self._cors_origin()
        if cors_origin is not None:
            self.send_header("Access-Control-Allow-Origin", cors_origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route_path = self._route_path(parsed.path)
        if route_path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown artifact endpoint")
            return

        params = urllib.parse.parse_qs(parsed.query)
        raw_path = params.get("path", [""])[0]
        artifact_path = Path(raw_path) if raw_path else None

        if route_path == "/artifact/recording":
            self._send_recording(artifact_path)
            return
        if route_path == "/artifact/image":
            self._send_file(artifact_path)
            return
        if route_path == "/artifact/json":
            self._send_json(artifact_path)
            return
        if route_path == "/artifact/text":
            self._send_text(artifact_path)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Unknown artifact endpoint")

    def _cors_origin(self) -> str | None:
        if not self.allowed_origins:
            return None
        request_origin = self.headers.get("Origin")
        if request_origin in self.allowed_origins:
            return request_origin
        if request_origin is None:
            return self.allowed_origins[0]
        return None

    def _route_path(self, parsed_path: str) -> str | None:
        if self.artifact_token is None:
            return parsed_path
        token_prefix = f"/{self.artifact_token}"
        if parsed_path == token_prefix:
            return "/"
        if not parsed_path.startswith(f"{token_prefix}/"):
            return None
        return parsed_path[len(token_prefix) :]

    def _validate_path(self, path: Path | None) -> Path | None:
        if path is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing path")
            return None
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid artifact path")
            return None
        if not any(_is_relative_to(resolved, root) for root in self.artifact_roots):
            self.send_error(HTTPStatus.FORBIDDEN, "Artifact path is outside the configured roots")
            return None
        if not resolved.exists() or not resolved.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Artifact not found")
            return None
        return resolved

    def _send_recording(self, path: Path | None) -> None:
        artifact_path = self._validate_path(path)
        range_header = self.headers.get("Range")
        if artifact_path is None:
            return
        if not range_header:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing range header")
            return

        video_size = artifact_path.stat().st_size
        match = re.match(r"bytes=(\d+)-", range_header)
        start = int(match.group(1)) if match else 0
        if start >= video_size:
            self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            return
        end = min(start + 1_000_000, video_size) - 1
        content_length = end - start + 1

        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Range", f"bytes {start}-{end}/{video_size}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(content_length))
        self.send_header("Content-Type", "video/mp4")
        self.end_headers()
        with artifact_path.open("rb") as stream:
            stream.seek(start)
            self.wfile.write(stream.read(content_length))

    def _send_file(self, path: Path | None) -> None:
        artifact_path = self._validate_path(path)
        if artifact_path is None:
            return
        content_type = mimetypes.guess_type(str(artifact_path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(artifact_path.stat().st_size))
        self.end_headers()
        with artifact_path.open("rb") as stream:
            shutil.copyfileobj(stream, self.wfile)

    def _send_json(self, path: Path | None) -> None:
        artifact_path = self._validate_path(path)
        if artifact_path is None:
            return
        try:
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_text(self, path: Path | None) -> None:
        artifact_path = self._validate_path(path)
        if artifact_path is None:
            return
        contents = artifact_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(contents)))
        self.end_headers()
        self.wfile.write(contents)


def _artifact_handler_class(
    *,
    artifact_token: str | None,
    artifact_roots: tuple[Path, ...],
    allowed_origins: tuple[str, ...],
) -> type[_ArtifactHandler]:
    class ConfiguredArtifactHandler(_ArtifactHandler):
        pass

    ConfiguredArtifactHandler.artifact_token = artifact_token
    ConfiguredArtifactHandler.artifact_roots = artifact_roots
    ConfiguredArtifactHandler.allowed_origins = allowed_origins
    return ConfiguredArtifactHandler


def serve_installed_ui(
    dist_dir: Path,
    *,
    ui_port: int = UI_PORT,
    artifact_port: int = ARTIFACT_PORT,
    artifact_token: str | None = None,
) -> None:
    """Serve prebuilt UI assets and local artifact endpoints without Node."""

    def ui_handler(*args: Any, **kwargs: Any) -> _SinglePageAppHandler:
        return _SinglePageAppHandler(*args, directory=str(dist_dir), **kwargs)

    allowed_origins = (f"http://localhost:{ui_port}", f"http://127.0.0.1:{ui_port}")
    artifact_handler = _artifact_handler_class(
        artifact_token=artifact_token,
        artifact_roots=_configured_artifact_roots(),
        allowed_origins=allowed_origins,
    )
    ui_server = _ReusableThreadingHTTPServer((UI_BIND_HOST, ui_port), ui_handler)
    artifact_server = _ReusableThreadingHTTPServer((UI_BIND_HOST, artifact_port), artifact_handler)
    artifact_thread = threading.Thread(target=artifact_server.serve_forever, daemon=True)
    artifact_thread.start()

    try:
        webbrowser.open(f"http://localhost:{ui_port}")
        ui_server.serve_forever()
    except KeyboardInterrupt:
        return
    finally:
        ui_server.shutdown()
        ui_server.server_close()
        artifact_server.shutdown()
        artifact_server.server_close()
        artifact_thread.join(timeout=5)
