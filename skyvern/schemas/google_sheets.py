import re
from typing import Any

_SPREADSHEET_URL_RE = re.compile(r"/spreadsheets(?:/u/\d+)?/d/([a-zA-Z0-9-_]+)")
_BARE_ID_RE = re.compile(r"^[a-zA-Z0-9-_]{20,}$")
_A1_CELL_RE = re.compile(r"^([A-Z]+)(\d+)$")
_A1_COLUMN_ONLY_RE = re.compile(r"^([A-Z]+)$")

MAX_COLUMN_INDEX = 18277


class GoogleSheetsAPIError(RuntimeError):
    """Raised when the Drive or Sheets API returns a non-2xx response."""

    def __init__(self, status: int, code: str | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def extract_spreadsheet_id(url_or_id: str) -> str:
    if not url_or_id:
        raise ValueError("Empty spreadsheet reference")
    match = _SPREADSHEET_URL_RE.search(url_or_id)
    if match:
        return match.group(1)
    if _BARE_ID_RE.match(url_or_id):
        return url_or_id
    raise ValueError(f"Could not extract spreadsheet id from: {url_or_id}")


def quote_sheet_name(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def build_a1(sheet_name: str | None, cell_range: str | None) -> str | None:
    quoted = quote_sheet_name(sheet_name) if sheet_name else None
    if quoted and cell_range:
        return f"{quoted}!{cell_range}"
    if quoted:
        return quoted
    if cell_range:
        return cell_range
    return None


def column_letters_to_index(letters: str) -> int:
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


def column_index_to_letter(index: int) -> str:
    # 0 -> "A", 25 -> "Z", 26 -> "AA"
    if index < 0:
        raise ValueError(f"column index must be non-negative, got: {index}")
    letters: list[str] = []
    n = index
    while True:
        letters.append(chr(ord("A") + (n % 26)))
        n = n // 26 - 1
        if n < 0:
            break
    return "".join(reversed(letters))


def build_append_dimension_request(*, sheet_id: int, dimension: str, length: int) -> dict[str, Any]:
    """Build a Sheets batchUpdate appendDimension request adding `length` columns or rows."""
    if dimension not in ("COLUMNS", "ROWS"):
        raise ValueError(f"dimension must be COLUMNS or ROWS, got: {dimension!r}")
    if length <= 0:
        raise ValueError(f"length must be positive, got: {length}")
    return {
        "appendDimension": {
            "sheetId": sheet_id,
            "dimension": dimension,
            "length": length,
        }
    }


def strip_a1_sheet_prefix(a1: str) -> str:
    if a1.startswith("'"):
        idx = 1
        while idx < len(a1):
            if a1[idx] == "'":
                if idx + 1 < len(a1) and a1[idx + 1] == "'":
                    idx += 2
                    continue
                break
            idx += 1
        if idx < len(a1) and idx + 1 < len(a1) and a1[idx + 1] == "!":
            return a1[idx + 2 :]
    if "!" in a1:
        return a1.rsplit("!", 1)[1]
    return a1


def extract_a1_sheet_prefix(a1: str | None) -> str | None:
    """Return the unquoted sheet title from an A1 string (e.g. ``'Target'!B2:C3`` -> ``Target``)."""
    if not a1:
        return None
    if a1.startswith("'"):
        idx = 1
        while idx < len(a1):
            if a1[idx] == "'":
                if idx + 1 < len(a1) and a1[idx + 1] == "'":
                    idx += 2
                    continue
                break
            idx += 1
        if idx < len(a1) and idx + 1 < len(a1) and a1[idx + 1] == "!":
            return a1[1:idx].replace("''", "'") or None
        return None
    if "!" in a1:
        sheet_part = a1.partition("!")[0]
        return sheet_part or None
    return None


def leading_column_offset(a1: str | None) -> int:
    if not a1:
        return 0
    body = strip_a1_sheet_prefix(a1)
    start = body.split(":", 1)[0].upper()
    cell_match = _A1_CELL_RE.match(start)
    if cell_match:
        return column_letters_to_index(cell_match.group(1))
    col_match = _A1_COLUMN_ONLY_RE.match(start)
    if col_match:
        return column_letters_to_index(col_match.group(1))
    return 0


def a1_to_grid_range(a1: str, sheet_id: int) -> dict[str, int]:
    body = strip_a1_sheet_prefix(a1)
    parts = body.split(":")
    if len(parts) == 1:
        parts = [parts[0], parts[0]]
    elif len(parts) != 2:
        raise ValueError(f"Unsupported A1 range: {a1!r}")
    start_match = _A1_CELL_RE.match(parts[0].upper())
    end_match = _A1_CELL_RE.match(parts[1].upper())
    if not start_match or not end_match:
        raise ValueError(f"Only fully qualified A1 ranges are supported, got: {a1!r}")
    start_col = column_letters_to_index(start_match.group(1))
    start_row = int(start_match.group(2)) - 1
    end_col = column_letters_to_index(end_match.group(1))
    end_row = int(end_match.group(2)) - 1
    return {
        "sheetId": sheet_id,
        "startRowIndex": start_row,
        "endRowIndex": end_row + 1,
        "startColumnIndex": start_col,
        "endColumnIndex": end_col + 1,
    }
