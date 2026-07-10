from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any, Literal, cast

import structlog
from pypdf import PdfReader, PdfWriter

from skyvern.config import settings
from skyvern.constants import MAX_FILE_PARSE_INPUT_TOKENS
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
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.utils.pdf_parser import validate_pdf_file
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import (
    Block,
    capture_block_download_baseline,
    extract_file_url_from_block_output,
    sanitize_filename,
)
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType
from skyvern.utils.token_counter import count_tokens

LOG = structlog.get_logger()

MIN_PER_PAGE_TOKENS = 256


class SplitPdfBlock(Block):
    block_type: Literal[BlockType.SPLIT_PDF] = BlockType.SPLIT_PDF  # type: ignore

    file_url: str
    prompt: str
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
            "Secondary LLM handler not configured; falling back to primary handler for SplitPdfBlock",
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
                "SplitPdfBlock LLM response was not a JSON object",
                expected_key=expected_key,
                response_type=type(parsed).__name__,
            )
            return {}
        if expected_key not in parsed:
            LOG.warning(
                "SplitPdfBlock LLM response missing expected key",
                expected_key=expected_key,
                response_keys=list(parsed.keys()),
            )
        return parsed

    def _build_page_texts(self, reader: PdfReader) -> list[dict[str, int | str]]:
        # Give every page a slice of the budget and truncate over-long pages, instead of blanking
        # all pages once a running total is hit: the planner needs some text from every page to
        # assign it to a document, and a single huge page must not blank everything after it.
        total_pages = len(reader.pages) or 1
        per_page_token_budget = max(MIN_PER_PAGE_TOKENS, MAX_FILE_PARSE_INPUT_TOKENS // total_pages)
        page_texts: list[dict[str, int | str]] = []
        for page_index, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text and count_tokens(text) > per_page_token_budget:
                # ~4 chars/token heuristic; document-type/boundary cues sit near the top of a page.
                text = text[: per_page_token_budget * 4]
            page_texts.append({"page_number": page_index + 1, "text": text})
        return page_texts

    async def _request_split_plan(
        self,
        page_texts: list[dict[str, int | str]],
        total_pages: int,
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
            "split-pdf",
            user_prompt=self.prompt,
            total_pages=total_pages,
            pages=page_texts,
        )
        response = await llm_api_handler(
            prompt=prompt,
            prompt_name="split-pdf",
            system_prompt=self.workflow_system_prompt,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )
        return self._parse_llm_json_response(response, expected_key="documents")

    @staticmethod
    def _sanitize_relative_folder(folder: Any) -> str:
        if folder is None:
            return ""

        segments: list[str] = []
        for segment in str(folder).replace("\\", "/").split("/"):
            if segment in {"", ".", ".."}:
                continue
            sanitized = sanitize_filename(segment, default="")
            if sanitized and sanitized not in {".", ".."}:
                segments.append(sanitized)
        return "/".join(segments)

    @staticmethod
    def _deduplicate_document_name(folder: str, name: str, used_paths: set[tuple[str, str]]) -> str:
        if (folder, name) not in used_paths:
            return name

        suffix = Path(name).suffix or ".pdf"
        stem = Path(name).stem or "document"
        counter = 2
        while True:
            candidate = f"{stem}_{counter}{suffix}"
            if (folder, candidate) not in used_paths:
                return candidate
            counter += 1

    @staticmethod
    def _sanitize_split_plan(
        raw_response: dict[str, Any],
        total_pages: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        raw_documents = raw_response.get("documents")
        if not isinstance(raw_documents, list) or not raw_documents:
            return [], [{"reason": "LLM response did not include a non-empty documents list"}]

        documents: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        used_paths: set[tuple[str, str]] = set()
        for idx, raw_document in enumerate(raw_documents):
            if not isinstance(raw_document, dict):
                skipped.append({"index": idx, "reason": "Document entry is not an object", "value": raw_document})
                continue

            start_page = raw_document.get("start_page")
            end_page = raw_document.get("end_page")
            if (
                not isinstance(start_page, int)
                or isinstance(start_page, bool)
                or not isinstance(end_page, int)
                or isinstance(end_page, bool)
            ):
                skipped.append(
                    {
                        "index": idx,
                        "reason": "start_page and end_page must be integers",
                        "start_page": start_page,
                        "end_page": end_page,
                    }
                )
                continue

            if start_page < 1 or end_page < start_page or end_page > total_pages:
                skipped.append(
                    {
                        "index": idx,
                        "reason": "Page range must satisfy 1 <= start_page <= end_page <= total_pages",
                        "start_page": start_page,
                        "end_page": end_page,
                        "total_pages": total_pages,
                    }
                )
                continue

            raw_name = raw_document.get("name")
            name = sanitize_filename("" if raw_name is None else str(raw_name), default=f"document_{idx + 1}")
            if not name.lower().endswith(".pdf"):
                name = f"{name}.pdf"
            folder = SplitPdfBlock._sanitize_relative_folder(raw_document.get("folder"))
            name = SplitPdfBlock._deduplicate_document_name(folder, name, used_paths)
            used_paths.add((folder, name))
            documents.append({"name": name, "folder": folder, "start_page": start_page, "end_page": end_page})

        return documents, skipped

    @staticmethod
    def _write_split_documents(
        reader: PdfReader,
        documents: list[dict[str, Any]],
        base_dir: Path,
    ) -> list[tuple[dict[str, Any], bytes]]:
        written: list[tuple[dict[str, Any], bytes]] = []
        for document in documents:
            start_page = cast(int, document["start_page"])
            end_page = cast(int, document["end_page"])
            writer = PdfWriter()
            for page_index in range(start_page - 1, end_page):
                writer.add_page(reader.pages[page_index])

            buffer = io.BytesIO()
            writer.write(buffer)
            pdf_bytes = buffer.getvalue()

            folder = cast(str, document.get("folder") or "")
            output_dir = base_dir.joinpath(*folder.split("/")) if folder else base_dir
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / cast(str, document["name"])
            output_path.write_bytes(pdf_bytes)

            doc_meta = dict(document)
            doc_meta.update(
                {
                    "file_path": str(output_path),
                    "file_name": document["name"],
                    "file_size": len(pdf_bytes),
                    "page_range": [start_page, end_page],
                    "page_count": end_page - start_page + 1,
                }
            )
            written.append((doc_meta, pdf_bytes))
        return written

    async def _resolve_workflow_run_block(
        self, workflow_run_block_id: str, organization_id: str | None
    ) -> WorkflowRunBlock | None:
        if not organization_id:
            return None
        try:
            return await app.DATABASE.observer.get_workflow_run_block(
                workflow_run_block_id, organization_id=organization_id
            )
        except NotFoundError:
            return None

    async def _upload_pdf_artifact(
        self,
        *,
        workflow_run_block: WorkflowRunBlock | None,
        pdf_bytes: bytes,
        workflow_run_id: str,
        organization_id: str | None,
    ) -> tuple[str | None, str | None, str | None]:
        # Uploads the split PDF's bytes as a block artifact (reaches S3 regardless of the on-disk
        # subfolder), unlike save_downloaded_files which only walks the top level of the run dir.
        if workflow_run_block is None or not organization_id:
            return None, None, None

        artifact_id, artifact_uri = await app.ARTIFACT_MANAGER.create_workflow_run_block_artifact_with_uri(
            workflow_run_block=workflow_run_block,
            artifact_type=ArtifactType.PDF,
            data=pdf_bytes,
        )
        try:
            await app.ARTIFACT_MANAGER.wait_for_upload_aiotasks([workflow_run_block.workflow_run_block_id])
        except Exception:
            LOG.warning("SplitPdfBlock failed to upload PDF artifact", workflow_run_id=workflow_run_id, exc_info=True)
            return None, None, None

        artifact_url = None
        try:
            artifact = await app.DATABASE.artifacts.get_artifact_by_id(artifact_id, organization_id=organization_id)
            if artifact:
                artifact_url = await app.ARTIFACT_MANAGER.get_share_link(artifact)
        except Exception:
            LOG.warning(
                "SplitPdfBlock failed to generate artifact download URL", artifact_id=artifact_id, exc_info=True
            )
        return artifact_id, artifact_uri, artifact_url

    @staticmethod
    def _document_output(meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": meta["name"],
            "folder": meta["folder"],
            "page_range": meta["page_range"],
            "page_count": meta["page_count"],
            "file_path": meta["file_path"],
            "file_name": meta["file_name"],
            "file_size": meta["file_size"],
            "artifact_id": meta.get("artifact_id"),
            "artifact_uri": meta.get("artifact_uri"),
            "artifact_url": meta.get("artifact_url"),
        }

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
                total_pages = len(reader.pages)
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

            try:
                page_texts = self._build_page_texts(reader)
                raw_response = await self._request_split_plan(
                    page_texts,
                    total_pages,
                    workflow_run_id,
                    workflow_run_block_id,
                    organization_id,
                )
                documents, skipped = self._sanitize_split_plan(raw_response, total_pages)
                if not documents:
                    skipped_summary = "; ".join(item.get("reason", "") for item in skipped[:5])
                    return await self._record_failure(
                        workflow_run_context,
                        workflow_run_id,
                        workflow_run_block_id,
                        organization_id,
                        "Split PDF could not produce any valid output documents. "
                        f"Skipped: {skipped_summary or 'no documents proposed'}",
                        output_parameter_value={"documents": [], "skipped": skipped},
                    )

                base_dir = get_path_for_workflow_download_directory(workflow_run_id)
                written = self._write_split_documents(reader, documents, base_dir)

                artifact_org_id = organization_id or workflow_run_context.organization_id
                artifact_block = await self._resolve_workflow_run_block(workflow_run_block_id, artifact_org_id)
                downloaded_files: list[FileInfo] = []
                for meta, pdf_bytes in written:
                    artifact_id, artifact_uri, artifact_url = await self._upload_pdf_artifact(
                        workflow_run_block=artifact_block,
                        pdf_bytes=pdf_bytes,
                        workflow_run_id=workflow_run_id,
                        organization_id=artifact_org_id,
                    )
                    meta["artifact_id"] = artifact_id
                    meta["artifact_uri"] = artifact_uri
                    meta["artifact_url"] = artifact_url
                    if artifact_url:
                        downloaded_files.append(
                            FileInfo(
                                url=artifact_url,
                                filename=meta["file_name"],
                                file_size=meta["file_size"],
                                artifact_id=artifact_id,
                            )
                        )

                output = {
                    "documents": [self._document_output(meta) for meta, _ in written],
                    "document_count": len(written),
                    "skipped": skipped,
                    "source_page_count": total_pages,
                    "downloaded_files": [fi.model_dump() for fi in downloaded_files],
                    "downloaded_file_urls": [fi.url for fi in downloaded_files],
                    "downloaded_file_artifact_ids": [fi.artifact_id for fi in downloaded_files if fi.artifact_id],
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
            except Exception as e:
                return await self._record_failure(
                    workflow_run_context,
                    workflow_run_id,
                    workflow_run_block_id,
                    organization_id,
                    f"Split PDF failed: {str(e)}",
                )
        finally:
            if source_is_temp and os.path.exists(source_pdf_path):
                try:
                    os.remove(source_pdf_path)
                except OSError:
                    LOG.warning("SplitPdfBlock failed to clean up downloaded source PDF", path=source_pdf_path)
