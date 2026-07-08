"""Shared foundation for workflow blocks: the abstract ``Block`` base class and helpers.

Extracted from ``block.py`` (SKY-11658) so that both ``block.py`` (the import facade)
and extracted block modules like ``task_blocks.py`` can import it without a circular
import. ``block.py`` re-exports every public name defined here, so existing
``from ...models.block import Block`` call sites keep working unchanged.
"""

from __future__ import annotations

import abc
import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from opentelemetry import trace as otel_trace
from pydantic import BaseModel, Field

from skyvern.config import settings
from skyvern.constants import GET_DOWNLOADED_FILES_TIMEOUT
from skyvern.exceptions import SkyvernException, get_user_facing_exception_message
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.aws import AsyncAWSClient
from skyvern.forge.sdk.api.files import resolve_run_download_id
from skyvern.forge.sdk.api.llm.custom_llm_registry import is_custom_llm_model_name
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.trace import traced
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import FailedToFormatJinjaStyleParameter, MissingJinjaVariables
from skyvern.forge.sdk.workflow.loop_download_filter import DOWNLOADED_FILE_SIGS_KEY, to_downloaded_file_signature
from skyvern.forge.sdk.workflow.models._jinja import _json_type_filter
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE, OutputParameter
from skyvern.schemas.runs import RunEngine
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType
from skyvern.utils.templating import get_missing_variables
from skyvern.webeye.browser_factory import rebind_download_dir
from skyvern.webeye.browser_state import BrowserState

if TYPE_CHECKING:
    from skyvern.forge.sdk.workflow.models.block import BaseTaskBlock

LOG = structlog.get_logger()


# SKY-8818: observability threshold for under-configured file_download blocks.
# Warning fires when `max_steps_per_run` is set below this value. Not a behavior change —
# purely a Datadog-searchable signal (`log_code=file_download_low_max_steps`).
MAX_STEPS_DOWNLOAD_WARNING_THRESHOLD = 5


def warn_if_file_download_max_steps_low(
    block: BaseTaskBlock,
    workflow_run_id: str | None = None,
) -> None:
    """Emit a structured warning if a file_download block has an under-configured step budget.

    A `max_steps_per_run` of None means "use org default", which is not a misconfiguration.
    Only configured values strictly below MAX_STEPS_DOWNLOAD_WARNING_THRESHOLD warn.
    """
    if block.block_type != BlockType.FILE_DOWNLOAD:
        return
    configured = block.max_steps_per_run
    if configured is None:
        return
    if configured >= MAX_STEPS_DOWNLOAD_WARNING_THRESHOLD:
        return
    LOG.warning(
        "file_download block configured with low max_steps_per_run",
        log_code="file_download_low_max_steps",
        block_label=block.label,
        max_steps_per_run=configured,
        recommended_minimum=MAX_STEPS_DOWNLOAD_WARNING_THRESHOLD,
        workflow_run_id=workflow_run_id,
    )


async def capture_block_download_baseline(
    context: SkyvernContext,
    organization_id: str,
    workflow_run_id: str,
    block_label: str,
) -> None:
    """Snapshot the files already downloaded before this block runs.

    Recorded in ``loop_internal_state`` so ``filter_downloaded_files_for_current_iteration``
    scopes the block's output to only the files it produced. Captured fresh for every
    block — including each block inside a loop iteration — so sibling download-producing
    blocks don't inherit one another's files. Best-effort: cleared on timeout/error.
    """
    try:
        async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
            baseline_files = await app.STORAGE.get_downloaded_files(
                organization_id=organization_id,
                run_id=resolve_run_download_id(context, fallback_run_id=workflow_run_id),
            )
            context.loop_internal_state = {
                DOWNLOADED_FILE_SIGS_KEY: [to_downloaded_file_signature(fi) for fi in baseline_files],
            }
            LOG.debug(
                "Captured block download baseline",
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                block_label=block_label,
                file_count=len(baseline_files),
            )
    except asyncio.TimeoutError:
        context.loop_internal_state = None
        LOG.warning(
            "Timeout capturing baseline downloaded files for task block",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            block_label=block_label,
        )
    except Exception:
        # Baseline capture is best-effort — transient S3/network errors should
        # not abort the block. Degrade to unscoped filtering (the pre-fix behavior).
        context.loop_internal_state = None
        LOG.warning(
            "Failed to capture baseline downloaded files for task block",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            block_label=block_label,
            exc_info=True,
        )


if settings.WORKFLOW_TEMPLATING_STRICTNESS == "strict":
    jinja_sandbox_env = SandboxedEnvironment(undefined=StrictUndefined)
else:
    jinja_sandbox_env = SandboxedEnvironment()


# Date format used for the built-in {{current_date}} reserved parameter.
CURRENT_DATE_FORMAT = "%Y-%m-%d"

jinja_sandbox_env.filters["json"] = _json_type_filter


class Block(BaseModel, abc.ABC):
    """Base class for workflow nodes (see branching spec [[s-4bnl]] for metadata semantics)."""

    # Must be unique within workflow definition
    label: str = Field(description="Author-facing identifier for a block; unique within a workflow.")
    next_block_label: str | None = Field(
        default=None,
        description="Optional pointer to the next block label when constructing a DAG. "
        "Defaults to sequential order when omitted.",
    )
    block_type: BlockType
    output_parameter: OutputParameter
    continue_on_failure: bool = False
    model: dict[str, Any] | None = None
    disable_cache: bool = False
    # Opt-out from workflow-level workflow_system_prompt inheritance (and, on a
    # WorkflowTriggerBlock, from propagating the parent chain's prompt into the
    # spawned child run). A no-op for deterministic blocks that don't call an LLM.
    ignore_workflow_system_prompt: bool = False
    # Runtime cache populated by ``Block._apply_workflow_system_prompt`` — not
    # user-settable. Excluded from serialization (``model_dump`` / JSON / API
    # responses) so the resolved prompt doesn't leak into logs, workflow
    # definition round-trips, or responses that weren't meant to carry it.
    # Deliberately absent from the BlockYAML schema so it can never be set
    # through YAML or the API. The user-facing opt-out is
    # ``ignore_workflow_system_prompt``. Only consumed by block types that call
    # an LLM; deterministic blocks ignore it.
    workflow_system_prompt: str | None = Field(default=None, exclude=True)

    # Only valid for blocks inside a for loop block
    # Whether to continue to the next iteration when the block fails
    next_loop_on_failure: bool = False

    @property
    def override_llm_key(self) -> str | None:
        return self.override_llm_key_for_organization(None)

    def override_llm_key_for_organization(self, organization_id: str | None) -> str | None:
        """
        If the `Block` has a `model` defined, then return the mapped llm_key for it.

        Otherwise return `None`.
        """
        if self.model:
            model_name = self.model.get("model_name")
            if model_name:
                mapping = SettingsManager.get_settings().get_model_name_to_llm_key(organization_id=organization_id)
                llm_key = mapping.get(model_name, {}).get("llm_key")
                if llm_key:
                    return llm_key
                if is_custom_llm_model_name(model_name):
                    raise ValueError("Custom LLM model not found for organization")

        return None

    async def record_output_parameter_value(
        self,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        value: dict[str, Any] | list | str | None = None,
    ) -> None:
        await workflow_run_context.register_output_parameter_value_post_execution(
            parameter=self.output_parameter,
            value=value,
        )
        await app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter(
            workflow_run_id=workflow_run_id,
            output_parameter_id=self.output_parameter.output_parameter_id,
            value=value,
        )
        LOG.info(
            "Registered output parameter value",
            sampling=True,
            output_parameter_id=self.output_parameter.output_parameter_id,
            workflow_run_id=workflow_run_id,
            output_parameter_value=value,
        )

    async def build_block_result(
        self,
        success: bool,
        failure_reason: str | None,
        output_parameter_value: dict[str, Any] | list | str | None = None,
        status: BlockStatus | None = None,
        workflow_run_block_id: str | None = None,
        organization_id: str | None = None,
        executed_branch_id: str | None = None,
        executed_branch_expression: str | None = None,
        executed_branch_result: bool | None = None,
        executed_branch_next_block: str | None = None,
        error_codes: list[str] | None = None,
        is_synthetic_loop_failure: bool = False,
    ) -> BlockResult:
        # TODO: update workflow run block status and failure reason
        if isinstance(output_parameter_value, str):
            output_parameter_value = {"value": output_parameter_value}

        if workflow_run_block_id:
            await app.DATABASE.observer.update_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id,
                output=output_parameter_value,
                status=status,
                failure_reason=failure_reason,
                organization_id=organization_id,
                executed_branch_id=executed_branch_id,
                executed_branch_expression=executed_branch_expression,
                executed_branch_result=executed_branch_result,
                executed_branch_next_block=executed_branch_next_block,
                error_codes=error_codes,
            )
        return BlockResult(
            success=success,
            failure_reason=failure_reason,
            error_codes=error_codes or [],
            output_parameter=self.output_parameter,
            output_parameter_value=output_parameter_value,
            status=status,
            workflow_run_block_id=workflow_run_block_id,
            is_synthetic_loop_failure=is_synthetic_loop_failure,
        )

    async def get_or_create_browser_state(
        self,
        workflow_run_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        download_run_id_override: str | None = None,
    ) -> BrowserState | None:
        """
        Acquire or create browser state for block execution.

        Checks persistent sessions first (debugger use case), then falls back to
        workflow run browser manager. If no state exists, creates a new one.

        Returns BrowserState if successful, None if creation failed.
        """
        browser_state: BrowserState | None = None

        if browser_session_id and organization_id:
            browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(browser_session_id, organization_id)
            if browser_state is not None:
                rebind_run_id = download_run_id_override or resolve_run_download_id(
                    skyvern_context.current(), fallback_run_id=workflow_run_id
                )
                try:
                    adopted_context = browser_state.browser_context
                    adopted_browser = adopted_context.browser if adopted_context else None
                    rebind_page = None if adopted_browser is not None else await browser_state.get_working_page()
                    if adopted_browser is not None or rebind_page is not None:
                        await rebind_download_dir(adopted_browser, run_id=rebind_run_id, page=rebind_page)
                        LOG.info(
                            "Rebound download dir on adopted persistent session",
                            browser_session_id=browser_session_id,
                            workflow_run_id=workflow_run_id,
                            run_id=rebind_run_id,
                        )
                except Exception:
                    LOG.warning(
                        "Failed to rebind download dir on adopted persistent session",
                        browser_session_id=browser_session_id,
                        workflow_run_id=workflow_run_id,
                        exc_info=True,
                    )
        else:
            browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id)

        # A reused browser state (a persistent debug session shared with the copilot, or a
        # cached workflow-run browser) can have a dead Playwright driver after a prior owner
        # stopped it — reusing it makes the block's first page.goto raise "Connection closed
        # while reading from the driver". Rebuild a fresh connection to the same browser first.
        if browser_state is not None and not browser_state.is_connected():
            workflow_run = await app.WORKFLOW_SERVICE.get_workflow_run(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            # Reconnect to the session's own remote browser, never the run's (possibly pooled)
            # browser_address. get_browser_address_if_ready resolves a dead session to None
            # instead of blocking on the workflow query, so a missing address aborts fast.
            if browser_session_id and organization_id:
                try:
                    browser_address = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_address_if_ready(
                        session_id=browser_session_id, organization_id=organization_id
                    )
                except Exception:
                    browser_address = None
                if not browser_address:
                    LOG.warning(
                        "No session browser address available to reconnect; aborting browser setup",
                        workflow_run_id=workflow_run_id,
                        browser_session_id=browser_session_id,
                    )
                    return None
            else:
                browser_address = workflow_run.browser_address
            try:
                await browser_state.reconnect(
                    proxy_location=workflow_run.proxy_location,
                    workflow_run_id=workflow_run_id,
                    workflow_permanent_id=workflow_run.workflow_permanent_id,
                    organization_id=workflow_run.organization_id,
                    extra_http_headers=workflow_run.extra_http_headers,
                    cdp_connect_headers=workflow_run.cdp_connect_headers,
                    browser_address=browser_address,
                    browser_profile_id=workflow_run.browser_profile_id,
                )
                LOG.info(
                    "Rebuilt a disconnected browser state before block execution",
                    workflow_run_id=workflow_run_id,
                    browser_session_id=browser_session_id,
                )
            except Exception:
                LOG.exception(
                    "Failed to rebuild disconnected browser state",
                    workflow_run_id=workflow_run_id,
                    browser_session_id=browser_session_id,
                )
                return None

        if not browser_state:
            workflow_run = await app.WORKFLOW_SERVICE.get_workflow_run(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            try:
                browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                    workflow_run=workflow_run,
                    url=None,
                    browser_session_id=browser_session_id,
                    browser_profile_id=workflow_run.browser_profile_id,
                )
                await browser_state.check_and_fix_state(
                    url=None,
                    proxy_location=workflow_run.proxy_location,
                    workflow_run_id=workflow_run_id,
                    workflow_permanent_id=workflow_run.workflow_permanent_id,
                    organization_id=workflow_run.organization_id,
                    extra_http_headers=workflow_run.extra_http_headers,
                    browser_address=workflow_run.browser_address,
                    browser_profile_id=workflow_run.browser_profile_id,
                )
            except Exception:
                LOG.exception(
                    "Failed to create browser state",
                    workflow_run_id=workflow_run_id,
                )
                return None

        if not (browser_session_id and organization_id) and browser_state is not None:
            rebind_run_id = download_run_id_override or resolve_run_download_id(
                skyvern_context.current(), fallback_run_id=workflow_run_id
            )
            try:
                owning_browser = browser_state.browser_context.browser if browser_state.browser_context else None
                rebind_page = None if owning_browser is not None else await browser_state.get_working_page()
                if owning_browser is not None or rebind_page is not None:
                    await rebind_download_dir(owning_browser, run_id=rebind_run_id, page=rebind_page)
                    LOG.info(
                        "Rebound download dir on workflow-run browser",
                        workflow_run_id=workflow_run_id,
                        run_id=rebind_run_id,
                    )
            except Exception:
                LOG.warning(
                    "Failed to rebind download dir on workflow-run browser",
                    workflow_run_id=workflow_run_id,
                    run_id=rebind_run_id,
                    exc_info=True,
                )

        return browser_state

    def format_block_parameter_template_from_workflow_run_context(
        self,
        potential_template: str,
        workflow_run_context: WorkflowRunContext,
        *,
        force_include_secrets: bool = False,
        env: SandboxedEnvironment | None = None,
    ) -> str:
        """
        Format a template string using the workflow run context.

        Security Note:
        Real secret values are ONLY resolved for blocks that do NOT expose data to the LLM
        (like HttpRequestBlock, CodeBlock), as determined by is_safe_block_for_secrets.
        """
        if not potential_template:
            return potential_template

        # Security: only allow real secret values for non-LLM blocks (HttpRequestBlock, CodeBlock)
        is_safe_block_for_secrets = self.block_type in [
            BlockType.CODE,
            BlockType.HTTP_REQUEST,
        ]

        try:
            template = (env or jinja_sandbox_env).from_string(potential_template)
        except Exception as exc:
            raise FailedToFormatJinjaStyleParameter(potential_template, str(exc)) from exc

        block_reference_data: dict[str, Any] = workflow_run_context.get_block_metadata(self.label)
        template_data = workflow_run_context.values.copy()

        include_secrets = workflow_run_context.include_secrets_in_templates or force_include_secrets

        # FORCE DISABLE if block is not safe (sends data to LLM)
        if include_secrets and not is_safe_block_for_secrets:
            include_secrets = False

        if include_secrets:
            template_data.update(workflow_run_context.secrets)

            # Create easier-to-access entries for credentials
            # Look for credential parameters and create real_username/real_password entries
            # First collect all credential parameters to avoid modifying dict during iteration
            credential_params = []
            for key, value in list(template_data.items()):
                if isinstance(value, dict) and "context" in value:
                    # PASSWORD credential: has username and password
                    if "username" in value and "password" in value:
                        credential_params.append((key, value))
                    # SECRET credential: has secret_value
                    elif "secret_value" in value:
                        credential_params.append((key, value))

            # Now add the real_username/real_password entries
            for key, value in credential_params:
                username_secret_id = value.get("username", "")
                password_secret_id = value.get("password", "")

                # Get the actual values from the secrets
                real_username = template_data.get(username_secret_id, "")
                real_password = template_data.get(password_secret_id, "")

                # Add easier-to-access entries
                template_data[f"{key}_real_username"] = real_username
                template_data[f"{key}_real_password"] = real_password

                if is_safe_block_for_secrets:
                    resolved_credential = value.copy()
                    for credential_field, credential_placeholder in value.items():
                        if credential_field == "context":
                            continue
                        secret_value = workflow_run_context.get_original_secret_value_or_none(credential_placeholder)
                        if secret_value is not None:
                            resolved_credential[credential_field] = secret_value
                    resolved_credential.pop("context", None)
                    template_data[key] = resolved_credential

        if self.label in template_data:
            current_value = template_data[self.label]
            if isinstance(current_value, dict):
                block_reference_data.update(current_value)
            else:
                LOG.warning(
                    f"Parameter {self.label} has a registered reference value, going to overwrite it by block metadata"
                )

        template_data[self.label] = block_reference_data

        # TODO (suchintan): This is pretty hacky - we should have a standard way to initialize the workflow run context
        # inject the forloop metadata as global variables
        if "current_index" in block_reference_data:
            template_data["current_index"] = block_reference_data["current_index"]
        if "current_item" in block_reference_data:
            template_data["current_item"] = block_reference_data["current_item"]
        if "current_value" in block_reference_data:
            template_data["current_value"] = block_reference_data["current_value"]

        # Initialize workflow-level parameters
        if "workflow_title" not in template_data:
            template_data["workflow_title"] = workflow_run_context.workflow_title
        if "workflow_id" not in template_data:
            template_data["workflow_id"] = workflow_run_context.workflow_id
        if "workflow_permanent_id" not in template_data:
            template_data["workflow_permanent_id"] = workflow_run_context.workflow_permanent_id
        if "workflow_run_id" not in template_data:
            template_data["workflow_run_id"] = workflow_run_context.workflow_run_id
        if "current_date" not in template_data:
            template_data["current_date"] = datetime.now(timezone.utc).strftime(CURRENT_DATE_FORMAT)
        if "browser_session_id" not in template_data:
            template_data["browser_session_id"] = workflow_run_context.browser_session_id or ""

        template_data["workflow_run_outputs"] = workflow_run_context.workflow_run_outputs
        template_data["workflow_run_summary"] = workflow_run_context.build_workflow_run_summary()

        if settings.WORKFLOW_TEMPLATING_STRICTNESS == "strict":
            if missing_variables := get_missing_variables(potential_template, template_data):
                raise MissingJinjaVariables(
                    template=potential_template,
                    variables=missing_variables,
                )

        try:
            return template.render(template_data)
        except SkyvernException:
            raise
        except Exception as exc:
            raise FailedToFormatJinjaStyleParameter(potential_template, str(exc)) from exc

    def _apply_workflow_system_prompt(
        self,
        workflow_run_context: WorkflowRunContext,
    ) -> None:
        """Resolve the workflow-level ``workflow_system_prompt`` for this block and
        materialize it onto ``self.workflow_system_prompt``.

        Concatenates any prompt inherited from ancestor workflows (propagated through
        ``WorkflowTriggerBlock``) with this workflow's own ``workflow_system_prompt``.
        Jinja substitutions on this workflow's own prompt are resolved against
        ``workflow_run_context``; the inherited portion is already resolved at the
        trigger boundary.

        Shared by every block type that needs to inherit the workflow system prompt
        into its own ``workflow_system_prompt`` runtime cache before dispatching an
        LLM call. Callers invoke this inside ``format_potential_template_parameters``
        so the value is available at execute time. ``workflow_system_prompt`` on each
        block is a runtime cache — it's deliberately absent from the BlockYAML schema
        and not user-settable.

        When a block opts out via ``ignore_workflow_system_prompt``, this leaves
        the block's own ``workflow_system_prompt`` untouched (falling back to the
        system default if none is set). The opt-out covers both this workflow's
        prompt and any inherited prompt from parent workflows.
        """
        if self.ignore_workflow_system_prompt:
            # Record the opt-out so the script path (``ai_extract``) reads the
            # same decision instead of re-resolving the flag from the
            # definition. See ``WorkflowRunContext.record_block_workflow_system_prompt``.
            workflow_run_context.record_block_workflow_system_prompt(self.label, None)
            return
        resolved = workflow_run_context.resolve_effective_workflow_system_prompt()
        if resolved is not None:
            self.workflow_system_prompt = resolved
        workflow_run_context.record_block_workflow_system_prompt(self.label, resolved)

    @classmethod
    def get_subclasses(cls) -> tuple[type[Block], ...]:
        return tuple(cls.__subclasses__())

    @staticmethod
    def get_workflow_run_context(workflow_run_id: str) -> WorkflowRunContext:
        return app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)

    @staticmethod
    def get_async_aws_client() -> AsyncAWSClient:
        return app.WORKFLOW_CONTEXT_MANAGER.aws_client

    @abc.abstractmethod
    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        pass

    async def _generate_workflow_run_block_description(
        self, workflow_run_block_id: str, organization_id: str | None = None
    ) -> None:
        description = None
        try:
            block_data = self.model_dump(
                exclude={
                    "workflow_run_block_id",
                    "organization_id",
                    "task_id",
                    "workflow_run_id",
                    "parent_workflow_run_block_id",
                    "label",
                    "status",
                    "output",
                    "continue_on_failure",
                    "failure_reason",
                    "actions",
                    "created_at",
                    "modified_at",
                },
                exclude_none=True,
            )
            description_generation_prompt = prompt_engine.load_prompt(
                "generate_workflow_run_block_description",
                block=block_data,
            )
            json_response = await app.SECONDARY_LLM_API_HANDLER(
                prompt=description_generation_prompt,
                prompt_name="generate-workflow-run-block-description",
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
            description = json_response.get("summary")
            LOG.info(
                "Generated description for the workflow run block",
                sampling=True,
                description=description,
                workflow_run_block_id=workflow_run_block_id,
            )
        except Exception as e:
            LOG.exception("Failed to generate description for the workflow run block", error=e)

        if description:
            await app.DATABASE.observer.update_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id,
                description=description,
                organization_id=organization_id,
            )

    def get_failure_error_codes(self) -> list[str]:
        """Return block-level error codes for unexpected failures. Override in subclasses."""
        return []

    def get_engine(self) -> RunEngine | None:
        """Return the run engine for task-executing blocks. Overridden by BaseTaskBlock."""
        return None

    @traced(name="skyvern.block.execute", role="wrapper")
    async def execute_safe(
        self,
        workflow_run_id: str,
        parent_workflow_run_block_id: str | None = None,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        current_value: str | None = None,
        current_index: int | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # block_type slices the 303s p95 by block kind — task/for_loop/code/extraction
        # have wildly different latency profiles. Set early so it's present even if
        # execute_safe raises before any child work.
        otel_trace.get_current_span().set_attribute("block_type", self.block_type.value)
        workflow_run_block_id = None
        engine: RunEngine | None = None
        try:
            engine = self.get_engine()

            workflow_run_block = await app.DATABASE.observer.create_workflow_run_block(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                parent_workflow_run_block_id=parent_workflow_run_block_id,
                label=self.label,
                block_type=self.block_type,
                continue_on_failure=self.continue_on_failure,
                engine=engine,
                current_value=current_value,
                current_index=current_index,
            )
            workflow_run_block_id = workflow_run_block.workflow_run_block_id

            # generate the description for the workflow run block asynchronously
            # Skip for subsequent for-loop iterations (current_index > 0) — the block
            # definition is identical across iterations and each iteration gets a fresh
            # model_copy(deep=True), so instance-level caching doesn't survive.
            if current_index is None or current_index == 0:
                asyncio.create_task(
                    self._generate_workflow_run_block_description(workflow_run_block_id, organization_id)
                )

            # create a screenshot
            browser_state = app.BROWSER_MANAGER.get_for_workflow_run(workflow_run_id)
            if not browser_state:
                LOG.warning(
                    "No browser state found when creating workflow_run_block",
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    browser_session_id=browser_session_id,
                    block_label=self.label,
                )
            else:
                try:
                    screenshot = await browser_state.take_fullpage_screenshot()
                except Exception:
                    LOG.warning(
                        "Failed to take screenshot before executing the block, ignoring the exception",
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                    )
                    screenshot = None
                if screenshot:
                    await app.ARTIFACT_MANAGER.create_workflow_run_block_artifact(
                        workflow_run_block=workflow_run_block,
                        artifact_type=ArtifactType.SCREENSHOT_LLM,
                        data=screenshot,
                    )

            LOG.info(
                "Executing block",
                sampling=True,
                workflow_run_id=workflow_run_id,
                block_label=self.label,
                block_type=self.block_type,
            )
            return await self.execute(
                workflow_run_id,
                workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                **kwargs,
            )
        except Exception as e:
            LOG.exception(
                "Block execution failed",
                workflow_run_id=workflow_run_id,
                block_label=self.label,
                block_type=self.block_type,
            )
            # Record output parameter value if it hasn't been recorded yet
            workflow_run_context = self.get_workflow_run_context(workflow_run_id)
            if not workflow_run_context.has_value(self.output_parameter.key):
                await self.record_output_parameter_value(workflow_run_context, workflow_run_id)

            failure_reason = get_user_facing_exception_message(e)

            return await self.build_block_result(
                success=False,
                failure_reason=failure_reason,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                error_codes=self.get_failure_error_codes() or None,
            )

    @abc.abstractmethod
    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        pass
