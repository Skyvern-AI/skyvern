"""
Utility functions for PDF parsing with fallback support.

This module provides robust PDF parsing that tries pypdf first and falls back
to pdfplumber if pypdf fails, ensuring maximum compatibility with various PDF formats.
For image-based/scanned PDFs where text extraction yields no content, pages can be
rendered as images via render_pdf_pages_as_images for vision-LLM OCR. A text layer that
is non-empty but glyph-scrambled needs the same OCR fallback; scrambled_text_layer_reason
identifies it.
"""

import io

import pdfplumber
import structlog
from pypdf import PdfReader

from skyvern.constants import MAX_FILE_PARSE_INPUT_TOKENS, MAX_PDF_OCR_PAGES
from skyvern.exceptions import PDFParsingError
from skyvern.forge.sdk.utils.sanitization import sanitize_postgres_text
from skyvern.utils.token_counter import count_tokens

LOG = structlog.get_logger(__name__)

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
        PDFParsingError: When both pypdf and pdfplumber fail to parse the PDF

    Example:
        >>> text = extract_pdf_file("/path/to/file.pdf", "document.pdf")
        >>> print(f"Extracted {len(text)} characters")
    """
    identifier = file_identifier or file_path

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
                        file_identifier=identifier,
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
            file_identifier=identifier,
            page_count=page_count,
            text_length=len(extracted_text),
        )
        return extracted_text

    except Exception as pypdf_error:
        LOG.warning(
            "Failed to parse PDF with pypdf, trying pdfplumber",
            file_identifier=identifier,
            error=str(pypdf_error),
            error_type=type(pypdf_error).__name__,
        )

        # Fallback to pdfplumber
        try:
            with pdfplumber.open(file_path) as pdf:
                extracted_text = ""
                current_tokens = 0
                page_count = len(pdf.pages)

                for i, page in enumerate(pdf.pages):
                    page_text = page.extract_text()
                    if page_text:
                        if max_tokens:
                            page_tokens = count_tokens(page_text)
                            if current_tokens + page_tokens > max_tokens:
                                LOG.warning(
                                    "PDF text exceeds token limit, truncating at page boundary",
                                    file_identifier=identifier,
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
                    "Successfully parsed PDF with pdfplumber",
                    file_identifier=identifier,
                    page_count=page_count,
                    text_length=len(extracted_text),
                )
                return extracted_text

        except Exception as pdfplumber_error:
            LOG.error(
                "Failed to parse PDF with both pypdf and pdfplumber",
                file_identifier=identifier,
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
        PDFParsingError: When both pypdf and pdfplumber fail to validate the PDF

    Example:
        >>> if validate_pdf_file("/path/to/file.pdf"):
        ...     print("Valid PDF file")
    """
    identifier = file_identifier or file_path

    # Try pypdf first
    try:
        reader = PdfReader(file_path)
        # Just check if we can access pages, don't read content yet
        _ = len(reader.pages)
        LOG.debug(
            "PDF validation successful with pypdf",
            file_identifier=identifier,
        )
        return True

    except Exception as pypdf_error:
        LOG.debug(
            "PDF validation with pypdf failed, trying pdfplumber",
            file_identifier=identifier,
            error=str(pypdf_error),
        )

        # Fallback to pdfplumber
        try:
            with pdfplumber.open(file_path) as pdf:
                _ = len(pdf.pages)

            LOG.info(
                "PDF validation: pypdf failed but pdfplumber succeeded",
                file_identifier=identifier,
                pypdf_error=str(pypdf_error),
            )
            return True

        except Exception as pdfplumber_error:
            LOG.error(
                "PDF validation failed with both pypdf and pdfplumber",
                file_identifier=identifier,
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
    page_images: list[bytes] = []

    with pdfplumber.open(file_path) as pdf:
        pages_to_render = len(pdf.pages) if max_pages is None else min(len(pdf.pages), max_pages)
        if pages_to_render < len(pdf.pages):
            LOG.warning(
                "PDF has more pages than max_pages for image rendering, truncating",
                file_identifier=identifier,
                total_pages=len(pdf.pages),
                max_pages=max_pages,
            )

        for page in pdf.pages[:pages_to_render]:
            img = page.to_image(resolution=resolution)
            buf = io.BytesIO()
            img.original.save(buf, format="PNG")
            page_images.append(buf.getvalue())

    LOG.info(
        "Rendered PDF pages as images for vision-LLM fallback",
        file_identifier=identifier,
        pages_rendered=len(page_images),
    )
    return page_images
