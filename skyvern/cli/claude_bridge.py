"""Materialize the shipped API-key MCP runtime without npm or OAuth bootstrap."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

_ARCHIVE = Path(__file__).parent / "mcpb" / "releases" / "skyvern-claude-desktop.mcpb"
_MAX_FILES = 5000
_MAX_BYTES = 64 * 1024 * 1024


class BridgeSetupError(ValueError):
    """A fixed diagnostic for invalid bridge configuration or local runtime."""


def _cache_root() -> Path:
    override = os.environ.get("SKYVERN_MCP_CACHE_DIR")
    if override:
        return Path(override).expanduser().absolute()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return (base / "skyvern" / "mcp").absolute()


def _target() -> Path:
    return _cache_root() / hashlib.sha256(_ARCHIVE.read_bytes()).hexdigest()


def bridge_entry(api_key: str, url: str) -> dict[str, Any]:
    """Build a side-effect-free config using the bundled runtime and env credentials."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
        _ = port
    except ValueError as exc:
        raise BridgeSetupError("Invalid HTTPS MCP URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or "#" in url
        or any(ord(c) <= 32 or ord(c) == 127 for c in url)
    ):
        raise BridgeSetupError("An HTTPS MCP URL without credentials or fragment is required")
    if not api_key.strip() or not api_key.isascii() or any(not 33 <= ord(c) <= 126 for c in api_key):
        raise BridgeSetupError("A nonempty header-safe Skyvern API key is required")
    env = {"SKYVERN_API_KEY": api_key, "SKYVERN_MCP_URL": url}
    if os.environ.get("NODE_EXTRA_CA_CERTS"):
        env["NODE_EXTRA_CA_CERTS"] = os.environ["NODE_EXTRA_CA_CERTS"]
    return {"command": shutil.which("node") or "node", "args": [str(_target() / "proxy.mjs")], "env": env}


def _files() -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    seen: set[str] = set()
    total = 0
    with zipfile.ZipFile(_ARCHIVE) as archive:
        if len(archive.infolist()) > _MAX_FILES:
            raise BridgeSetupError("Bundled runtime exceeds file limit")
        for member in archive.infolist():
            name = member.orig_filename.rstrip("/")
            parts = name.split("/")
            kind = stat.S_IFMT(member.external_attr >> 16)
            if (
                not name
                or chr(92) in name
                or ":" in name
                or PurePosixPath(name).is_absolute()
                or any(p in ("", ".", "..") or p.endswith((".", " ")) for p in parts)
                or any(ord(c) < 32 or c in '<>"|?*' for c in name)
                or len(parts) > 32
                or len(name) > 1024
                or any(
                    p.split(".")[0].upper()
                    in {
                        "CON",
                        "PRN",
                        "AUX",
                        "NUL",
                        *(f"COM{i}" for i in range(1, 10)),
                        *(f"LPT{i}" for i in range(1, 10)),
                    }
                    for p in parts
                )
                or name.casefold() in seen
                or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                or bool(member.flag_bits & 1)
            ):
                raise BridgeSetupError("Unsafe member in bundled MCP runtime")
            seen.add(name.casefold())
            if member.is_dir():
                continue
            total += member.file_size
            if member.file_size > 16 * 1024 * 1024 or total > _MAX_BYTES:
                raise BridgeSetupError("Bundled runtime exceeds size limit")
            result[name] = archive.read(member)
    try:
        manifest = json.loads(result["manifest.json"])
        entrypoint = manifest["server"]["entry_point"]
        if entrypoint not in {"proxy.mjs", "desktop.mjs"} or entrypoint not in result or "proxy.mjs" not in result:
            raise BridgeSetupError("Bundled MCP runtime entrypoint is incompatible")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BridgeSetupError("Bundled MCP runtime manifest is invalid") from exc
    return result


def _no_links(path: Path) -> None:
    for ancestor in (path, *path.parents):
        try:
            metadata = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise BridgeSetupError("Linked MCP runtime cache paths are not supported")


def _verify(target: Path, files: dict[str, bytes]) -> None:
    _no_links(target)
    actual: set[str] = set()
    for path in target.rglob("*"):
        _no_links(path)
        if path.is_file():
            name = path.relative_to(target).as_posix()
            actual.add(name)
            if name not in files or path.read_bytes() != files[name]:
                raise BridgeSetupError("Cached MCP runtime changed; choose a fresh cache directory")
        elif not path.is_dir():
            raise BridgeSetupError("Invalid MCP runtime cache member")
    if actual != set(files):
        raise BridgeSetupError("Cached MCP runtime is incomplete; choose a fresh cache directory")


def ensure_runtime() -> None:
    """Extract the package's exact MCPB atomically, refusing unsafe or changed cache data."""
    root, target = _cache_root(), _target()
    try:
        files = _files()
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise BridgeSetupError("Bundled MCP runtime archive is invalid") from exc
    _no_links(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _no_links(root)
    if target.exists():
        _verify(target, files)
        return
    stage = Path(tempfile.mkdtemp(prefix=".bridge-", dir=root))
    try:
        for name, data in files.items():
            out = stage / name
            out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with out.open("xb") as stream:
                stream.write(data)
        _verify(stage, files)
        try:
            stage.rename(target)
        except FileExistsError:
            _verify(target, files)
    finally:
        if stage.exists():
            if stage.parent.resolve() != root.resolve() or not stage.name.startswith(".bridge-"):
                raise BridgeSetupError("Unexpected MCP runtime staging path")
            shutil.rmtree(stage)
