from __future__ import annotations

import ast
import asyncio
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
from playwright.async_api import Error as PlaywrightError
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
from skyvern.forge.sdk.copilot.block_goal_wrapping import compose_mini_goal
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.tasks import Task, TaskOutput, TaskStatus
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants
from skyvern.forge.sdk.services.credentials import AzureVaultConstants, OnePasswordConstants, generate_totp_code
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import CustomizedCodeException, InsecureCodeDetected
from skyvern.forge.sdk.workflow.loop_download_filter import filter_downloaded_files_for_current_iteration
from skyvern.forge.sdk.workflow.models.block_base import Block, capture_block_download_baseline
from skyvern.forge.sdk.workflow.models.code_block_recorder import (
    CODE_BLOCK_FILENAME,
    RecordingPage,
    user_code_line_from_exception,
)
from skyvern.forge.sdk.workflow.models.code_block_recording import CodeBlockActionRecording
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
    ParameterType,
    WorkflowParameter,
    WorkflowParameterType,
)
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
            "format",
            "format_map",
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
        if str(uri).startswith("/"):
            try:
                local_path = validate_local_file_path(str(uri), workflow_run_id)
                if not os.path.isfile(local_path):
                    raise FileNotFoundError(f"Local file not found: {uri}")
                return local_path
            except FileNotFoundError:
                pass
            except PermissionError:
                LOG.warning(
                    "CodeBlock file parameter path is outside the run's download directory; leaving the original value",
                    workflow_run_id=workflow_run_id,
                )
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

    def _match_step_for_failing_line(self, failing_line: int) -> CodeBlockStep | None:
        """Advisory nearest-preceding-start match; a lone ``line_start`` (``line_end`` None) is
        open-ended. Step spans are display-oriented and can drift, so a miss never blocks the heal."""
        best: CodeBlockStep | None = None
        best_start = -1
        for step in self.steps or []:
            if step.line_start is None or step.line_start > failing_line:
                continue
            if step.line_end is not None and failing_line > step.line_end:
                continue
            if step.line_start > best_start:
                best = step
                best_start = step.line_start
        return best

    async def _self_heal_enabled(self, workflow_run_context: WorkflowRunContext) -> bool:
        # User-facing per-workflow setting, restricted to copilot-authored workflows —
        # pre-copilot code blocks must never gain agentic recovery from the toggle alone.
        # The env default stays as the OSS/standalone and local-dev override.
        if settings.ENABLE_CODE_BLOCK_SELF_HEALING:
            return True
        workflow = workflow_run_context.workflow
        if workflow is None or not workflow.enable_self_healing:
            return False
        if "copilot" in (workflow.created_by, workflow.edited_by):
            return True
        # User saves re-stamp both fields with the user id, so the current version alone is
        # not durable; fall back to lineage (copilot stamps every version it writes and
        # back-stamps v1 on copilot-born workflows). This runs inside the block's exception
        # handler — a lookup failure must fail closed, never mask the original block failure.
        try:
            return await app.DATABASE.workflows.is_workflow_copilot_authored(
                workflow_permanent_id=workflow.workflow_permanent_id,
                organization_id=workflow.organization_id,
            )
        except Exception:
            LOG.warning(
                "Self-heal copilot-lineage lookup failed; failing closed (no heal)",
                workflow_permanent_id=workflow.workflow_permanent_id,
                exc_info=True,
            )
            return False

    def _is_healable_page_failure(self, exception: Exception, recording_page: RecordingPage) -> bool:
        """Heal genuine page failures only: a recorded page call raised, or an (unmapped) Playwright
        page error surfaced. A deliberate non-Playwright raise in user logic stays non-healable."""
        if recording_page.last_recorded_exception() is exception:
            return True
        return isinstance(exception, PlaywrightError)  # locator/timeout/navigation errors subclass this

    async def _finalize_recovery_block(
        self,
        recovery_block_id: str | None,
        status: BlockStatus,
        organization_id: str | None,
        failure_reason: str | None = None,
    ) -> None:
        # The child recovery block surfaces the heal's actions on the run timeline (parented to the code
        # block); keep its status synced with the heal outcome so it doesn't dangle in `running`.
        if recovery_block_id is None:
            return
        try:
            await app.DATABASE.observer.update_workflow_run_block(
                workflow_run_block_id=recovery_block_id,
                organization_id=organization_id,
                status=status,
                failure_reason=failure_reason,
            )
        except Exception:
            LOG.warning(
                "Failed to finalize self-heal recovery block",
                workflow_run_block_id=recovery_block_id,
                exc_info=True,
            )

    async def _fail_escalation_task(
        self,
        escalation_task: Task | None,
        escalation_step: Step | None,
        recovery_block_id: str | None,
        organization_id: str | None,
    ) -> None:
        # Best-effort so an aborted heal never strands its escalation task/step/recovery block in `running`.
        await self._finalize_recovery_block(recovery_block_id, BlockStatus.failed, organization_id)
        if escalation_task is None:
            return
        try:
            await app.DATABASE.tasks.update_task(
                task_id=escalation_task.task_id,
                organization_id=organization_id,
                status=TaskStatus.failed,
            )
            if escalation_step is not None:
                await app.DATABASE.tasks.update_step(
                    task_id=escalation_task.task_id,
                    step_id=escalation_step.step_id,
                    status=StepStatus.failed,
                    is_last=True,
                    organization_id=organization_id,
                )
        except Exception:
            LOG.warning(
                "Failed to finalize stranded self-heal escalation task",
                task_id=escalation_task.task_id,
                exc_info=True,
            )

    async def _attempt_self_heal(
        self,
        *,
        exception: Exception,
        failing_line: int | None,
        recording_page: RecordingPage,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        browser_session_id: str | None,
    ) -> BlockResult | None:
        """Run one bounded agent mini-run on the same workflow-run browser to finish the block's goal
        (narrowed to the failing step when one is confidently matched). Returns a BlockResult when a
        heal was attempted, or None to fall through to the caller's fail-closed path."""
        if not await self._self_heal_enabled(workflow_run_context):
            return None
        if not self._is_healable_page_failure(exception, recording_page):
            return None
        if not self.prompt:
            return None
        if not organization_id:
            return None
        organization = await app.DATABASE.organizations.get_organization(organization_id=organization_id)
        if organization is None:
            return None

        escalation_task: Task | None = None
        escalation_step: Step | None = None
        recovery_block_id: str | None = None
        try:
            # block.prompt is the operative goal; a confidently-matched step only narrows it. The match
            # is advisory (spans are display-oriented and render-shifted), so any miss heals on the prompt.
            safe_main = workflow_run_context.mask_secrets_in_data(self.prompt)
            matched_step = self._match_step_for_failing_line(failing_line) if failing_line is not None else None
            if matched_step is not None and matched_step.description:
                # The heal owns the failing step plus every subsequent authored step — a
                # step-only goal would complete the block while trailing steps ran by no one.
                steps = self.steps or []
                # Identity scan, not .index(): value equality would match an earlier duplicate step.
                matched_index = next((i for i, step in enumerate(steps) if step is matched_step), None)
                if matched_index is None:
                    matched_index = len(steps) - 1  # defensive; matched_step always comes from self.steps
                descriptions = [matched_step.description] + [
                    step.description for step in steps[matched_index + 1 :] if step.description
                ]
                safe_mini = "\nThen: ".join(
                    workflow_run_context.mask_secrets_in_data(description) for description in descriptions
                )
                navigation_goal = compose_mini_goal(main_goal=safe_main, mini_goal=safe_mini)
            else:
                navigation_goal = safe_main

            workflow_system_prompt = (
                None
                if self.ignore_workflow_system_prompt
                else workflow_run_context.resolve_effective_workflow_system_prompt()
            )
            from skyvern.forge.sdk.workflow.models.block import BaseTaskBlock

            task_order, task_retry = await BaseTaskBlock.get_task_order(workflow_run_id, 0)
            # Bound by the global default but never above the org's per-run cap — execute_step gives
            # task.max_steps_per_run precedence over organization.max_steps_per_run.
            heal_max_steps = settings.MAX_STEPS_PER_RUN
            if organization.max_steps_per_run is not None:
                heal_max_steps = min(heal_max_steps, organization.max_steps_per_run)
            # Blank url: the heal takes over the live, half-mutated page rather than re-navigating to it
            # (a truthy task.url makes the browser manager reload the page on the browser-session path).
            escalation_task = await app.DATABASE.tasks.create_task(
                url="",
                title=self.label,
                navigation_goal=navigation_goal,
                data_extraction_goal=None,
                navigation_payload=None,
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                order=task_order,
                retry=task_retry,
                max_steps_per_run=heal_max_steps,
                model=self.model,
                workflow_system_prompt=workflow_system_prompt,
                # Heal goals are action-phrased; after the page navigates, only the action
                # history can evidence completion.
                include_action_history_in_verification=True,
            )
            escalation_task = await app.DATABASE.tasks.update_task(
                task_id=escalation_task.task_id,
                organization_id=organization_id,
                status=TaskStatus.running,
            )
            escalation_step = await app.DATABASE.tasks.create_step(
                escalation_task.task_id,
                order=0,
                retry_index=0,
                organization_id=organization_id,
            )
            # Child block parented to the code block, linked to the escalation task: the timeline attaches
            # the heal's actions to this nested node (actions join by task_id), while the code block keeps
            # the seat task so its own pre-failure actions stay visible too.
            recovery_block = await app.DATABASE.observer.create_workflow_run_block(
                workflow_run_id=workflow_run_id,
                parent_workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                task_id=escalation_task.task_id,
                label="Self-heal recovery",
                block_type=BlockType.TASK,
            )
            recovery_block_id = recovery_block.workflow_run_block_id

            # Attribute the heal's steps to the escalation task for its duration; restored in finally.
            current_context = skyvern_context.ensure_context()
            previous_task_id = current_context.task_id
            current_context.task_id = escalation_task.task_id
            try:
                # execute_step self-drives to a terminal across multiple steps (execute_all_steps),
                # bounded by the task's max_steps_per_run — not a single step.
                await app.agent.execute_step(
                    organization=organization,
                    task=escalation_task,
                    step=escalation_step,
                    task_block=None,
                    browser_session_id=browser_session_id,
                    close_browser_on_completion=False,
                )
            finally:
                current_context.task_id = previous_task_id

            updated_task = await app.DATABASE.tasks.get_task(
                task_id=escalation_task.task_id, organization_id=organization_id
            )
            if updated_task is None or not updated_task.status.is_final():
                await self._fail_escalation_task(escalation_task, escalation_step, recovery_block_id, organization_id)
                return await self.build_block_result(
                    success=False,
                    failure_reason=f"Self-heal escalation did not reach a final status for block {self.label}",
                    output_parameter_value=None,
                    status=BlockStatus.failed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

            if updated_task.status == TaskStatus.completed:
                downloaded_files: list[FileInfo] = []
                try:
                    async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                        downloaded_files = await app.STORAGE.get_downloaded_files(
                            organization_id=organization_id,
                            run_id=current_context.run_id if current_context.run_id else workflow_run_id,
                        )
                except asyncio.TimeoutError:
                    LOG.warning("Timeout getting downloaded files", task_id=updated_task.task_id)
                downloaded_files = filter_downloaded_files_for_current_iteration(
                    downloaded_files,
                    current_context.loop_internal_state,
                )
                task_output = TaskOutput.from_task(updated_task, downloaded_files)
                output_parameter_value = workflow_run_context.mask_secrets_in_data(task_output.model_dump())
                await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output_parameter_value)
                await self._finalize_recovery_block(recovery_block_id, BlockStatus.completed, organization_id)
                return await self.build_block_result(
                    success=True,
                    failure_reason=None,
                    output_parameter_value=output_parameter_value,
                    status=BlockStatus.completed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            from skyvern.forge.sdk.workflow.models.block import TASK_TO_BLOCK_STATUS

            recovery_status = TASK_TO_BLOCK_STATUS.get(updated_task.status, BlockStatus.failed)
            recovery_failure_reason = (
                updated_task.failure_reason or f"Self-heal escalation finished with status {updated_task.status}"
            )
            await self._finalize_recovery_block(
                recovery_block_id, recovery_status, organization_id, failure_reason=recovery_failure_reason
            )
            return await self.build_block_result(
                success=False,
                failure_reason=recovery_failure_reason,
                output_parameter_value=None,
                status=recovery_status,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except asyncio.CancelledError:
            # CancelledError is BaseException, not Exception — finalize explicitly, then never swallow it.
            await self._fail_escalation_task(escalation_task, escalation_step, recovery_block_id, organization_id)
            raise
        except Exception:
            LOG.warning(
                "Code block self-heal escalation failed; falling back to fail-closed",
                workflow_run_block_id=workflow_run_block_id,
                workflow_run_id=workflow_run_id,
                exc_info=True,
            )
            await self._fail_escalation_task(escalation_task, escalation_step, recovery_block_id, organization_id)
            return None

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

        # A prompt-bearing code block gets a task v1 + step so its recorded calls render through
        # the standard action/artifact timeline and the agent can later take over on failure.
        # Promptless blocks have no task and persist neither actions nor screenshots.
        recorder = CodeBlockActionRecording(
            code_block=self,
            page=page,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            workflow_run_context=workflow_run_context,
        )
        await recorder.create_task_and_step()
        recording_page = recorder.recording_page

        try:
            await recorder.link_block()
            user_function = self.generate_async_user_function(self.code, recording_page, parameter_values)
            result = await self.execute_user_function_with_timeout(
                user_function,
                settings.CODE_BLOCK_EXECUTION_TIMEOUT_SECONDS,
            )
        except InsecureCodeDetected as e:
            await recorder.persist(recorder.recorded_actions())
            await recorder.finalize(success=False)
            return await self.build_block_result(
                success=False,
                failure_reason=str(e),
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except asyncio.TimeoutError:
            await recorder.persist(recorder.recorded_actions())
            await recorder.finalize(success=False)
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
            recorded = recorder.recorded_actions()
            if recorder.last_recorded_exception() is not e:
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
            await recorder.persist(recorded)
            healed = await self._attempt_self_heal(
                exception=e,
                failing_line=failing_line,
                recording_page=recording_page,
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
            )
            if healed is not None:
                # Finalize the seat task to the heal outcome before the idempotent `finally` no-ops it,
                # so a healed success no longer leaves the seat row failed under a completed block.
                await recorder.finalize(success=healed.success)
                return healed
            await recorder.finalize(success=False)
            return await self.build_block_result(
                success=False,
                failure_reason=failure_reason,
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        else:
            await recorder.persist(recorder.recorded_actions())
            await recorder.finalize(success=True)
        finally:
            # Safety net for paths the except arms miss (CancelledError, link_block failure).
            await recorder.finalize(success=False)

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
