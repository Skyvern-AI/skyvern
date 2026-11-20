"""A1 ranges must be 1-based before they are turned into a Sheets grid range."""

import pytest

from skyvern.schemas.google_sheets import a1_to_grid_range


@pytest.mark.parametrize("a1", ["A0", "A0:B2", "B2:A0", "Sheet1!A0"])
def test_row_zero_is_rejected(a1: str) -> None:
    # `_A1_CELL_RE` accepts a "0" row, which used to produce startRowIndex=-1: the
    # Sheets API rejects that range with a 400, and callers that read startRowIndex as
    # the merge origin shifted their merges by a row.
    with pytest.raises(ValueError, match="1-based"):
        a1_to_grid_range(a1, sheet_id=1)


def test_first_row_maps_to_zero_based_index() -> None:
    assert a1_to_grid_range("A1", sheet_id=1) == {
        "sheetId": 1,
        "startRowIndex": 0,
        "endRowIndex": 1,
        "startColumnIndex": 0,
        "endColumnIndex": 1,
    }


def test_range_with_sheet_prefix_still_parses() -> None:
    assert a1_to_grid_range("Sheet1!A1:B2", sheet_id=2)["endRowIndex"] == 2
