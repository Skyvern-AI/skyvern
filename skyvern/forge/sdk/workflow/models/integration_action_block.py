from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any, ClassVar, Literal

import structlog
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from skyvern.forge import app
from skyvern.forge.sdk.integrations.action_runners import (
    ACTION_RUNNERS,
    ActionRunContext,
    ActionRunFailed,
    ActionRunner,
)
from skyvern.forge.sdk.integrations.catalog import (
    IntegrationAction,
    IntegrationEntry,
    action_credential_backends,
    all_integration_entries,
    entry_action,
    entry_credential_backends,
)
from skyvern.forge.sdk.integrations.connected_accounts import connected_account_fields
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import Block, render_failure_reason
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE
from skyvern.schemas.workflows import BLOCK_TYPE_MAP, BlockResult, BlockStatus, BlockType

LOG = structlog.get_logger()


class IntegrationActionError(Exception):
    pass


def _templated(value: Any) -> bool:
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def _accepts_text(schema: dict[str, Any]) -> bool:
    return schema.get("type") == "string" or any(_accepts_text(option) for option in schema.get("anyOf", []))


def validate_action_inputs(action: IntegrationAction, inputs: dict[str, Any]) -> None:
    """Raise IntegrationActionError naming the first input that does not fit the action's schema. A templated
    text value renders at run time, so only its presence is checked."""
    properties = action.inputs.get("properties", {})
    if unknown := sorted(set(inputs) - set(properties)):
        raise IntegrationActionError(f"Unknown input {unknown[0]!r}; this action takes {sorted(properties)}.")
    templated = set()
    for name, value in inputs.items():
        if _templated(value) and "const" not in properties[name]:
            if not _accepts_text(properties[name]):
                raise IntegrationActionError(f"Input {name!r} is not text, so it cannot use a template.")
            templated.add(name)
    schema = {**action.inputs, "properties": {k: v for k, v in properties.items() if k not in templated}}
    errors = sorted(Draft202012Validator(schema).iter_errors(inputs), key=lambda e: [str(p) for p in e.path])
    if errors:
        if errors[0].validator == "required":
            missing = sorted(set(errors[0].validator_value) - set(errors[0].instance))
            field = ".".join([*(str(part) for part in errors[0].path), missing[0]])
            raise IntegrationActionError(f"Input {field!r} is required.")
        # The rule, never the message: jsonschema's message repeats the input value.
        field = ".".join(str(part) for part in errors[0].path) or "inputs"
        expected = errors[0].validator_value if errors[0].validator in ("type", "enum", "const") else None
        detail = f"expected {expected}" if expected is not None else f"fails {errors[0].validator}"
        raise IntegrationActionError(f"Input {field!r} does not fit this action ({detail}).")


def _takes_nango(entry: IntegrationEntry) -> bool:
    return "nango" in entry_credential_backends(entry)


def _uses_nango(entry: IntegrationEntry, action: IntegrationAction, nango: bool) -> bool:
    # An entry that takes both kinds of account cannot tell them apart from the id; the caller looked it up.
    return "nango" in action_credential_backends(entry, action) and (entry.oauth_provider is None or nango)


def _unsupported_account_reason(entry: IntegrationEntry) -> str:
    return f"{entry.name}: this connected account is not available for this action yet."


def runnable_blocks(blocks: Sequence[Block], nango_connection_ids: Collection[str] = frozenset()) -> list[Block]:
    """Each saved block as it runs: an integration action becomes the existing block it runs via, under the same
    label, so per-block checks keyed on that block type apply to it unchanged. One that cannot resolve is left out.
    ``nango_connection_ids`` are the account ids a lookup found to be Nango connections."""
    runnable: list[Block] = []
    for block in blocks:
        if not isinstance(block, IntegrationActionBlock):
            runnable.append(block)
            continue
        try:
            runnable.append(block.inner_block(nango=block.connection_id in nango_connection_ids))
        except IntegrationActionError:
            continue
    return runnable


async def nango_account_ids(blocks: Sequence[Block], organization_id: str) -> set[str]:
    """The integration action accounts among ``blocks`` that are Nango connections of the organization, by lookup
    (ADR 0033). A failed lookup finds none, which still fails closed: no other backend resolves a Nango account."""
    candidates = {
        block.connection_id
        for block in blocks
        if isinstance(block, IntegrationActionBlock) and block.connection_id and block.may_use_nango()
    }
    if not candidates:
        return set()
    try:
        return await app.AGENT_FUNCTION.nango_connection_ids(organization_id, candidates)
    except Exception as exc:  # noqa: BLE001 - one unreadable lookup must not stop a run's other blocks.
        LOG.warning("Nango account lookup failed", organization_id=organization_id, error_type=type(exc).__name__)
        return set()


_INHERITED_FIELDS = frozenset(
    {"ignore_workflow_system_prompt", "model", "notes", "trailing_notes", "description", "prompt_images", "doc_images"}
)


class IntegrationActionBlock(Block):
    block_type: Literal[BlockType.INTEGRATION_ACTION] = BlockType.INTEGRATION_ACTION  # type: ignore

    integration_id: str
    action_id: str
    connection_id: str | None = None
    inputs: dict[str, Any] = {}
    parameters: list[PARAMETER_TYPE] = []

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"connection_id"})

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        return self.parameters

    def runs_block_type(self) -> BlockType | None:
        """The existing block type this action runs as, or None for an unknown or non-block action."""
        found = entry_action(all_integration_entries(), self.integration_id, self.action_id)
        kind, _, block_type = found[1].runs_via.partition(":") if found else ("", "", "")
        return BlockType(block_type) if kind == "block" else None

    def action_runner(self) -> tuple[IntegrationEntry, IntegrationAction, ActionRunner] | None:
        """The catalog action and our own runner for a ``runner:`` action, or None for any other action."""
        found = entry_action(all_integration_entries(), self.integration_id, self.action_id)
        kind, _, name = found[1].runs_via.partition(":") if found else ("", "", "")
        if found is None or kind != "runner" or name not in ACTION_RUNNERS:
            return None
        return found[0], found[1], ACTION_RUNNERS[name]

    async def _run_with_runner(
        self,
        entry: IntegrationEntry,
        action: IntegrationAction,
        runner: ActionRunner,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str,
    ) -> BlockResult:
        try:
            validate_action_inputs(action, self.inputs)
        except IntegrationActionError as e:
            return await self._failure(str(e), workflow_run_block_id, organization_id)
        connection_id = self.connection_id and (
            workflow_run_context.get_original_secret_value_or_none(self.connection_id) or self.connection_id
        )
        if not connection_id:
            return await self._failure(
                f"{entry.name}: {action.name} needs a connected account.", workflow_run_block_id, organization_id
            )
        broker_connection = _takes_nango(entry) and bool(
            await app.AGENT_FUNCTION.nango_connection_ids(organization_id, {connection_id})
        )
        if broker_connection and "nango" not in action_credential_backends(entry, action):
            return await self._failure(
                _unsupported_account_reason(entry),
                workflow_run_block_id,
                organization_id,
            )
        inputs = dict(self.inputs)
        # Values an agent wrote are text, never templates.
        if "inputs" not in self._prerendered_fields:
            try:
                inputs = {
                    name: self.format_block_parameter_template_from_workflow_run_context(value, workflow_run_context)
                    if _templated(value)
                    else [
                        self.format_block_parameter_template_from_workflow_run_context(item, workflow_run_context)
                        if _templated(item)
                        else item
                        for item in value
                    ]
                    if action.runs_via == "runner:google_drive_download" and name == "items" and isinstance(value, list)
                    else value
                    for name, value in inputs.items()
                }
            except Exception as e:
                return await self._template_format_failure_result(
                    e,
                    render_failure_reason(
                        "Failed to format jinja template:", e, self.secret_redactor(workflow_run_context)
                    ),
                    workflow_run_context,
                    workflow_run_id,
                    workflow_run_block_id,
                    organization_id,
                )
        try:
            run_context = ActionRunContext(
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                hop_token=(
                    workflow_run_context.get_google_drive_hop_token(self.label)
                    if broker_connection and action.drive_hop_operations
                    else None
                ),
                broker_connection=broker_connection,
                hop_refusal=(
                    workflow_run_context.get_google_drive_hop_refusal(self.label)
                    if broker_connection and action.drive_hop_operations
                    else None
                ),
            )
            output = await runner(organization_id, connection_id, action.id, inputs, run_context)
        except ActionRunFailed as e:
            if e.output is not None:
                await self.record_output_parameter_value(workflow_run_context, workflow_run_id, e.output)
            return await self.build_block_result(
                success=False,
                failure_reason=e.reason,
                output_parameter_value=e.output,
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                error_codes=[e.error_code] if e.error_code else None,
            )
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=output,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )

    def may_use_nango(self) -> bool:
        found = entry_action(all_integration_entries(), self.integration_id, self.action_id)
        return found is not None and _takes_nango(found[0])

    def inner_block(self, nango: bool = False) -> Block:
        """The existing block this action runs as, under this block's label and output."""
        # Late import: the converter imports every block model, this one included.
        from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block

        found = entry_action(all_integration_entries(), self.integration_id, self.action_id)
        if found is None:
            raise IntegrationActionError(f"Unknown integration action {self.integration_id}.{self.action_id}.")
        entry, action = found
        kind, _, block_type = action.runs_via.partition(":")
        if kind != "block":
            raise IntegrationActionError(
                f"{entry.name}: {action.name} cannot run: every action must run through a block."
            )
        validate_action_inputs(action, self.inputs)

        # The block's own settings first, so an action input or pin of the same name is never shadowed by them.
        fields: dict[str, Any] = self.model_dump(include=_INHERITED_FIELDS, exclude_none=True)
        fields |= {name: schema["const"] for name, schema in action.inputs["properties"].items() if "const" in schema}
        fields |= self.inputs
        yaml_class = BLOCK_TYPE_MAP[block_type]
        account_fields = connected_account_fields({"block_type": block_type, **fields})
        if self.connection_id:
            if _uses_nango(entry, action, nango):
                if "credential_source" in yaml_class.model_fields:
                    fields["credential_source"] = "nango"
                account_fields = ("connection_id",)
            elif not account_fields:
                raise IntegrationActionError(f"{entry.name}: {action.name} takes no connected account.")
            fields[account_fields[0]] = self.connection_id
        elif account_fields:
            raise IntegrationActionError(f"{entry.name}: {action.name} needs a connected account.")
        if "parameter_keys" in yaml_class.model_fields:
            fields["parameter_keys"] = [parameter.key for parameter in self.parameters]
        try:
            block_yaml = yaml_class.model_validate({**fields, "block_type": block_type, "label": self.label})
        except ValidationError as e:
            # Field names only: pydantic's message repeats the input values, which may be sensitive.
            names = sorted({".".join(str(part) for part in error["loc"]) for error in e.errors()})
            raise IntegrationActionError(f"{entry.name}: {action.name} has invalid inputs: {names}.") from None
        parameters: dict[str, PARAMETER_TYPE] = {parameter.key: parameter for parameter in self.parameters}
        parameters[self.output_parameter.key] = self.output_parameter
        inner = block_yaml_to_block(block_yaml, parameters)  # type: ignore[arg-type]
        if self.connection_id:
            # This block renders the account reference itself; rendering it again would evaluate a run value as Jinja.
            inner.mark_fields_prerendered(account_fields[:1])
        return inner

    async def _failure(self, reason: str, workflow_run_block_id: str, organization_id: str | None) -> BlockResult:
        return await self.build_block_result(
            success=False,
            failure_reason=reason,
            output_parameter_value=None,
            status=BlockStatus.failed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: Any,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        effective_org_id = organization_id or workflow_run_context.organization_id
        if not effective_org_id:
            return await self._failure("Integration action has no organization to run in.", workflow_run_block_id, None)
        rendered = self
        if self.connection_id:
            try:
                # Rendered into a copy, so a later loop iteration still sees the template.
                rendered = self.model_copy(
                    update={
                        "connection_id": self.render_templatable_field(
                            "connection_id", self.connection_id, workflow_run_context
                        )
                    }
                )
            except Exception as e:
                return await self._template_format_failure_result(
                    e,
                    render_failure_reason(
                        "Failed to format jinja template:", e, self.secret_redactor(workflow_run_context)
                    ),
                    workflow_run_context,
                    workflow_run_id,
                    workflow_run_block_id,
                    effective_org_id,
                )
        if runner := rendered.action_runner():
            ran: Block = rendered
            try:
                return await rendered._run_with_runner(
                    *runner, workflow_run_context, workflow_run_id, workflow_run_block_id, effective_org_id
                )
            finally:
                # The runner recorded on the rendered copy; the outer wrapper reads this block's flag.
                self._output_recorded_this_execution = ran._output_recorded_this_execution
        nango = False
        if rendered.connection_id and rendered.may_use_nango():
            try:
                nango = bool(await app.AGENT_FUNCTION.nango_connection_ids(effective_org_id, {rendered.connection_id}))
            except Exception as exc:  # noqa: BLE001 - an account of unknown kind must not run as either kind.
                LOG.warning(
                    "Nango account lookup failed", organization_id=effective_org_id, error_type=type(exc).__name__
                )
                return await self._failure(
                    "Skyvern could not check this block's account. Run it again.",
                    workflow_run_block_id,
                    effective_org_id,
                )
        found = entry_action(all_integration_entries(), rendered.integration_id, rendered.action_id)
        if nango and found is not None and "nango" not in action_credential_backends(*found):
            return await self._failure(
                _unsupported_account_reason(found[0]),
                workflow_run_block_id,
                effective_org_id,
            )
        try:
            inner = rendered.inner_block(nango=nango)
        except IntegrationActionError as e:
            return await self._failure(str(e), workflow_run_block_id, effective_org_id)
        # Values an agent wrote are text, never templates, so the inner block must not render them either.
        if "inputs" in self._prerendered_fields:
            inner.mark_fields_prerendered(self.inputs)
        inner._agent_smtp_isolation = self._agent_smtp_isolation
        try:
            return await inner.execute(
                workflow_run_id,
                workflow_run_block_id,
                organization_id=effective_org_id,
                browser_session_id=browser_session_id,
                **kwargs,
            )
        finally:
            # The outer block's wrapper clears a failed block's output unless this block recorded one.
            self._output_recorded_this_execution = inner._output_recorded_this_execution

    async def _generate_workflow_run_block_description(
        self, workflow_run_block_id: str, organization_id: str | None = None
    ) -> None:
        # Described as the block it runs, so that block's exclusions apply, such as keeping Gmail contents out.
        try:
            inner = self.inner_block()
        except IntegrationActionError:
            return
        await inner._generate_workflow_run_block_description(workflow_run_block_id, organization_id)
