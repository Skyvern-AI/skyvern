from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import aiofiles
import pdfplumber
import structlog
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from skyvern.config import settings
from skyvern.constants import GET_DOWNLOADED_FILES_TIMEOUT, SAVE_DOWNLOADED_FILES_TIMEOUT
from skyvern.exceptions import PDFParsingError
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import (
    download_file,
    get_path_for_workflow_download_directory,
    validate_local_file_path,
)
from skyvern.forge.sdk.api.llm.api_handler import LLMAPIHandler
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.experimentation.llm_prompt_config import get_llm_handler_for_prompt_type
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.utils.pdf_parser import render_pdf_pages_as_images, validate_pdf_file
from skyvern.forge.sdk.utils.tesseract_languages import DEFAULT_FLAT_FILL_OCR_LANGUAGES
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.loop_download_filter import filter_downloaded_files_for_current_iteration
from skyvern.forge.sdk.workflow.models._jinja import render_templates_in_json_value
from skyvern.forge.sdk.workflow.models.block import (
    Block,
    capture_block_download_baseline,
    extract_file_url_from_block_output,
    sanitize_filename,
)
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType

LOG = structlog.get_logger()

FLAT_PDF_OCR_MISSING_MESSAGE = (
    "This PDF has no fillable AcroForm fields; filling flat PDFs requires the tesseract OCR binary, "
    "which is not installed on this host."
)
FLAT_FILL_RENDER_RESOLUTION = 150
FLAT_FILL_FONT_SIZE = 11
FLAT_FILL_MIN_FONT_SIZE = 7.0
FLAT_FILL_FONT_SIZE_GAP = 3.0
FLAT_FILL_TEXT_END_GAP_PT = 3.0
# Helvetica AFM: Ascender 718, Descender -207 (per 1000 em). The overlay value's
# ink extends this far above/below its baseline; the OCR anchor box is the printed
# label, not the value, so collision bands are modeled from the value's own ink.
FLAT_FILL_GLYPH_ASCENT_RATIO = 0.718
FLAT_FILL_GLYPH_DESCENT_RATIO = 0.207
FLAT_FILL_LINE_EPS_PX = 2.0
# Adobe AFM glyph widths (per 1000 units) for Helvetica, chars 32..126; fallback 600.
# fmt: off
_HELVETICA_WIDTHS_1000 = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)
# fmt: on
FLAT_FILL_OCR_MIN_CONFIDENCE = 30.0
FLAT_FILL_FONT_RESOURCE = "/SkyvernHelv"
# Tesseract merges visually aligned table cells into one line; split anchors on column-sized gaps.
FLAT_FILL_LINE_SPLIT_GAP_PX = 30
FLAT_FILL_OCR_TIMEOUT_SECONDS = 60
# Outer budget across all pages so a flat PDF can't pin a worker for the full per-page timeout x max pages.
FLAT_FILL_OCR_TOTAL_TIMEOUT_SECONDS = 300
FLAT_FILL_MAX_PAGES = 25
FLAT_FILL_OCR_LANGUAGES = os.getenv("FLAT_FILL_OCR_LANGUAGES", DEFAULT_FLAT_FILL_OCR_LANGUAGES).strip()


def _flat_text_width_pt(value: str, font_size: float) -> float:
    single_line = " ".join(value.split())
    total = 0
    for ch in single_line:
        code = ord(ch)
        total += _HELVETICA_WIDTHS_1000[code - 32] if 32 <= code <= 126 else 600
    return total * font_size / 1000.0


def _flat_value_band_px(position: str, anchor: FlatPdfAnchor, scale_y: float, font_size: float) -> tuple[float, float]:
    if position == "below":
        baseline_px = anchor.bottom + (FLAT_FILL_FONT_SIZE_GAP + font_size) / scale_y
    else:
        baseline_px = anchor.bottom + 1 / scale_y
    ascent_px = FLAT_FILL_GLYPH_ASCENT_RATIO * font_size / scale_y
    descent_px = FLAT_FILL_GLYPH_DESCENT_RATIO * font_size / scale_y
    return baseline_px - ascent_px, baseline_px + descent_px


@dataclass(frozen=True)
class PdfFieldInventory:
    name: str
    field_type: str
    current_value: Any
    allowed_values: list[str]
    # Printed label nearest the field's widget on the page; AcroForm field names like "f1_03[0]"
    # are opaque, so this is what lets the LLM map a value to the right field.
    context_label: str = ""


@dataclass(frozen=True)
class FlatPdfAnchor:
    anchor_id: int
    page_index: int
    text: str
    x0: int
    x1: int
    top: int
    bottom: int
    page_width_px: int
    page_height_px: int


@dataclass(frozen=True)
class FlatPlacement:
    anchor: FlatPdfAnchor
    value: str
    position: str


@dataclass(frozen=True)
class ResolvedFlatText:
    value: str
    x: float
    y: float
    font_size: float
    overflowed: bool = False


class PdfFillBlock(Block):
    block_type: Literal[BlockType.PDF_FILL] = BlockType.PDF_FILL  # type: ignore

    file_url: str
    prompt: str
    payload: dict[str, Any] | list | str | None = None
    llm_key: str | None = None
    parameters: list[PARAMETER_TYPE] = []

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        parameters = list(self.parameters)
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        if self.file_url and workflow_run_context.has_parameter(self.file_url):
            parameter = workflow_run_context.get_parameter(self.file_url)
            if parameter.key not in {existing.key for existing in parameters}:
                parameters.append(parameter)
        return parameters

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        template_kwargs = {"force_include_secrets": True}

        def _render_string(value: str) -> str:
            return self.format_block_parameter_template_from_workflow_run_context(
                value, workflow_run_context, **template_kwargs
            )

        if (
            self.file_url
            and workflow_run_context.has_parameter(self.file_url)
            and workflow_run_context.has_value(self.file_url)
        ):
            file_url_parameter_value = workflow_run_context.get_value(self.file_url)
            if file_url_parameter_value:
                self.file_url = extract_file_url_from_block_output(file_url_parameter_value) or file_url_parameter_value

        self.file_url = _render_string(self.file_url)
        self.prompt = _render_string(self.prompt)
        if self.llm_key:
            self.llm_key = _render_string(self.llm_key)
        self.payload = cast(
            dict[str, Any] | list | str | None, render_templates_in_json_value(self.payload, _render_string)
        )

        extracted_url = extract_file_url_from_block_output(self.file_url)
        if extracted_url:
            self.file_url = extracted_url

        self._apply_workflow_system_prompt(workflow_run_context)

    async def _resolve_default_llm_handler(self, workflow_run_id: str, organization_id: str | None) -> LLMAPIHandler:
        prompt_config_handler = await get_llm_handler_for_prompt_type("text-prompt", workflow_run_id, organization_id)
        if prompt_config_handler:
            return prompt_config_handler

        secondary_handler = app.SECONDARY_LLM_API_HANDLER
        if secondary_handler:
            return secondary_handler

        LOG.warning(
            "Secondary LLM handler not configured; falling back to primary handler for PdfFillBlock",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        return app.LLM_API_HANDLER

    async def _record_failure(
        self,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        failure_reason: str,
        output_parameter_value: dict[str, Any] | None = None,
    ) -> BlockResult:
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output_parameter_value)
        return await self.build_block_result(
            success=False,
            failure_reason=failure_reason,
            output_parameter_value=output_parameter_value,
            status=BlockStatus.failed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )

    async def _resolve_source_pdf(self, workflow_run_id: str, organization_id: str | None) -> tuple[str, bool]:
        # Returns (path, is_temp): is_temp marks a download_file temp the caller must clean up;
        # a user-supplied local path is never deleted.
        if settings.ENV == "local" and os.path.exists(self.file_url):
            return self.file_url, False
        if self.file_url.startswith("/"):
            context = skyvern_context.current()
            run_id = context.run_id if context and context.run_id else workflow_run_id
            resolved = validate_local_file_path(self.file_url, run_id)
            if not os.path.isfile(resolved):
                raise FileNotFoundError(f"Local file not found: {self.file_url}")
            return resolved, False
        return await download_file(self.file_url, organization_id=organization_id), True

    @staticmethod
    def _dereference(value: Any) -> Any:
        if hasattr(value, "get_object"):
            return value.get_object()
        return value

    @classmethod
    def _field_allowed_values(cls, field: Any) -> list[str]:
        values: list[str] = []
        states = field.get("/_States_")
        if isinstance(states, list):
            values.extend(str(state) for state in states)

        kids = field.get("/Kids") or []
        for kid_ref in kids:
            kid = cls._dereference(kid_ref)
            appearances = cls._dereference(kid.get("/AP", {}))
            normal_appearances = cls._dereference(appearances.get("/N", {}))
            if isinstance(normal_appearances, dict):
                values.extend(str(key) for key in normal_appearances.keys())

        options = field.get("/Opt")
        if options:
            for option in options:
                option_value = cls._dereference(option)
                if isinstance(option_value, (list, tuple)) and option_value:
                    option_value = cls._dereference(option_value[0])
                values.append(str(option_value))

        deduped: list[str] = []
        for value in values:
            if value not in deduped:
                deduped.append(value)
        return deduped

    @staticmethod
    def _field_type(field: Any) -> str:
        pdf_field_type = str(field.get("/FT", ""))
        if pdf_field_type == "/Tx":
            return "text"
        if pdf_field_type == "/Ch":
            return "choice"
        if pdf_field_type == "/Btn":
            flags = int(field.get("/Ff", 0) or 0)
            if flags & (1 << 15):
                return "radio"
            return "checkbox"
        return "text"

    def _extract_field_inventory(self, reader: PdfReader, pdf_path: str | None = None) -> dict[str, PdfFieldInventory]:
        fields = reader.get_fields() or {}
        if not fields:
            return {}
        labels = self._extract_field_labels(pdf_path) if pdf_path else {}
        inventory: dict[str, PdfFieldInventory] = {}
        for name, field in fields.items():
            inventory[name] = PdfFieldInventory(
                name=name,
                field_type=self._field_type(field),
                current_value=field.get("/V"),
                allowed_values=self._field_allowed_values(field),
                context_label=labels.get(name.split(".")[-1], ""),
            )
        return inventory

    @staticmethod
    def _extract_field_labels(pdf_path: str) -> dict[str, str]:
        """Best-effort map of leaf field name (e.g. 'f1_03[0]') -> nearest printed label on the page.

        AcroForm field names carry no meaning, so the LLM mapping mislocates values without this
        spatial context. Failures here are non-fatal: the fill falls back to name-only mapping.
        """
        try:
            reader = PdfReader(pdf_path)
            leaf_rects: dict[str, tuple[int, float, float, float, float, bool]] = {}
            for page_index, page in enumerate(reader.pages):
                page_height = float(page.mediabox.height)
                for annotation in page.get("/Annots") or []:
                    widget = annotation.get_object()
                    if widget.get("/Subtype") != "/Widget" or not widget.get("/Rect"):
                        continue
                    leaf = widget.get("/T")
                    field_type = widget.get("/FT")
                    if widget.get("/Parent"):
                        parent = widget["/Parent"].get_object()
                        leaf = leaf or parent.get("/T")
                        field_type = field_type or parent.get("/FT")
                    if not leaf or str(leaf) in leaf_rects:
                        continue
                    x0, y0, x1, y1 = (float(v) for v in widget["/Rect"])
                    is_button = str(field_type) == "/Btn"
                    leaf_rects[str(leaf)] = (page_index, x0, page_height - y1, x1, page_height - y0, is_button)

            if not leaf_rects:
                return {}

            with pdfplumber.open(pdf_path) as pdf:
                words_by_page = {
                    pi: [w for w in pg.extract_words(extra_attrs=["upright"]) if w.get("upright", True)]
                    for pi, pg in enumerate(pdf.pages)
                }

            labels: dict[str, str] = {}
            for leaf, (page_index, fx0, ftop, fx1, fbot, is_button) in leaf_rects.items():
                labels[leaf] = PdfFillBlock._nearest_label(
                    words_by_page.get(page_index, []), fx0, ftop, fx1, fbot, is_button
                )
            return labels
        except Exception:
            LOG.warning("PdfFillBlock failed to derive field labels; falling back to name-only mapping", exc_info=True)
            return {}

    @staticmethod
    def _nearest_label(
        words: list[dict[str, Any]], fx0: float, ftop: float, fx1: float, fbot: float, is_button: bool = False
    ) -> str:
        field_height = fbot - ftop
        per_side: dict[str, list[tuple[float, dict[str, Any]]]] = {"above": [], "left": [], "right": []}
        for w in words:
            wx0, wtop, wx1, wbot = w["x0"], w["top"], w["x1"], w["bottom"]
            vertical_overlap = min(wbot, fbot) - max(wtop, ftop) > 0
            horizontal_overlap = min(wx1, fx1) - max(wx0, fx0) > -120
            if vertical_overlap and wx1 <= fx0 + 2:
                per_side["left"].append((fx0 - wx1, w))
            if vertical_overlap and wx0 >= fx1 - 2:
                per_side["right"].append((wx0 - fx1, w))
            if wbot <= ftop + 2 and horizontal_overlap:
                per_side["above"].append((ftop - wbot, w))

        # Checkbox/radio labels sit to the right of the box (the option text); text-field labels
        # sit above or to the left of the entry box.
        side_order = ("right", "above", "left") if is_button else ("above", "left", "right")
        for side in side_order:
            candidates = sorted(per_side[side], key=lambda c: c[0])
            # distances in PDF user-space points (~1/72"), calibrated on IRS AcroForms
            threshold = 220.0 if side in ("left", "right") else 26.0
            near = [c for c in candidates if c[0] <= threshold]
            if not near:
                continue
            anchor = near[0][1]
            anchor_cy = (anchor["top"] + anchor["bottom"]) / 2
            band = [c[1] for c in near if abs((c[1]["top"] + c[1]["bottom"]) / 2 - anchor_cy) < field_height * 1.5 + 4]
            text = " ".join(w["text"] for w in sorted(band, key=lambda w: w["x0"])).strip()
            if text:
                return text[:70]
        return ""

    async def _map_fields_with_llm(
        self,
        inventory: dict[str, PdfFieldInventory],
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
    ) -> dict[str, Any]:
        default_llm_handler = await self._resolve_default_llm_handler(workflow_run_id, organization_id)
        llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
            self.override_llm_key_for_organization(organization_id) or self.llm_key,
            default=default_llm_handler,
        )
        prompt = prompt_engine.load_prompt(
            "pdf-fill-field-mapping",
            field_inventory=[field.__dict__ for field in inventory.values()],
            user_prompt=self.prompt,
            payload_json=json.dumps(self.payload, indent=2, default=str),
        )
        response = await llm_api_handler(
            prompt=prompt,
            prompt_name="pdf-fill-field-mapping",
            system_prompt=self.workflow_system_prompt,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )
        return self._parse_llm_json_response(response, expected_key="fields")

    @staticmethod
    def _parse_llm_json_response(response: Any, expected_key: str) -> dict[str, Any]:
        parsed: Any
        if isinstance(response, str):
            parsed = json.loads(response)
        elif expected_key in response:
            return cast(dict[str, Any], response)
        elif "llm_response" in response and isinstance(response["llm_response"], str):
            parsed = json.loads(response["llm_response"])
        else:
            parsed = response
        if not isinstance(parsed, dict):
            LOG.warning(
                "PdfFillBlock LLM response was not a JSON object",
                expected_key=expected_key,
                response_type=type(parsed).__name__,
            )
            return {}
        if expected_key not in parsed:
            LOG.warning(
                "PdfFillBlock LLM response missing expected key",
                expected_key=expected_key,
                response_keys=list(parsed.keys()),
            )
        return parsed

    @staticmethod
    def _checked_state(allowed_values: list[str]) -> str | None:
        for value in allowed_values:
            if value != "/Off":
                return value
        return None

    @staticmethod
    def _coerce_allowed_value(value: Any, allowed_values: list[str]) -> str | None:
        value_str = str(value)
        if value_str in allowed_values:
            return value_str
        prefixed = f"/{value_str}"
        if prefixed in allowed_values:
            return prefixed
        return None

    def _sanitize_mapping(
        self,
        raw_response: dict[str, Any],
        inventory: dict[str, PdfFieldInventory],
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        raw_fields = raw_response.get("fields")
        if not isinstance(raw_fields, dict):
            return {}, [{"field_name": None, "reason": "LLM response did not include a fields object"}]

        sanitized: dict[str, str] = {}
        skipped: list[dict[str, Any]] = []
        for field_name, value in raw_fields.items():
            if field_name not in inventory:
                skipped.append({"field_name": field_name, "reason": "Field is not present in the PDF", "value": value})
                continue

            field = inventory[field_name]
            if field.field_type == "checkbox":
                checked_state = self._checked_state(field.allowed_values)
                value_str = str(value).strip().lower()
                wants_checked = value is True or value_str in {"true", "yes", "y", "1", "checked"}
                wants_unchecked = value is False or value_str in {"false", "no", "n", "0", "off", "/off", "unchecked"}
                if wants_checked and not checked_state:
                    skipped.append(
                        {"field_name": field_name, "reason": "Checkbox has no known checked state", "value": value}
                    )
                    continue
                if wants_checked:
                    sanitized[field_name] = cast(str, checked_state)
                    continue
                if wants_unchecked:
                    sanitized[field_name] = "/Off"
                    continue
                allowed = self._coerce_allowed_value(value, field.allowed_values)
                if allowed:
                    sanitized[field_name] = allowed
                    continue
                skipped.append({"field_name": field_name, "reason": "Invalid checkbox value", "value": value})
                continue

            if field.field_type in {"radio", "choice"}:
                allowed = (
                    self._coerce_allowed_value(value, field.allowed_values) if field.allowed_values else str(value)
                )
                if allowed:
                    sanitized[field_name] = allowed
                else:
                    skipped.append(
                        {
                            "field_name": field_name,
                            "reason": "Value is not one of the allowed options",
                            "value": value,
                            "allowed_values": field.allowed_values,
                        }
                    )
                continue

            sanitized[field_name] = "" if value is None else str(value)

        return sanitized, skipped

    def _output_path(self, workflow_run_id: str, workflow_run_block_id: str) -> Path:
        output_dir = get_path_for_workflow_download_directory(workflow_run_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        # The run-block id keeps loop iterations from overwriting each other's filled PDF.
        suffix = f"_{workflow_run_block_id}" if workflow_run_block_id else ""
        filename = f"{sanitize_filename(self.label, default='pdf_fill')}{suffix}_filled.pdf"
        return output_dir / filename

    async def _fill_with_skyvern(self, reader: PdfReader, fields: dict[str, str], output_path: Path) -> bytes:
        writer = PdfWriter()
        writer.clone_document_from_reader(reader)
        writer.set_need_appearances_writer(True)
        # page=None fills all pages (writer.pages is a _VirtualList and fails pypdf's isinstance(page, list) check).
        # auto_regenerate=None keeps NeedAppearances as set above; a bool would overwrite it.
        writer.update_page_form_field_values(None, fields, auto_regenerate=None)
        buffer = io.BytesIO()
        writer.write(buffer)
        pdf_bytes = buffer.getvalue()
        async with aiofiles.open(output_path, "wb") as f:
            await f.write(pdf_bytes)
        return pdf_bytes

    @staticmethod
    def _tesseract_available() -> bool:
        return shutil.which("tesseract") is not None

    async def _extract_flat_anchors(self, pdf_path: str) -> list[FlatPdfAnchor]:
        page_images = render_pdf_pages_as_images(
            pdf_path,
            file_identifier=self.file_url,
            max_pages=FLAT_FILL_MAX_PAGES,
            resolution=FLAT_FILL_RENDER_RESOLUTION,
        )
        anchors: list[FlatPdfAnchor] = []
        deadline = time.monotonic() + FLAT_FILL_OCR_TOTAL_TIMEOUT_SECONDS
        with tempfile.TemporaryDirectory() as tmp_dir:
            for page_index, image_bytes in enumerate(page_images):
                page_budget = min(FLAT_FILL_OCR_TIMEOUT_SECONDS, deadline - time.monotonic())
                if page_budget <= 0:
                    raise ValueError(
                        f"OCR exceeded the total budget of {FLAT_FILL_OCR_TOTAL_TIMEOUT_SECONDS}s "
                        f"before finishing all pages (stopped at page {page_index})"
                    )
                image_path = os.path.join(tmp_dir, f"page_{page_index}.png")
                async with aiofiles.open(image_path, "wb") as f:
                    await f.write(image_bytes)
                process = await asyncio.create_subprocess_exec(
                    *self._tesseract_command(image_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    async with asyncio.timeout(page_budget):
                        stdout, _ = await process.communicate()
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    raise ValueError(f"tesseract OCR timed out on page {page_index}")
                if process.returncode != 0:
                    raise ValueError(f"tesseract OCR failed on page {page_index} (exit code {process.returncode})")
                anchors.extend(
                    self._parse_tesseract_tsv(stdout.decode("utf-8", errors="replace"), page_index, len(anchors))
                )
        return anchors

    @staticmethod
    def _tesseract_command(image_path: str) -> list[str]:
        command = ["tesseract", image_path, "stdout"]
        if FLAT_FILL_OCR_LANGUAGES:
            command.extend(["-l", FLAT_FILL_OCR_LANGUAGES])
        command.append("tsv")
        return command

    @staticmethod
    def _tsv_int(row: dict[str, str], key: str) -> int | None:
        try:
            return int(row.get(key) or 0)
        except ValueError:
            return None

    @classmethod
    def _parse_tesseract_tsv(cls, tsv_text: str, page_index: int, id_offset: int) -> list[FlatPdfAnchor]:
        rows = csv.DictReader(io.StringIO(tsv_text), delimiter="\t", quoting=csv.QUOTE_NONE)
        page_width_px = 0
        page_height_px = 0
        lines: dict[tuple[str, str, str], list[dict[str, int | str]]] = {}
        for row in rows:
            level = cls._tsv_int(row, "level")
            if level == 1:
                page_width_px = cls._tsv_int(row, "width") or 0
                page_height_px = cls._tsv_int(row, "height") or 0
            if level != 5:
                continue
            text = (row.get("text") or "").strip()
            if not text:
                continue
            try:
                confidence = float(row.get("conf") or -1)
            except ValueError:
                confidence = -1.0
            if confidence < FLAT_FILL_OCR_MIN_CONFIDENCE:
                continue
            left = cls._tsv_int(row, "left")
            top = cls._tsv_int(row, "top")
            width = cls._tsv_int(row, "width")
            height = cls._tsv_int(row, "height")
            if left is None or top is None or width is None or height is None:
                continue
            line_key = (row.get("block_num") or "0", row.get("par_num") or "0", row.get("line_num") or "0")
            lines.setdefault(line_key, []).append(
                {"text": text, "left": left, "top": top, "right": left + width, "bottom": top + height}
            )

        anchors: list[FlatPdfAnchor] = []
        for words in lines.values():
            ordered = sorted(words, key=lambda word: cast(int, word["left"]))
            segments: list[list[dict[str, int | str]]] = [[ordered[0]]]
            for word in ordered[1:]:
                previous_right = cast(int, segments[-1][-1]["right"])
                if cast(int, word["left"]) - previous_right > FLAT_FILL_LINE_SPLIT_GAP_PX:
                    segments.append([word])
                else:
                    segments[-1].append(word)
            for segment in segments:
                anchors.append(
                    FlatPdfAnchor(
                        anchor_id=id_offset + len(anchors),
                        page_index=page_index,
                        text=" ".join(cast(str, word["text"]) for word in segment),
                        x0=min(cast(int, word["left"]) for word in segment),
                        x1=max(cast(int, word["right"]) for word in segment),
                        top=min(cast(int, word["top"]) for word in segment),
                        bottom=max(cast(int, word["bottom"]) for word in segment),
                        page_width_px=page_width_px,
                        page_height_px=page_height_px,
                    )
                )
        return anchors

    async def _map_flat_placements_with_llm(
        self,
        anchors: list[FlatPdfAnchor],
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
    ) -> dict[str, Any]:
        default_llm_handler = await self._resolve_default_llm_handler(workflow_run_id, organization_id)
        llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(
            self.override_llm_key_for_organization(organization_id) or self.llm_key,
            default=default_llm_handler,
        )
        prompt = prompt_engine.load_prompt(
            "pdf-fill-flat-placement",
            anchors=[anchor.__dict__ for anchor in anchors],
            user_prompt=self.prompt,
            payload_json=json.dumps(self.payload, indent=2, default=str),
        )
        response = await llm_api_handler(
            prompt=prompt,
            prompt_name="pdf-fill-flat-placement",
            system_prompt=self.workflow_system_prompt,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )
        return self._parse_llm_json_response(response, expected_key="placements")

    @staticmethod
    def _sanitize_placements(
        raw_response: dict[str, Any],
        anchors: list[FlatPdfAnchor],
    ) -> tuple[list[FlatPlacement], list[dict[str, Any]]]:
        raw_placements = raw_response.get("placements")
        if not isinstance(raw_placements, list):
            return [], [{"anchor_id": None, "reason": "LLM response did not include a placements list"}]

        anchors_by_id = {anchor.anchor_id: anchor for anchor in anchors}
        placements: list[FlatPlacement] = []
        used_anchor_ids: set[int] = set()
        skipped: list[dict[str, Any]] = []
        for item in raw_placements:
            if not isinstance(item, dict):
                skipped.append({"anchor_id": None, "reason": "Placement is not an object", "value": item})
                continue
            try:
                anchor_id = int(item.get("anchor_id"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                skipped.append({"anchor_id": item.get("anchor_id"), "reason": "Invalid anchor_id"})
                continue
            anchor = anchors_by_id.get(anchor_id)
            if anchor is None:
                skipped.append({"anchor_id": anchor_id, "reason": "anchor_id was not detected on the PDF"})
                continue
            if anchor_id in used_anchor_ids:
                skipped.append({"anchor_id": anchor_id, "reason": "Duplicate placement for the same anchor"})
                continue
            value = item.get("value")
            if not isinstance(value, str) or not value.strip():
                skipped.append({"anchor_id": anchor_id, "reason": "Missing or empty value", "value": value})
                continue
            try:
                value.encode("latin-1")
            except UnicodeEncodeError:
                skipped.append(
                    {
                        "anchor_id": anchor_id,
                        "reason": "Value contains characters the flat-PDF overlay font cannot render",
                        "value": value,
                    }
                )
                continue
            position = item.get("position")
            if position not in ("right", "below"):
                position = "right"
            used_anchor_ids.add(anchor_id)
            placements.append(FlatPlacement(anchor=anchor, value=value.strip(), position=position))
        return placements, skipped

    @staticmethod
    def _escape_pdf_text(value: str) -> str:
        single_line = " ".join(value.split())
        return single_line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    def _resolve_flat_page_layout(
        self,
        page_placements: list[FlatPlacement],
        page_anchors: list[FlatPdfAnchor],
        *,
        page_width: float,
        page_height: float,
        origin_x: float,
        origin_y: float,
    ) -> list[ResolvedFlatText]:
        resolved: list[ResolvedFlatText] = []
        resolved_rects: list[tuple[float, float, float, float]] = []

        for placement in sorted(page_placements, key=lambda item: (item.anchor.top, item.anchor.x0)):
            anchor = placement.anchor
            if not anchor.page_width_px or not anchor.page_height_px:
                continue
            scale_x = page_width / anchor.page_width_px
            scale_y = page_height / anchor.page_height_px
            chosen_position = "below" if placement.position == "below" else "right"
            alternate_position = "right" if chosen_position == "below" else "below"
            start_x_by_position = {
                "right": anchor.x1 + 6 / scale_x,
                "below": anchor.x0 + 8 / scale_x,
            }

            def available_span(position: str) -> float:
                start_x_px = start_x_by_position[position]
                band_top, band_bottom = _flat_value_band_px(position, anchor, scale_y, float(FLAT_FILL_FONT_SIZE))
                obstacles: list[tuple[float, float]] = []
                for other_anchor in page_anchors:
                    if other_anchor.anchor_id == anchor.anchor_id:
                        continue
                    if position == "below" and other_anchor.bottom <= anchor.bottom + FLAT_FILL_LINE_EPS_PX:
                        continue
                    if min(band_bottom, other_anchor.bottom) > max(band_top, other_anchor.top):
                        obstacles.append((float(other_anchor.x0), float(other_anchor.x1)))
                for rect_x0, rect_x1, rect_top, rect_bottom in resolved_rects:
                    if min(band_bottom, rect_bottom) > max(band_top, rect_top):
                        obstacles.append((rect_x0, rect_x1))

                if any(obstacle_x0 <= start_x_px < obstacle_x1 for obstacle_x0, obstacle_x1 in obstacles):
                    return 0.0
                page_right_px = anchor.page_width_px - 6 / scale_x
                next_obstacle_x = min(
                    (obstacle_x0 for obstacle_x0, _ in obstacles if obstacle_x0 > start_x_px),
                    default=page_right_px,
                )
                return min(page_right_px, next_obstacle_x) - start_x_px

            spans = {position: available_span(position) for position in ("right", "below")}

            def fits(position: str, font_size: float) -> bool:
                width_px = _flat_text_width_pt(placement.value, font_size) / scale_x
                return width_px <= spans[position] - FLAT_FILL_TEXT_END_GAP_PT / scale_x

            # Reject below ink past the page bottom, keeping bottom-row values visible to the right.
            def on_page(position: str, font_size: float) -> bool:
                if position != "below":
                    return True
                _, band_bottom_px = _flat_value_band_px("below", anchor, scale_y, font_size)
                return band_bottom_px <= anchor.page_height_px

            candidates: list[tuple[str, float]] = [
                (chosen_position, float(FLAT_FILL_FONT_SIZE)),
                (alternate_position, float(FLAT_FILL_FONT_SIZE)),
            ]
            font_size = FLAT_FILL_FONT_SIZE - 0.5
            while font_size >= FLAT_FILL_MIN_FONT_SIZE:
                candidates.append((chosen_position, font_size))
                font_size -= 0.5
            font_size = FLAT_FILL_FONT_SIZE - 0.5
            while font_size >= FLAT_FILL_MIN_FONT_SIZE:
                candidates.append((alternate_position, font_size))
                font_size -= 0.5

            selected = next((candidate for candidate in candidates if on_page(*candidate) and fits(*candidate)), None)
            overflowed = selected is None
            if selected is None:
                fallback_positions = ["right"] + (["below"] if on_page("below", FLAT_FILL_MIN_FONT_SIZE) else [])
                final_position = max(fallback_positions, key=lambda position: spans[position])
                final_font_size = FLAT_FILL_MIN_FONT_SIZE
                LOG.warning(
                    "Flat PDF text placement overflow",
                    value_length=len(placement.value),
                    right_span_px=spans["right"],
                    below_span_px=spans["below"],
                )
            else:
                final_position, final_font_size = selected

            if final_position != chosen_position or final_font_size != FLAT_FILL_FONT_SIZE:
                LOG.info(
                    "Adjusted flat PDF text placement",
                    anchor_id=anchor.anchor_id,
                    from_position=chosen_position,
                    to_position=final_position,
                    from_font_size=FLAT_FILL_FONT_SIZE,
                    to_font_size=final_font_size,
                )

            if final_position == "below":
                text_x = origin_x + anchor.x0 * scale_x + 8
                text_y = origin_y + page_height - anchor.bottom * scale_y - final_font_size - FLAT_FILL_FONT_SIZE_GAP
            else:
                text_x = origin_x + anchor.x1 * scale_x + 6
                text_y = origin_y + page_height - anchor.bottom * scale_y - 1
            resolved.append(
                ResolvedFlatText(
                    value=placement.value,
                    x=text_x,
                    y=text_y,
                    font_size=final_font_size,
                    overflowed=overflowed,
                )
            )
            start_x_px = start_x_by_position[final_position]
            band_top, band_bottom = _flat_value_band_px(final_position, anchor, scale_y, final_font_size)
            width_px = _flat_text_width_pt(placement.value, final_font_size) / scale_x
            resolved_rects.append((start_x_px, start_x_px + width_px, band_top, band_bottom))

        return resolved

    async def _fill_flat_overlay(
        self,
        reader: PdfReader,
        placements: list[FlatPlacement],
        anchors: list[FlatPdfAnchor],
        output_path: Path,
        *,
        overflow_records: list[dict[str, Any]] | None = None,
    ) -> bytes:
        writer = PdfWriter()
        writer.clone_document_from_reader(reader)

        placements_by_page: dict[int, list[FlatPlacement]] = {}
        for placement in placements:
            placements_by_page.setdefault(placement.anchor.page_index, []).append(placement)

        for page_index, page_placements in placements_by_page.items():
            if page_index >= len(writer.pages):
                continue
            page = writer.pages[page_index]
            # pdfplumber renders the page's CROPBOX region, so OCR pixel coordinates map to the
            # cropbox (which can differ from the media box and have a nonzero origin).
            crop_box = page.cropbox
            page_width = float(crop_box.width)
            page_height = float(crop_box.height)
            origin_x = float(crop_box.left)
            origin_y = float(crop_box.bottom)
            page_anchors = [anchor for anchor in anchors if anchor.page_index == page_index]
            resolvable_placements = [
                placement
                for placement in sorted(page_placements, key=lambda item: (item.anchor.top, item.anchor.x0))
                if placement.anchor.page_width_px and placement.anchor.page_height_px
            ]
            resolved_texts = self._resolve_flat_page_layout(
                resolvable_placements,
                page_anchors,
                page_width=page_width,
                page_height=page_height,
                origin_x=origin_x,
                origin_y=origin_y,
            )
            if overflow_records is not None:
                for placement, resolved in zip(resolvable_placements, resolved_texts, strict=True):
                    if resolved.overflowed:
                        overflow_records.append(
                            {
                                "anchor_id": placement.anchor.anchor_id,
                                "value": resolved.value,
                                "reason": "Placement overflowed the available space; value may overlap adjacent content",
                            }
                        )
            text_ops = [
                f"BT {FLAT_FILL_FONT_RESOURCE} {resolved.font_size:g} Tf 0 0 0 rg "
                f"{resolved.x:.1f} {resolved.y:.1f} Td ({self._escape_pdf_text(resolved.value)}) Tj ET"
                for resolved in resolved_texts
            ]
            if not text_ops:
                continue

            overlay_writer = PdfWriter()
            overlay_page = overlay_writer.add_blank_page(width=page_width, height=page_height)
            # Mirror the target's cropbox bounds on the overlay page: merge_page composites and clips
            # relative to the incoming page's boxes, so matching boxes keep absolute coordinates intact.
            overlay_page.mediabox.lower_left = (origin_x, origin_y)
            overlay_page.mediabox.upper_right = (origin_x + page_width, origin_y + page_height)
            content_stream = DecodedStreamObject()
            # _sanitize_placements already rejects non-latin-1 values, so strict makes any future gap
            # fail loudly instead of silently writing a corrupted overlay.
            content_stream.set_data("\n".join(text_ops).encode("latin-1"))
            overlay_page[NameObject("/Contents")] = overlay_writer._add_object(content_stream)
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                    NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
                }
            )
            overlay_page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject(FLAT_FILL_FONT_RESOURCE): overlay_writer._add_object(font)}
                    )
                }
            )
            # merge_page wraps the original content in a balanced graphics state; appending raw streams
            # inherits any unbalanced CTM left by the source document and teleports the overlay.
            page.merge_page(overlay_page)

        buffer = io.BytesIO()
        writer.write(buffer)
        pdf_bytes = buffer.getvalue()
        async with aiofiles.open(output_path, "wb") as f:
            await f.write(pdf_bytes)
        return pdf_bytes

    async def _upload_pdf_artifact(
        self,
        *,
        pdf_bytes: bytes,
        workflow_run_id: str,
        workflow_run_block_id: str,
        workflow_run_context: WorkflowRunContext,
        organization_id: str | None,
    ) -> tuple[str | None, str | None]:
        artifact_org_id = organization_id or workflow_run_context.organization_id
        if not artifact_org_id:
            return None, None
        try:
            workflow_run_block = await app.DATABASE.observer.get_workflow_run_block(
                workflow_run_block_id,
                organization_id=artifact_org_id,
            )
        except NotFoundError:
            return None, None

        artifact_id, artifact_uri = await app.ARTIFACT_MANAGER.create_workflow_run_block_artifact_with_uri(
            workflow_run_block=workflow_run_block,
            artifact_type=ArtifactType.PDF,
            data=pdf_bytes,
        )
        try:
            await app.ARTIFACT_MANAGER.wait_for_upload_aiotasks([workflow_run_block.workflow_run_block_id])
        except Exception:
            LOG.warning("PdfFillBlock failed to upload PDF artifact", workflow_run_id=workflow_run_id, exc_info=True)
            return None, None

        artifact_url = None
        try:
            artifact = await app.DATABASE.artifacts.get_artifact_by_id(artifact_id, organization_id=artifact_org_id)
            if artifact:
                artifact_url = await app.ARTIFACT_MANAGER.get_share_link(artifact)
        except Exception:
            LOG.warning("PdfFillBlock failed to generate artifact download URL", artifact_id=artifact_id, exc_info=True)
        return artifact_uri, artifact_url

    async def _register_pdf_as_downloaded_file(
        self,
        *,
        organization_id: str | None,
        workflow_run_id: str,
        workflow_run_block_id: str,
    ) -> list[FileInfo]:
        if not organization_id:
            return []
        try:
            async with asyncio.timeout(SAVE_DOWNLOADED_FILES_TIMEOUT):
                await app.STORAGE.save_downloaded_files(organization_id=organization_id, run_id=workflow_run_id)
        except Exception:
            LOG.warning(
                "PdfFillBlock failed to register filled PDF as downloaded file",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                exc_info=True,
            )
            return []
        try:
            async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                return await app.STORAGE.get_downloaded_files(organization_id=organization_id, run_id=workflow_run_id)
        except Exception:
            return []

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: Any,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        block_context = skyvern_context.current()
        if block_context and organization_id:
            await capture_block_download_baseline(block_context, organization_id, workflow_run_id, self.label)

        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self._record_failure(
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
                f"Failed to format jinja template: {str(e)}",
            )

        try:
            source_pdf_path, source_is_temp = await self._resolve_source_pdf(workflow_run_id, organization_id)
        except Exception as e:
            return await self._record_failure(
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
                f"Failed to load PDF: {str(e)}",
            )

        try:
            try:
                validate_pdf_file(source_pdf_path, file_identifier=self.file_url)
                reader = PdfReader(source_pdf_path)
                inventory = self._extract_field_inventory(reader, source_pdf_path)
            except PDFParsingError as e:
                return await self._record_failure(
                    workflow_run_context,
                    workflow_run_id,
                    workflow_run_block_id,
                    organization_id,
                    f"Invalid PDF file: {str(e)}",
                )
            except Exception as e:
                return await self._record_failure(
                    workflow_run_context,
                    workflow_run_id,
                    workflow_run_block_id,
                    organization_id,
                    f"Failed to load PDF: {str(e)}",
                )

            if not inventory and not self._tesseract_available():
                return await self._record_failure(
                    workflow_run_context,
                    workflow_run_id,
                    workflow_run_block_id,
                    organization_id,
                    FLAT_PDF_OCR_MISSING_MESSAGE,
                )

            fill_mode = "acroform" if inventory else "flat_overlay"
            skipped_fields: list[dict[str, Any]] = []
            overflowed_placements: list[dict[str, Any]] = []
            try:
                output_path = self._output_path(workflow_run_id, workflow_run_block_id)
                if inventory:
                    raw_mapping = await self._map_fields_with_llm(
                        inventory,
                        workflow_run_id,
                        workflow_run_block_id,
                        organization_id,
                    )
                    fields, skipped_fields = self._sanitize_mapping(raw_mapping, inventory)
                    if not fields:
                        skipped_summary = "; ".join(
                            f"{item.get('field_name')}: {item.get('reason')}" for item in skipped_fields[:5]
                        )
                        return await self._record_failure(
                            workflow_run_context,
                            workflow_run_id,
                            workflow_run_block_id,
                            organization_id,
                            "PDF Fill could not map any payload values to the PDF's form fields. "
                            f"Skipped: {skipped_summary or 'no fields proposed'}",
                            output_parameter_value={"fields": {}, "skipped_fields": skipped_fields},
                        )
                    pdf_bytes = await self._fill_with_skyvern(reader, fields, output_path)
                else:
                    if len(reader.pages) > FLAT_FILL_MAX_PAGES:
                        return await self._record_failure(
                            workflow_run_context,
                            workflow_run_id,
                            workflow_run_block_id,
                            organization_id,
                            f"Flat PDF filling supports up to {FLAT_FILL_MAX_PAGES} pages; "
                            f"this PDF has {len(reader.pages)}.",
                        )
                    anchors = await self._extract_flat_anchors(source_pdf_path)
                    if not anchors:
                        return await self._record_failure(
                            workflow_run_context,
                            workflow_run_id,
                            workflow_run_block_id,
                            organization_id,
                            "OCR did not detect any text on the PDF pages, so there are no labels to fill against.",
                        )
                    raw_placements = await self._map_flat_placements_with_llm(
                        anchors,
                        workflow_run_id,
                        workflow_run_block_id,
                        organization_id,
                    )
                    placements, skipped_fields = self._sanitize_placements(raw_placements, anchors)
                    if not placements:
                        skipped_summary = "; ".join(
                            f"{item.get('anchor_id')}: {item.get('reason')}" for item in skipped_fields[:5]
                        )
                        return await self._record_failure(
                            workflow_run_context,
                            workflow_run_id,
                            workflow_run_block_id,
                            organization_id,
                            "PDF Fill could not place any payload values on the flat PDF. "
                            f"Skipped: {skipped_summary or 'no placements proposed'}",
                            output_parameter_value={"fields": {}, "skipped_fields": skipped_fields},
                        )
                    fields = {placement.anchor.text: placement.value for placement in placements}
                    pdf_bytes = await self._fill_flat_overlay(
                        reader,
                        placements,
                        anchors,
                        output_path,
                        overflow_records=overflowed_placements,
                    )
            except Exception as e:
                return await self._record_failure(
                    workflow_run_context,
                    workflow_run_id,
                    workflow_run_block_id,
                    organization_id,
                    f"PDF Fill failed: {str(e)}",
                )

            artifact_uri, artifact_url = await self._upload_pdf_artifact(
                pdf_bytes=pdf_bytes,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                workflow_run_context=workflow_run_context,
                organization_id=organization_id,
            )
            artifact_org_id = organization_id or workflow_run_context.organization_id
            downloaded_files = await self._register_pdf_as_downloaded_file(
                organization_id=artifact_org_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            current_context = skyvern_context.current()
            downloaded_files = filter_downloaded_files_for_current_iteration(
                downloaded_files,
                current_context.loop_internal_state if current_context else None,
            )
            # The run-level download list can hold other blocks' files; narrow to this block's filled PDF so a
            # downstream block consuming `{{ this_output }}` resolves to the right file (extract_file_url reads [0]).
            own_downloaded_files = [
                fi for fi in downloaded_files if fi.filename == output_path.name
            ] or downloaded_files

            output = {
                "fill_mode": fill_mode,
                "fields": fields,
                "skipped_fields": skipped_fields,
                "overflowed_placements": overflowed_placements,
                "file_path": str(output_path),
                "file_name": output_path.name,
                "file_size": output_path.stat().st_size,
                "artifact_uri": artifact_uri,
                "artifact_url": artifact_url,
                "downloaded_files": [fi.model_dump() for fi in own_downloaded_files],
                "downloaded_file_urls": [fi.url for fi in own_downloaded_files],
                "downloaded_file_artifact_ids": [fi.artifact_id for fi in own_downloaded_files if fi.artifact_id],
            }
            output = workflow_run_context.mask_secrets_in_data(output)
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output)
            return await self.build_block_result(
                success=True,
                failure_reason=None,
                output_parameter_value=output,
                status=BlockStatus.completed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        finally:
            if source_is_temp and os.path.exists(source_pdf_path):
                try:
                    os.remove(source_pdf_path)
                except OSError:
                    LOG.warning("PdfFillBlock failed to clean up downloaded source PDF", path=source_pdf_path)
