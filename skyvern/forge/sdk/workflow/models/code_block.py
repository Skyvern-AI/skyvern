from __future__ import annotations

import ast
import asyncio
import codecs
import html
import json
import keyword
import os
import re
import shutil
import textwrap
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, ClassVar, Literal

import structlog
from playwright.async_api import Page
from pydantic import BaseModel

from skyvern.config import settings
from skyvern.constants import GET_DOWNLOADED_FILES_TIMEOUT, SAVE_DOWNLOADED_FILES_TIMEOUT
from skyvern.exceptions import (
    CodeBlockRunnerSelectionError,
    FailedToGetTOTPVerificationCode,
    NoTOTPVerificationCodeFound,
)
from skyvern.forge import app
from skyvern.forge.sdk.api.files import (
    download_file,
    get_download_dir,
    resolve_run_download_id,
    validate_local_file_path,
)
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants
from skyvern.forge.sdk.services.credentials import AzureVaultConstants, OnePasswordConstants, generate_totp_code
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import CustomizedCodeException, InsecureCodeDetected
from skyvern.forge.sdk.workflow.loop_download_filter import filter_downloaded_files_for_current_iteration
from skyvern.forge.sdk.workflow.models.block import Block, capture_block_download_baseline
from skyvern.forge.sdk.workflow.models.code_block_recorder import (
    CODE_BLOCK_FILENAME,
    RecordingPage,
    user_code_line_from_exception,
)
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
    ParameterType,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.schemas.steps import AgentStepOutput
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType
from skyvern.services import otp_service
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, ActionStatus
from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()


class Credential(SimpleNamespace):
    pass


class CodeBlockStep(BaseModel):
    title: str | None = None
    description: str | None = None
    action_type: ActionType = ActionType.NULL_ACTION
    line_start: int | None = None
    line_end: int | None = None


class CodeBlockOTPError(Exception):
    """Sanitized OTP-primitive error: never includes the identifier, URL, code, or seed."""


def _register_code_block_secret(workflow_run_context: WorkflowRunContext, value: str) -> None:
    fresh_key = workflow_run_context.generate_random_secret_id()
    workflow_run_context.secrets[fresh_key] = value


async def _resolve_code_block_otp(
    credential_parameter_key: str,
    organization_id: str | None,
    workflow_run_id: str | None,
    *,
    budget_seconds: int,
) -> str:
    """Resolve a fresh OTP at call time for one credential: re-mint its TOTP (the staleness
    fix) or poll its email/SMS/magic-link, registering the value as a secret before return.
    The run context is re-resolved from workflow_run_id, never captured in the bound method's
    closure, so user code cannot reach the seed through the method's cells."""
    if not workflow_run_id:
        raise CodeBlockOTPError("OTP is unavailable: no workflow run is associated with this code block.")

    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
    if workflow_run_context is None:
        raise CodeBlockOTPError("OTP is unavailable: the workflow run context could not be resolved.")

    otp_value = otp_service.try_generate_totp_for_credential(
        workflow_run_context, credential_parameter_key, workflow_run_id
    )
    if otp_value is not None:
        _register_code_block_secret(workflow_run_context, otp_value.value)
        return otp_value.value

    totp_identifier = workflow_run_context.get_credential_totp_identifier(credential_parameter_key)
    if not totp_identifier:
        raise CodeBlockOTPError(
            "No OTP source is configured for this credential. "
            "Add a TOTP secret or an email/SMS identifier to the credential."
        )

    if not organization_id:
        raise CodeBlockOTPError("OTP is unavailable: no organization is associated with this code block.")

    # Run-start anchor disqualifies codes that predate this run (identifiers are shared across runs).
    workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(workflow_run_id)
    if workflow_run is None:
        raise CodeBlockOTPError("OTP is unavailable: the workflow run could not be loaded.")

    try:
        polled = await asyncio.wait_for(
            otp_service.poll_otp_value(
                organization_id=organization_id,
                workflow_id=workflow_run.workflow_id,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow_run.workflow_permanent_id,
                totp_identifier=totp_identifier,
                created_after=workflow_run.started_at,
            ),
            timeout=budget_seconds,
        )
    except asyncio.TimeoutError:
        raise CodeBlockOTPError(f"OTP was not received within {budget_seconds} seconds.")
    except (NoTOTPVerificationCodeFound, FailedToGetTOTPVerificationCode):
        raise CodeBlockOTPError("OTP could not be retrieved for this credential.")

    if polled is None:
        raise CodeBlockOTPError("OTP could not be retrieved for this credential.")

    _register_code_block_secret(workflow_run_context, polled.value)
    return polled.value


def _bind_code_block_otp(
    credential_parameter_key: str,
    organization_id: str | None,
    workflow_run_id: str | None,
) -> Callable[[], Awaitable[str]]:
    """Build the awaitable ``otp`` method bound onto a code block's Credential, closing over
    only opaque ids so the seed stays unreachable from the method's cells."""

    async def otp() -> str:
        return await _resolve_code_block_otp(
            credential_parameter_key,
            organization_id,
            workflow_run_id,
            budget_seconds=settings.CODE_BLOCK_OTP_POLL_TIMEOUT_SECONDS,
        )

    return otp


async def _code_block_otp_builtin(credential: object) -> str:
    """Top-level ``await otp(credential)`` sugar that forwards to the credential's bound otp()."""
    bound = getattr(credential, "otp", None)
    if not callable(bound):
        raise CodeBlockOTPError("otp() expects a credential with an OTP source.")
    return await bound()


class CodeBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.CODE] = BlockType.CODE  # type: ignore

    code: str
    parameters: list[PARAMETER_TYPE] = []
    prompt: str | None = None
    steps: list[CodeBlockStep] | None = None

    # Dangerous attribute names that must never be accessed in user code.
    # This blocks subprocess creation, OS access, and sandbox-escape primitives.
    # NOTE: This is a blocklist-based sandbox, not real process-level isolation.
    # It is inherently incomplete — a determined attacker may find bypasses.
    # Long-term we should run user code in a proper sandbox. This blocklist is
    # a defense-in-depth layer, not a security boundary.
    # NOTE: Do not add names that collide with safe module methods (e.g. re.compile).
    # Builtin functions like compile(), eval(), exec() are already blocked via __builtins__: {}.
    BLOCKED_ATTRS: ClassVar[frozenset[str]] = frozenset(
        {
            # Subprocess / OS execution
            "create_subprocess_exec",
            "create_subprocess_shell",
            "system",
            "popen",
            "Popen",
            "exec",
            "spawn",
            "spawnl",
            "spawnle",
            "spawnlp",
            "spawnlpe",
            "check_call",
            "check_output",
            "execv",
            "execve",
            "execvp",
            "execvpe",
            "execl",
            "execlp",
            "execlpe",
            "fork",
            # Network primitives
            "open_connection",
            "start_server",
            "create_connection",
            "create_server",
            # Frame / code object internals (classic RestrictedPython escape vectors)
            "f_globals",
            "f_locals",
            "f_builtins",
            "f_code",
            "co_code",
            "co_consts",
            "co_names",
            "co_varnames",
            "gi_frame",
            "gi_code",
            "cr_frame",
            "cr_code",
            "tb_frame",
            "tb_next",
            # Class hierarchy escape
            "mro",
            # Filesystem operations (unambiguous — these only appear on os/pathlib, not user objects)
            "listdir",
            "makedirs",
            "rmdir",
            # Module traversal (json.codecs.sys.modules etc.)
            "codecs",
            "modules",
            "builtins",
            "stdout",
            "stderr",
            "stdin",
            # Sandbox-escape helpers (builtin equivalents already blocked via __builtins__: {})
            "getattr",
            "setattr",
            "delattr",
            "globals",
            "eval",
            "vars",
        }
    )

    @staticmethod
    def is_safe_code(code: str) -> None:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            # Block dunder attribute access (obj.__foo__)
            if hasattr(node, "attr") and str(node.attr).startswith("__"):
                raise InsecureCodeDetected("Not allowed to access private methods or attributes")
            # Block bare dunder identifiers (__capture_locals, __builtins__, etc.)
            if isinstance(node, ast.Name) and node.id.startswith("__"):
                raise InsecureCodeDetected("Not allowed to access private methods or attributes")
            if isinstance(node, ast.Import) or isinstance(node, ast.ImportFrom):
                raise InsecureCodeDetected("Not allowed to import modules")
            # Block dangerous method/attribute access on any object
            if hasattr(node, "attr") and node.attr in CodeBlock.BLOCKED_ATTRS:
                raise InsecureCodeDetected(f"Not allowed to access '{node.attr}'")

    @staticmethod
    def build_safe_vars() -> dict[str, Any]:
        return {
            "__builtins__": {},  # only allow several builtins due to security concerns
            "print": print,
            "len": len,
            "range": range,
            "str": str,
            "int": int,
            "float": float,
            "dict": dict,
            "list": list,
            "tuple": tuple,
            "set": set,
            "bool": bool,
            "isinstance": isinstance,
            "enumerate": enumerate,
            "any": any,
            "all": all,
            "max": max,
            "min": min,
            "sum": sum,
            "sorted": sorted,
            "sleep": asyncio.sleep,
            "asyncio": SimpleNamespace(sleep=asyncio.sleep),
            "re": SimpleNamespace(
                match=re.match,
                search=re.search,
                findall=re.findall,
                finditer=re.finditer,
                fullmatch=re.fullmatch,
                sub=re.sub,
                compile=re.compile,
                split=re.split,
                escape=re.escape,
                I=re.I,
                S=re.S,
                IGNORECASE=re.IGNORECASE,
                MULTILINE=re.MULTILINE,
                DOTALL=re.DOTALL,
            ),
            "json": SimpleNamespace(dumps=json.dumps, loads=json.loads),
            "html": SimpleNamespace(escape=html.escape),
            "Exception": Exception,
            "otp": _code_block_otp_builtin,
        }

    def generate_async_user_function(
        self, code: str, page: Page | RecordingPage, parameters: dict[str, Any] | None = None
    ) -> Callable[[], Awaitable[dict[str, Any]]]:
        # SECURITY: validate before exec(). The AST check must run on the raw
        # user code so it can block dunder identifiers like __capture_locals.
        self.is_safe_code(code)
        code = textwrap.indent(textwrap.dedent(code), "    ")
        runtime_variables: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {}
        safe_vars = self.build_safe_vars()
        parameter_defaults: dict[str, Any] = {}
        if parameters:
            for key, value in parameters.items():
                if key not in safe_vars:
                    safe_vars[key] = value
                    if key.isidentifier() and not keyword.iskeyword(key) and not key.startswith("__"):
                        parameter_defaults[key] = value
        default_args = ", ".join(f"{key}=__param_defaults[{key!r}]" for key in parameter_defaults)
        full_code = f"""
async def wrapper({default_args}):
{code}
    return __capture_locals()
"""
        safe_vars["page"] = page
        safe_vars["__capture_locals"] = locals
        safe_vars["__param_defaults"] = parameter_defaults
        # Compile under a recognizable filename so tracebacks map back to user code lines.
        compiled_code = compile(full_code, CODE_BLOCK_FILENAME, "exec")
        exec(compiled_code, safe_vars, runtime_variables)  # nosemgrep
        user_function = runtime_variables["wrapper"]
        if not parameter_defaults:
            return user_function

        excluded_parameter_keys = frozenset(parameter_defaults)

        async def filtered_user_function() -> dict[str, Any]:
            result: Any = await user_function()
            # An explicit `return <non-dict>` in user code yields that value directly,
            # not the __capture_locals() dict; only the implicit dict needs the injected
            # parameter keys stripped. SKY-10789: this guard avoids result.items() on a list.
            if not isinstance(result, dict):
                return result
            return {key: value for key, value in result.items() if key not in excluded_parameter_keys}

        return filtered_user_function

    @staticmethod
    async def execute_user_function_with_timeout(
        user_function: Callable[[], Awaitable[dict[str, Any]]],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        if timeout_seconds <= 0:
            return await user_function()
        return await asyncio.wait_for(user_function(), timeout=timeout_seconds)

    async def _ensure_run_recording_artifact(
        self,
        browser_state: BrowserState,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        browser_session_id: str | None = None,
    ) -> None:
        """Register the run-scoped RECORDING row a fresh-browser code block otherwise lacks; no-op when the
        browser stays open on completion (persistent session or pinned browser_address)."""
        if browser_session_id:
            return
        # get_video_artifacts returns the same VideoArtifact objects held on browser_state, so the id
        # written below is observed by the early-return guard on later blocks sharing the browser.
        # Idempotency assumes those blocks run sequentially (no concurrent first-time registration).
        browser_artifacts = browser_state.browser_artifacts
        if not browser_artifacts or all(va.video_artifact_id for va in browser_artifacts.video_artifacts):
            return
        try:
            # A pinned browser_address run also keeps its browser open, so its per-run webm never
            # finalizes — skip it like a session run and let the clip path serve the recording.
            workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(workflow_run_id, organization_id)
            if workflow_run is not None and workflow_run.browser_address:
                return
            video_artifacts = await app.BROWSER_MANAGER.get_video_artifacts(
                workflow_run_id=workflow_run_id, browser_state=browser_state, finalize=False
            )
            pending_indexes = [idx for idx, va in enumerate(video_artifacts) if not va.video_artifact_id]
            if not pending_indexes:
                return
            workflow_run_block = await app.DATABASE.observer.get_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id, organization_id=organization_id
            )
            for idx in pending_indexes:
                video_artifacts[idx].video_artifact_id = await app.ARTIFACT_MANAGER.create_workflow_run_block_artifact(
                    workflow_run_block=workflow_run_block,
                    artifact_type=ArtifactType.RECORDING,
                    data=video_artifacts[idx].video_data,
                )
        except Exception:
            LOG.warning(
                "Failed to register run-scoped recording artifact for code block",
                workflow_run_block_id=workflow_run_block_id,
                exc_info=True,
            )

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        return self.parameters

    def format_potential_template_parameters(self, workflow_run_context: WorkflowRunContext) -> None:
        self.code = self.format_block_parameter_template_from_workflow_run_context(self.code, workflow_run_context)
        if self.prompt:
            self.prompt = self.format_block_parameter_template_from_workflow_run_context(
                self.prompt, workflow_run_context
            )

    async def _register_downloaded_files(
        self,
        *,
        organization_id: str | None,
        workflow_run_id: str,
        workflow_run_block_id: str,
        download_run_id: str | None = None,
    ) -> list[FileInfo]:
        # Register up front so the block output carries downloaded_file_urls for
        # downstream blocks in the same run; workflow finalization re-runs the save safely.
        if not organization_id:
            return []
        storage_run_id = download_run_id or workflow_run_id
        try:
            async with asyncio.timeout(SAVE_DOWNLOADED_FILES_TIMEOUT):
                await app.STORAGE.save_downloaded_files(
                    organization_id=organization_id,
                    run_id=storage_run_id,
                )
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout to save downloaded files",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return []
        except Exception:
            LOG.warning(
                "CodeBlock failed to register downloaded files; will retry at workflow finalization",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                exc_info=True,
            )
            return []
        try:
            async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                return await app.STORAGE.get_downloaded_files(
                    organization_id=organization_id,
                    run_id=storage_run_id,
                )
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout getting downloaded files",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return []
        except Exception:
            LOG.warning(
                "CodeBlock failed to read back downloaded files; will retry at workflow finalization",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                exc_info=True,
            )
            return []

    async def _materialize_file_parameter_path(
        self,
        value: str | dict[str, Any] | None,
        *,
        workflow_run_id: str,
        organization_id: str | None,
    ) -> str | dict[str, Any] | None:
        uri: str | None = None
        if isinstance(value, str):
            uri = value
        elif isinstance(value, dict):
            uri = value.get("s3uri")
        if not uri or not str(uri).strip():
            return value
        try:
            output_dir = get_download_dir(workflow_run_id)
            local_path = await download_file(str(uri), output_dir=output_dir, organization_id=organization_id)
            # download_file routes managed storage (s3/azure) to a temp file outside the run
            # dir; copy it under the run dir so it passes validate_local_file_path containment.
            resolved = os.path.realpath(local_path)
            allowed_dir = os.path.realpath(output_dir)
            if not resolved.startswith(allowed_dir + os.sep):
                contained = os.path.join(output_dir, os.path.basename(local_path))
                shutil.copyfile(local_path, contained)
                local_path = contained
            return validate_local_file_path(local_path, workflow_run_id)
        except Exception:
            LOG.warning(
                "Failed to materialize file parameter to a local path; leaving the original value",
                workflow_run_id=workflow_run_id,
                exc_info=True,
            )
            return value

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        await app.AGENT_FUNCTION.validate_code_block(organization_id=organization_id)

        block_context = skyvern_context.current()
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        if block_context:
            await capture_block_download_baseline(
                block_context,
                organization_id or workflow_run_context.organization_id or "",
                workflow_run_id,
                self.label,
            )

        resolved_download_id = resolve_run_download_id(block_context, fallback_run_id=workflow_run_id)
        browser_state = await self.get_or_create_browser_state(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            download_run_id_override=resolved_download_id,
        )
        if not browser_state:
            return await self.build_block_result(
                success=False,
                failure_reason="No browser found to run the code block",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        page = await browser_state.get_working_page()
        if not page:
            return await self.build_block_result(
                success=False,
                failure_reason="No page found to run the code block",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        await self._ensure_run_recording_artifact(
            browser_state=browser_state,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
        )

        try:
            self.format_potential_template_parameters(workflow_run_context)
        except Exception as e:
            return await self.build_block_result(
                success=False,
                failure_reason=f"Failed to format jinja template: {str(e)}",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        # get all parameters into a dictionary
        parameter_values = {}
        credential_parameter_keys: set[str] = set()
        for parameter in self.parameters:
            value = workflow_run_context.get_value(parameter.key)
            if not parameter.parameter_type.is_secret_or_credential() and not (
                # NOTE: skyvern credential is a 'credential_id' workflow parameter type
                parameter.parameter_type == ParameterType.WORKFLOW
                and parameter.workflow_parameter_type is not None
                and parameter.workflow_parameter_type.is_credential_type()
            ):
                if (
                    isinstance(parameter, WorkflowParameter)
                    and parameter.workflow_parameter_type == WorkflowParameterType.FILE_URL
                ):
                    value = await self._materialize_file_parameter_path(
                        value,
                        workflow_run_id=workflow_run_id,
                        organization_id=organization_id,
                    )
                parameter_values[parameter.key] = value
                continue
            credential_parameter_keys.add(parameter.key)
            if isinstance(value, dict):
                real_secret_values = {}
                for credential_field, credential_place_holder in value.items():
                    # "context" is a skyvern-defined field to reduce LLM hallucination
                    if credential_field == "context":
                        continue
                    secret_value = workflow_run_context.get_original_secret_value_or_none(credential_place_holder)
                    if (
                        secret_value == BitwardenConstants.TOTP
                        or secret_value == OnePasswordConstants.TOTP
                        or secret_value == AzureVaultConstants.TOTP
                    ):
                        totp_secret_key = workflow_run_context.totp_secret_value_key(credential_place_holder)
                        totp_secret = workflow_run_context.get_original_secret_value_or_none(totp_secret_key)
                        if totp_secret:
                            secret_value = generate_totp_code(totp_secret)
                            # The pre-minted .totp string is exposed to user code (legacy path),
                            # so register it for masking like any other resolved secret.
                            _register_code_block_secret(workflow_run_context, secret_value)
                        else:
                            LOG.warning(
                                "No TOTP secret found, returning the parameter value as is",
                                parameter=credential_place_holder,
                            )

                    real_secret_value = secret_value if secret_value is not None else credential_place_holder
                    parameter_values[credential_field] = real_secret_value
                    real_secret_values[credential_field] = real_secret_value
                credential_namespace = Credential(**real_secret_values)
                credential_namespace.otp = _bind_code_block_otp(parameter.key, organization_id, workflow_run_id)
                parameter_values[parameter.key] = credential_namespace
            else:
                secret_value = workflow_run_context.get_original_secret_value_or_none(value)
                parameter_values[parameter.key] = secret_value if secret_value is not None else value

        try:
            use_codeblock_runner = await app.AGENT_FUNCTION.should_use_codeblock_runner(
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                workflow_run_context=workflow_run_context,
                organization_id=organization_id,
                block_label=self.label,
                browser_session_id=browser_session_id,
            )
        except CodeBlockRunnerSelectionError as selection_error:
            return await self.build_block_result(
                success=False,
                failure_reason=str(selection_error),
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        LOG.info(
            "CodeBlock runner selection at block",
            use_codeblock_runner=use_codeblock_runner,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            block_label=self.label,
        )
        if use_codeblock_runner:
            secure_code_block_result = await app.AGENT_FUNCTION.execute_code_block_override(
                block=self,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                workflow_run_context=workflow_run_context,
                parameter_values=parameter_values,
                credential_parameter_keys=credential_parameter_keys,
            )
            LOG.info(
                "Secure CodeBlock override returned",
                override_returned_none=secure_code_block_result is None,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                block_label=self.label,
            )
            if secure_code_block_result is not None:
                return secure_code_block_result

        workflow_run_block = None

        # A prompt-bearing code block gets a task v1 + step so its recorded calls render through
        # the standard action/artifact timeline and the agent can later take over on failure.
        # Promptless blocks have no task and persist neither actions nor screenshots.
        task: Task | None = None
        step: Step | None = None
        if self.prompt:
            task, step = await app.agent.create_task_and_step_from_code_block(
                code_block=self,
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                task_url=page.url,
            )

        screenshot_tasks: list[asyncio.Task[None]] = []

        async def _screenshot_sink(action: Action) -> None:
            # No task means no action row will reference the screenshot, so skip it rather than orphan an artifact.
            nonlocal workflow_run_block
            if task is None:
                return
            # Every action that reaches this sink is one the recorder chose to surface on the timeline
            # (goto, click, input, page.evaluate, select, hover, ...), so each one earns a screenshot.
            # Re-listing eligible types here only drifts from the recorder's maps — which is exactly how
            # page.evaluate (EXECUTE_JS) ended up with no screenshot.
            # page.screenshot() shares the CDP channel with the user's page calls, so it must run synchronously
            # in the user-await chain (a backgrounded capture races the next action and clips a mid-nav frame);
            # only the page-free S3 upload is deferred off the critical path.
            try:
                if workflow_run_block is None:
                    workflow_run_block = await app.DATABASE.observer.get_workflow_run_block(
                        workflow_run_block_id=workflow_run_block_id, organization_id=organization_id
                    )
                run_block = workflow_run_block
                screenshot = await page.screenshot(timeout=settings.BROWSER_SCREENSHOT_TIMEOUT_MS)
            except Exception:
                LOG.warning(
                    "Code block screenshot capture failed",
                    workflow_run_block_id=workflow_run_block_id,
                    exc_info=True,
                )
                return

            async def _upload() -> None:
                try:
                    action.screenshot_artifact_id = await app.ARTIFACT_MANAGER.create_workflow_run_block_artifact(
                        workflow_run_block=run_block,
                        artifact_type=ArtifactType.SCREENSHOT_ACTION,
                        data=screenshot,
                    )
                except Exception:
                    LOG.warning(
                        "Code block screenshot upload failed",
                        workflow_run_block_id=workflow_run_block_id,
                        exc_info=True,
                    )

            screenshot_tasks.append(asyncio.create_task(_upload()))

        async def _drain_screenshots() -> None:
            if screenshot_tasks:
                await asyncio.gather(*screenshot_tasks, return_exceptions=True)

        recording_page = RecordingPage(page, on_action=_screenshot_sink)

        async def _persist_recorded_actions(recorded: list[Action]) -> None:
            # Best-effort like the screenshot sink: recording must never change block outcome.
            await _drain_screenshots()
            if not recorded or task is None or step is None:
                return
            try:
                masked = workflow_run_context.mask_secrets_in_data([a.model_dump(mode="json") for a in recorded])
                for raw in masked:
                    action = Action.model_validate(raw)
                    action.task_id = task.task_id
                    action.step_id = step.step_id
                    action.step_order = step.order
                    action.organization_id = organization_id
                    await app.DATABASE.workflow_params.create_action(action)
            except Exception:
                LOG.warning(
                    "Failed to persist recorded code block actions",
                    workflow_run_block_id=workflow_run_block_id,
                    exc_info=True,
                )

        finalized = False

        async def _finalize_code_block_task(success: bool) -> None:
            # Finalize both task and step on every exit path (incl. CancelledError via the finally); idempotent.
            nonlocal finalized
            if task is None or finalized:
                return
            finalized = True
            try:
                await app.DATABASE.tasks.update_task(
                    task_id=task.task_id,
                    organization_id=organization_id,
                    status=TaskStatus.completed if success else TaskStatus.failed,
                )
                if step is not None:
                    await app.DATABASE.tasks.update_step(
                        task_id=task.task_id,
                        step_id=step.step_id,
                        status=StepStatus.completed if success else StepStatus.failed,
                        output=AgentStepOutput(action_results=[]) if success else None,
                        is_last=True,
                        organization_id=organization_id,
                    )
            except Exception:
                LOG.warning(
                    "Failed to finalize code block task status",
                    workflow_run_block_id=workflow_run_block_id,
                    exc_info=True,
                )

        try:
            if task is not None:
                await app.DATABASE.observer.update_workflow_run_block(
                    workflow_run_block_id=workflow_run_block_id,
                    task_id=task.task_id,
                    organization_id=organization_id,
                )
            user_function = self.generate_async_user_function(self.code, recording_page, parameter_values)
            result = await self.execute_user_function_with_timeout(
                user_function,
                settings.CODE_BLOCK_EXECUTION_TIMEOUT_SECONDS,
            )
        except InsecureCodeDetected as e:
            await _drain_screenshots()
            await _finalize_code_block_task(success=False)
            return await self.build_block_result(
                success=False,
                failure_reason=str(e),
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except asyncio.TimeoutError:
            await _persist_recorded_actions(recording_page.recorded_actions())
            await _finalize_code_block_task(success=False)
            return await self.build_block_result(
                success=False,
                failure_reason=(
                    "Failed to execute code block. Reason: TimeoutError: code block exceeded "
                    f"{settings.CODE_BLOCK_EXECUTION_TIMEOUT_SECONDS} seconds"
                ),
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except Exception as e:
            exc = CustomizedCodeException(e)
            failing_line = user_code_line_from_exception(e)
            # User code can raise an exception carrying a resolved secret (e.g.
            # `raise Exception(await cred.otp())`); mask before it reaches the persisted reason.
            failure_reason = workflow_run_context.mask_secrets_in_data(exc.message)
            recorded = recording_page.recorded_actions()
            if recording_page.last_recorded_exception() is not e:
                # The exception did not come from a recorded page call; add a synthetic failure row.
                recorded.append(
                    Action(
                        action_type=ActionType.NULL_ACTION,
                        status=ActionStatus.failed,
                        action_order=len(recorded),
                        description=f"code error at line {failing_line}" if failing_line else "code error",
                        response=(failure_reason or "")[:500],
                        output={"code_line": failing_line},
                    )
                )
            await _persist_recorded_actions(recorded)
            await _finalize_code_block_task(success=False)
            return await self.build_block_result(
                success=False,
                failure_reason=failure_reason,
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        else:
            await _persist_recorded_actions(recording_page.recorded_actions())
            await _finalize_code_block_task(success=True)
        finally:
            # Safety net for paths the except arms miss (CancelledError, update_workflow_run_block failure).
            await _finalize_code_block_task(success=False)

        result = json.loads(
            json.dumps(result, default=lambda value: f"Object '{type(value)}' is not JSON serializable")
        )
        # Mask resolved secrets (OTP codes, passwords) a user assigned to a local before they
        # reach captured locals, the persisted output, or the logged value. Mirrors
        # HttpRequestBlock and is stronger than the name-based excluded_parameter_keys filter.
        result = workflow_run_context.mask_secrets_in_data(result)

        downloaded_files = await self._register_downloaded_files(
            organization_id=organization_id or workflow_run_context.organization_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            download_run_id=resolved_download_id,
        )
        current_context = skyvern_context.current()
        downloaded_files = filter_downloaded_files_for_current_iteration(
            downloaded_files,
            current_context.loop_internal_state if current_context else None,
        )
        if downloaded_files and not isinstance(result, dict):
            result = {"value": result} if result is not None else {}
        if downloaded_files and isinstance(result, dict):
            result["downloaded_files"] = [fi.model_dump() for fi in downloaded_files]
            result["downloaded_file_urls"] = [fi.url for fi in downloaded_files]
            result["downloaded_file_artifact_ids"] = [fi.artifact_id for fi in downloaded_files if fi.artifact_id]

        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, result)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=result,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )
