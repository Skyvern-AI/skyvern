import json
import re
from dataclasses import dataclass
from typing import Any, Literal, cast

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models._jinja import jinja_json_finalize_env
from skyvern.forge.sdk.workflow.models.block import Block
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE
from skyvern.schemas.google_sheets import (
    MAX_COLUMN_INDEX,
    GoogleSheetsAPIError,
    a1_to_grid_range,
    build_a1,
    column_letters_to_index,
    extract_a1_sheet_prefix,
    extract_spreadsheet_id,
    leading_column_offset,
)
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType

LOG = structlog.get_logger()


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


class GoogleSheetsReadBlock(Block):
    block_type: Literal[BlockType.GOOGLE_SHEETS_READ] = BlockType.GOOGLE_SHEETS_READ  # type: ignore

    spreadsheet_url: str
    sheet_name: str | None = None
    range: str | None = None
    credential_id: str | None = None
    has_header_row: bool = True
    parameters: list[PARAMETER_TYPE] = []

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        return self.parameters

    def _render_templates(self, workflow_run_context: WorkflowRunContext) -> None:
        if self.spreadsheet_url:
            self.spreadsheet_url = self.format_block_parameter_template_from_workflow_run_context(
                self.spreadsheet_url, workflow_run_context
            )
        if self.sheet_name:
            self.sheet_name = self.format_block_parameter_template_from_workflow_run_context(
                self.sheet_name, workflow_run_context
            )
        if self.range:
            self.range = self.format_block_parameter_template_from_workflow_run_context(
                self.range, workflow_run_context
            )
        if self.credential_id:
            self.credential_id = self.format_block_parameter_template_from_workflow_run_context(
                self.credential_id, workflow_run_context
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
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to format jinja template: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
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
        access_token = await app.AGENT_FUNCTION.get_google_sheets_credentials(
            organization_id=effective_org_id,
            credential_id=self.credential_id,
        )
        if not access_token:
            return await self.build_block_result(
                success=False,
                failure_reason="Reconnect the Google account: no valid access token",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

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
        # spreadsheets.get returns every sheet object even when ranges= is set, so
        # selecting [0] would silently grab the first tab instead of the requested one.
        target_sheet_title = self.sheet_name or extract_a1_sheet_prefix(a1)
        sheet_block: dict[str, Any] = {}
        if target_sheet_title:
            for candidate in sheets:
                candidate_title = (candidate.get("properties") or {}).get("title")
                if candidate_title == target_sheet_title:
                    sheet_block = candidate
                    break
        if not sheet_block:
            sheet_block = sheets[0] if sheets else {}
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


def _failure_reason_from_sheets_error(action: str, exc: GoogleSheetsAPIError) -> str:
    if exc.status == 403 and exc.code == "reconnect_required":
        return f"Reconnect the Google account: {exc.message}"
    if exc.status == 429:
        return f"Google Sheets rate limit on {action}: {exc.message}"
    return f"Google Sheets {action} failed (HTTP {exc.status}): {exc.message}"


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

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        return self.parameters

    def _render_templates(self, workflow_run_context: WorkflowRunContext) -> None:
        if self.spreadsheet_url:
            self.spreadsheet_url = self.format_block_parameter_template_from_workflow_run_context(
                self.spreadsheet_url, workflow_run_context
            )
        if self.sheet_name:
            self.sheet_name = self.format_block_parameter_template_from_workflow_run_context(
                self.sheet_name, workflow_run_context
            )
        if self.range:
            self.range = self.format_block_parameter_template_from_workflow_run_context(
                self.range, workflow_run_context
            )
        if self.credential_id:
            self.credential_id = self.format_block_parameter_template_from_workflow_run_context(
                self.credential_id, workflow_run_context
            )
        if self.values:
            self.values = self.format_block_parameter_template_from_workflow_run_context(
                self.values, workflow_run_context, env=jinja_json_finalize_env
            )

    def _coerce_values(self, raw: Any, *, column_offset: int = 0) -> list[list[Any]]:
        if isinstance(raw, dict):
            if isinstance(raw.get("values"), list) and isinstance(raw.get("rows"), list):
                LOG.warning("Google Sheets write payload has both 'values' and 'rows'; using 'values'")
            for key in ("values", "rows"):
                inner = raw.get(key)
                if isinstance(inner, list):
                    raw = inner
                    break
        if not isinstance(raw, list):
            raise ValueError("Google Sheets write expects a JSON array of rows")
        if not raw:
            return []
        if all(isinstance(row, list) for row in raw):
            return cast(list[list[Any]], raw)
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
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to format jinja template: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
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
        access_token = await app.AGENT_FUNCTION.get_google_sheets_credentials(
            organization_id=effective_org_id,
            credential_id=self.credential_id,
        )
        if not access_token:
            return await self.build_block_result(
                success=False,
                failure_reason="Reconnect the Google account: no valid access token",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

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
            extra = ""
            if isinstance(parsed_values, dict):
                extra = (
                    " Rendered a single object instead of a list; reference a list field "
                    "(e.g. {{ block_1.output.rows | tojson }}) or wrap the object in an array."
                )
            return await self.build_block_result(
                success=False,
                failure_reason=f"Invalid values payload: {str(e)}.{extra} Rendered values: {snippet!r}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        try:
            if self.write_mode == "append":
                payload = await app.AGENT_FUNCTION.google_sheets_values_append(
                    access_token=access_token,
                    spreadsheet_id=spreadsheet_id,
                    range_=a1,
                    values=rows,
                )
            else:
                payload = await app.AGENT_FUNCTION.google_sheets_values_update(
                    access_token=access_token,
                    spreadsheet_id=spreadsheet_id,
                    range_=a1,
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

        output_data: dict[str, Any] = {
            "spreadsheet_id": spreadsheet_id,
            "write_mode": self.write_mode,
            "rows_written": len(rows),
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

        for merge in rich.merges:
            if self.write_mode == "append":
                # Append mode appends after the last table row; we'd need the response's
                # updatedRange to shift merges correctly. Skip for now.
                continue
            row_offset = merge_origin_row or 0
            requests.append(
                {
                    "mergeCells": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": merge["start_row_index"] + row_offset,
                            "endRowIndex": merge["end_row_index"] + row_offset,
                            "startColumnIndex": merge["start_column_index"] + merge_origin_col,
                            "endColumnIndex": merge["end_column_index"] + merge_origin_col,
                        },
                        "mergeType": "MERGE_ALL",
                    }
                }
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
