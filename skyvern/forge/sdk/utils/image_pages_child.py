"""TIFF and PDF page counting and splitting that run in a capped child process: opening a TIFF decodes its strip
tables into Python objects and a page can declare far more pixels than its size, so a crafted file can need gigabytes.
Imports stay to the stdlib, Pillow and pypdf."""

import io

from PIL import Image
from pypdf import PdfReader, PdfWriter

_TIFF_MAGIC = (b"II*\x00", b"MM\x00*")
_PDF_MAGIC = b"%PDF-"


def count_image_pages(content: bytes, max_pages: int) -> tuple[str, int] | None:
    """The file extension and page count of a TIFF or PDF of 1 to max_pages pages; None for anything else."""
    try:
        if content.startswith(_TIFF_MAGIC):
            with Image.open(io.BytesIO(content)) as image:
                frames = 1
                while frames <= max_pages:
                    try:
                        image.seek(frames)
                    except EOFError:
                        return "tif", frames
                    frames += 1
                return None
        if content.startswith(_PDF_MAGIC):
            pages = len(PdfReader(io.BytesIO(content)).pages)
            return ("pdf", pages) if 0 < pages <= max_pages else None
    except Exception:  # noqa: BLE001
        return None
    return None


def split_image_pages(content: bytes, extension: str, start: int, count: int) -> bytes | None:
    """Pages start to start + count of a TIFF or PDF as a file of the same type; None when they cannot be read."""
    try:
        buffer = io.BytesIO()
        if extension == "pdf":
            writer = PdfWriter()
            for page in PdfReader(io.BytesIO(content)).pages[start : start + count]:
                writer.add_page(page)
            writer.write(buffer)
            return buffer.getvalue()
        with Image.open(io.BytesIO(content)) as image:
            frames = []
            for index in range(start, start + count):
                try:
                    image.seek(index)
                except EOFError:
                    break
                frames.append(image.copy())
        if not frames:
            return None
        frames[0].save(buffer, format="TIFF", save_all=True, append_images=frames[1:], compression="tiff_deflate")
        return buffer.getvalue()
    except Exception:  # noqa: BLE001
        return None
