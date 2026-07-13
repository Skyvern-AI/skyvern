"""End-to-end smoke tests for the pdf_fill block: YAML definition -> converter -> execute.

External boundaries (LLM, tesseract subprocess) are faked; PDF parsing, templating,
sanitization, filling, and overlay rendering all run for real.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pdfplumber
import pytest
from pypdf import PdfReader

from skyvern.forge.sdk.workflow.models.block import PdfFillBlock
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.workflows import BlockStatus, PdfFillBlockYAML, WorkflowDefinitionYAML
from tests.unit.fake_workflow_run_context import FakeWorkflowRunContext
from tests.unit.test_pdf_fill_block import _write_fillable_pdf, _write_flat_pdf


class SmokePdfFillContext(FakeWorkflowRunContext):
    organization_id = None

    def has_parameter(self, key: str) -> bool:
        return key in self.values

    def get_value(self, key: str) -> Any:
        return self.values[key]

    def resolve_effective_workflow_system_prompt(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def record_block_workflow_system_prompt(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def register_output_parameter_value_post_execution(self, parameter: Any, value: Any) -> None:
        self.set_value(parameter.key, value)


def _build_pdf_fill_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **yaml_overrides: Any) -> PdfFillBlock:
    yaml_kwargs: dict[str, Any] = {
        "block_type": "pdf_fill",
        "label": "fill_form",
        "file_url": "{{ source_pdf }}",
        "prompt": "Fill the form from the payload.",
        "payload": {"name": "Jane A Doe"},
    }
    yaml_kwargs.update(yaml_overrides)
    definition = convert_workflow_definition(
        WorkflowDefinitionYAML(parameters=[], blocks=[PdfFillBlockYAML(**yaml_kwargs)]),
        workflow_id="wf_pdf_fill_smoke",
    )
    block = definition.blocks[0]
    assert isinstance(block, PdfFillBlock)
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.pdf_fill_block.get_path_for_workflow_download_directory",
        lambda workflow_run_id: tmp_path / "downloads" / workflow_run_id,
    )
    return block


def _run_local_pdf_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: str, name: str) -> Path:
    # Local source paths must resolve inside the run's download directory, or _resolve_source_pdf's
    # path-containment check rejects them.
    download_root = tmp_path / "downloads"
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.pdf_fill_block.settings.DOWNLOAD_PATH", str(download_root))
    run_dir = download_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir / name


def _install_context(monkeypatch: pytest.MonkeyPatch, values: dict[str, Any]) -> SmokePdfFillContext:
    context = SmokePdfFillContext(values=values)
    monkeypatch.setattr(PdfFillBlock, "get_workflow_run_context", staticmethod(lambda _wr_id: context))
    mock_app = MagicMock()
    mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()
    mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.pdf_fill_block.app", mock_app)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.app", mock_app)
    return context


@pytest.mark.asyncio
async def test_acroform_fill_end_to_end_through_converter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source_pdf = _run_local_pdf_path(monkeypatch, tmp_path, "wr_smoke_acroform", "form.pdf")
    _write_fillable_pdf(source_pdf)

    async def _llm_response(**_: Any) -> dict[str, Any]:
        return {"fields": {"name": "Jane A Doe", "subscribe": True}, "thought": "mapped"}

    block = _build_pdf_fill_block(monkeypatch, tmp_path)
    context = _install_context(monkeypatch, values={"source_pdf": str(source_pdf)})
    monkeypatch.setattr(PdfFillBlock, "_resolve_default_llm_handler", AsyncMock(return_value=_llm_response))

    result = await block.execute(workflow_run_id="wr_smoke_acroform", workflow_run_block_id="", organization_id=None)

    assert result.success is True
    assert result.status == BlockStatus.completed
    output = result.output_parameter_value
    assert output["fill_mode"] == "acroform"
    filled_fields = PdfReader(output["file_path"]).get_fields()
    assert filled_fields["name"]["/V"] == "Jane A Doe"
    assert filled_fields["subscribe"]["/V"] == "/Yes"
    assert context.values["fill_form_output"]["fill_mode"] == "acroform"


@pytest.mark.asyncio
async def test_flat_fill_end_to_end_with_fake_tesseract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source_pdf = _run_local_pdf_path(monkeypatch, tmp_path, "wr_smoke_flat", "flat.pdf")
    _write_flat_pdf(source_pdf)

    # The 300x200pt flat fixture renders to 625x417px at 150dpi; the TSV mirrors a real tesseract run.
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "1\t1\t0\t0\t0\t0\t0\t0\t625\t417\t-1\t\n"
        "5\t1\t1\t1\t1\t1\t40\t60\t60\t14\t92.0\tName:\n"
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return tsv.encode(), b""

        def kill(self) -> None:
            pass

        async def wait(self) -> int:
            return 0

    async def fake_subprocess(*_args: Any, **_kwargs: Any) -> FakeProcess:
        return FakeProcess()

    async def _llm_response(**_: Any) -> dict[str, Any]:
        return {"placements": [{"anchor_id": 0, "value": "Jane A Doe", "position": "right"}], "thought": "placed"}

    block = _build_pdf_fill_block(monkeypatch, tmp_path)
    context = _install_context(monkeypatch, values={"source_pdf": str(source_pdf)})
    monkeypatch.setattr(PdfFillBlock, "_tesseract_available", staticmethod(lambda: True))
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.pdf_fill_block.asyncio.create_subprocess_exec", fake_subprocess
    )
    monkeypatch.setattr(PdfFillBlock, "_resolve_default_llm_handler", AsyncMock(return_value=_llm_response))

    result = await block.execute(workflow_run_id="wr_smoke_flat", workflow_run_block_id="", organization_id=None)

    assert result.success is True
    output = result.output_parameter_value
    assert output["fill_mode"] == "flat_overlay"
    assert output["fields"] == {"Name:": "Jane A Doe"}
    with pdfplumber.open(output["file_path"]) as pdf:
        words = {w["text"] for w in pdf.pages[0].extract_words()}
    assert "Jane" in words
    assert context.values["fill_form_output"]["fill_mode"] == "flat_overlay"


@pytest.mark.asyncio
async def test_file_url_unwraps_upstream_block_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source_pdf = _run_local_pdf_path(monkeypatch, tmp_path, "wr_smoke_chained", "form.pdf")
    _write_fillable_pdf(source_pdf)

    async def _llm_response(**_: Any) -> dict[str, Any]:
        return {"fields": {"name": "Jane A Doe"}, "thought": "mapped"}

    block = _build_pdf_fill_block(monkeypatch, tmp_path, file_url="downloader_output")
    _install_context(
        monkeypatch,
        values={"downloader_output": {"downloaded_files": [{"url": str(source_pdf)}]}},
    )
    monkeypatch.setattr(PdfFillBlock, "_resolve_default_llm_handler", AsyncMock(return_value=_llm_response))

    result = await block.execute(workflow_run_id="wr_smoke_chained", workflow_run_block_id="", organization_id=None)

    assert result.success is True
    assert PdfReader(result.output_parameter_value["file_path"]).get_fields()["name"]["/V"] == "Jane A Doe"
