import os


def replace_file_extension(path: str, extension: str) -> str:
    normalized_extension = extension.lstrip(".")
    if not normalized_extension:
        return path
    stem, _ = os.path.splitext(path)
    return f"{stem}.{normalized_extension}"
