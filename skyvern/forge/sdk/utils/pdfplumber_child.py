"""pdfplumber calls that pdf_parser runs in a capped child process. Imports stay to the stdlib, pdfplumber and the
child's own bootstrap, because every call pays for them again in a fresh interpreter."""

import io
from typing import Any

import pdfplumber

from skyvern.forge.sdk.utils.capped_subprocess_child import ResultTooLarge


class PageImagesTooLarge(Exception):
    pass


# Room kept under the result cap for the rest of the pickle: the page count, the flag and framing.
_RESULT_MARGIN_BYTES = 1024 * 1024
# What pickling a str adds to its UTF-8 bytes, rounded up.
_PAGE_OVERHEAD_BYTES = 16


def page_texts(file_path: str, char_limit: int | None, max_bytes: int) -> tuple[list[str], int, bool]:
    """Return the page texts in order, the page count, and whether every page was read. Reading stops once char_limit
    characters are collected, or before a page that would push the pickled texts past max_bytes less a margin, and a
    first page that does not fit is a result that is too large."""
    budget = max_bytes - min(_RESULT_MARGIN_BYTES, max_bytes // 2)
    texts: list[str] = []
    chars = 0
    pickled_bytes = 0
    with pdfplumber.open(file_path) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            if char_limit is not None and chars > char_limit:
                return texts, page_count, False
            page_text = page.extract_text() or ""
            page.close()
            utf8_bytes = len(page_text) if page_text.isascii() else len(page_text.encode("utf-8", "surrogatepass"))
            if pickled_bytes + utf8_bytes + _PAGE_OVERHEAD_BYTES > budget:
                if not texts:
                    raise ResultTooLarge("the text of the first page passes the limit on page texts")
                return texts, page_count, False
            texts.append(page_text)
            chars += len(page_text)
            pickled_bytes += utf8_bytes + _PAGE_OVERHEAD_BYTES
    return texts, page_count, True


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
