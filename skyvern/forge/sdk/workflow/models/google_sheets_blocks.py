import json
import os
import re
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any, ClassVar, Literal, cast

import structlog
from jinja2 import UndefinedError

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.services import google_oauth_service
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import FailedToFormatJinjaStyleParameter
from skyvern.forge.sdk.workflow.models._jinja import (
    jinja_json_finalize_required_binding_env,
)
from skyvern.forge.sdk.workflow.models.block import Block
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE
from skyvern.schemas.google_sheets import (
    MAX_COLUMN_INDEX,
    GoogleSheetsAPIError,
    a1_to_grid_range,
    build_a1,
    build_append_dimension_request,
    column_index_to_letter,
    column_letters_to_index,
    destination_start_column,
    extract_a1_sheet_prefix,
    extract_spreadsheet_id,
    leading_column_offset,
    strip_a1_sheet_prefix,
)
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType

LOG = structlog.get_logger()

_SHEETS_WRITE_SEQ = count()


def _occupancy_marker(cell: dict[str, Any]) -> dict[str, Any]:
    # Which field carried the occupancy has to survive redaction: a formula rendering "" is occupied
    # only via userEnteredValue, so collapsing both to formattedValue would make a replayed dump
    # anchor where the live run did not.
    if cell.get("formattedValue"):
        return {"formattedValue": "x"}
    if cell.get("userEnteredValue"):
        return {"userEnteredValue": {"stringValue": "x"}}
    return {}


def _occupancy_only_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """The snapshot with cell text replaced by a marker. The anchor only reads whether a cell is
    occupied, so this replays identically without carrying the customer's cell contents."""
    if snapshot is None:
        return None
    sheets = []
    for sheet in snapshot.get("sheets") or []:
        data = []
        for block in sheet.get("data") or []:
            rows = [
                {"values": [_occupancy_marker(cell) for cell in (row.get("values") or [])]}
                for row in (block.get("rowData") or [])
            ]
            data.append({"startRow": block.get("startRow", 0), "rowData": rows})
        # Carry the absence of data through: inventing the key would make a replay anchor where the
        # live run declined to.
        redacted: dict[str, Any] = {"properties": sheet.get("properties") or {}}
        if "data" in sheet:
            redacted["data"] = data
        sheets.append(redacted)
    return {"sheets": sheets}


def _maybe_dump_sheets_write(record: dict[str, Any]) -> None:
    """Record the destination a write asked for next to the one the API reports it got.

    The range actually sent is otherwise not observable from a run, so writing it takes both an
    explicit path and a local environment: the record carries the rendered cell values.
    """
    dump_dir = os.getenv("COPILOT_DUMP_SHEETS_WRITE")
    if not dump_dir or settings.ENV != "local":
        return
    try:
        target = Path(dump_dir).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        # The counter restarts with the process, so a later run would otherwise overwrite the
        # records of an earlier one in the same directory.
        path = target / f"sheets-write-{next(_SHEETS_WRITE_SEQ):04d}.json"
        while path.exists():
            path = target / f"sheets-write-{next(_SHEETS_WRITE_SEQ):04d}.json"
        path.write_text(json.dumps(record, indent=2, default=str))
        path.chmod(0o600)
    except Exception:
        LOG.warning("Failed to dump Google Sheets write")


def _disambiguate_header(header: list[str]) -> list[str]:
    """Rename empty/duplicate header cells so dict(zip(header, row)) does not drop columns."""
    counts: dict[str, int] = {}
    reserved = {h.strip() for h in header if h and h.strip()}
    disambiguated: list[str] = []
    for idx, raw in enumerate(header):
        name = raw.strip() if raw else ""
        if not name:
            candidate = f"col_{idx + 1}"
            suffix = 2
            while candidate in reserved or candidate in disambiguated:
                candidate = f"col_{idx + 1}_{suffix}"
                suffix += 1
            disambiguated.append(candidate)
            reserved.add(candidate)
            continue
        if counts.get(name, 0) == 0:
            counts[name] = 1
            disambiguated.append(name)
            continue
        counts[name] += 1
        candidate = f"{name}_{counts[name]}"
        while candidate in reserved or candidate in disambiguated:
            counts[name] += 1
            candidate = f"{name}_{counts[name]}"
        disambiguated.append(candidate)
        reserved.add(candidate)
    return disambiguated


@dataclass(frozen=True)
class GoogleSheetsAccess:
    access_token: str | None
    failure_reason: str | None


async def _google_sheets_access(organization_id: str, credential_id: str) -> GoogleSheetsAccess:
    """Mint an access token for a block's ``credential_id``, diagnosing failure before reporting it.

    A reference naming no connection and a connection whose token cannot be minted are different
    problems: reporting both as "reconnect" sends someone round the reconnect loop instead of at
    the wrong identifier.
    """
    access_token = await app.AGENT_FUNCTION.get_google_sheets_credentials(
        organization_id=organization_id,
        credential_id=credential_id,
    )
    if access_token:
        return GoogleSheetsAccess(access_token=access_token, failure_reason=None)
    return GoogleSheetsAccess(
        access_token=None,
        failure_reason=await google_oauth_service.describe_credential_failure(organization_id, credential_id),
    )


class GoogleSheetsReadBlock(Block):
    block_type: Literal[BlockType.GOOGLE_SHEETS_READ] = BlockType.GOOGLE_SHEETS_READ  # type: ignore

    spreadsheet_url: str
    sheet_name: str | None = None
    range: str | None = None
    credential_id: str | None = None
    has_header_row: bool = True
    parameters: list[PARAMETER_TYPE] = []

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "credential_id",
            "range",
            "sheet_name",
            "spreadsheet_url",
        }
    )

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        return self.parameters

    def _render_templates(self, workflow_run_context: WorkflowRunContext) -> None:
        if self.spreadsheet_url:
            self.spreadsheet_url = self.render_templatable_field(
                "spreadsheet_url", self.spreadsheet_url, workflow_run_context
            )
        if self.sheet_name:
            self.sheet_name = self.render_templatable_field("sheet_name", self.sheet_name, workflow_run_context)
        if self.range:
            self.range = self.render_templatable_field("range", self.range, workflow_run_context)
        if self.credential_id:
            self.credential_id = self.render_templatable_field(
                "credential_id", self.credential_id, workflow_run_context
            )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: Any,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        try:
            self._render_templates(workflow_run_context)
        except Exception as e:
            return await self._template_format_failure_result(
                e,
                f"Failed to format jinja template: {str(e)}",
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )

        if not self.credential_id:
            return await self.build_block_result(
                success=False,
                failure_reason="Google credential_id is required",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        try:
            spreadsheet_id = extract_spreadsheet_id(self.spreadsheet_url)
        except ValueError:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Could not resolve spreadsheet id from: {self.spreadsheet_url}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        a1 = build_a1(self.sheet_name, self.range)
        if not a1:
            return await self.build_block_result(
                success=False,
                failure_reason="Either sheet_name or range must be provided",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        effective_org_id = organization_id or workflow_run_context.organization_id
        if not effective_org_id:
            return await self.build_block_result(
                success=False,
                failure_reason="organization_id is required to load Google Sheets credentials",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        access = await _google_sheets_access(effective_org_id, self.credential_id)
        if not access.access_token:
            return await self.build_block_result(
                success=False,
                failure_reason=access.failure_reason,
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        access_token = access.access_token

        fields = (
            "spreadsheetId,sheets("
            "properties(sheetId,title,index),"
            "merges,"
            "data(startRow,startColumn,rowData(values("
            "userEnteredValue,userEnteredFormat,formattedValue,note,hyperlink"
            ")))"
            ")"
        )
        try:
            payload = await app.AGENT_FUNCTION.google_sheets_values_get(
                access_token=access_token,
                spreadsheet_id=spreadsheet_id,
                ranges=a1,
                fields=fields,
            )
        except GoogleSheetsAPIError as e:
            failure_reason = _failure_reason_from_sheets_error("read", e)
            error_data = {"status_code": e.status, "code": e.code, "error": e.message}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=failure_reason,
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except Exception as e:
            error_data = {"error": str(e), "error_type": "unknown"}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=f"Google Sheets read failed: {str(e)}",
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        if payload is None:
            error_data = {"error": "Google Sheets read returned no payload"}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason="Google Sheets runtime is not available in this build",
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        sheets = payload.get("sheets") or []
        target_sheet_title = self.sheet_name or extract_a1_sheet_prefix(a1)
        sheet_block = _select_sheet_block(sheets, target_sheet_title) or (sheets[0] if sheets else {})
        properties = sheet_block.get("properties") or {}
        data_blocks = sheet_block.get("data") or []
        first_data = data_blocks[0] if data_blocks else {}
        row_data = first_data.get("rowData") or []

        cells: list[list[dict[str, Any]]] = []
        values: list[list[Any]] = []
        for row in row_data:
            row_values = row.get("values") or []
            cells.append([dict(cell) for cell in row_values])
            values.append([cell.get("formattedValue", "") for cell in row_values])

        start_row = int(first_data.get("startRow", 0))
        start_column = int(first_data.get("startColumn", 0))
        row_count = len(cells)
        col_count = max((len(r) for r in cells), default=0)
        end_row_exclusive = start_row + row_count
        end_col_exclusive = start_column + col_count

        merges: list[dict[str, int]] = []
        for merge in sheet_block.get("merges") or []:
            m_start_row = int(merge.get("startRowIndex", 0))
            m_end_row = int(merge.get("endRowIndex", 0))
            m_start_col = int(merge.get("startColumnIndex", 0))
            m_end_col = int(merge.get("endColumnIndex", 0))
            # Clip to the intersection so a merge that begins outside the read window
            # does not produce negative offsets when a downstream rich write replays it.
            clipped_start_row = max(m_start_row, start_row)
            clipped_end_row = min(m_end_row, end_row_exclusive)
            clipped_start_col = max(m_start_col, start_column)
            clipped_end_col = min(m_end_col, end_col_exclusive)
            if clipped_start_row >= clipped_end_row or clipped_start_col >= clipped_end_col:
                continue
            merges.append(
                {
                    "start_row_index": clipped_start_row - start_row,
                    "end_row_index": clipped_end_row - start_row,
                    "start_column_index": clipped_start_col - start_column,
                    "end_column_index": clipped_end_col - start_column,
                }
            )

        rows: list[dict[str, Any]] | None = None
        if self.has_header_row and values:
            header = _disambiguate_header([str(h) for h in values[0]])
            rows = [dict(zip(header, row)) for row in values[1:]]

        output_data: dict[str, Any] = {
            "spreadsheet_id": spreadsheet_id,
            "range": a1,
            "sheet_id": properties.get("sheetId"),
            "sheet_title": properties.get("title"),
            "start_row": start_row,
            "start_column": start_column,
            "values": values,
            "rows": rows,
            "cells": cells,
            "merges": merges,
        }
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output_data)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=output_data,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


@dataclass(frozen=True)
class RichSheetsInput:
    cells: list[list[dict[str, Any]]]
    merges: list[dict[str, int]]
    sheet_id: int | None
    sheet_title: str | None


_COLUMN_OVERFLOW_RE = re.compile(
    r"Attempting to write column:\s*(\d+),?\s*beyond the last requested column of:\s*(\d+)",
    re.IGNORECASE,
)


def _maybe_rewrite_column_overflow(message: str) -> str | None:
    """Translate Google's 0-indexed 'attempting to write column N' error into a letter-friendly message.
    Returns the rewritten message, or None if the pattern does not match.
    """
    match = _COLUMN_OVERFLOW_RE.search(message)
    if not match:
        return None
    write_col_idx = int(match.group(1))
    max_col_idx = int(match.group(2))
    sheet_columns = max_col_idx + 1
    write_letter = column_index_to_letter(write_col_idx)
    max_letter = column_index_to_letter(max_col_idx)
    return (
        f"sheet has {sheet_columns} columns (last column is {max_letter}), "
        f"but this write needs column {write_letter}. "
        f"Widen the sheet, narrow the data, or remove the leading column offset on the range."
    )


def _failure_reason_from_sheets_error(action: str, exc: GoogleSheetsAPIError) -> str:
    if exc.status == 403 and exc.code == "reconnect_required":
        return f"Reconnect the Google account: {exc.message}"
    if exc.status == 429:
        return f"Google Sheets rate limit on {action}: {exc.message}"
    rewritten = _maybe_rewrite_column_overflow(exc.message)
    if rewritten is not None:
        return f"Google Sheets {action} failed: {rewritten}"
    return f"Google Sheets {action} failed (HTTP {exc.status}): {exc.message}"


def _normalized_sheet_title(title: str | None) -> str | None:
    """Sheets resolves a requested tab title leniently and echoes the canonical one back, so titles
    only compare meaningfully once quoting, surrounding space, and case are removed. Only a matched
    pair is unquoted: an apostrophe is legal in a tab title, and `'24 Data` is its own sheet."""
    if not title:
        return None
    stripped = title.strip()
    if len(stripped) > 1 and stripped.startswith("'") and stripped.endswith("'"):
        stripped = stripped[1:-1]
    return stripped.strip().casefold() or None


def _select_sheet_block(sheets: list[dict[str, Any]], title: str | None) -> dict[str, Any] | None:
    """The sheet object for `title`, or None when the snapshot does not carry it. Anchoring off an
    unrelated tab reads as an empty column and produces a row-1 anchor over populated data.

    Titles compare case-insensitively, so two tabs differing only in case resolve to whichever comes
    first in the response."""
    wanted = _normalized_sheet_title(title)
    if wanted is None:
        return sheets[0] if sheets else None
    for candidate in sheets:
        if _normalized_sheet_title((candidate.get("properties") or {}).get("title")) == wanted:
            return candidate
    return None


def _same_sheet_title(left: str | None, right: str | None) -> bool:
    left_title = _normalized_sheet_title(left)
    right_title = _normalized_sheet_title(right)
    if left_title is None or right_title is None:
        return True
    return left_title == right_title


def _destination_mismatch(
    *,
    requested_a1: str,
    start_letters: str | None,
    sheet_name: str | None,
    rows: list[list[Any]],
    updated_range: str | None,
    write_mode: str,
) -> str | None:
    """Failure text when the API reports the write reached outside the destination the block asked
    for: a different sheet, a different start column, or more columns than the block sent. The bound
    is one-sided because a report narrower than the rows sent still lands where the block asked, and
    a sheet-only destination names no column to compare at all.

    Rows are laid out relative to the range's start column whether or not column_mapping is used, so
    this holds without a resolved anchor: a report starting elsewhere means the data landed there."""
    if not start_letters or not updated_range:
        return None
    requested_sheet = sheet_name or extract_a1_sheet_prefix(requested_a1)
    if not _same_sheet_title(requested_sheet, extract_a1_sheet_prefix(updated_range)):
        return _mismatch_reason(requested_a1, updated_range, write_mode)
    start_column = column_letters_to_index(start_letters)
    if leading_column_offset(updated_range) != start_column:
        return _mismatch_reason(requested_a1, updated_range, write_mode)
    widest_row = max((len(row) for row in rows), default=1)
    actual_end = strip_a1_sheet_prefix(updated_range).split(":")[-1]
    if leading_column_offset(actual_end) > start_column + max(widest_row, 1) - 1:
        return _mismatch_reason(requested_a1, updated_range, write_mode)
    return None


def _mismatch_reason(requested_a1: str, updated_range: str, write_mode: str) -> str:
    rerun = " so re-running this workflow appends them again" if write_mode == "append" else ""
    return (
        f"Google Sheets reported the write landed outside the requested destination: "
        f"requested {requested_a1}, actually updated {updated_range}. The rows were written to that "
        f"range,{rerun or ' and were not rolled back'}."
    )


def _try_rich_sheets_input(parsed: Any) -> RichSheetsInput | None:
    if not isinstance(parsed, dict):
        return None
    cells = parsed.get("cells")
    if not isinstance(cells, list) or not cells:
        return None
    if not all(isinstance(row, list) for row in cells):
        return None
    merges_in = parsed.get("merges") or []
    merges: list[dict[str, int]] = []
    for m in merges_in:
        if not isinstance(m, dict):
            continue
        try:
            merges.append(
                {
                    "start_row_index": int(m.get("start_row_index", 0)),
                    "end_row_index": int(m.get("end_row_index", 0)),
                    "start_column_index": int(m.get("start_column_index", 0)),
                    "end_column_index": int(m.get("end_column_index", 0)),
                }
            )
        except (TypeError, ValueError):
            continue
    sheet_id_raw = parsed.get("sheet_id")
    sheet_title_raw = parsed.get("sheet_title")
    return RichSheetsInput(
        cells=cells,
        merges=merges,
        sheet_id=int(sheet_id_raw) if isinstance(sheet_id_raw, int) else None,
        sheet_title=str(sheet_title_raw) if isinstance(sheet_title_raw, str) else None,
    )


class GoogleSheetsWriteBlock(Block):
    block_type: Literal[BlockType.GOOGLE_SHEETS_WRITE] = BlockType.GOOGLE_SHEETS_WRITE  # type: ignore

    spreadsheet_url: str
    sheet_name: str | None = None
    range: str | None = None
    credential_id: str | None = None
    write_mode: Literal["append", "update"] = "append"
    values: str = ""
    column_mapping: dict[str, str] | None = None
    create_sheet_if_missing: bool = False
    parameters: list[PARAMETER_TYPE] = []

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "credential_id",
            "range",
            "sheet_name",
            "spreadsheet_url",
            "values",
        }
    )

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        return self.parameters

    def _render_templates(self, workflow_run_context: WorkflowRunContext) -> None:
        if self.spreadsheet_url:
            self.spreadsheet_url = self.render_templatable_field(
                "spreadsheet_url", self.spreadsheet_url, workflow_run_context
            )
        if self.sheet_name:
            self.sheet_name = self.render_templatable_field("sheet_name", self.sheet_name, workflow_run_context)
        if self.range:
            self.range = self.render_templatable_field("range", self.range, workflow_run_context)
        if self.credential_id:
            self.credential_id = self.render_templatable_field(
                "credential_id", self.credential_id, workflow_run_context
            )
        if self.values:
            self.values = self._render_values_or_raise(workflow_run_context)

    def _render_values_or_raise(self, workflow_run_context: WorkflowRunContext) -> str:
        """A reference no upstream block answered would otherwise render as "" and be appended as a
        blank cell the Sheets API reports as a successful write. `| default(...)` passes an empty."""
        try:
            return self.render_templatable_field(
                "values",
                self.values,
                workflow_run_context,
                env=jinja_json_finalize_required_binding_env,
                skip_missing_variable_preflight=True,
            )
        except FailedToFormatJinjaStyleParameter as exc:
            if not isinstance(exc.__cause__, UndefinedError):
                raise
            raise ValueError(
                f"block `{self.label}` field `values` references a value no upstream block produced: "
                f"{exc.__cause__}. Return that key from the producing block, or write an explicit "
                "default (e.g. {{ block_label.field | default('') }}) if an empty cell is intended."
            ) from exc

    def _coerce_values(self, raw: Any, *, column_offset: int = 0) -> list[list[Any]]:
        """Rows for the write payload, addressed relative to the column the write starts at."""
        if isinstance(raw, dict):
            if isinstance(raw.get("values"), list) and isinstance(raw.get("rows"), list):
                LOG.warning("Google Sheets write payload has both 'values' and 'rows'; using 'values'")
            for key in ("values", "rows"):
                inner = raw.get(key)
                if isinstance(inner, list):
                    raw = inner
                    break
            else:
                # A bare object (no values/rows wrapper) is a single record; write it as one row.
                raw = [raw]
        if not isinstance(raw, list):
            raise ValueError("Google Sheets write expects a JSON array of rows")
        if not raw:
            return []
        if all(isinstance(row, list) for row in raw):
            rows = cast(list[list[Any]], raw)
            return rows
        if all(isinstance(row, dict) for row in raw):
            if not self.column_mapping:
                raise ValueError("column_mapping is required when writing a list of objects to Google Sheets")
            indexed: list[tuple[int, str]] = []
            seen_columns: set[int] = set()
            for field_key, target in self.column_mapping.items():
                target_str = str(target).strip().upper()
                # str.isalpha accepts non-ASCII letters; restrict to A-Z so we never silently
                # map "Ω" or "Α" to a column index.
                if not target_str or not re.fullmatch(r"[A-Z]+", target_str):
                    raise ValueError(f"column_mapping target must be a column letter (A, B, ... ZZ), got: {target!r}")
                col_index = column_letters_to_index(target_str)
                if col_index > MAX_COLUMN_INDEX:
                    raise ValueError(f"column_mapping target {target!r} exceeds the Google Sheets column limit (ZZZ)")
                if col_index in seen_columns:
                    raise ValueError(f"column_mapping has duplicate destination column: {target!r}")
                pos = col_index - column_offset
                if pos < 0:
                    raise ValueError(
                        f"column_mapping target {target!r} falls before the range start column; "
                        f"widen the range or remap this field"
                    )
                seen_columns.add(col_index)
                indexed.append((pos, field_key))
            available_keys: set[str] = set()
            for row in raw:
                available_keys.update(row.keys())
            if available_keys.isdisjoint(self.column_mapping.keys()):
                raise ValueError(
                    "column_mapping does not match the data: it maps fields "
                    f"{sorted(self.column_mapping.keys())}, but the data rows only contain "
                    f"{sorted(available_keys)}. Field names are case-sensitive; check that each "
                    "column_mapping key exactly matches a key in your data."
                )
            width = max(pos for pos, _ in indexed) + 1
            coerced: list[list[Any]] = []
            for row in raw:
                padded: list[Any] = [None] * width
                for pos, field_key in indexed:
                    padded[pos] = row.get(field_key)
                coerced.append(padded)
            return coerced
        raise ValueError("Google Sheets write expects rows to be all lists or all objects")

    def _parse_values_or_raise(self) -> Any:
        """Parse `self.values` JSON text into raw Python data.

        Returns raw parsed JSON so the caller can rich-detect or coerce as needed.
        Callers performing a flat write must pass the result through `_coerce_values`.
        """
        if not self.values:
            return []
        snippet = self.values[:200]
        try:
            return json.loads(self.values)
        except (ValueError, json.JSONDecodeError) as e:
            stripped = self.values.lstrip()
            hint = ""
            if stripped.startswith("{'") or stripped.startswith("[{'") or stripped.startswith("['"):
                hint = (
                    " Looks like a Python dict/list repr - wrap your template with | tojson "
                    "(e.g. {{ block_1.output | tojson }})."
                )
            raise ValueError(f"{str(e)}.{hint} Rendered values: {snippet!r}") from e

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: Any,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        try:
            self._render_templates(workflow_run_context)
        except Exception as e:
            return await self._template_format_failure_result(
                e,
                f"Failed to format jinja template: {str(e)}",
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )

        if not self.credential_id:
            return await self.build_block_result(
                success=False,
                failure_reason="Google credential_id is required",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        try:
            spreadsheet_id = extract_spreadsheet_id(self.spreadsheet_url)
        except ValueError:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Could not resolve spreadsheet id from: {self.spreadsheet_url}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        a1 = build_a1(self.sheet_name, self.range)
        if not a1:
            return await self.build_block_result(
                success=False,
                failure_reason="Either sheet_name or range must be provided",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        try:
            parsed_values: Any = self._parse_values_or_raise()
        except ValueError as e:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Invalid values payload: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        effective_org_id = organization_id or workflow_run_context.organization_id
        if not effective_org_id:
            return await self.build_block_result(
                success=False,
                failure_reason="organization_id is required to load Google Sheets credentials",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        access = await _google_sheets_access(effective_org_id, self.credential_id)
        if not access.access_token:
            return await self.build_block_result(
                success=False,
                failure_reason=access.failure_reason,
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        access_token = access.access_token

        created_sheet_id: int | None = None
        target_sheet_title = self.sheet_name or extract_a1_sheet_prefix(a1)
        if self.create_sheet_if_missing and target_sheet_title:
            try:
                existing_sheet_id = await self._resolve_sheet_id(
                    spreadsheet_id=spreadsheet_id,
                    access_token=access_token,
                    sheet_title=target_sheet_title,
                )
            except GoogleSheetsAPIError as e:
                failure_reason = _failure_reason_from_sheets_error("lookup", e)
                error_data = {"status_code": e.status, "code": e.code, "error": e.message}
                await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
                return await self.build_block_result(
                    success=False,
                    failure_reason=failure_reason,
                    output_parameter_value=error_data,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            if existing_sheet_id is None:
                try:
                    created_sheet_id = await app.AGENT_FUNCTION.ensure_sheet_tab(
                        access_token=access_token,
                        spreadsheet_id=spreadsheet_id,
                        title=target_sheet_title,
                    )
                except Exception as e:
                    return await self.build_block_result(
                        success=False,
                        failure_reason=f"Failed to create sheet '{target_sheet_title}': {str(e)}",
                        output_parameter_value=None,
                        status=BlockStatus.failed,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                    )
            else:
                created_sheet_id = existing_sheet_id

        rich = _try_rich_sheets_input(parsed_values)
        if rich is not None:
            return await self._execute_rich(
                spreadsheet_id=spreadsheet_id,
                a1=a1,
                access_token=access_token,
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                rich=rich,
                known_sheet_id=created_sheet_id,
            )

        try:
            rows = self._coerce_values(parsed_values, column_offset=leading_column_offset(a1))
        except ValueError as e:
            snippet = self.values[:200] if self.values else ""
            return await self.build_block_result(
                success=False,
                failure_reason=f"Invalid values payload: {str(e)} Rendered values: {snippet!r}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        range_sent = a1
        start_letters = destination_start_column(self.range)
        destination_snapshot: dict[str, Any] | None = None
        anchor_source: str = "not_applicable"
        if self.write_mode == "append" and start_letters:
            destination_snapshot = await self._fetch_destination_snapshot(
                spreadsheet_id=spreadsheet_id,
                access_token=access_token,
                a1=a1,
                column=start_letters,
                width=max((len(row) for row in rows), default=1),
            )
            anchored = (
                self._append_anchor(destination_snapshot, a1=a1, column=start_letters)
                if destination_snapshot is not None
                else None
            )
            if anchored is None:
                anchor_source = "snapshot_unavailable"
            else:
                range_sent = anchored
                anchor_source = "resolved"

        try:
            if self.write_mode == "append":
                payload = await app.AGENT_FUNCTION.google_sheets_values_append(
                    access_token=access_token,
                    spreadsheet_id=spreadsheet_id,
                    range_=range_sent,
                    values=rows,
                    insert_data_option="OVERWRITE" if anchor_source == "resolved" else "INSERT_ROWS",
                )
            else:
                payload = await app.AGENT_FUNCTION.google_sheets_values_update(
                    access_token=access_token,
                    spreadsheet_id=spreadsheet_id,
                    range_=range_sent,
                    values=rows,
                )
        except GoogleSheetsAPIError as e:
            failure_reason = _failure_reason_from_sheets_error("write", e)
            error_data = {"status_code": e.status, "code": e.code, "error": e.message}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=failure_reason,
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except Exception as e:
            error_data = {"error": str(e), "error_type": "unknown"}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=f"Google Sheets write failed: {str(e)}",
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        if payload is None:
            error_data = {"error": "Google Sheets write returned no payload"}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason="Google Sheets runtime is not available in this build",
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        updates = payload.get("updates") or payload
        updated_range = updates.get("updatedRange")
        output_data: dict[str, Any] = {
            "spreadsheet_id": spreadsheet_id,
            "write_mode": self.write_mode,
            "requested_destination": a1,
            "range_sent": range_sent,
            "updated_range": updated_range,
            "updated_range_source": "values_api_updated_range",
            "destination_anchor_source": anchor_source,
            "rows_written": updates.get("updatedRows", len(rows)),
            "response": payload,
        }
        _maybe_dump_sheets_write(
            {
                "workflow_run_id": workflow_run_id,
                "workflow_run_block_id": workflow_run_block_id,
                "credential_id": self.credential_id,
                "spreadsheet_url": self.spreadsheet_url,
                "sheet_name": self.sheet_name,
                "range": self.range,
                "write_mode": self.write_mode,
                "column_mapping": self.column_mapping,
                "values": self.values,
                "requested_destination": a1,
                "range_sent": range_sent,
                "destination_anchor_source": anchor_source,
                "rows": rows,
                "destination_snapshot": _occupancy_only_snapshot(destination_snapshot),
                "response": payload,
            }
        )
        mismatch = _destination_mismatch(
            requested_a1=a1,
            start_letters=start_letters,
            sheet_name=self.sheet_name,
            rows=rows,
            updated_range=updated_range,
            write_mode=self.write_mode,
        )
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output_data)
        if mismatch:
            return await self.build_block_result(
                success=False,
                failure_reason=mismatch,
                output_parameter_value=output_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=output_data,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )

    async def _fetch_destination_snapshot(
        self,
        *,
        spreadsheet_id: str,
        access_token: str,
        a1: str,
        column: str,
        width: int,
    ) -> dict[str, Any] | None:
        # Whole columns, not the configured range, which would truncate the rows we can see. Every
        # column this write fills, not just the pinned one: a mapped row can leave the start column
        # empty on every run, and reading it alone would report row 1 forever.
        end_column = column_index_to_letter(column_letters_to_index(column) + max(width, 1) - 1)
        column_range = build_a1(self.sheet_name or extract_a1_sheet_prefix(a1), f"{column}:{end_column}") or a1
        try:
            return await app.AGENT_FUNCTION.google_sheets_values_get(
                access_token=access_token,
                spreadsheet_id=spreadsheet_id,
                ranges=column_range,
                fields=(
                    "sheets(properties(title,gridProperties(rowCount)),"
                    "data(startRow,rowData(values(formattedValue,userEnteredValue))))"
                ),
            )
        except Exception as e:
            LOG.warning(
                "Failed to read the destination column; sending the configured range",
                spreadsheet_id=spreadsheet_id,
                range=a1,
                error=str(e),
            )
            return None

    def _append_anchor(self, snapshot: dict[str, Any], *, a1: str, column: str) -> str | None:
        """The pinned column's first free cell, so an append starts where the block asked rather than
        wherever the API's own table search lands. None when the snapshot cannot answer that."""
        sheet_title = self.sheet_name or extract_a1_sheet_prefix(a1)
        sheet_block = _select_sheet_block(snapshot.get("sheets") or [], sheet_title)
        # A missing data key means the read could not answer; only a present-but-empty one means the
        # column is free. Conflating them anchors at row 1 of a populated sheet.
        if sheet_block is None or "data" not in sheet_block:
            return None
        data_blocks = sheet_block.get("data") or []
        first_data = data_blocks[0] if data_blocks else {}
        row_data = first_data.get("rowData") or []
        last_filled = 0
        for index, row in enumerate(row_data, start=1):
            # A formula rendering "" carries no formattedValue, so an anchor keyed on it alone would
            # overwrite the formula.
            if any(cell.get("formattedValue") or cell.get("userEnteredValue") for cell in (row.get("values") or [])):
                last_filled = index
        next_row = int(first_data.get("startRow", 0)) + last_filled + 1
        # Past the last grid row an anchored A1 is rejected outright, while the unanchored range lets
        # Sheets grow the sheet the way it did before anchoring existed.
        row_count = ((sheet_block.get("properties") or {}).get("gridProperties") or {}).get("rowCount")
        if isinstance(row_count, int) and next_row > row_count:
            return None
        return build_a1(sheet_title, f"{column}{next_row}")

    async def _execute_rich(
        self,
        *,
        spreadsheet_id: str,
        a1: str,
        access_token: str,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        rich: RichSheetsInput,
        known_sheet_id: int | None = None,
    ) -> BlockResult:
        sheet_id: int | None = known_sheet_id
        try:
            if sheet_id is None and self.sheet_name:
                sheet_id = await self._resolve_sheet_id(
                    spreadsheet_id=spreadsheet_id,
                    access_token=access_token,
                    sheet_title=self.sheet_name,
                )
            if sheet_id is None:
                # Honor an explicit sheet prefix in the configured A1 (e.g. "'Target'!B2:C3")
                # before falling back to the rich payload's sheet metadata.
                a1_prefix = extract_a1_sheet_prefix(a1)
                if a1_prefix:
                    sheet_id = await self._resolve_sheet_id(
                        spreadsheet_id=spreadsheet_id,
                        access_token=access_token,
                        sheet_title=a1_prefix,
                    )
            if sheet_id is None and rich.sheet_title:
                # rich.sheet_id is local to the source spreadsheet; resolve by title against the destination.
                sheet_id = await self._resolve_sheet_id(
                    spreadsheet_id=spreadsheet_id,
                    access_token=access_token,
                    sheet_title=rich.sheet_title,
                )
        except GoogleSheetsAPIError as e:
            failure_reason = _failure_reason_from_sheets_error("lookup", e)
            error_data = {"status_code": e.status, "code": e.code, "error": e.message}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=failure_reason,
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        if sheet_id is None:
            return await self.build_block_result(
                success=False,
                failure_reason=(
                    "Could not resolve sheet_id for batchUpdate; "
                    "set sheet_name on the Write block or include sheet_title in the input"
                ),
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        fields_mask = "userEnteredValue,userEnteredFormat,note,hyperlink"
        requests: list[dict[str, Any]] = []
        required_width = 0

        range_sent: str | None = None
        if self.write_mode == "append":
            append_col_offset = leading_column_offset(a1)
            padded_cells: list[list[dict[str, Any]]]
            if append_col_offset:
                padded_cells = [[{}] * append_col_offset + row for row in rich.cells]
            else:
                padded_cells = list(rich.cells)
            rows_payload = [{"values": row} for row in padded_cells]
            requests.append(
                {
                    "appendCells": {
                        "sheetId": sheet_id,
                        "rows": rows_payload,
                        "fields": fields_mask,
                    }
                }
            )
            merge_origin_row: int | None = None
            merge_origin_col = append_col_offset
            if padded_cells:
                required_width = max(len(row) for row in padded_cells)
        else:
            rows_payload = [{"values": row} for row in rich.cells]
            try:
                grid_range = a1_to_grid_range(a1, sheet_id)
            except ValueError as e:
                return await self.build_block_result(
                    success=False,
                    failure_reason=f"Update mode requires a fully-qualified A1 range: {e}",
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            requests.append(
                {
                    "updateCells": {
                        "range": grid_range,
                        "rows": rows_payload,
                        "fields": fields_mask,
                    }
                }
            )
            merge_origin_row = grid_range["startRowIndex"]
            merge_origin_col = grid_range["startColumnIndex"]
            required_width = grid_range["endColumnIndex"]
            range_sent = a1

        for merge in rich.merges:
            if self.write_mode == "append":
                # Append mode appends after the last table row; we'd need the response's
                # updatedRange to shift merges correctly. Skip for now.
                continue
            row_offset = merge_origin_row or 0
            end_col = merge["end_column_index"] + merge_origin_col
            requests.append(
                {
                    "mergeCells": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": merge["start_row_index"] + row_offset,
                            "endRowIndex": merge["end_row_index"] + row_offset,
                            "startColumnIndex": merge["start_column_index"] + merge_origin_col,
                            "endColumnIndex": end_col,
                        },
                        "mergeType": "MERGE_ALL",
                    }
                }
            )
            if end_col > required_width:
                required_width = end_col

        # Per-tab cap is unconditional: the write can never succeed past ZZZ regardless
        # of whether grid lookup is available, so fail fast before touching Google.
        max_columns = MAX_COLUMN_INDEX + 1
        if required_width > max_columns:
            last_letter = column_index_to_letter(MAX_COLUMN_INDEX)
            needed_letter = column_index_to_letter(required_width - 1)
            sheet_label = self.sheet_name or extract_a1_sheet_prefix(a1) or rich.sheet_title or "destination"
            failure_reason = (
                f"Sheet '{sheet_label}' cannot fit this write: needs column "
                f"{needed_letter} but the Google Sheets per-tab limit is {max_columns} "
                f"columns ({last_letter}). Narrow the data or split it across tabs."
            )
            error_data = {"status_code": 400, "code": "column_overflow", "error": failure_reason}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=failure_reason,
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # Pre-flight the destination's column_count and prepend an appendDimension
        # so a write that's wider than the current grid succeeds in the same atomic batch.
        # If the grid lookup is unavailable (no AgentFunction impl, transient error of any
        # kind including transport failures), fall through; the existing error mapper
        # will produce a friendly message if the eventual write fails.
        if required_width > 0:
            grid_props: Any = None
            try:
                grid_props = await app.AGENT_FUNCTION.google_sheets_get_grid_properties_by_id(
                    access_token=access_token,
                    spreadsheet_id=spreadsheet_id,
                    sheet_id=sheet_id,
                )
            except Exception as e:
                LOG.warning(
                    "Failed to fetch grid properties for pre-flight; falling through to write",
                    spreadsheet_id=spreadsheet_id,
                    sheet_id=sheet_id,
                    error=str(e),
                )
            # Duck-type the response: cloud returns SheetGridProperties; the OSS no-op
            # returns None. Anything else (e.g. an AsyncMock in unrelated test fixtures)
            # is treated as no-info to avoid false-positive failures.
            current_column_count = getattr(grid_props, "column_count", None)
            if isinstance(current_column_count, int) and current_column_count < required_width:
                requests.insert(
                    0,
                    build_append_dimension_request(
                        sheet_id=sheet_id,
                        dimension="COLUMNS",
                        length=required_width - current_column_count,
                    ),
                )

        try:
            payload = await app.AGENT_FUNCTION.google_sheets_batch_update(
                access_token=access_token,
                spreadsheet_id=spreadsheet_id,
                requests=requests,
            )
        except GoogleSheetsAPIError as e:
            failure_reason = _failure_reason_from_sheets_error("batchUpdate", e)
            error_data = {"status_code": e.status, "code": e.code, "error": e.message}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=failure_reason,
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except Exception as e:
            error_data = {"error": str(e), "error_type": "unknown"}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason=f"Google Sheets batchUpdate failed: {str(e)}",
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        if payload is None:
            error_data = {"error": "Google Sheets batchUpdate returned no payload"}
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
            return await self.build_block_result(
                success=False,
                failure_reason="Google Sheets runtime is not available in this build",
                output_parameter_value=error_data,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        output_data: dict[str, Any] = {
            "spreadsheet_id": spreadsheet_id,
            "write_mode": self.write_mode,
            "requested_destination": a1,
            "range_sent": range_sent,
            "updated_range": None,
            "updated_range_source": "batch_update_reports_no_range",
            "destination_anchor_source": "not_applicable",
            "rows_written": len(rich.cells),
            "response": payload,
        }
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output_data)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=output_data,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )

    async def _resolve_sheet_id(
        self,
        *,
        spreadsheet_id: str,
        access_token: str,
        sheet_title: str | None,
    ) -> int | None:
        if not sheet_title:
            return None
        return await app.AGENT_FUNCTION.google_sheets_get_sheet_id(
            access_token=access_token,
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
        )
