"""Tells which pypdf limit an error came from. Imports stay to the stdlib and pypdf, so the capped pypdf child can
classify its own failure."""

from collections.abc import Callable
from types import FrameType

from pypdf import get_configuration
from pypdf.errors import LimitReachedError

# pypdf raises one LimitReachedError class for every limit, sometimes re-raised as PdfReadError, so only the raising
# frame (as of pypdf 6.21, the floor in pyproject.toml) tells a size cap from a structural limit pdfminer reads safely.
# Any limit in the filter modules is a size cap unless carved out below, so one a later pypdf adds fails closed; the
# frames after the carve-outs are the other caps pdfminer lacks: a content array, element and token counts, a ToUnicode
# range, CID widths and an eager page tree.
_PYPDF_DECOMPRESSION_LIMIT_MODULES = frozenset({"pypdf.filters", "pypdf._codecs._codecs"})
# A predictor row length pdfminer never allocates, and JBIG2, which pdfminer passes through undecoded.
_PYPDF_STRUCTURAL_FILTER_FRAMES = frozenset(
    {("pypdf.filters", "FlateDecode.decode"), ("pypdf.filters", "JBIG2Decode.decode")}
)
_PYPDF_SIZE_LIMIT_FRAMES = frozenset(
    {
        ("pypdf.generic._data_structures", "ContentStream.__init__"),
        ("pypdf.generic._data_structures", "ContentStream._check_limits"),
        ("pypdf.generic._data_structures", "ArrayObject._check_limits"),
        ("pypdf.generic._data_structures", "DictionaryObject.read_from_stream"),
        ("pypdf.generic._data_structures", "DictionaryObject._read_stream_data"),
        ("pypdf.generic._data_structures", "DictionaryObject._read_unsized_from_stream"),
        ("pypdf._cmap", "_check_mapping_size"),
        ("pypdf._doc_common", "PdfDocCommon._pop_next_page_tree_kid"),
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
            ("pypdf.generic._data_structures", "DictionaryObject._read_stream_data"),
        ),
        (
            ("pypdf.generic._font", "Font.__check_entry_count"),
            ("pypdf.generic._font", "Font._collect_cid_character_widths"),
        ),
    }
)
_PYPDF_CID_WIDTH_RANGE_FRAME = ("pypdf.generic._font", "Font.__check_range_length")
# pdfplumber misreads some trees past this depth (2 of 512 pages, mirrored text), so these are read with pypdf again
# in a capped child: looking up each page's inherited attributes costs pages times depth.
_PYPDF_PAGE_TREE_DEPTH_FRAME = ("pypdf._doc_common", "PdfDocCommon._flatten")
_PYPDF_PREDICTOR_PARAMETERS_FRAME = ("pypdf.filters", "FlateDecode._get_parameters")


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


def _hit_pypdf_limit(error: BaseException, is_limit_frame: Callable[[FrameType], bool]) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, LimitReachedError) and current.__traceback__ is not None:
            innermost = current.__traceback__
            while innermost.tb_next is not None:
                innermost = innermost.tb_next
            if is_limit_frame(innermost.tb_frame):
                return True
        current = current.__cause__ or current.__context__
    return False


def hit_pypdf_page_tree_depth_limit(error: BaseException) -> bool:
    return _hit_pypdf_limit(error, lambda frame: _frame_site(frame) == _PYPDF_PAGE_TREE_DEPTH_FRAME)


def hit_pypdf_size_limit(error: BaseException) -> bool:
    return _hit_pypdf_limit(error, _is_size_limit_frame)
