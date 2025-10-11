import os
import re
import tempfile
from typing import Any

import structlog
from fastapi import HTTPException, UploadFile
from pypdf import PdfReader

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest

LOG = structlog.get_logger(__name__)


class PDFImportService:
    @staticmethod
    def _sanitize_workflow_json(raw: dict[str, Any]) -> dict[str, Any]:
        """Clean LLM JSON to match Skyvern schema conventions and avoid Jinja errors.

        - Replace Jinja refs like {{workflow.foo}} or {{parameters.foo}} with {{foo}}
        - Auto-populate block.parameter_keys with any referenced parameter keys
        - Ensure all block labels are unique by appending indices to duplicates
        """

        def strip_prefixes(text: str) -> tuple[str, set[str]]:
            # Replace {{ workflow.xxx }} and {{ parameters.xxx }} with {{ xxx }}
            cleaned = text
            cleaned = re.sub(r"\{\{\s*workflow\.([a-zA-Z0-9_\.]+)\s*\}\}", r"{{ \1 }}", cleaned)
            cleaned = re.sub(r"\{\{\s*parameters\.([a-zA-Z0-9_\.]+)\s*\}\}", r"{{ \1 }}", cleaned)

            # Collect jinja variable names (take first segment before any dot)
            used: set[str] = set()
            for match in re.finditer(r"\{\{\s*([^\}\s\|]+)\s*[^}]*\}\}", cleaned):
                var = match.group(1)
                # Use base segment before dot to match parameter keys
                base = var.split(".")[0]
                used.add(base)
            return cleaned, used

        workflow_def = raw.get("workflow_definition", {})
        param_defs = workflow_def.get("parameters", []) or []
        param_keys = {p.get("key") for p in param_defs if isinstance(p, dict) and p.get("key")}

        blocks = workflow_def.get("blocks", []) or []

        # First pass: deduplicate block labels
        seen_labels: dict[str, int] = {}
        deduplicated_count = 0
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            label = blk.get("label", "")
            if not label:
                continue

            if label in seen_labels:
                # This label has been seen before, append index
                seen_labels[label] += 1
                new_label = f"{label}_{seen_labels[label]}"
                LOG.info(
                    "Deduplicating block label",
                    original_label=label,
                    new_label=new_label,
                    occurrence=seen_labels[label],
                )
                blk["label"] = new_label
                deduplicated_count += 1
            else:
                # First time seeing this label
                seen_labels[label] = 1

        if deduplicated_count > 0:
            LOG.info(
                "Deduplicated block labels",
                total_deduplicated=deduplicated_count,
                duplicate_labels=sorted([label for label, count in seen_labels.items() if count > 1]),
            )
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            referenced: set[str] = set()
            # Fields that commonly contain Jinja
            for field in [
                "url",
                "navigation_goal",
                "data_extraction_goal",
                "complete_criterion",
                "terminate_criterion",
                "title",
            ]:
                val = blk.get(field)
                if isinstance(val, str):
                    cleaned, used = strip_prefixes(val)
                    blk[field] = cleaned
                    referenced.update(used)

            # Ensure required fields for text_prompt blocks
            if blk.get("block_type") == "text_prompt":
                if not blk.get("prompt"):
                    # Prefer an instruction-bearing field if present
                    blk["prompt"] = (
                        blk.get("navigation_goal")
                        or blk.get("title")
                        or blk.get("label")
                        or "Provide the requested text response."
                    )
                # Track jinja usage within the prompt
                prompt_val = blk.get("prompt")
                if isinstance(prompt_val, str):
                    cleaned, used = strip_prefixes(prompt_val)
                    blk["prompt"] = cleaned
                    referenced.update(used)

            # parameter_keys should include only known parameter keys
            if param_keys:
                keys_to_include = sorted(k for k in referenced if k in param_keys)
                if keys_to_include:
                    blk["parameter_keys"] = keys_to_include

            # Ensure engine where needed
            if blk.get("block_type") in {"navigation", "action", "extraction", "login", "file_download"}:
                blk.setdefault("engine", "skyvern-1.0")

            # Ensure url exists (can be empty string)
            if blk.get("block_type") in {"navigation", "action", "extraction", "file_download"}:
                if blk.get("url") is None:
                    blk["url"] = ""

        return raw

    async def import_workflow_from_pdf(self, file: UploadFile, organization: Organization) -> dict[str, Any]:
        LOG.info("Starting PDF import", filename=file.filename, organization_id=organization.organization_id)

        if not file.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Only PDF files are supported.")

        # Save the uploaded file to a temporary location
        LOG.info("Saving PDF to temporary file", filename=file.filename)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
            temp_file.write(await file.read())
            temp_file_path = temp_file.name

        try:
            # Extract text from PDF
            LOG.info("Extracting text from PDF", filename=file.filename, temp_file=temp_file_path)
            reader = PdfReader(temp_file_path)
            sop_text = ""
            for page_num, page in enumerate(reader.pages, 1):
                page_text = page.extract_text()
                sop_text += page_text + "\n"
                LOG.debug("Extracted text from page", page=page_num, text_length=len(page_text))

            LOG.info(
                "PDF text extraction complete",
                total_text_length=len(sop_text),
                organization_id=organization.organization_id,
            )

            if not sop_text.strip():
                raise HTTPException(status_code=400, detail="No readable content found in the PDF.")

            # Load and render the prompt template
            prompt = prompt_engine.load_prompt(
                "build-workflow-from-pdf",
                sop_text=sop_text,
            )

            # Use the LLM to convert SOP to workflow
            llm_key = settings.LLM_KEY or "gpt-4o-mini"
            LOG.info(
                "Calling LLM to convert SOP to workflow",
                llm_key=llm_key,
                prompt_length=len(prompt),
                sop_text_length=len(sop_text),
                sop_chars_sent=len(sop_text),
                organization_id=organization.organization_id,
            )

            llm_api_handler = LLMAPIHandlerFactory.get_llm_api_handler(llm_key)

            response = await llm_api_handler(
                prompt=prompt,
                prompt_name="sop_to_workflow_conversion",
                organization_id=organization.organization_id,
                parameters={"max_completion_tokens": 32768},  # Override the default 4096 limit for PDF conversion
            )

            LOG.info(
                "LLM response received",
                response_type=type(response),
                response_keys=list(response.keys()) if isinstance(response, dict) else None,
                organization_id=organization.organization_id,
            )

            # The LLM API handler automatically parses JSON responses
            # The response should be a dict with the workflow structure
            if not isinstance(response, dict):
                LOG.error(
                    "LLM returned non-dict response",
                    response_type=type(response),
                    response=str(response)[:500],
                    organization_id=organization.organization_id,
                )
                raise HTTPException(
                    status_code=422, detail="LLM returned invalid response format - expected JSON object"
                )

            # Validate that it has the required structure
            if "workflow_definition" not in response:
                LOG.error(
                    "LLM response missing workflow_definition",
                    response_keys=list(response.keys()),
                    organization_id=organization.organization_id,
                )
                raise HTTPException(status_code=422, detail="LLM response missing 'workflow_definition' field")

            if "blocks" not in response.get("workflow_definition", {}):
                LOG.error(
                    "LLM workflow_definition missing blocks",
                    workflow_def_keys=list(response.get("workflow_definition", {}).keys()),
                    organization_id=organization.organization_id,
                )
                raise HTTPException(status_code=422, detail="LLM workflow definition missing 'blocks' field")

            LOG.info(
                "Workflow JSON validated",
                title=response.get("title"),
                block_count=len(response.get("workflow_definition", {}).get("blocks", [])),
                organization_id=organization.organization_id,
            )

            LOG.info(
                "Creating workflow from JSON",
                response_keys=list(response.keys()),
                organization_id=organization.organization_id,
            )

            try:
                # Sanitize LLM output for Jinja and required fields before validation
                response = self._sanitize_workflow_json(response)
                workflow_create_request = WorkflowCreateYAMLRequest.model_validate(response)
            except Exception as e:
                LOG.error(
                    "Failed to validate workflow request",
                    error=str(e),
                    error_type=type(e).__name__,
                    response_sample=str(response)[:1000],
                    organization_id=organization.organization_id,
                    exc_info=True,
                )
                raise HTTPException(status_code=422, detail=f"Failed to validate workflow structure: {str(e)}")

            try:
                workflow = await app.WORKFLOW_SERVICE.create_workflow_from_request(
                    organization=organization,
                    request=workflow_create_request,
                )
            except Exception as e:
                LOG.error(
                    "Failed to create workflow",
                    error=str(e),
                    error_type=type(e).__name__,
                    organization_id=organization.organization_id,
                    exc_info=True,
                )
                raise HTTPException(status_code=422, detail=f"Failed to create workflow: {str(e)}")

            workflow_dict = workflow.model_dump(by_alias=True)
            LOG.info(
                "PDF import completed successfully",
                workflow_id=workflow.workflow_permanent_id,
                workflow_permanent_id_in_dict=workflow_dict.get("workflow_permanent_id"),
                dict_keys=list(workflow_dict.keys()),
                organization_id=organization.organization_id,
            )
            return workflow_dict

        finally:
            # Clean up the temporary file
            os.unlink(temp_file_path)


pdf_import_service = PDFImportService()
