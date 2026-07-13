from __future__ import annotations

import io
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    RectangleObject,
    TextStringObject,
)

from skyvern.forge import app
from skyvern.forge.sdk.utils.tesseract_languages import tesseract_language_arg, tesseract_ocr_packages
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models import pdf_fill_block
from skyvern.forge.sdk.workflow.models.block import PdfFillBlock, extract_file_url_from_block_output
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.models.pdf_fill_block import FlatPdfAnchor, FlatPlacement, PdfFieldInventory
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.workflows import BlockStatus, BlockType, PdfFillBlockYAML


def _make_output_parameter(label: str = "fill_pdf") -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        output_parameter_id=f"{label}_output_id",
        workflow_id="workflow-id",
        key=f"{label}_output",
        created_at=now,
        modified_at=now,
    )


def _make_block(**overrides: Any) -> PdfFillBlock:
    data: dict[str, Any] = {
        "label": "fill_pdf",
        "output_parameter": _make_output_parameter(),
        "file_url": "source.pdf",
        "prompt": "Fill the PDF.",
    }
    data.update(overrides)
    return PdfFillBlock(**data)


def _flat_text_ops(pdf_bytes: bytes) -> list[tuple[str, float, float, float, str]]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    contents = reader.pages[0].get_contents()
    assert contents is not None
    data = contents.get_data().decode("latin-1")
    pattern = re.compile(
        r"BT\s+/SkyvernHelv(?:-\d+)?\s+(?P<font>\d+(?:\.\d+)?)\s+Tf\s+0\s+0\s+0\s+rg\s+"
        r"(?P<x>-?\d+(?:\.\d+)?)\s+(?P<y>-?\d+(?:\.\d+)?)\s+Td\s+"
        r"\((?P<value>(?:\\.|[^\\)])*)\)\s+Tj\s+ET"
    )
    operations: list[tuple[str, float, float, float, str]] = []
    for match in pattern.finditer(data):
        value = re.sub(
            r"\\([0-7]{3}|.)",
            lambda escaped: chr(int(escaped.group(1), 8)) if escaped.group(1).isdigit() else escaped.group(1),
            match.group("value"),
        )
        font = match.group("font")
        x = match.group("x")
        y = match.group("y")
        operation = f"BT /SkyvernHelv {font} Tf 0 0 0 rg {float(x):.1f} {float(y):.1f} Td ({value}) Tj ET"
        operations.append((operation, float(x), float(y), float(font), value))
    return operations


def _flat_collision_anchors() -> list[FlatPdfAnchor]:
    dimensions = {"page_width_px": 1275, "page_height_px": 1651}
    return [
        FlatPdfAnchor(anchor_id=0, page_index=0, text="a. Name:", x0=88, x1=218, top=309, bottom=322, **dimensions),
        FlatPdfAnchor(
            anchor_id=1, page_index=0, text="b. Date of birth", x0=494, x1=672, top=309, bottom=322, **dimensions
        ),
        FlatPdfAnchor(
            anchor_id=2, page_index=0, text="c. Member ID #:", x0=761, x1=952, top=309, bottom=325, **dimensions
        ),
        FlatPdfAnchor(
            anchor_id=3,
            page_index=0,
            text="d. Street address:",
            x0=88,
            x1=285,
            top=371,
            bottom=384,
            **dimensions,
        ),
        FlatPdfAnchor(anchor_id=4, page_index=0, text="e. City:", x0=106, x1=138, top=395, bottom=411, **dimensions),
        FlatPdfAnchor(anchor_id=5, page_index=0, text="f. State:", x0=493, x1=594, top=394, bottom=408, **dimensions),
        FlatPdfAnchor(
            anchor_id=6, page_index=0, text="g. Zip code:", x0=744, x1=829, top=395, bottom=411, **dimensions
        ),
    ]


def _tight_flat_anchors() -> list[FlatPdfAnchor]:
    dimensions = {"page_width_px": 1275, "page_height_px": 1651}
    return [
        FlatPdfAnchor(anchor_id=0, page_index=0, text="Ref #:", x0=100, x1=160, top=100, bottom=113, **dimensions),
        FlatPdfAnchor(anchor_id=1, page_index=0, text="Next:", x0=300, x1=360, top=100, bottom=113, **dimensions),
        FlatPdfAnchor(anchor_id=2, page_index=0, text="Below:", x0=100, x1=200, top=120, bottom=133, **dimensions),
    ]


def _write_blank_flat_pdf(tmp_path: Path) -> Path:
    source_path = tmp_path / "source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with source_path.open("wb") as file:
        writer.write(file)
    return source_path


def _install_context(monkeypatch: pytest.MonkeyPatch, run_id: str = "run_pdf_fill") -> WorkflowRunContext:
    context = WorkflowRunContext(
        workflow_title="PDF Fill Test",
        workflow_id="workflow-id",
        workflow_permanent_id="wpid-pdf-fill",
        workflow_run_id=run_id,
        aws_client=AsyncMock(),
    )
    monkeypatch.setattr(PdfFillBlock, "get_workflow_run_context", staticmethod(lambda _wr_id: context))
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "create_or_update_workflow_run_output_parameter",
        AsyncMock(),
    )
    return context


def _run_local_pdf_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: str, name: str) -> Path:
    download_root = tmp_path / "downloads"
    monkeypatch.setattr(pdf_fill_block.settings, "DOWNLOAD_PATH", str(download_root))
    run_dir = download_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir / name


def _add_empty_appearance(writer: PdfWriter, states: list[str]) -> DictionaryObject:
    normal_appearances = DictionaryObject()
    for state in states:
        stream = DecodedStreamObject()
        stream.set_data(b"")
        normal_appearances[NameObject(state)] = writer._add_object(stream)
    return DictionaryObject({NameObject("/N"): normal_appearances})


def _write_fillable_pdf(path: Path) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    fields = ArrayObject()
    annotations = ArrayObject()

    text_field = DictionaryObject(
        {
            NameObject("/FT"): NameObject("/Tx"),
            NameObject("/T"): TextStringObject("name"),
            NameObject("/V"): TextStringObject(""),
        }
    )
    text_field_ref = writer._add_object(text_field)
    text_widget = DictionaryObject(
        {
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Rect"): ArrayObject([FloatObject(50), FloatObject(150), FloatObject(250), FloatObject(170)]),
            NameObject("/F"): NumberObject(4),
            NameObject("/P"): page.indirect_reference,
            NameObject("/Parent"): text_field_ref,
        }
    )
    text_widget_ref = writer._add_object(text_widget)
    text_field[NameObject("/Kids")] = ArrayObject([text_widget_ref])
    fields.append(text_field_ref)
    annotations.append(text_widget_ref)

    checkbox_field = DictionaryObject(
        {
            NameObject("/FT"): NameObject("/Btn"),
            NameObject("/T"): TextStringObject("subscribe"),
            NameObject("/V"): NameObject("/Off"),
        }
    )
    checkbox_field_ref = writer._add_object(checkbox_field)
    checkbox_widget = DictionaryObject(
        {
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Rect"): ArrayObject([FloatObject(50), FloatObject(100), FloatObject(65), FloatObject(115)]),
            NameObject("/F"): NumberObject(4),
            NameObject("/P"): page.indirect_reference,
            NameObject("/Parent"): checkbox_field_ref,
            NameObject("/AP"): _add_empty_appearance(writer, ["/Yes", "/Off"]),
            NameObject("/AS"): NameObject("/Off"),
        }
    )
    checkbox_widget_ref = writer._add_object(checkbox_widget)
    checkbox_field[NameObject("/Kids")] = ArrayObject([checkbox_widget_ref])
    fields.append(checkbox_field_ref)
    annotations.append(checkbox_widget_ref)

    page[NameObject("/Annots")] = annotations
    writer._root_object.update(
        {
            NameObject("/AcroForm"): DictionaryObject(
                {NameObject("/Fields"): fields, NameObject("/NeedAppearances"): BooleanObject(True)}
            )
        }
    )
    with path.open("wb") as f:
        writer.write(f)


def _write_flat_pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=200)
    with path.open("wb") as f:
        writer.write(f)


async def _fake_llm_response(**_: Any) -> dict[str, Any]:
    return {"fields": {"name": "Jane", "subscribe": True}, "thought": "matched fields"}


@pytest.mark.asyncio
async def test_skyvern_engine_fills_pdf_with_llm_mapping(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_id = "run_skyvern_pdf_fill"
    _install_context(monkeypatch, run_id)
    source_pdf = _run_local_pdf_path(monkeypatch, tmp_path, run_id, "source.pdf")
    _write_fillable_pdf(source_pdf)
    assert PdfReader(str(source_pdf)).get_fields()

    monkeypatch.setattr(PdfFillBlock, "_resolve_default_llm_handler", AsyncMock(return_value=_fake_llm_response))
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.pdf_fill_block.get_path_for_workflow_download_directory",
        lambda workflow_run_id: tmp_path / "downloads" / workflow_run_id,
    )

    block = _make_block(file_url=str(source_pdf), payload={"name": "Jane", "subscribe": True})
    result = await block.execute(workflow_run_id=run_id, workflow_run_block_id="", organization_id=None)

    assert result.success is True
    assert result.status == BlockStatus.completed
    assert result.output_parameter_value is not None
    output = result.output_parameter_value
    output_path = Path(output["file_path"])
    assert output_path.exists()
    assert output["file_name"] == "fill_pdf_filled.pdf"
    assert output["file_size"] == output_path.stat().st_size
    assert output["fields"] == {"name": "Jane", "subscribe": "/Yes"}
    assert output["skipped_fields"] == []
    assert output["overflowed_placements"] == []

    output_reader = PdfReader(str(output_path))
    fields = output_reader.get_fields()
    assert fields["name"]["/V"] == "Jane"
    assert fields["subscribe"]["/V"] == "/Yes"
    assert bool(output_reader.trailer["/Root"]["/AcroForm"]["/NeedAppearances"]) is True


def test_mapping_sanitization_skips_unknown_fields_and_coerces_checkbox(tmp_path: Path) -> None:
    source_pdf = tmp_path / "source.pdf"
    _write_fillable_pdf(source_pdf)
    block = _make_block(file_url=str(source_pdf))
    inventory = block._extract_field_inventory(PdfReader(str(source_pdf)))

    fields, skipped = block._sanitize_mapping(
        {"fields": {"name": "Jane", "subscribe": True, "unknown": "ignored"}},
        inventory,
    )

    assert fields == {"name": "Jane", "subscribe": "/Yes"}
    assert skipped == [{"field_name": "unknown", "reason": "Field is not present in the PDF", "value": "ignored"}]


def test_extract_file_url_from_block_output_precedence() -> None:
    # downloaded_files[0] wins when present; url before file_path within the entry.
    assert (
        extract_file_url_from_block_output(
            {"downloaded_files": [{"url": "s3://a", "file_path": "/tmp/a"}], "artifact_url": "s3://art"}
        )
        == "s3://a"
    )
    assert extract_file_url_from_block_output({"downloaded_files": [{"file_path": "/tmp/a"}]}) == "/tmp/a"
    # FileDownload/HttpRequest-style outputs without downloaded_files fall back to the widened keys, in order.
    assert extract_file_url_from_block_output({"artifact_url": "s3://art", "file_path": "/tmp/a"}) == "s3://art"
    assert extract_file_url_from_block_output({"file_url": "s3://f", "file_path": "/tmp/a"}) == "s3://f"
    assert extract_file_url_from_block_output({"file_path": "/tmp/a"}) == "/tmp/a"
    # empty downloaded_files is not treated as a match; falls through to the keyed fallback.
    assert extract_file_url_from_block_output({"downloaded_files": [], "file_path": "/tmp/a"}) == "/tmp/a"
    assert extract_file_url_from_block_output({"nothing": "useful"}) is None
    # string inputs are parsed (JSON, then python-literal) before unwrapping.
    assert extract_file_url_from_block_output('{"downloaded_files": [{"url": "s3://a"}]}') == "s3://a"
    assert extract_file_url_from_block_output("not a url") is None


def test_parse_llm_json_response_non_dict_returns_empty() -> None:
    # A JSON array/primitive string must not blow up callers that do .get(expected_key).
    assert PdfFillBlock._parse_llm_json_response("[]", expected_key="fields") == {}
    assert PdfFillBlock._parse_llm_json_response("null", expected_key="fields") == {}
    assert PdfFillBlock._parse_llm_json_response('{"fields": {"a": "b"}}', expected_key="fields") == {
        "fields": {"a": "b"}
    }


@pytest.mark.asyncio
async def test_downloaded_source_pdf_is_cleaned_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_id = "run_cleanup"
    _install_context(monkeypatch, run_id)
    downloaded = tmp_path / "downloaded_source.pdf"
    _write_fillable_pdf(downloaded)

    async def _fake_download(url: str, **_: Any) -> str:
        return str(downloaded)

    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.pdf_fill_block.download_file", _fake_download)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.pdf_fill_block.settings.ENV", "production")
    monkeypatch.setattr(PdfFillBlock, "_resolve_default_llm_handler", AsyncMock(return_value=_fake_llm_response))
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.pdf_fill_block.get_path_for_workflow_download_directory",
        lambda workflow_run_id: tmp_path / "downloads" / workflow_run_id,
    )

    block = _make_block(file_url="https://example.com/source.pdf", payload={"name": "Jane"})
    result = await block.execute(workflow_run_id=run_id, workflow_run_block_id="", organization_id=None)

    assert result.success is True
    assert not downloaded.exists(), "the downloaded source temp should be removed after execute"


def test_nearest_label_picks_aligned_side() -> None:
    # field box at x 100-200, top 100-114
    field = (100.0, 100.0, 200.0, 114.0)
    # label above the field
    above = [
        {"text": "Social", "x0": 100, "x1": 140, "top": 84, "bottom": 96, "upright": True},
        {"text": "security", "x0": 142, "x1": 190, "top": 84, "bottom": 96, "upright": True},
    ]
    assert PdfFillBlock._nearest_label(above, *field) == "Social security"
    # checkbox: label to the right
    right = [{"text": "LLC", "x0": 210, "x1": 240, "top": 100, "bottom": 114, "upright": True}]
    assert PdfFillBlock._nearest_label(right, *field) == "LLC"
    # nothing nearby
    assert PdfFillBlock._nearest_label([], *field) == ""


def test_nearest_label_button_prefers_right_option_text() -> None:
    # A classification checkbox with a section header ABOVE and its option label to the RIGHT:
    # text fields should take the header, button fields should take the option text.
    field = (100.0, 100.0, 112.0, 112.0)
    words = [
        {"text": "Check", "x0": 60, "x1": 95, "top": 80, "bottom": 92, "upright": True},
        {"text": "one", "x0": 97, "x1": 120, "top": 80, "bottom": 92, "upright": True},
        {"text": "Individual", "x0": 120, "x1": 200, "top": 100, "bottom": 112, "upright": True},
    ]
    assert PdfFillBlock._nearest_label(words, *field, is_button=False) == "Check one"
    assert PdfFillBlock._nearest_label(words, *field, is_button=True) == "Individual"


def test_extract_field_labels_on_fillable_fixture(tmp_path: Path) -> None:
    source_pdf = tmp_path / "labelled.pdf"
    _write_fillable_pdf(source_pdf)
    labels = PdfFillBlock._extract_field_labels(str(source_pdf))
    # the fixture has no printed text, so labels are empty strings but extraction must not crash
    assert isinstance(labels, dict)


def test_checkbox_without_known_state_is_skipped_not_silently_unchecked() -> None:
    block = _make_block()
    inventory = {
        "agree": PdfFieldInventory(name="agree", field_type="checkbox", current_value=None, allowed_values=[]),
    }
    fields, skipped = block._sanitize_mapping({"fields": {"agree": True}}, inventory)

    assert fields == {}
    assert skipped == [{"field_name": "agree", "reason": "Checkbox has no known checked state", "value": True}]


@pytest.mark.asyncio
async def test_empty_field_mapping_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_id = "run_empty_mapping"
    _install_context(monkeypatch, run_id)
    source_pdf = _run_local_pdf_path(monkeypatch, tmp_path, run_id, "source.pdf")
    _write_fillable_pdf(source_pdf)

    async def _unmappable_llm_response(**_: Any) -> dict[str, Any]:
        return {"fields": {"not_a_real_field": "value"}, "thought": "no match"}

    monkeypatch.setattr(PdfFillBlock, "_resolve_default_llm_handler", AsyncMock(return_value=_unmappable_llm_response))

    block = _make_block(file_url=str(source_pdf))
    result = await block.execute(workflow_run_id=run_id, workflow_run_block_id="", organization_id=None)

    assert result.success is False
    assert result.status == BlockStatus.failed
    assert "could not map any payload values" in (result.failure_reason or "")
    assert result.output_parameter_value["skipped_fields"][0]["field_name"] == "not_a_real_field"


@pytest.mark.asyncio
async def test_absolute_path_rejected_outside_run_download_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_id = "run_non_local_path"
    _install_context(monkeypatch, run_id)
    source_pdf = tmp_path / "source.pdf"
    _write_fillable_pdf(source_pdf)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.pdf_fill_block.settings.ENV", "production")
    monkeypatch.setattr(pdf_fill_block.settings, "DOWNLOAD_PATH", str(tmp_path / "downloads"))

    block = _make_block(file_url=str(source_pdf))
    result = await block.execute(workflow_run_id=run_id, workflow_run_block_id="", organization_id=None)

    assert result.success is False
    assert result.status == BlockStatus.failed


@pytest.mark.asyncio
async def test_resolve_source_pdf_accepts_run_local_absolute_path_in_non_local_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_id = "run_cloud_local_path"
    _install_context(monkeypatch, run_id)
    source_pdf = _run_local_pdf_path(monkeypatch, tmp_path, run_id, "source.pdf")
    _write_fillable_pdf(source_pdf)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.pdf_fill_block.settings.ENV", "production")

    block = _make_block(file_url=str(source_pdf))
    resolved, is_temp = await block._resolve_source_pdf(run_id, organization_id="org-1")

    assert resolved == str(source_pdf.resolve())
    assert is_temp is False


@pytest.mark.asyncio
async def test_flat_pdf_without_tesseract_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_id = "run_flat_pdf_no_ocr"
    _install_context(monkeypatch, run_id)
    flat_pdf = _run_local_pdf_path(monkeypatch, tmp_path, run_id, "flat.pdf")
    _write_flat_pdf(flat_pdf)
    monkeypatch.setattr(PdfFillBlock, "_tesseract_available", staticmethod(lambda: False))

    block = _make_block(file_url=str(flat_pdf))
    result = await block.execute(workflow_run_id=run_id, workflow_run_block_id="", organization_id=None)

    assert result.success is False
    assert result.status == BlockStatus.failed
    assert "tesseract" in (result.failure_reason or "")


@pytest.mark.asyncio
async def test_flat_pdf_overlay_fills_with_ocr_anchors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_id = "run_flat_pdf_overlay"
    _install_context(monkeypatch, run_id)
    flat_pdf = _run_local_pdf_path(monkeypatch, tmp_path, run_id, "flat.pdf")
    _write_flat_pdf(flat_pdf)

    anchor = FlatPdfAnchor(
        anchor_id=0,
        page_index=0,
        text="Enrollee name:",
        x0=20,
        x1=120,
        top=30,
        bottom=44,
        page_width_px=600,
        page_height_px=400,
    )

    async def _placement_llm_response(**_: Any) -> dict[str, Any]:
        return {"placements": [{"anchor_id": 0, "value": "Luis Ortiz", "position": "right"}], "thought": "ok"}

    monkeypatch.setattr(PdfFillBlock, "_tesseract_available", staticmethod(lambda: True))
    monkeypatch.setattr(PdfFillBlock, "_extract_flat_anchors", AsyncMock(return_value=[anchor]))
    monkeypatch.setattr(PdfFillBlock, "_resolve_default_llm_handler", AsyncMock(return_value=_placement_llm_response))
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.pdf_fill_block.get_path_for_workflow_download_directory",
        lambda workflow_run_id: tmp_path / "downloads" / workflow_run_id,
    )

    block = _make_block(file_url=str(flat_pdf), payload={"personalInfo": {"firstName": "Luis", "lastName": "Ortiz"}})
    result = await block.execute(workflow_run_id=run_id, workflow_run_block_id="", organization_id=None)

    assert result.success is True
    output = result.output_parameter_value
    assert output["fill_mode"] == "flat_overlay"
    assert output["fields"] == {"Enrollee name:": "Luis Ortiz"}
    assert output["skipped_fields"] == []
    assert output["overflowed_placements"] == []

    import pdfplumber

    with pdfplumber.open(output["file_path"]) as pdf:
        words = pdf.pages[0].extract_words()
    texts = {w["text"] for w in words}
    assert "Luis" in texts and "Ortiz" in texts
    luis = next(w for w in words if w["text"] == "Luis")
    # page 300x200pt, anchor px scaled by 0.5: expected x ~= 120*0.5+6 = 66, baseline y=177 -> top ~15
    assert 60 <= luis["x0"] <= 75
    assert 5 <= luis["top"] <= 25


@pytest.mark.asyncio
async def test_flat_pdf_over_page_limit_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_id = "run_flat_pdf_too_many_pages"
    _install_context(monkeypatch, run_id)
    big_pdf = _run_local_pdf_path(monkeypatch, tmp_path, run_id, "big.pdf")
    writer = PdfWriter()
    for _ in range(26):
        writer.add_blank_page(width=300, height=200)
    with big_pdf.open("wb") as f:
        writer.write(f)
    monkeypatch.setattr(PdfFillBlock, "_tesseract_available", staticmethod(lambda: True))

    block = _make_block(file_url=str(big_pdf))
    result = await block.execute(workflow_run_id=run_id, workflow_run_block_id="", organization_id=None)

    assert result.success is False
    assert "supports up to 25 pages" in (result.failure_reason or "")


@pytest.mark.asyncio
async def test_flat_overlay_right_value_moves_below_when_line_is_full(tmp_path: Path) -> None:
    source_path = _write_blank_flat_pdf(tmp_path)
    anchors = _flat_collision_anchors()
    dob_label = anchors[1]
    member_label = anchors[2]
    placements = [
        FlatPlacement(anchor=dob_label, value="01/01/2000", position="right"),
        FlatPlacement(anchor=member_label, value="10000001", position="right"),
    ]
    block = _make_block(file_url=str(source_path))
    result_bytes = await block._fill_flat_overlay(
        PdfReader(str(source_path)), placements, anchors, tmp_path / "filled.pdf"
    )

    PdfReader(io.BytesIO(result_bytes))
    operations = _flat_text_ops(result_bytes)
    assert len(operations) == 2
    operations_by_value = {operation[4]: operation for operation in operations}
    date_op = operations_by_value["01/01/2000"]
    member_op = operations_by_value["10000001"]
    scale_x = 612 / 1275
    scale_y = 792 / 1651
    expected_date_x = 494 * scale_x + 8
    expected_date_y = 792 - 322 * scale_y - 11 - 3
    assert date_op[1] == pytest.approx(expected_date_x, abs=0.05)
    assert date_op[2] == pytest.approx(expected_date_y, abs=0.05)
    assert date_op[3] == 11
    assert member_op[1] == pytest.approx(952 * scale_x + 6, abs=0.05)
    assert member_op[2] == pytest.approx(792 - 325 * scale_y - 1, abs=0.05)
    assert member_op[3] == 11
    assert date_op[1] + pdf_fill_block._flat_text_width_pt("01/01/2000", 11) <= member_label.x0 * scale_x


@pytest.mark.asyncio
async def test_flat_overlay_below_value_moves_right_when_next_row_occupied(tmp_path: Path) -> None:
    source_path = _write_blank_flat_pdf(tmp_path)
    anchors = _flat_collision_anchors()
    addr_label = anchors[3]
    city_label = anchors[4]
    placements = [
        FlatPlacement(anchor=addr_label, value="350 Test Street", position="below"),
        FlatPlacement(anchor=city_label, value="Detroit", position="right"),
    ]
    block = _make_block(file_url=str(source_path))
    result_bytes = await block._fill_flat_overlay(
        PdfReader(str(source_path)), placements, anchors, tmp_path / "filled.pdf"
    )

    PdfReader(io.BytesIO(result_bytes))
    operations = _flat_text_ops(result_bytes)
    assert len(operations) == 2
    operations_by_value = {operation[4]: operation for operation in operations}
    address_op = operations_by_value["350 Test Street"]
    city_op = operations_by_value["Detroit"]
    scale_x = 612 / 1275
    scale_y = 792 / 1651
    expected_address_x = 285 * scale_x + 6
    expected_address_y = 792 - 384 * scale_y - 1
    assert address_op[1] == pytest.approx(expected_address_x, abs=0.05)
    assert address_op[2] == pytest.approx(expected_address_y, abs=0.05)
    assert address_op[3] == 11
    assert city_op[1] == pytest.approx(138 * scale_x + 6, abs=0.05)
    assert city_op[2] == pytest.approx(792 - 411 * scale_y - 1, abs=0.05)
    assert city_op[3] == 11

    address_rect = (
        address_op[1],
        address_op[2],
        address_op[1] + pdf_fill_block._flat_text_width_pt("350 Test Street", 11),
        address_op[2] + 11,
    )
    city_rect = (
        city_op[1],
        city_op[2],
        city_op[1] + pdf_fill_block._flat_text_width_pt("Detroit", 11),
        city_op[2] + 11,
    )
    assert not (
        address_rect[0] < city_rect[2]
        and address_rect[2] > city_rect[0]
        and address_rect[1] < city_rect[3]
        and address_rect[3] > city_rect[1]
    )
    city_label_rect = (
        city_label.x0 * scale_x,
        792 - city_label.bottom * scale_y,
        city_label.x1 * scale_x,
        792 - city_label.top * scale_y,
    )
    assert not (
        address_rect[0] < city_label_rect[2]
        and address_rect[2] > city_label_rect[0]
        and address_rect[1] < city_label_rect[3]
        and address_rect[3] > city_label_rect[1]
    )


@pytest.mark.asyncio
async def test_flat_overlay_below_rejected_when_it_would_fall_off_page(tmp_path: Path) -> None:
    source_path = _write_blank_flat_pdf(tmp_path)
    dimensions = {"page_width_px": 1275, "page_height_px": 1651}
    anchor = FlatPdfAnchor(
        anchor_id=0, page_index=0, text="Bottom:", x0=88, x1=200, top=1600, bottom=1632, **dimensions
    )
    obstacle = FlatPdfAnchor(anchor_id=1, page_index=0, text="X:", x0=210, x1=280, top=1600, bottom=1632, **dimensions)
    block = _make_block(file_url=str(source_path))
    result_bytes = await block._fill_flat_overlay(
        PdfReader(str(source_path)),
        [FlatPlacement(anchor=anchor, value="W" * 100, position="right")],
        [anchor, obstacle],
        tmp_path / "filled.pdf",
    )

    operation = _flat_text_ops(result_bytes)[0]
    scale_x = 612 / 1275
    scale_y = 792 / 1651
    right_x = anchor.x1 * scale_x + 6
    below_x = anchor.x0 * scale_x + 8
    below_y = (
        792 - anchor.bottom * scale_y - pdf_fill_block.FLAT_FILL_MIN_FONT_SIZE - pdf_fill_block.FLAT_FILL_FONT_SIZE_GAP
    )
    assert operation[1] == pytest.approx(right_x, abs=0.05)
    assert operation[1] != pytest.approx(below_x, abs=0.05)
    assert 0 <= operation[2] <= 792
    assert below_y < 0


@pytest.mark.asyncio
async def test_flat_overlay_right_avoids_obstacle_touching_band_edge(tmp_path: Path) -> None:
    source_path = _write_blank_flat_pdf(tmp_path)
    dimensions = {"page_width_px": 1275, "page_height_px": 1651}
    anchor = FlatPdfAnchor(anchor_id=0, page_index=0, text="Ref:", x0=88, x1=200, top=300, bottom=313, **dimensions)
    obstacle = FlatPdfAnchor(anchor_id=1, page_index=0, text="X:", x0=250, x1=320, top=286, bottom=300, **dimensions)
    value = "WWWWWWWWWW"
    block = _make_block(file_url=str(source_path))
    result_bytes = await block._fill_flat_overlay(
        PdfReader(str(source_path)),
        [FlatPlacement(anchor=anchor, value=value, position="right")],
        [anchor, obstacle],
        tmp_path / "filled.pdf",
    )

    operation = _flat_text_ops(result_bytes)[0]
    scale_x = 612 / 1275
    right_x = anchor.x1 * scale_x + 6
    below_x = anchor.x0 * scale_x + 8
    text_end_x = operation[1] + pdf_fill_block._flat_text_width_pt(value, operation[3])
    assert operation[1] != pytest.approx(right_x, abs=0.05) or operation[3] < 11
    assert operation[1] == pytest.approx(below_x, abs=0.05) or text_end_x <= obstacle.x0 * scale_x


@pytest.mark.asyncio
async def test_flat_overlay_below_detects_tall_obstacle_spanning_label_line(tmp_path: Path) -> None:
    source_path = _write_blank_flat_pdf(tmp_path)
    dimensions = {"page_width_px": 1275, "page_height_px": 1651}
    anchor = FlatPdfAnchor(anchor_id=0, page_index=0, text="Notes:", x0=88, x1=180, top=300, bottom=313, **dimensions)
    obstacle = FlatPdfAnchor(anchor_id=1, page_index=0, text="Box", x0=95, x1=180, top=305, bottom=340, **dimensions)
    block = _make_block(file_url=str(source_path))
    result_bytes = await block._fill_flat_overlay(
        PdfReader(str(source_path)),
        [FlatPlacement(anchor=anchor, value="OK", position="below")],
        [anchor, obstacle],
        tmp_path / "filled.pdf",
    )

    operation = _flat_text_ops(result_bytes)[0]
    scale_x = 612 / 1275
    assert operation[1] == pytest.approx(anchor.x1 * scale_x + 6, abs=0.05)
    assert operation[3] == 11


@pytest.mark.asyncio
async def test_flat_overlay_zero_height_anchor_detects_obstacle(tmp_path: Path) -> None:
    source_path = _write_blank_flat_pdf(tmp_path)
    dimensions = {"page_width_px": 1275, "page_height_px": 1651}
    anchor = FlatPdfAnchor(anchor_id=0, page_index=0, text="Ref:", x0=88, x1=200, top=300, bottom=300, **dimensions)
    obstacle = FlatPdfAnchor(anchor_id=1, page_index=0, text="X:", x0=250, x1=320, top=286, bottom=305, **dimensions)
    value = "WWWWWWWWWW"
    block = _make_block(file_url=str(source_path))
    result_bytes = await block._fill_flat_overlay(
        PdfReader(str(source_path)),
        [FlatPlacement(anchor=anchor, value=value, position="right")],
        [anchor, obstacle],
        tmp_path / "filled.pdf",
    )

    operation = _flat_text_ops(result_bytes)[0]
    scale_x = 612 / 1275
    right_x = anchor.x1 * scale_x + 6
    below_x = anchor.x0 * scale_x + 8
    text_end_x = operation[1] + pdf_fill_block._flat_text_width_pt(value, operation[3])
    assert operation[1] != pytest.approx(right_x, abs=0.05) or operation[3] < 11
    assert operation[1] == pytest.approx(below_x, abs=0.05) or text_end_x <= obstacle.x0 * scale_x


@pytest.mark.asyncio
async def test_flat_overlay_fitting_placement_is_byte_identical(tmp_path: Path) -> None:
    source_path = _write_blank_flat_pdf(tmp_path)
    anchors = _flat_collision_anchors()
    city_label = anchors[4]
    block = _make_block(file_url=str(source_path))
    result_bytes = await block._fill_flat_overlay(
        PdfReader(str(source_path)),
        [FlatPlacement(anchor=city_label, value="Detroit", position="right")],
        anchors,
        tmp_path / "filled.pdf",
    )

    PdfReader(io.BytesIO(result_bytes))
    operations = _flat_text_ops(result_bytes)
    assert len(operations) == 1
    scale_x = 612 / 1275
    scale_y = 792 / 1651
    expected_op = (
        f"BT /SkyvernHelv 11 Tf 0 0 0 rg {138 * scale_x + 6:.1f} {792 - 411 * scale_y - 1:.1f} Td (Detroit) Tj ET"
    )
    assert operations[0][0] == expected_op


@pytest.mark.asyncio
async def test_flat_overlay_shrinks_font_when_neither_position_fits_at_full_size(tmp_path: Path) -> None:
    source_path = _write_blank_flat_pdf(tmp_path)
    anchors = _tight_flat_anchors()
    block = _make_block(file_url=str(source_path))
    value = "ABCDEFGHIJ"
    result_bytes = await block._fill_flat_overlay(
        PdfReader(str(source_path)),
        [FlatPlacement(anchor=anchors[0], value=value, position="right")],
        anchors,
        tmp_path / "filled.pdf",
    )

    PdfReader(io.BytesIO(result_bytes))
    operations = _flat_text_ops(result_bytes)
    assert len(operations) == 1
    operation = operations[0]
    assert 7 <= operation[3] <= 10.5
    scale_x = 612 / 1275
    assert operation[1] + pdf_fill_block._flat_text_width_pt(value, operation[3]) <= 300 * scale_x


@pytest.mark.asyncio
async def test_flat_overlay_shrunk_below_does_not_overblock_later_placement(tmp_path: Path) -> None:
    source_path = _write_blank_flat_pdf(tmp_path)
    dimensions = {"page_width_px": 1275, "page_height_px": 1651}
    first_anchor = FlatPdfAnchor(
        anchor_id=0, page_index=0, text="Primary:", x0=100, x1=150, top=90, bottom=100, **dimensions
    )
    right_blocker = FlatPdfAnchor(
        anchor_id=1, page_index=0, text="Side:", x0=160, x1=200, top=90, bottom=105, **dimensions
    )
    below_blocker = FlatPdfAnchor(
        anchor_id=2, page_index=0, text="Limit:", x0=230, x1=260, top=127, bottom=135, **dimensions
    )
    later_anchor = FlatPdfAnchor(
        anchor_id=3, page_index=0, text="Next:", x0=120, x1=140, top=140, bottom=140, **dimensions
    )
    first_value = "WWWWWWW"
    block = _make_block(file_url=str(source_path))
    result_bytes = await block._fill_flat_overlay(
        PdfReader(str(source_path)),
        [
            FlatPlacement(anchor=first_anchor, value=first_value, position="below"),
            FlatPlacement(anchor=later_anchor, value="OK", position="right"),
        ],
        [first_anchor, right_blocker, below_blocker, later_anchor],
        tmp_path / "filled.pdf",
    )

    operations_by_value = {operation[4]: operation for operation in _flat_text_ops(result_bytes)}
    assert operations_by_value[first_value][3] == 7.5
    scale_x = 612 / 1275
    scale_y = 792 / 1651
    later_op = operations_by_value["OK"]
    assert later_op[1] == pytest.approx(later_anchor.x1 * scale_x + 6, abs=0.05)
    assert later_op[2] == pytest.approx(792 - later_anchor.bottom * scale_y - 1, abs=0.05)
    assert later_op[3] == 11


@pytest.mark.asyncio
async def test_flat_overlay_overflow_falls_back_to_min_font(tmp_path: Path) -> None:
    source_path = _write_blank_flat_pdf(tmp_path)
    anchors = _tight_flat_anchors()
    block = _make_block(file_url=str(source_path))
    result_bytes = await block._fill_flat_overlay(
        PdfReader(str(source_path)),
        [FlatPlacement(anchor=anchors[0], value="W" * 100, position="right")],
        anchors,
        tmp_path / "filled.pdf",
    )

    PdfReader(io.BytesIO(result_bytes))
    operations = _flat_text_ops(result_bytes)
    assert len(operations) == 1
    assert operations[0][3] == 7


@pytest.mark.asyncio
async def test_flat_overlay_overflow_recorded_in_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_id = "run_flat_pdf_overflow"
    _install_context(monkeypatch, run_id)
    flat_pdf = _run_local_pdf_path(monkeypatch, tmp_path, run_id, "flat.pdf")
    _write_flat_pdf(flat_pdf)
    dimensions = {"page_width_px": 600, "page_height_px": 400}
    anchor = FlatPdfAnchor(anchor_id=0, page_index=0, text="Ref:", x0=20, x1=120, top=30, bottom=44, **dimensions)
    obstacle = FlatPdfAnchor(anchor_id=1, page_index=0, text="X:", x0=130, x1=190, top=30, bottom=44, **dimensions)
    value = "W" * 100

    async def _placement_llm_response(**_: Any) -> dict[str, Any]:
        return {"placements": [{"anchor_id": 0, "value": value, "position": "right"}], "thought": "ok"}

    monkeypatch.setattr(PdfFillBlock, "_tesseract_available", staticmethod(lambda: True))
    monkeypatch.setattr(PdfFillBlock, "_extract_flat_anchors", AsyncMock(return_value=[anchor, obstacle]))
    monkeypatch.setattr(PdfFillBlock, "_resolve_default_llm_handler", AsyncMock(return_value=_placement_llm_response))
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.pdf_fill_block.get_path_for_workflow_download_directory",
        lambda workflow_run_id: tmp_path / "downloads" / workflow_run_id,
    )

    block = _make_block(file_url=str(flat_pdf), payload={"reference": value})
    result = await block.execute(workflow_run_id=run_id, workflow_run_block_id="", organization_id=None)

    assert result.success is True
    overflowed_placements = result.output_parameter_value["overflowed_placements"]
    assert overflowed_placements == [
        {
            "anchor_id": anchor.anchor_id,
            "value": value,
            "reason": "Placement overflowed the available space; value may overlap adjacent content",
        }
    ]
    assert "overlap" in overflowed_placements[0]["reason"]


@pytest.mark.asyncio
async def test_flat_overlay_second_below_value_avoids_first(tmp_path: Path) -> None:
    source_path = _write_blank_flat_pdf(tmp_path)
    dimensions = {"page_width_px": 1275, "page_height_px": 1651}
    notes_label = FlatPdfAnchor(
        anchor_id=0, page_index=0, text="Notes:", x0=88, x1=218, top=309, bottom=322, **dimensions
    )
    code_label = FlatPdfAnchor(
        anchor_id=1, page_index=0, text="Code:", x0=494, x1=672, top=309, bottom=322, **dimensions
    )
    anchors = [notes_label, code_label]
    first_value = "A" * 65
    block = _make_block(file_url=str(source_path))
    result_bytes = await block._fill_flat_overlay(
        PdfReader(str(source_path)),
        [
            FlatPlacement(anchor=notes_label, value=first_value, position="below"),
            FlatPlacement(anchor=code_label, value="X7", position="below"),
        ],
        anchors,
        tmp_path / "filled.pdf",
    )

    PdfReader(io.BytesIO(result_bytes))
    operations = _flat_text_ops(result_bytes)
    assert len(operations) == 2
    operations_by_value = {operation[4]: operation for operation in operations}
    first_op = operations_by_value[first_value]
    second_op = operations_by_value["X7"]
    first_rect = (
        first_op[1],
        first_op[2],
        first_op[1] + pdf_fill_block._flat_text_width_pt(first_value, first_op[3]),
        first_op[2] + first_op[3],
    )
    second_rect = (
        second_op[1],
        second_op[2],
        second_op[1] + pdf_fill_block._flat_text_width_pt("X7", second_op[3]),
        second_op[2] + second_op[3],
    )
    assert not (
        first_rect[0] < second_rect[2]
        and first_rect[2] > second_rect[0]
        and first_rect[1] < second_rect[3]
        and first_rect[3] > second_rect[1]
    )
    scale_x = 612 / 1275
    scale_y = 792 / 1651
    assert second_op[1] == pytest.approx(672 * scale_x + 6, abs=0.05)
    assert second_op[2] == pytest.approx(792 - 322 * scale_y - 1, abs=0.05)


@pytest.mark.asyncio
async def test_flat_overlay_font_uses_winansi_encoding(tmp_path: Path) -> None:
    source_path = _write_blank_flat_pdf(tmp_path)
    anchor = FlatPdfAnchor(
        anchor_id=0,
        page_index=0,
        text="Name:",
        x0=88,
        x1=180,
        top=300,
        bottom=313,
        page_width_px=1275,
        page_height_px=1651,
    )
    block = _make_block(file_url=str(source_path))
    result_bytes = await block._fill_flat_overlay(
        PdfReader(str(source_path)),
        [FlatPlacement(anchor=anchor, value="Renée", position="right")],
        [anchor],
        tmp_path / "filled.pdf",
    )

    reader = PdfReader(io.BytesIO(result_bytes))
    font = reader.pages[0]["/Resources"]["/Font"][pdf_fill_block.FLAT_FILL_FONT_RESOURCE].get_object()
    assert font["/Encoding"] == "/WinAnsiEncoding"
    assert font["/BaseFont"] == "/Helvetica"
    operations = _flat_text_ops(result_bytes)
    assert len(operations) == 1
    assert operations[0][4] == "Renée"


def test_resolve_flat_page_layout_emits_exact_coords_for_fitting_value() -> None:
    anchor = FlatPdfAnchor(
        anchor_id=0,
        page_index=0,
        text="Ref:",
        x0=88,
        x1=200,
        top=300,
        bottom=313,
        page_width_px=1275,
        page_height_px=1651,
    )
    block = _make_block()
    resolved = block._resolve_flat_page_layout(
        [FlatPlacement(anchor=anchor, value="OK", position="right")],
        [anchor],
        page_width=612,
        page_height=792,
        origin_x=0,
        origin_y=0,
    )

    assert len(resolved) == 1
    assert resolved[0].x == 200 * (612 / 1275) + 6
    assert resolved[0].y == 792 - 313 * (792 / 1651) - 1
    assert resolved[0].font_size == 11.0


@pytest.mark.asyncio
async def test_flat_overlay_respects_shifted_mediabox(tmp_path: Path) -> None:
    flat_pdf = tmp_path / "shifted.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    page.mediabox.lower_left = (50, 40)
    page.mediabox.upper_right = (350, 240)
    with flat_pdf.open("wb") as f:
        writer.write(f)

    anchor = FlatPdfAnchor(
        anchor_id=0,
        page_index=0,
        text="Name:",
        x0=20,
        x1=120,
        top=30,
        bottom=44,
        page_width_px=600,
        page_height_px=400,
    )
    block = _make_block(file_url=str(flat_pdf))
    output_path = tmp_path / "shifted_filled.pdf"
    await block._fill_flat_overlay(
        PdfReader(str(flat_pdf)),
        [FlatPlacement(anchor=anchor, value="Jane", position="right")],
        [anchor],
        output_path,
    )

    # pdfplumber's coordinate conventions are unreliable for shifted boxes; assert on rendered pixels.
    import io as _io

    from PIL import Image

    from skyvern.forge.sdk.utils.pdf_parser import render_pdf_pages_as_images

    image = Image.open(_io.BytesIO(render_pdf_pages_as_images(str(output_path), resolution=150)[0])).convert("L")
    width, height = image.size
    pixels = image.load()
    dark = [(x, y) for y in range(height) for x in range(width) if pixels[x, y] < 128]
    assert dark, "overlay text did not render inside the page box"
    min_x = min(x for x, _ in dark)
    min_y = min(y for _, y in dark)
    # expected: text starts ~66pt of the 300pt-wide box -> ~0.22 * width; top ~15pt of 200pt -> ~0.075 * height
    assert 0.18 * width <= min_x <= 0.27 * width
    assert 0.04 * height <= min_y <= 0.13 * height


@pytest.mark.asyncio
async def test_flat_overlay_respects_cropbox_distinct_from_mediabox(tmp_path: Path) -> None:
    flat_pdf = tmp_path / "cropped.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=300)
    page.cropbox = RectangleObject((50, 40, 350, 240))
    with flat_pdf.open("wb") as f:
        writer.write(f)

    anchor = FlatPdfAnchor(
        anchor_id=0,
        page_index=0,
        text="Name:",
        x0=20,
        x1=120,
        top=30,
        bottom=44,
        page_width_px=600,
        page_height_px=400,
    )
    block = _make_block(file_url=str(flat_pdf))
    output_path = tmp_path / "cropped_filled.pdf"
    await block._fill_flat_overlay(
        PdfReader(str(flat_pdf)),
        [FlatPlacement(anchor=anchor, value="Jane", position="right")],
        [anchor],
        output_path,
    )

    import io as _io

    from PIL import Image

    from skyvern.forge.sdk.utils.pdf_parser import render_pdf_pages_as_images

    image = Image.open(_io.BytesIO(render_pdf_pages_as_images(str(output_path), resolution=150)[0])).convert("L")
    width, height = image.size
    pixels = image.load()
    dark = [(x, y) for y in range(height) for x in range(width) if pixels[x, y] < 128]
    assert dark, "overlay text did not render inside the cropbox"
    min_x = min(x for x, _ in dark)
    min_y = min(y for _, y in dark)
    # the rendered image covers the 300x200 cropbox; correct mapping puts text at ~66pt/15pt of it
    assert 0.18 * width <= min_x <= 0.27 * width
    assert 0.04 * height <= min_y <= 0.13 * height


def test_sanitize_placements_filters_invalid_entries() -> None:
    anchor = FlatPdfAnchor(
        anchor_id=3,
        page_index=0,
        text="Name:",
        x0=0,
        x1=50,
        top=0,
        bottom=10,
        page_width_px=600,
        page_height_px=400,
    )
    block = _make_block()
    placements, skipped = block._sanitize_placements(
        {
            "placements": [
                {"anchor_id": 3, "value": "Jane", "position": "sideways"},
                {"anchor_id": 99, "value": "ignored"},
                {"anchor_id": "not-an-int", "value": "ignored"},
                {"anchor_id": 3, "value": "duplicate anchor"},
                "not-a-dict",
            ]
        },
        [anchor],
    )

    assert len(placements) == 1
    assert placements[0].value == "Jane"
    assert placements[0].position == "right"
    assert len(skipped) == 4
    assert any(item.get("reason") == "Duplicate placement for the same anchor" for item in skipped)


def test_sanitize_placements_rejects_non_latin1_values() -> None:
    anchor = FlatPdfAnchor(
        anchor_id=1,
        page_index=0,
        text="Name:",
        x0=0,
        x1=50,
        top=0,
        bottom=10,
        page_width_px=600,
        page_height_px=400,
    )
    block = _make_block()
    placements, skipped = block._sanitize_placements(
        {"placements": [{"anchor_id": 1, "value": "名前テスト"}]},
        [anchor],
    )

    assert placements == []
    assert "cannot render" in skipped[0]["reason"]


def test_parse_tesseract_tsv_skips_malformed_numeric_rows() -> None:
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "1\t1\t0\t0\t0\t0\t0\t0\t600\t400\t-1\t\n"
        "5\t1\t1\t1\t1\t1\tgarbage\t30\t60\t14\t90.5\tBroken\n"
        "5\t1\t1\t1\t2\t1\t20\t60\t40\t14\t91.0\tCity:\n"
    )
    anchors = PdfFillBlock._parse_tesseract_tsv(tsv, page_index=0, id_offset=0)

    assert [a.text for a in anchors] == ["City:"]


def test_tesseract_command_includes_configured_languages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_fill_block, "FLAT_FILL_OCR_LANGUAGES", "eng+spa+fra")

    assert PdfFillBlock._tesseract_command("/tmp/page.png") == [
        "tesseract",
        "/tmp/page.png",
        "stdout",
        "-l",
        "eng+spa+fra",
        "tsv",
    ]


def test_tesseract_command_allows_empty_language_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_fill_block, "FLAT_FILL_OCR_LANGUAGES", "")

    assert PdfFillBlock._tesseract_command("/tmp/page.png") == ["tesseract", "/tmp/page.png", "stdout", "tsv"]


def test_tesseract_language_helpers_share_package_defaults() -> None:
    language_packs = ("eng", "spa", "chi-sim", "chi-tra")

    assert tesseract_ocr_packages(language_packs) == [
        "tesseract-ocr-eng",
        "tesseract-ocr-spa",
        "tesseract-ocr-chi-sim",
        "tesseract-ocr-chi-tra",
    ]
    assert tesseract_language_arg(language_packs) == "eng+spa+chi_sim+chi_tra"


def test_escape_pdf_text() -> None:
    assert PdfFillBlock._escape_pdf_text("A (NM) \\ B\nC") == r"A \(NM\) \\ B C"


def test_parse_tesseract_tsv_groups_lines() -> None:
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "1\t1\t0\t0\t0\t0\t0\t0\t600\t400\t-1\t\n"
        "5\t1\t1\t1\t1\t1\t20\t30\t60\t14\t90.5\tEnrollee\n"
        "5\t1\t1\t1\t1\t2\t85\t31\t35\t13\t88.0\tname:\n"
        "5\t1\t1\t1\t1\t3\t300\t30\t40\t14\t89.0\tDate:\n"
        "5\t1\t1\t1\t2\t1\t20\t60\t40\t14\t91.0\tCity:\n"
        "5\t1\t1\t1\t2\t2\t70\t60\t30\t14\t10.0\tnoise\n"
    )
    anchors = PdfFillBlock._parse_tesseract_tsv(tsv, page_index=1, id_offset=5)

    # the table-cell gap between "name:" (ends at 120) and "Date:" (starts at 300) splits the OCR line
    assert [a.text for a in anchors] == ["Enrollee name:", "Date:", "City:"]
    first = anchors[0]
    assert (first.anchor_id, first.page_index) == (5, 1)
    assert (first.x0, first.x1, first.top, first.bottom) == (20, 120, 30, 44)
    assert (first.page_width_px, first.page_height_px) == (600, 400)
    assert anchors[1].x0 == 300


@pytest.mark.asyncio
async def test_loop_iterations_get_unique_filenames(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_id = "run_unique_filename"
    _install_context(monkeypatch, run_id)
    source_pdf = _run_local_pdf_path(monkeypatch, tmp_path, run_id, "source.pdf")
    _write_fillable_pdf(source_pdf)
    monkeypatch.setattr(PdfFillBlock, "_resolve_default_llm_handler", AsyncMock(return_value=_fake_llm_response))
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.pdf_fill_block.get_path_for_workflow_download_directory",
        lambda workflow_run_id: tmp_path / "downloads" / workflow_run_id,
    )
    block = _make_block(file_url=str(source_pdf), payload={"name": "Jane"})

    first = await block.execute(workflow_run_id=run_id, workflow_run_block_id="wrb_iter_0", organization_id=None)
    second = await block.execute(workflow_run_id=run_id, workflow_run_block_id="wrb_iter_1", organization_id=None)

    assert first.output_parameter_value["file_name"] == "fill_pdf_wrb_iter_0_filled.pdf"
    assert second.output_parameter_value["file_name"] == "fill_pdf_wrb_iter_1_filled.pdf"
    assert first.output_parameter_value["file_path"] != second.output_parameter_value["file_path"]


def test_pdf_fill_yaml_to_block_conversion() -> None:
    yaml_block = PdfFillBlockYAML(
        block_type=BlockType.PDF_FILL,
        label="fill_pdf",
        file_url="{{ source_pdf }}",
        prompt="Fill using payload",
        payload={"name": "{{ applicant.name }}"},
        llm_key="{{ llm_key }}",
        parameter_keys=[],
    )
    block = block_yaml_to_block(yaml_block, {"fill_pdf_output": _make_output_parameter()})

    assert isinstance(block, PdfFillBlock)
    assert block.file_url == "{{ source_pdf }}"
    assert block.prompt == "Fill using payload"
    assert block.payload == {"name": "{{ applicant.name }}"}
    assert block.llm_key == "{{ llm_key }}"
