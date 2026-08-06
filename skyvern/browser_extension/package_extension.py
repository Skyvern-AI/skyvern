from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

EXTENSION_DIR = Path(__file__).with_name("extension")
DEFAULT_OUTPUT_PATH = Path("tmp/skyvern-agent-extension.zip")
# fixed metadata so identical sources produce byte-identical archives
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_ZIP_FILE_MODE = 0o644 << 16


def _normalized_entry(archive_name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(archive_name, date_time=_ZIP_EPOCH)
    info.external_attr = _ZIP_FILE_MODE
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def package_extension(output_path: str | Path = DEFAULT_OUTPUT_PATH) -> Path:
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    source_files = sorted(path for path in EXTENSION_DIR.rglob("*") if path.is_file())
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source_path in source_files:
            relative_path = source_path.relative_to(EXTENSION_DIR)
            if relative_path == Path("README.md") or source_path.resolve() == output_path:
                continue
            if relative_path == Path("manifest.json"):
                manifest = json.loads(source_path.read_text())
                manifest.pop("key", None)
                content = (json.dumps(manifest, indent=2) + "\n").encode()
            else:
                content = source_path.read_bytes()
            archive.writestr(_normalized_entry(relative_path.as_posix()), content)

    with zipfile.ZipFile(output_path) as archive:
        packaged_manifest = json.loads(archive.read("manifest.json"))
    assert "key" not in packaged_manifest
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Skyvern Agent Chrome Web Store upload zip.")
    parser.add_argument("output", nargs="?", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()
    print(package_extension(args.output))


if __name__ == "__main__":
    main()
