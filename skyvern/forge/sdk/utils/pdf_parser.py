"""
Utility functions for PDF parsing with fallback support.

This module provides robust PDF parsing that tries pypdf first and falls back
to pdfplumber if pypdf fails, ensuring maximum compatibility with various PDF formats.
"""

import pdfplumber
import structlog
from pypdf import PdfReader

from skyvern.exceptions import PDFParsingError

LOG = structlog.get_logger(__name__)


def extract_pdf_file(
    file_path: str,
    file_identifier: str | None = None,
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
        page_count = len(reader.pages)

        for i in range(page_count):
            page_text = reader.pages[i].extract_text() or ""
            extracted_text += page_text + "\n"

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
                page_count = len(pdf.pages)

                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        extracted_text += page_text + "\n"

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
