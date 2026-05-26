from __future__ import annotations

import json
import os
import posixpath
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
_IMAGE_CONTENT_TYPES = {
    ".avif": "image/avif",
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}
_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


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


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIR_MODE)
    path.chmod(_PRIVATE_DIR_MODE)


def _copy_resource_tree(source: Traversable, destination: Path) -> None:
    _ensure_private_dir(destination)
    for child in source.iterdir():
        child_destination = destination / child.name
        if child.is_dir():
            _copy_resource_tree(child, child_destination)
        else:
            _ensure_private_dir(child_destination.parent)
            child_destination.write_bytes(child.read_bytes())
            child_destination.chmod(_PRIVATE_FILE_MODE)


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
            asset.chmod(_PRIVATE_FILE_MODE)


def prepare_installed_ui_dist(config: InstalledUiConfig) -> Path:
    """Copy installed UI assets to a writable cache and inject runtime values."""
    source_dist = installed_ui_dist()
    if source_dist is None:
        raise FileNotFoundError('Prebuilt Skyvern UI assets are not installed. Run `pip install "skyvern[ui]"`.')

    cache_root = _ui_cache_root()
    version_cache_dir = cache_root / installed_ui_version()
    _ensure_private_dir(cache_root)
    _ensure_private_dir(version_cache_dir)

    runtime_dist = version_cache_dir / "runtime"
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


def _should_serve_spa_index(request_path: str) -> bool:
    if request_path in {"", "/"}:
        return False
    return posixpath.splitext(request_path.rstrip("/"))[1] == ""


def _validate_tcp_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or port <= 0 or port > 65535:
        raise ValueError(f"Invalid TCP port: {port!r}")
    return port


def _local_ui_origins(ui_port: int) -> tuple[str, str]:
    port = _validate_tcp_port(ui_port)
    return (f"http://localhost:{port}", f"http://127.0.0.1:{port}")


def _normalize_artifact_path(raw_path: str) -> str | None:
    if "\x00" in raw_path or "\r" in raw_path or "\n" in raw_path:
        return None
    if urllib.parse.urlparse(raw_path).scheme.lower() == "file":
        return None
    return os.path.realpath(os.path.expanduser(raw_path))


def _image_content_type(path: str) -> str:
    return _IMAGE_CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class _SinglePageAppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, directory: str, **kwargs: Any) -> None:
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def send_head(self) -> Any:
        parsed = urllib.parse.urlparse(self.path)
        if self.command in {"GET", "HEAD"} and _should_serve_spa_index(parsed.path):
            self.path = "/index.html"
        return super().send_head()


class _ArtifactHandler(BaseHTTPRequestHandler):
    server_version = "SkyvernArtifactServer/1.0"
    artifact_token: str | None = None
    artifact_roots: tuple[Path, ...] = ()
    ui_port: int = UI_PORT

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def end_headers(self) -> None:
        cors_origin = self._cors_origin()
        if cors_origin is not None:
            self.send_header("Access-Control-Allow-Origin", cors_origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
        self.send_header("Referrer-Policy", "no-referrer")
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
        raw_path = params.get("path", [None])[0]

        if route_path == "/artifact/recording":
            self._send_recording(raw_path)
            return
        if route_path == "/artifact/image":
            self._send_file(raw_path)
            return
        if route_path == "/artifact/json":
            self._send_json(raw_path)
            return
        if route_path == "/artifact/text":
            self._send_text(raw_path)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Unknown artifact endpoint")

    def _cors_origin(self) -> str | None:
        localhost_origin, loopback_origin = _local_ui_origins(self.ui_port)
        request_origin = self.headers.get("Origin")
        if request_origin == localhost_origin:
            return localhost_origin
        if request_origin == loopback_origin:
            return loopback_origin
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

    def _send_validated_artifact(self, raw_path: str | None, artifact_kind: str) -> None:
        if raw_path is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing path")
            return
        normalized = _normalize_artifact_path(raw_path)
        if normalized is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid artifact path")
            return
        for root in self.artifact_roots:
            root_path = os.path.realpath(root)
            root_prefix = root_path if root_path.endswith(os.sep) else f"{root_path}{os.sep}"
            if normalized.startswith(root_prefix):
                try:
                    with open(normalized, "rb") as stream:
                        file_size = os.fstat(stream.fileno()).st_size

                        if artifact_kind == "recording":
                            range_header = self.headers.get("Range")
                            if not range_header:
                                self.send_error(HTTPStatus.BAD_REQUEST, "Missing range header")
                                return

                            match = re.match(r"bytes=(\d+)-", range_header)
                            start = int(match.group(1)) if match else 0
                            if start >= file_size:
                                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                                return
                            end = min(start + 1_000_000, file_size) - 1
                            content_length = end - start + 1

                            self.send_response(HTTPStatus.PARTIAL_CONTENT)
                            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                            self.send_header("Accept-Ranges", "bytes")
                            self.send_header("Content-Length", str(content_length))
                            self.send_header("Content-Type", "video/mp4")
                            self.end_headers()
                            stream.seek(start)
                            self.wfile.write(stream.read(content_length))
                            return
                        if artifact_kind == "image":
                            self.send_response(HTTPStatus.OK)
                            self.send_header("Content-Type", _image_content_type(normalized))
                            self.send_header("Content-Length", str(file_size))
                            self.end_headers()
                            shutil.copyfileobj(stream, self.wfile)
                            return
                        if artifact_kind == "json":
                            try:
                                payload = json.loads(stream.read().decode("utf-8"))
                            except Exception:
                                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Invalid artifact JSON")
                                return
                            encoded = json.dumps(payload).encode("utf-8")
                            self.send_response(HTTPStatus.OK)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(encoded)))
                            self.end_headers()
                            self.wfile.write(encoded)
                            return
                        if artifact_kind == "text":
                            contents = stream.read()
                            self.send_response(HTTPStatus.OK)
                            self.send_header("Content-Type", "text/plain; charset=utf-8")
                            self.send_header("Content-Length", str(len(contents)))
                            self.end_headers()
                            self.wfile.write(contents)
                            return

                        self.send_error(HTTPStatus.NOT_FOUND, "Unknown artifact endpoint")
                        return
                except OSError:
                    self.send_error(HTTPStatus.NOT_FOUND, "Artifact not found")
                    return

        self.send_error(HTTPStatus.FORBIDDEN, "Artifact path is outside the configured roots")
        return

    def _send_recording(self, raw_path: str | None) -> None:
        self._send_validated_artifact(raw_path, "recording")

    def _send_file(self, raw_path: str | None) -> None:
        self._send_validated_artifact(raw_path, "image")

    def _send_json(self, raw_path: str | None) -> None:
        self._send_validated_artifact(raw_path, "json")

    def _send_text(self, raw_path: str | None) -> None:
        self._send_validated_artifact(raw_path, "text")


def _artifact_handler_class(
    *,
    artifact_token: str | None,
    artifact_roots: tuple[Path, ...],
    ui_port: int,
) -> type[_ArtifactHandler]:
    class ConfiguredArtifactHandler(_ArtifactHandler):
        pass

    ConfiguredArtifactHandler.artifact_token = artifact_token
    ConfiguredArtifactHandler.artifact_roots = artifact_roots
    ConfiguredArtifactHandler.ui_port = _validate_tcp_port(ui_port)
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

    artifact_handler = _artifact_handler_class(
        artifact_token=artifact_token,
        artifact_roots=_configured_artifact_roots(),
        ui_port=ui_port,
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
