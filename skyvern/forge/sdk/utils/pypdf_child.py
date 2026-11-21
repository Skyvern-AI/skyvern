"""pypdf text extraction that pdf_parser runs in a capped child process for page trees deeper than pypdf's default
limit, where looking up each page's inherited attributes costs pages times depth. Imports stay to the stdlib, pypdf,
pypdf_limits and the child's own bootstrap."""

from collections.abc import Iterator
from contextlib import contextmanager

from pypdf import PageObject, PdfReader, apply_configuration

from skyvern.forge.sdk.utils.capped_subprocess_child import collect_texts
from skyvern.forge.sdk.utils.pypdf_limits import hit_pypdf_size_limit

# The child's time and memory caps bound the work, and page_tree_maximum_entries still bounds the tree walk.
_PAGE_TREE_MAXIMUM_DEPTH = 100_000


class PypdfSizeLimitError(Exception):
    """A pypdf size limit the parent fails closed on; its class name tells the parent not to try pdfplumber."""


@contextmanager
def _deep_page_tree() -> Iterator[None]:
    with apply_configuration(page_tree_maximum_depth=_PAGE_TREE_MAXIMUM_DEPTH):
        try:
            yield
        except Exception as error:
            if hit_pypdf_size_limit(error):
                raise PypdfSizeLimitError(str(error)) from error
            raise


def _page_text(page: PageObject) -> str:
    return page.extract_text() or ""


def page_texts(file_path: str, char_limit: int | None, max_bytes: int) -> tuple[list[str], int, bool]:
    """Return the page texts in order, the page count, and whether every page was read, within collect_texts'
    limits."""
    with _deep_page_tree():
        pages = PdfReader(file_path).pages
        texts, read_every_page = collect_texts(pages, _page_text, char_limit, max_bytes)
        return texts, len(pages), read_every_page


def page_count(file_path: str) -> int:
    with _deep_page_tree():
        return len(PdfReader(file_path).pages)
