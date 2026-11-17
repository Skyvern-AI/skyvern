from __future__ import annotations

from typing import Any, ClassVar, Literal

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from skyvern.forge.sdk.integrations.catalog import (
    IntegrationAction,
    IntegrationEntry,
    all_integration_entries,
    entry_action,
)
from skyvern.forge.sdk.integrations.connected_accounts import connected_account_fields
from skyvern.forge.sdk.workflow.models.block import Block, render_failure_reason
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE
from skyvern.schemas.emails import is_connected_gmail_id
from skyvern.schemas.workflows import BLOCK_TYPE_MAP, BlockResult, BlockStatus, BlockType


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


def _nango_only(entry: IntegrationEntry) -> bool:
    return entry.credential_backend == "nango" and entry.oauth_provider is None


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

    def inner_block(self) -> Block:
        """The existing block this action runs as, under this block's label and output."""
        # Late import: the converter imports every block model, this one included.
        from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block  # noqa: PLC0415

        found = entry_action(all_integration_entries(), self.integration_id, self.action_id)
        if found is None:
            raise IntegrationActionError(f"Unknown integration action {self.integration_id}.{self.action_id}.")
        entry, action = found
        kind, _, block_type = action.runs_via.partition(":")
        # A Nango account's per-run hop token is minted only for the concrete block types, so the inner block would
        # fail its capability check.
        if kind != "block" or _nango_only(entry):
            raise IntegrationActionError(f"{entry.name}: {action.name} is not supported yet.")
        if is_connected_gmail_id(self.connection_id):
            raise IntegrationActionError(f"{entry.name}: {action.name} with this account is not supported yet.")
        validate_action_inputs(action, self.inputs)

        # The block's own settings first, so an action input or pin of the same name is never shadowed by them.
        fields: dict[str, Any] = self.model_dump(include=_INHERITED_FIELDS, exclude_none=True)
        fields |= {name: schema["const"] for name, schema in action.inputs["properties"].items() if "const" in schema}
        fields |= self.inputs
        account_fields = connected_account_fields({"block_type": block_type, **fields})
        if self.connection_id:
            if not account_fields:
                raise IntegrationActionError(f"{entry.name}: {action.name} takes no connected account.")
            fields[account_fields[0]] = self.connection_id
        elif account_fields:
            raise IntegrationActionError(f"{entry.name}: {action.name} needs a connected account.")
        yaml_class = BLOCK_TYPE_MAP[block_type]
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
        try:
            inner = rendered.inner_block()
        except IntegrationActionError as e:
            return await self._failure(str(e), workflow_run_block_id, effective_org_id)
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
