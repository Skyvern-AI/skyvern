"""Install a staged dataset directory without replacing an existing destination."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def install_directory(source: Path, destination: Path) -> None:
    """Move ``source`` to ``destination`` when the destination is absent."""
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Destination already exists: {destination}")
    if not source.is_dir() or source.is_symlink():
        raise NotADirectoryError(f"Source is not a directory: {source}")

    source.rename(destination)


def main() -> int:
    """Parse command-line arguments and install the staged directory."""
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()

    try:
        install_directory(args.source, args.destination)
    except OSError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
