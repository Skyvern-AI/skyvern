import os
from pathlib import Path


def publish_unique_download(temporary_path: Path, destination_dir: Path, filename: str) -> Path:
    """Atomically publish a download without replacing an earlier run artifact."""
    requested_path = destination_dir / filename
    collision_index = 0
    while True:
        destination_path = (
            requested_path
            if collision_index == 0
            else requested_path.with_name(f"{requested_path.stem} ({collision_index}){requested_path.suffix}")
        )
        try:
            os.link(temporary_path, destination_path)
        except FileExistsError:
            collision_index += 1
            continue
        temporary_path.unlink()
        return destination_path
