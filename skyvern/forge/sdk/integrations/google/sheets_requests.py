"""Google Sheets API requests, built once and handed to whichever sender holds the account's access."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

SHEETS_API_ORIGIN = "https://sheets.googleapis.com"
SHEETS_API_PATH_PREFIX = "/v4/spreadsheets/"


@dataclass(frozen=True)
class SheetsRequest:
    method: Literal["GET", "POST"]
    path: str
    params: Mapping[str, str] | None = None
    json_body: dict[str, object] | None = None
    # A write may already have been applied when its reply is lost, so a sender never repeats it.
    replay_safe: bool = True


def _path(spreadsheet_id: str, suffix: str = "") -> str:
    return f"{SHEETS_API_PATH_PREFIX}{spreadsheet_id}{suffix}"


def values_get_request(spreadsheet_id: str, *, fields: str, ranges: str | None = None) -> SheetsRequest:
    params = {"fields": fields}
    if ranges is not None:
        params["ranges"] = ranges
    return SheetsRequest("GET", _path(spreadsheet_id), params=params)


def values_append_request(
    spreadsheet_id: str,
    range_: str,
    values: list[list[object]],
    *,
    insert_data_option: Literal["INSERT_ROWS", "OVERWRITE"] = "INSERT_ROWS",
) -> SheetsRequest:
    return SheetsRequest(
        "POST",
        _path(spreadsheet_id, f"/values/{quote(range_, safe='')}:append"),
        params={"valueInputOption": "USER_ENTERED", "insertDataOption": insert_data_option},
        json_body={"range": range_, "majorDimension": "ROWS", "values": values},
        replay_safe=False,
    )


def values_update_request(spreadsheet_id: str, range_: str, values: list[list[object]]) -> SheetsRequest:
    return SheetsRequest(
        "POST",
        _path(spreadsheet_id, "/values:batchUpdate"),
        json_body={
            "valueInputOption": "USER_ENTERED",
            "data": [{"range": range_, "majorDimension": "ROWS", "values": values}],
        },
        replay_safe=False,
    )


def batch_update_request(spreadsheet_id: str, requests: list[dict[str, object]]) -> SheetsRequest:
    return SheetsRequest(
        "POST", _path(spreadsheet_id, ":batchUpdate"), json_body={"requests": requests}, replay_safe=False
    )


def updated_range(response: object) -> str | None:
    """The range an UpdateValuesResponse says was written; None when the reply gives no such evidence."""
    if not isinstance(response, dict):
        return None
    value = response.get("updatedRange")
    return value if isinstance(value, str) and value else None


def appended_range(payload: object) -> str | None:
    return updated_range(payload.get("updates")) if isinstance(payload, dict) else None
