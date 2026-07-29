import hashlib
import os
import stat
import tempfile

DEFAULT_PROFILE_COPY_IGNORE = {
    "Snapshots",
    "GrShaderCache",
    "ShaderCache",
    "GraphiteDawnCache",
    "DawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "Guest Profile",
    "Profile 2",
    "Profile 3",
    "BrowserMetrics",
    "Crashpad",
    "CrashpadMetrics-active.pma",
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
    "DevToolsActivePort",
}


def operator_profile_generation(path: str) -> str | None:
    digest = hashlib.sha256()

    def add(item: str, ancestors: frozenset[tuple[int, int]]) -> None:
        metadata = os.stat(item)
        relative_path = os.path.relpath(item, path)
        digest.update(
            f"{relative_path}\0{metadata.st_mode}\0{metadata.st_size}\0{metadata.st_mtime_ns}\0"
            f"{metadata.st_ctime_ns}\0{metadata.st_dev}\0{metadata.st_ino}\n".encode()
        )
        if stat.S_ISDIR(metadata.st_mode):
            identity = (metadata.st_dev, metadata.st_ino)
            if identity in ancestors:
                raise OSError(f"Symlink cycle in operator profile: {relative_path}")
            for entry in sorted(os.scandir(item), key=lambda entry: entry.name):
                if relative_path != "." or entry.name not in DEFAULT_PROFILE_COPY_IGNORE:
                    add(entry.path, ancestors | {identity})

    try:
        add(path, frozenset())
    except OSError:
        return None
    return digest.hexdigest()


def operator_profile_marker_path(base_dir: str, browser_type: str, generation: str) -> str:
    digest = hashlib.sha256(os.path.realpath(base_dir).encode()).hexdigest()
    return os.path.join(
        tempfile.gettempdir(), "skyvern-browser-profile-invalidations", f"{digest}_{browser_type}_{generation}"
    )


def write_operator_profile_marker(base_dir: str, browser_type: str, generation: str) -> None:
    marker_path = operator_profile_marker_path(base_dir, browser_type, generation)
    os.makedirs(os.path.dirname(marker_path), exist_ok=True)
    descriptor = os.open(marker_path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)


def valid_operator_profile_generation(
    base_dir: str,
    browser_type: str,
    path: str,
    invalidated_generation: str | None = None,
) -> str | None:
    generation = operator_profile_generation(path)
    if generation is None or generation == invalidated_generation:
        return None
    return None if os.path.exists(operator_profile_marker_path(base_dir, browser_type, generation)) else generation
