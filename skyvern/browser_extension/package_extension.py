from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

EXTENSION_DIR = Path(__file__).with_name("extension")
DEFAULT_OUTPUT_PATH = Path("tmp/skyvern-agent-extension.zip")
BUILD_HASH_FILENAME = "build_hash.json"
# fixed metadata so identical sources produce byte-identical archives
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_ZIP_FILE_MODE = 0o644 << 16
# build_hash.json is excluded because it embeds the hash of everything else;
# hashing it too would make the hash depend on its own previous value.
_HASH_EXCLUDED_NAMES = frozenset({"README.md", BUILD_HASH_FILENAME})


def _normalized_entry(archive_name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(archive_name, date_time=_ZIP_EPOCH)
    info.external_attr = _ZIP_FILE_MODE
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _normalized_content(source_path: Path, relative_path: Path) -> bytes:
    if relative_path == Path("manifest.json"):
        manifest = json.loads(source_path.read_text())
        manifest.pop("key", None)
        return (json.dumps(manifest, indent=2) + "\n").encode()
    return source_path.read_bytes()


def _source_files(extension_dir: Path, *, exclude: frozenset[str]) -> list[tuple[Path, Path]]:
    files = []
    for source_path in sorted(path for path in extension_dir.rglob("*") if path.is_file()):
        relative_path = source_path.relative_to(extension_dir)
        if relative_path.as_posix() in exclude or source_path.resolve() == extension_dir.resolve():
            continue
        files.append((source_path, relative_path))
    return files


def compute_extension_source_hash(extension_dir: Path = EXTENSION_DIR) -> str:
    """SHA-256 over sorted (path, content) pairs, so it changes iff Chrome-visible bytes change."""
    digest = hashlib.sha256()
    for source_path, relative_path in _source_files(extension_dir, exclude=_HASH_EXCLUDED_NAMES):
        digest.update(relative_path.as_posix().encode())
        digest.update(b"\0")
        digest.update(_normalized_content(source_path, relative_path))
    return digest.hexdigest()


def write_build_hash(extension_dir: Path = EXTENSION_DIR) -> str:
    """Regenerate build_hash.json from current sources; run before committing extension/** changes."""
    build_hash = compute_extension_source_hash(extension_dir)
    (extension_dir / BUILD_HASH_FILENAME).write_text(json.dumps({"sha256": build_hash}, indent=2) + "\n")
    return build_hash


def package_extension(output_path: str | Path = DEFAULT_OUTPUT_PATH) -> Path:
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source_path, relative_path in _source_files(EXTENSION_DIR, exclude=frozenset({"README.md"})):
            if source_path.resolve() == output_path:
                continue
            content = _normalized_content(source_path, relative_path)
            archive.writestr(_normalized_entry(relative_path.as_posix()), content)

    with zipfile.ZipFile(output_path) as archive:
        packaged_manifest = json.loads(archive.read("manifest.json"))
    assert "key" not in packaged_manifest
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Skyvern Agent Chrome Web Store upload zip.")
    parser.add_argument("output", nargs="?", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument(
        "--write-build-hash",
        action="store_true",
        help="Regenerate extension/build_hash.json from current sources and exit (no zip is built).",
    )
    args = parser.parse_args()
    if args.write_build_hash:
        print(write_build_hash())
        return
    print(package_extension(args.output))


if __name__ == "__main__":
    main()
