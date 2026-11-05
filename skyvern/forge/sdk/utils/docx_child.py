"""python-docx calls that pdf_parser runs in a capped child process. Imports stay to the stdlib, python-docx and the
child's own bootstrap, because every call pays for them again in a fresh interpreter."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import docx
from docx.oxml.ns import qn

from skyvern.forge.sdk.utils.capped_subprocess_child import collect_texts

if TYPE_CHECKING:
    from lxml.etree import _Element

_PARAGRAPH = qn("w:p")
_TABLE = qn("w:tbl")
_ROW = qn("w:tr")
_CELL = qn("w:tc")
_CONTENT_CONTROL = qn("w:sdt")
_CONTENT_CONTROL_CONTENT = qn("w:sdtContent")
_CUSTOM_XML = qn("w:customXml")
_TEXT = qn("w:t")
_TABS = frozenset({qn("w:tab"), qn("w:ptab")})
_NO_BREAK_HYPHEN = qn("w:noBreakHyphen")
_BREAKS = frozenset({qn("w:br"), qn("w:cr")})
# Properties hold tab stops and content-control settings, not text; a text box is a separate story. A tracked
# deletion and the source of a tracked move hold text the document no longer has.
_NOT_TEXT = frozenset(
    {
        qn("w:pPr"),
        qn("w:rPr"),
        qn("w:sdtPr"),
        qn("w:sdtEndPr"),
        qn("w:customXmlPr"),
        qn("w:txbxContent"),
        qn("w:del"),
        qn("w:moveFrom"),
    }
)


def _children(element: _Element) -> Iterator[_Element]:
    for child in element:
        if child.tag == _CONTENT_CONTROL:
            content = child.find(_CONTENT_CONTROL_CONTENT)
            if content is not None:
                yield from _children(content)
        elif child.tag == _CUSTOM_XML:
            yield from _children(child)
        else:
            yield child


def _run_text(element: _Element, parts: list[str]) -> None:
    for child in element:
        if child.tag == _TEXT:
            parts.append(child.text or "")
        elif child.tag in _TABS:
            parts.append("\t")
        elif child.tag == _NO_BREAK_HYPHEN:
            parts.append("-")
        elif child.tag in _BREAKS:
            parts.append("\n")
        elif child.tag not in _NOT_TEXT:
            _run_text(child, parts)


def _blocks(container: _Element) -> Iterator[str]:
    for child in _children(container):
        if child.tag == _PARAGRAPH:
            parts: list[str] = []
            _run_text(child, parts)
            yield "".join(parts)
        elif child.tag == _TABLE:
            for row in _children(child):
                if row.tag != _ROW:
                    continue
                cells = [
                    " ".join(text for text in _blocks(cell) if text.strip())
                    for cell in _children(row)
                    if cell.tag == _CELL
                ]
                if any(cells):
                    yield " | ".join(cells)


def block_texts(file_path: str, char_limit: int, max_bytes: int) -> tuple[list[str], bool]:
    """Return the paragraph and table-row texts that are not blank, in document order, and whether every block was
    read, within collect_texts' limits."""
    blocks = (text for text in _blocks(docx.Document(file_path).element.body) if text.strip())
    return collect_texts(blocks, str, char_limit, max_bytes)
