from __future__ import annotations

import sys
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from skyvern.cli import ui_runtime


def _install_fake_ui_package(tmp_path: Path, monkeypatch) -> Path:
    package_root = tmp_path / "package"
    ui_package = package_root / "skyvern_ui"
    asset_dir = ui_package / "dist" / "assets"
    asset_dir.mkdir(parents=True)
    (ui_package / "__init__.py").write_text('__version__ = "test-version"\n')
    (ui_package / "dist" / "index.html").write_text("<html>__VITE_API_BASE_URL_PLACEHOLDER__</html>")
    (asset_dir / "app.js").write_text(
        "__VITE_WSS_BASE_URL_PLACEHOLDER__ "
        "__VITE_ARTIFACT_API_BASE_URL_PLACEHOLDER__ "
        "__SKYVERN_API_KEY_PLACEHOLDER__ "
        "__VITE_BROWSER_STREAMING_MODE_PLACEHOLDER__"
    )
    monkeypatch.syspath_prepend(str(package_root))
    sys.modules.pop("skyvern_ui", None)
    return package_root


def test_installed_ui_dist_detects_package_assets(tmp_path, monkeypatch) -> None:
    _install_fake_ui_package(tmp_path, monkeypatch)

    dist = ui_runtime.installed_ui_dist()

    assert dist is not None
    assert dist.joinpath("index.html").is_file()


def test_prepare_installed_ui_dist_copies_assets_and_injects_runtime_values(tmp_path, monkeypatch) -> None:
    _install_fake_ui_package(tmp_path, monkeypatch)
    monkeypatch.setenv(ui_runtime.UI_CACHE_ENV_VAR, str(tmp_path / "cache"))

    runtime_dist = ui_runtime.prepare_installed_ui_dist(
        ui_runtime.InstalledUiConfig(
            api_base_url="http://localhost:8000/api/v1",
            wss_base_url="ws://localhost:8000/api/v1",
            artifact_api_base_url="http://localhost:9090",
            skyvern_api_key="test-key",
            browser_streaming_mode="cdp",
        )
    )

    assert runtime_dist == tmp_path / "cache" / "test-version" / "runtime"
    assert (runtime_dist / "index.html").read_text() == "<html>http://localhost:8000/api/v1</html>"
    assert (runtime_dist / "assets" / "app.js").read_text() == (
        "ws://localhost:8000/api/v1 http://localhost:9090 test-key cdp"
    )


def test_artifact_api_base_url_with_token() -> None:
    assert ui_runtime.artifact_api_base_url_with_token("http://localhost:9090/", "secret") == (
        "http://localhost:9090/secret"
    )


def test_has_frontend_runtime_accepts_source_checkout(monkeypatch) -> None:
    monkeypatch.setattr(ui_runtime, "resolve_frontend_env_path", lambda: Path("skyvern-frontend/.env"))
    monkeypatch.setattr(ui_runtime, "installed_ui_dist_available", lambda: False)

    assert ui_runtime.has_frontend_runtime() is True


def test_has_frontend_runtime_accepts_installed_package(monkeypatch) -> None:
    monkeypatch.setattr(ui_runtime, "resolve_frontend_env_path", lambda: None)
    monkeypatch.setattr(ui_runtime, "installed_ui_dist_available", lambda: True)

    assert ui_runtime.has_frontend_runtime() is True


def test_artifact_handler_options_includes_cors_headers() -> None:
    handler = ui_runtime._artifact_handler_class(
        artifact_token="token",
        artifact_roots=(),
        allowed_origins=("http://localhost:8080",),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("OPTIONS", "/artifact/text")
        response = connection.getresponse()

        assert response.status == 204
        assert response.getheader("Access-Control-Allow-Origin") == "http://localhost:8080"
        assert "Range" in (response.getheader("Access-Control-Allow-Headers") or "")
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_artifact_handler_rejects_missing_token(tmp_path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("secret")
    handler = ui_runtime._artifact_handler_class(
        artifact_token="token",
        artifact_roots=(tmp_path.resolve(),),
        allowed_origins=("http://localhost:8080",),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", f"/artifact/text?path={artifact}")
        response = connection.getresponse()

        assert response.status == 404
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_artifact_handler_rejects_paths_outside_allowed_roots(tmp_path) -> None:
    allowed_root = tmp_path / "allowed"
    outside_root = tmp_path / "outside"
    allowed_root.mkdir()
    outside_root.mkdir()
    artifact = outside_root / "artifact.txt"
    artifact.write_text("secret")
    handler = ui_runtime._artifact_handler_class(
        artifact_token="token",
        artifact_roots=(allowed_root.resolve(),),
        allowed_origins=("http://localhost:8080",),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", f"/token/artifact/text?path={artifact}")
        response = connection.getresponse()

        assert response.status == 403
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_artifact_handler_serves_paths_inside_allowed_roots(tmp_path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("hello")
    handler = ui_runtime._artifact_handler_class(
        artifact_token="token",
        artifact_roots=(tmp_path.resolve(),),
        allowed_origins=("http://localhost:8080",),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "GET",
            f"/token/artifact/text?path={artifact}",
            headers={"Origin": "http://localhost:8080"},
        )
        response = connection.getresponse()
        body = response.read().decode()

        assert response.status == 200
        assert response.getheader("Access-Control-Allow-Origin") == "http://localhost:8080"
        assert body == "hello"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
