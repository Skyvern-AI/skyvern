from __future__ import annotations

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
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
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
    source_pdf = tmp_path / "source.pdf"
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
    source_pdf = tmp_path / "source.pdf"
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
async def test_local_path_rejected_outside_local_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_id = "run_non_local_path"
    _install_context(monkeypatch, run_id)
    source_pdf = tmp_path / "source.pdf"
    _write_fillable_pdf(source_pdf)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.pdf_fill_block.settings.ENV", "production")

    block = _make_block(file_url=str(source_pdf))
    result = await block.execute(workflow_run_id=run_id, workflow_run_block_id="", organization_id=None)

    assert result.success is False
    assert result.status == BlockStatus.failed


@pytest.mark.asyncio
async def test_flat_pdf_without_tesseract_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_id = "run_flat_pdf_no_ocr"
    _install_context(monkeypatch, run_id)
    flat_pdf = tmp_path / "flat.pdf"
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
    flat_pdf = tmp_path / "flat.pdf"
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
    big_pdf = tmp_path / "big.pdf"
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
    source_pdf = tmp_path / "source.pdf"
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
