"""pdfplumber calls that pdf_parser runs in a capped child process. Imports stay to the stdlib, pdfplumber and the
child's own bootstrap, because every call pays for them again in a fresh interpreter."""

import io
from typing import Any

import pdfplumber
from pdfplumber.page import Page

from skyvern.forge.sdk.utils.capped_subprocess_child import collect_texts


class PageImagesTooLarge(Exception):
    pass


def _page_text(page: Page) -> str:
    text = page.extract_text() or ""
    page.close()
    return text


def page_texts(file_path: str, char_limit: int | None, max_bytes: int) -> tuple[list[str], int, bool]:
    """Return the page texts in order, the page count, and whether every page was read, within collect_texts'
    limits."""
    with pdfplumber.open(file_path) as pdf:
        texts, read_every_page = collect_texts(pdf.pages, _page_text, char_limit, max_bytes)
        return texts, len(pdf.pages), read_every_page


def page_count(file_path: str) -> int:
    with pdfplumber.open(file_path) as pdf:
        return len(pdf.pages)


def page_images(
    file_path: str, max_pages: int | None, resolution: int, max_total_bytes: int
) -> tuple[list[bytes], int]:
    """Return a PNG per rendered page and the page count, failing once the PNGs pass max_total_bytes in all."""
    images: list[bytes] = []
    total_bytes = 0
    with pdfplumber.open(file_path) as pdf:
        total_pages = len(pdf.pages)
        pages_to_render = total_pages if max_pages is None else min(total_pages, max_pages)
        for page in pdf.pages[:pages_to_render]:
            img = page.to_image(resolution=resolution)
            buf = io.BytesIO()
            img.original.save(buf, format="PNG")
            page.close()
            total_bytes += buf.tell()
            if total_bytes > max_total_bytes:
                raise PageImagesTooLarge(
                    f"the first {len(images) + 1} rendered pages pass the "
                    f"{max_total_bytes // (1024 * 1024)} MiB limit on page images"
                )
            images.append(buf.getvalue())
    return images, total_pages


def upright_words(file_path: str, page_indexes: list[int]) -> dict[int, list[dict[str, Any]]]:
    wanted = set(page_indexes)
    words_by_page: dict[int, list[dict[str, Any]]] = {}
    with pdfplumber.open(file_path) as pdf:
        for page_index, page in enumerate(pdf.pages):
            if page_index in wanted:
                words = page.extract_words(extra_attrs=["upright"])
                words_by_page[page_index] = [w for w in words if w.get("upright", True)]
            page.close()
    return words_by_page
