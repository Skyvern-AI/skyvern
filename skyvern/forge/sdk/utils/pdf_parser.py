"""
Utility functions for PDF parsing with fallback support.

This module provides robust PDF parsing that tries pypdf first and falls back
to pdfplumber if pypdf fails, ensuring maximum compatibility with various PDF formats.
For image-based/scanned PDFs where text extraction yields no content, pages can be
rendered as images via render_pdf_pages_as_images for vision-LLM OCR. A text layer that
is non-empty but glyph-scrambled needs the same OCR fallback; scrambled_text_layer_reason
identifies it.
"""

import time
from collections.abc import Callable, Collection
from types import FrameType
from typing import Any, NamedTuple, TypeVar, TypeVarTuple, Unpack

import structlog
from pypdf import PdfReader, get_configuration
from pypdf.errors import LimitReachedError

from skyvern.config import settings
from skyvern.constants import FILE_PARSE_STEP_TIMEOUT_SECONDS, MAX_FILE_PARSE_INPUT_TOKENS, MAX_PDF_OCR_PAGES
from skyvern.exceptions import PDFParsingError
from skyvern.forge.sdk.utils import pdfplumber_child
from skyvern.forge.sdk.utils.capped_subprocess import CappedSubprocessError, run_in_capped_subprocess
from skyvern.forge.sdk.utils.sanitization import sanitize_postgres_text
from skyvern.utils.token_counter import count_tokens

LOG = structlog.get_logger(__name__)

_T = TypeVar("_T")
_Ts = TypeVarTuple("_Ts")

# o200k averages about 4 characters a token in English prose, so a child that stops reading pages at 8 characters
# per allowed token still returns every page the token cap keeps; the exact cut happens in the parent.
_CHARS_PER_TOKEN_STOP = 8


class _ResultLimit(NamedTuple):
    max_bytes: int
    memory_factor: int


# A result is capped in pickled bytes, but the parent holds it decoded: word dicts take 5.9x for real words and up to
# 6.4x for crafted ones on Python 3.11, text with astral characters up to 6x, and page PNGs 1x because they are read
# straight into their final buffer.
_PAGE_COUNT_RESULT = _ResultLimit(64 * 1024, 6)
_PAGE_TEXTS_RESULT = _ResultLimit(32 * 1024 * 1024, 6)
_UPRIGHT_WORDS_RESULT = _ResultLimit(32 * 1024 * 1024, 7)
_PAGE_IMAGES_MEMORY_FACTOR = 1
# Fallback text is read up to this many characters; the child also stops before a page that would push the pickled
# text past _PAGE_TEXTS_RESULT, so the page that crosses a limit is cut instead of failing the whole parse.
_PAGE_TEXTS_CHAR_LIMIT = _PAGE_TEXTS_RESULT.max_bytes // 4

# A font whose ToUnicode map is wrong substitutes its glyphs out of an unrelated Unicode
# block. Latin Extended-A/B and the private-use area are where those substitutions land;
# a Latin-script document's own characters only appear there as in-word diacritics.
_REMAPPED_GLYPH_RANGES: tuple[tuple[int, int], ...] = ((0x0100, 0x024F), (0xE000, 0xF8FF), (0xFFFD, 0xFFFD))
# Below these floors the shares computed from the text layer are too noisy to act on.
_MIN_TEXT_LAYER_CHARS = 40
_MIN_TEXT_LAYER_WORDS = 10
_MIN_REMAPPED_GLYPHS = 8
_MIN_ISOLATED_WORDS = 3
# A remapped number is a run of glyphs; a checkbox or bullet drawn from an icon font is a
# single glyph standing alone. Only runs count, so an ordinary form keeps its text layer.
_MIN_ISOLATED_WORD_LENGTH = 2
# Diacritics sit inside words next to ASCII letters, so a legitimately accented language
# keeps this share low (measured ~0.3 for Vietnamese, 0.0 for Polish/Czech/Turkish) while a
# scrambled digit run is a whole word of nothing else (~0.9).
_MIN_ISOLATED_GLYPH_SHARE = 0.6
# Non-Latin scripts are read as-is: their characters fall outside the ranges above, so the
# shares would be measuring the wrong thing.
_MIN_ASCII_LETTER_SHARE = 0.5
# pypdf raises one LimitReachedError class for every limit, sometimes re-raised as PdfReadError, so only the raising
# frame tells a size cap from a structural limit pdfminer reads safely. Any limit in the filter modules is a size cap
# unless carved out below, so one a later pypdf adds fails closed; the frames after the carve-outs are the other caps
# pdfminer lacks: a content array, a ToUnicode range, CID widths and an eager page tree.
_PYPDF_DECOMPRESSION_LIMIT_MODULES = frozenset({"pypdf.filters", "pypdf._codecs._codecs"})
# A predictor row length pdfminer never allocates, and JBIG2, which pdfminer passes through undecoded.
_PYPDF_STRUCTURAL_FILTER_FRAMES = frozenset(
    {("pypdf.filters", "FlateDecode.decode"), ("pypdf.filters", "JBIG2Decode.decode")}
)
_PYPDF_SIZE_LIMIT_FRAMES = frozenset(
    {
        ("pypdf.generic._data_structures", "ContentStream.__init__"),
        ("pypdf.generic._data_structures", "DictionaryObject.read_from_stream"),
        ("pypdf.generic._data_structures", "DictionaryObject._read_unsized_from_stream"),
        ("pypdf._cmap", "_check_mapping_size"),
        ("pypdf._doc_common", "PdfDocCommon._flatten_page_tree_node"),
    }
)
# These frames also enforce structural limits (token lengths, simple-font widths), so the caller tells them apart.
_PYPDF_SIZE_LIMIT_CALLS = frozenset(
    {
        (
            ("pypdf._cmap", "_check_token_length"),
            ("pypdf._cmap", "parse_bfrange"),
        ),
        (
            ("pypdf._utils", "read_until_regex"),
            ("pypdf.generic._data_structures", "DictionaryObject.read_from_stream"),
        ),
        (
            ("pypdf._font", "Font.__check_entry_count"),
            ("pypdf._font", "Font._collect_cid_character_widths"),
        ),
    }
)
_PYPDF_CID_WIDTH_RANGE_FRAME = ("pypdf._font", "Font.__check_range_length")
_PYPDF_PREDICTOR_PARAMETERS_FRAME = ("pypdf.filters", "FlateDecode._get_parameters")


def _is_remapped_glyph(char: str) -> bool:
    code_point = ord(char)
    return any(start <= code_point <= end for start, end in _REMAPPED_GLYPH_RANGES)


def _is_isolated_word(word: str) -> bool:
    return (
        len(word) >= _MIN_ISOLATED_WORD_LENGTH
        and any(_is_remapped_glyph(char) for char in word)
        and not any(char.isascii() and char.isalnum() for char in word)
    )


def scrambled_text_layer_reason(text: str) -> str | None:
    """Return why a PDF text layer looks glyph-scrambled, or None if it can be trusted.

    A subset font with a broken ToUnicode map keeps the document's layout and its ASCII
    words but replaces whole runs — typically every digit — with characters from an
    unrelated Unicode block, so the extracted text is plausible-looking and wrong.
    """
    words = text.split()
    if len(words) < _MIN_TEXT_LAYER_WORDS:
        return None
    if sum(1 for char in text if not char.isspace()) < _MIN_TEXT_LAYER_CHARS:
        return None

    # Remapped glyphs are Latin letters to str.isalpha, so counting them here would let the
    # corruption vote on which script the document is written in — and a page of scrambled
    # numbers would read as non-Latin and escape every check below.
    letters = [char for char in text if char.isalpha() and not _is_remapped_glyph(char)]
    if letters and sum(1 for char in letters if char.isascii()) / len(letters) < _MIN_ASCII_LETTER_SHARE:
        return None

    remapped_glyphs = sum(1 for char in text if _is_remapped_glyph(char))
    if remapped_glyphs < _MIN_REMAPPED_GLYPHS:
        return None

    isolated_words = [word for word in words if _is_isolated_word(word)]
    if len(isolated_words) < _MIN_ISOLATED_WORDS:
        return None

    isolated_glyphs = sum(1 for word in isolated_words for char in word if _is_remapped_glyph(char))
    isolated_share = isolated_glyphs / remapped_glyphs
    if isolated_share < _MIN_ISOLATED_GLYPH_SHARE:
        return None

    return (
        f"{len(isolated_words)} of {len(words)} words in the PDF text layer are built only from characters "
        f"outside the document's script ({isolated_share:.0%} of the {remapped_glyphs} such characters found), "
        "which means the embedded font maps its glyphs to the wrong code points"
    )


def _frame_site(frame: FrameType) -> tuple[str | None, str]:
    return frame.f_globals.get("__name__"), frame.f_code.co_qualname


def _is_size_limit_frame(frame: FrameType) -> bool:
    site = _frame_site(frame)
    if site == _PYPDF_PREDICTOR_PARAMETERS_FRAME:
        # The same frame rejects a small Colors over 16, which pdfminer reads, but a huge Columns or row it would hold
        # as Python ints without any limit. The row is sized at 8 bits per component, the most pdfminer decodes,
        # because pypdf reads BitsPerComponent after this check.
        columns, colors = frame.f_locals.get("columns"), frame.f_locals.get("colors")
        configuration = get_configuration()
        return isinstance(columns, int) and (
            columns > configuration.flate_maximum_columns
            or (isinstance(colors, int) and columns * colors + 1 > configuration.flate_maximum_row_length)
        )
    if site in _PYPDF_STRUCTURAL_FILTER_FRAMES:
        return False
    if site[0] in _PYPDF_DECOMPRESSION_LIMIT_MODULES or site in _PYPDF_SIZE_LIMIT_FRAMES:
        return True
    if frame.f_back is not None and (site, _frame_site(frame.f_back)) in _PYPDF_SIZE_LIMIT_CALLS:
        return True
    if site == _PYPDF_CID_WIDTH_RANGE_FRAME:
        # The same frame rejects an inverted range, which expands to nothing in pdfminer.
        start, end = frame.f_locals.get("start"), frame.f_locals.get("end")
        return isinstance(start, int) and isinstance(end, int) and end >= start
    return False


def _hit_pypdf_size_limit(error: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, LimitReachedError) and current.__traceback__ is not None:
            innermost = current.__traceback__
            while innermost.tb_next is not None:
                innermost = innermost.tb_next
            if _is_size_limit_frame(innermost.tb_frame):
                return True
        current = current.__cause__ or current.__context__
    return False


def _size_limit_failure(identifier: str, pypdf_error: Exception) -> PDFParsingError:
    LOG.warning(
        "PDF exceeds a pypdf size limit that pdfplumber lacks, not falling back to pdfplumber",
        file_identifier=identifier.partition("?")[0],
        error=str(pypdf_error),
        error_type=type(pypdf_error).__name__,
    )
    return PDFParsingError(
        file_identifier=identifier,
        pypdf_error=str(pypdf_error),
        pdfplumber_error="not attempted: the PDF exceeds a size limit",
    )


def _run_pdfplumber(
    func: Callable[[Unpack[_Ts]], _T], *args: Unpack[_Ts], result: _ResultLimit, log_identifier: str, started_at: float
) -> _T:
    # pdfminer, which pdfplumber parses with, decodes streams with no size limit, so a crafted PDF can grow memory
    # until the pod is OOM-killed. The child is capped and gets what is left of the caller's parse budget.
    return run_in_capped_subprocess(
        func,
        *args,
        memory_limit_bytes=settings.PDFPLUMBER_MAX_MEMORY_BYTES,
        max_result_bytes=result.max_bytes,
        result_memory_factor=result.memory_factor,
        timeout_seconds=max(1.0, FILE_PARSE_STEP_TIMEOUT_SECONDS - (time.monotonic() - started_at)),
        log_fields={"file_identifier": log_identifier},
    )


def _join_within_token_limit(page_texts: list[str], max_tokens: int) -> tuple[str, int | None]:
    """Join page texts until the next one would pass max_tokens; also return that page's index, if any."""
    extracted_text = ""
    current_tokens = 0
    for i, page_text in enumerate(page_texts):
        if page_text:
            if max_tokens:
                page_tokens = count_tokens(page_text)
                if current_tokens + page_tokens > max_tokens:
                    return extracted_text, i
                current_tokens += page_tokens
            extracted_text += page_text + "\n"
    return extracted_text, None


def extract_pdf_file(
    file_path: str,
    file_identifier: str | None = None,
    max_tokens: int = MAX_FILE_PARSE_INPUT_TOKENS,
) -> str:
    """
    Extract text from a PDF file with fallback support.

    This function attempts to parse the PDF using pypdf first. If that fails,
    it automatically falls back to pdfplumber. This provides robust handling
    of various PDF formats, including those with corrupted streams or non-standard
    formatting that may cause pypdf to fail.

    Args:
        file_path: Path to the PDF file to parse
        file_identifier: Optional identifier for logging (e.g., URL or filename).
                        If not provided, uses file_path.

    Returns:
        Extracted text from all pages of the PDF

    Raises:
        PDFParsingError: When both pypdf and pdfplumber fail to parse the PDF, or when the PDF
                         exceeds a pypdf size limit that pdfplumber lacks (pdfplumber is not tried then)

    Example:
        >>> text = extract_pdf_file("/path/to/file.pdf", "document.pdf")
        >>> print(f"Extracted {len(text)} characters")
    """
    identifier = file_identifier or file_path
    log_identifier = identifier.partition("?")[0]
    started_at = time.monotonic()

    # Try pypdf first
    try:
        reader = PdfReader(file_path)
        extracted_text = ""
        current_tokens = 0
        page_count = len(reader.pages)

        for i in range(page_count):
            page_text = reader.pages[i].extract_text() or ""
            if max_tokens:
                page_tokens = count_tokens(page_text)
                if current_tokens + page_tokens > max_tokens:
                    LOG.warning(
                        "PDF text exceeds token limit, truncating at page boundary",
                        file_identifier=log_identifier,
                        pages_included=i,
                        total_pages=page_count,
                        max_tokens=max_tokens,
                    )
                    break
                current_tokens += page_tokens
            extracted_text += page_text + "\n"

        # Sanitize text to remove characters that cannot be stored in PostgreSQL
        extracted_text = sanitize_postgres_text(extracted_text)

        LOG.info(
            "Successfully parsed PDF with pypdf",
            file_identifier=log_identifier,
            page_count=page_count,
            text_length=len(extracted_text),
        )
        return extracted_text

    except Exception as pypdf_error:
        if _hit_pypdf_size_limit(pypdf_error):
            raise _size_limit_failure(identifier, pypdf_error) from pypdf_error
        LOG.warning(
            "Failed to parse PDF with pypdf, trying pdfplumber",
            file_identifier=log_identifier,
            error=str(pypdf_error),
            error_type=type(pypdf_error).__name__,
        )

        # Fallback to pdfplumber
        try:
            # Only text averaging over 8 characters a token, such as long runs of one symbol, stops at this limit
            # before the token cap; the second pass then reads on to the most text a result may hold.
            first_char_limit = (
                min(max_tokens * _CHARS_PER_TOKEN_STOP, _PAGE_TEXTS_CHAR_LIMIT)
                if max_tokens
                else _PAGE_TEXTS_CHAR_LIMIT
            )
            page_texts, page_count, read_every_page = _run_pdfplumber(
                pdfplumber_child.page_texts,
                file_path,
                first_char_limit,
                _PAGE_TEXTS_RESULT.max_bytes,
                result=_PAGE_TEXTS_RESULT,
                log_identifier=log_identifier,
                started_at=started_at,
            )
            extracted_text, truncated_at_page = _join_within_token_limit(page_texts, max_tokens)
            read_on_failed = False
            if truncated_at_page is None and not read_every_page and first_char_limit < _PAGE_TEXTS_CHAR_LIMIT:
                try:
                    more_texts, more_page_count, more_read_every_page = _run_pdfplumber(
                        pdfplumber_child.page_texts,
                        file_path,
                        _PAGE_TEXTS_CHAR_LIMIT,
                        _PAGE_TEXTS_RESULT.max_bytes,
                        result=_PAGE_TEXTS_RESULT,
                        log_identifier=log_identifier,
                        started_at=started_at,
                    )
                except CappedSubprocessError as read_on_error:
                    # The first pass already holds usable text, so a second pass that fails does not lose it.
                    read_on_failed = True
                    LOG.warning(
                        "Reading the rest of the PDF with pdfplumber failed, keeping the pages already read",
                        file_identifier=log_identifier,
                        failure=read_on_error.kind,
                        error=str(read_on_error),
                        pages_included=len(page_texts),
                        total_pages=page_count,
                    )
                else:
                    page_texts, page_count, read_every_page = more_texts, more_page_count, more_read_every_page
                    extracted_text, truncated_at_page = _join_within_token_limit(page_texts, max_tokens)
            if truncated_at_page is not None:
                LOG.warning(
                    "PDF text exceeds token limit, truncating at page boundary",
                    file_identifier=log_identifier,
                    pages_included=truncated_at_page,
                    total_pages=page_count,
                    max_tokens=max_tokens,
                )
            elif not read_every_page and not read_on_failed:
                LOG.warning(
                    "PDF text exceeds the size limit for a fallback parse, truncating at page boundary",
                    file_identifier=log_identifier,
                    pages_included=len(page_texts),
                    total_pages=page_count,
                    char_limit=_PAGE_TEXTS_CHAR_LIMIT,
                )

            # Sanitize text to remove characters that cannot be stored in PostgreSQL
            extracted_text = sanitize_postgres_text(extracted_text)

            LOG.info(
                "Successfully parsed PDF with pdfplumber",
                file_identifier=log_identifier,
                page_count=page_count,
                text_length=len(extracted_text),
            )
            return extracted_text

        except Exception as pdfplumber_error:
            LOG.error(
                "Failed to parse PDF with both pypdf and pdfplumber",
                file_identifier=log_identifier,
                pypdf_error=str(pypdf_error),
                pdfplumber_error=str(pdfplumber_error),
            )
            raise PDFParsingError(
                file_identifier=identifier,
                pypdf_error=str(pypdf_error),
                pdfplumber_error=str(pdfplumber_error),
            )


def validate_pdf_file(
    file_path: str,
    file_identifier: str | None = None,
) -> bool:
    """
    Validate that a file is a readable PDF.

    This function attempts to validate the PDF using pypdf first. If that fails,
    it automatically falls back to pdfplumber validation.

    Args:
        file_path: Path to the PDF file to validate
        file_identifier: Optional identifier for logging (e.g., URL or filename).
                        If not provided, uses file_path.

    Returns:
        True if the PDF can be opened and read by at least one parser

    Raises:
        PDFParsingError: When both pypdf and pdfplumber fail to validate the PDF, or when the PDF
                         exceeds a pypdf size limit that pdfplumber lacks (pdfplumber is not tried then)

    Example:
        >>> if validate_pdf_file("/path/to/file.pdf"):
        ...     print("Valid PDF file")
    """
    identifier = file_identifier or file_path
    log_identifier = identifier.partition("?")[0]
    started_at = time.monotonic()

    # Try pypdf first
    try:
        reader = PdfReader(file_path)
        # Just check if we can access pages, don't read content yet
        _ = len(reader.pages)
        LOG.debug(
            "PDF validation successful with pypdf",
            file_identifier=log_identifier,
        )
        return True

    except Exception as pypdf_error:
        if _hit_pypdf_size_limit(pypdf_error):
            raise _size_limit_failure(identifier, pypdf_error) from pypdf_error
        LOG.debug(
            "PDF validation with pypdf failed, trying pdfplumber",
            file_identifier=log_identifier,
            error=str(pypdf_error),
        )

        # Fallback to pdfplumber
        try:
            _run_pdfplumber(
                pdfplumber_child.page_count,
                file_path,
                result=_PAGE_COUNT_RESULT,
                log_identifier=log_identifier,
                started_at=started_at,
            )

            LOG.info(
                "PDF validation: pypdf failed but pdfplumber succeeded",
                file_identifier=log_identifier,
                pypdf_error=str(pypdf_error),
            )
            return True

        except Exception as pdfplumber_error:
            LOG.error(
                "PDF validation failed with both pypdf and pdfplumber",
                file_identifier=log_identifier,
                pypdf_error=str(pypdf_error),
                pdfplumber_error=str(pdfplumber_error),
            )
            raise PDFParsingError(
                file_identifier=identifier,
                pypdf_error=str(pypdf_error),
                pdfplumber_error=str(pdfplumber_error),
            )


def render_pdf_pages_as_images(
    file_path: str,
    file_identifier: str | None = None,
    max_pages: int | None = MAX_PDF_OCR_PAGES,
    resolution: int = 150,
) -> list[bytes]:
    """Render PDF pages as PNG images using pdfplumber.

    Returns a list of PNG byte buffers, one per page. Defaults to a bounded page
    cap; pass ``max_pages=None`` to deliberately render every page. Intended for
    vision-LLM fallback when text extraction returns nothing (e.g. scanned /
    image-based PDFs).
    """
    identifier = file_identifier or file_path
    log_identifier = identifier.partition("?")[0]
    page_images, total_pages = _run_pdfplumber(
        pdfplumber_child.page_images,
        file_path,
        max_pages,
        resolution,
        settings.PDFPLUMBER_MAX_RESULT_BYTES,
        result=_ResultLimit(settings.PDFPLUMBER_MAX_RESULT_BYTES, _PAGE_IMAGES_MEMORY_FACTOR),
        log_identifier=log_identifier,
        started_at=time.monotonic(),
    )
    if len(page_images) < total_pages:
        LOG.warning(
            "PDF has more pages than max_pages for image rendering, truncating",
            file_identifier=log_identifier,
            total_pages=total_pages,
            max_pages=max_pages,
        )

    LOG.info(
        "Rendered PDF pages as images for vision-LLM fallback",
        file_identifier=log_identifier,
        pages_rendered=len(page_images),
    )
    return page_images


def extract_upright_pdf_words(file_path: str, page_indexes: Collection[int]) -> dict[int, list[dict[str, Any]]]:
    """Map each requested 0-based page index to pdfplumber's upright words on that page."""
    return _run_pdfplumber(
        pdfplumber_child.upright_words,
        file_path,
        sorted(page_indexes),
        result=_UPRIGHT_WORDS_RESULT,
        log_identifier=file_path,
        started_at=time.monotonic(),
    )
