from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter

from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.models.split_pdf_block import SplitPdfBlock
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.workflows import BlockType, SplitPdfBlockYAML


def _make_output_parameter(label: str = "split_pdf") -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        output_parameter_id=f"{label}_output_id",
        workflow_id="workflow-id",
        key=f"{label}_output",
        created_at=now,
        modified_at=now,
    )


def _write_blank_pdf(path: Path, page_count: int) -> None:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=300, height=400)
    with path.open("wb") as f:
        writer.write(f)


def test_sanitize_split_plan_accepts_valid_documents_and_preserves_order() -> None:
    raw_response = {
        "documents": [
            {"name": "first", "folder": "packets", "start_page": 1, "end_page": 2},
            {"name": "second.pdf", "folder": "", "start_page": 3, "end_page": 3},
        ]
    }

    documents, skipped = SplitPdfBlock._sanitize_split_plan(raw_response, total_pages=3)

    assert skipped == []
    assert documents == [
        {"name": "first.pdf", "folder": "packets", "start_page": 1, "end_page": 2},
        {"name": "second.pdf", "folder": "", "start_page": 3, "end_page": 3},
    ]


def test_sanitize_split_plan_skips_invalid_page_ranges() -> None:
    raw_response: dict[str, Any] = {
        "documents": [
            {"name": "zero", "folder": "", "start_page": 0, "end_page": 1},
            {"name": "reversed", "folder": "", "start_page": 3, "end_page": 2},
            {"name": "too-large", "folder": "", "start_page": 1, "end_page": 4},
            {"name": "not-int", "folder": "", "start_page": "1", "end_page": 2},
            {"name": "valid", "folder": "", "start_page": 2, "end_page": 2},
        ]
    }

    documents, skipped = SplitPdfBlock._sanitize_split_plan(raw_response, total_pages=3)

    assert documents == [{"name": "valid.pdf", "folder": "", "start_page": 2, "end_page": 2}]
    assert len(skipped) == 4
    assert all("reason" in item for item in skipped)


def test_sanitize_split_plan_sanitizes_paths_and_deduplicates_names() -> None:
    raw_response = {
        "documents": [
            {"name": "summary/report", "folder": "/../packets/./2026/..", "start_page": 1, "end_page": 1},
            {"name": "summary_report.pdf", "folder": "packets/2026", "start_page": 2, "end_page": 2},
            {"name": "summary_report", "folder": "packets/2026", "start_page": 3, "end_page": 3},
        ]
    }

    documents, skipped = SplitPdfBlock._sanitize_split_plan(raw_response, total_pages=3)

    assert skipped == []
    assert documents == [
        {"name": "summary_report.pdf", "folder": "packets/2026", "start_page": 1, "end_page": 1},
        {"name": "summary_report_2.pdf", "folder": "packets/2026", "start_page": 2, "end_page": 2},
        {"name": "summary_report_3.pdf", "folder": "packets/2026", "start_page": 3, "end_page": 3},
    ]


def test_sanitize_split_plan_rejects_missing_documents_list() -> None:
    assert SplitPdfBlock._sanitize_split_plan({}, total_pages=5)[0] == []
    assert SplitPdfBlock._sanitize_split_plan({"documents": []}, total_pages=5)[0] == []
    assert SplitPdfBlock._sanitize_split_plan({"documents": "nope"}, total_pages=5)[0] == []

    for raw_response in ({}, {"documents": []}, {"documents": "nope"}):
        documents, skipped = SplitPdfBlock._sanitize_split_plan(raw_response, total_pages=5)
        assert documents == []
        assert skipped
        assert "reason" in skipped[0]


def test_write_split_documents_writes_expected_pdf_ranges(tmp_path: Path) -> None:
    source_pdf = tmp_path / "source.pdf"
    _write_blank_pdf(source_pdf, page_count=5)
    reader = PdfReader(source_pdf)
    documents = [
        {"name": "first.pdf", "folder": "packet_a", "start_page": 1, "end_page": 2},
        {"name": "second.pdf", "folder": "packet_b/nested", "start_page": 3, "end_page": 5},
    ]

    written = SplitPdfBlock._write_split_documents(reader, documents, tmp_path / "downloads")

    assert len(written) == 2
    for meta, pdf_bytes in written:
        output_path = Path(meta["file_path"])
        assert output_path.exists()
        assert pdf_bytes == output_path.read_bytes()
        assert meta["file_name"] == meta["name"]
        assert meta["file_size"] == len(pdf_bytes)
        assert meta["page_range"] == [meta["start_page"], meta["end_page"]]
        assert meta["page_count"] == meta["end_page"] - meta["start_page"] + 1
        assert len(PdfReader(output_path).pages) == meta["page_count"]

    assert Path(written[0][0]["file_path"]).relative_to(tmp_path / "downloads") == Path("packet_a/first.pdf")
    assert Path(written[1][0]["file_path"]).relative_to(tmp_path / "downloads") == Path("packet_b/nested/second.pdf")


def test_split_pdf_yaml_to_block_conversion() -> None:
    yaml_block = SplitPdfBlockYAML(
        block_type=BlockType.SPLIT_PDF,
        label="split_pdf",
        file_url="{{ source_pdf }}",
        prompt="Split by document.",
        llm_key="{{ llm_key }}",
        parameter_keys=[],
    )
    block = block_yaml_to_block(yaml_block, {"split_pdf_output": _make_output_parameter()})

    assert isinstance(block, SplitPdfBlock)
    assert block.file_url == "{{ source_pdf }}"
    assert block.prompt == "Split by document."
    assert block.llm_key == "{{ llm_key }}"
