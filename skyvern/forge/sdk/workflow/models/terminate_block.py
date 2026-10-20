from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, ClassVar, Literal

from jinja2 import meta as jinja2_meta
from jinja2 import nodes

from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import Block, jinja_sandbox_env
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType, error_code_key_error
from skyvern.utils.secret_redaction import (
    MIN_NUMERIC_SECRET_LENGTH,
    MIN_SECRET_LENGTH,
    REDACTED_SECRET_PLACEHOLDER,
    redact_secrets_from_text,
)


class TerminateBlock(Block):
    block_type: Literal[BlockType.TERMINATE] = BlockType.TERMINATE  # type: ignore

    reason: str
    error_code: str | None = None

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"reason", "error_code"})
    FAILURE_IS_CONTINUABLE: ClassVar[bool] = False

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        return []

    def _template_contains_registered_secret(self, template: str, workflow_run_context: WorkflowRunContext) -> bool:
        secrets = self._registered_secret_values(workflow_run_context)
        # Template syntax is not output; use the deletion floor to avoid short-secret collisions in source text.
        floored_secrets = {
            secret
            for secret in secrets
            if len(secret) >= MIN_SECRET_LENGTH and not (secret.isdigit() and len(secret) < MIN_NUMERIC_SECRET_LENGTH)
        }

        def is_exact_secret_or_encoding(value: str) -> bool:
            return redact_secrets_from_text(value, secrets, match_inside_words=True) == REDACTED_SECRET_PLACEHOLDER

        source_literals = (
            value for _, token_type, value in jinja_sandbox_env.lex(template) if token_type in {"data", "comment"}
        )
        if any(
            is_exact_secret_or_encoding(value)
            or redact_secrets_from_text(value, floored_secrets, match_inside_words=True) != value
            for value in source_literals
        ):
            return True

        parsed_template = jinja_sandbox_env.parse(template)
        if any(
            isinstance(node.value, str)
            and (
                is_exact_secret_or_encoding(node.value)
                or redact_secrets_from_text(node.value, floored_secrets, match_inside_words=True) != node.value
            )
            for node in parsed_template.find_all(nodes.Const)
        ):
            return True
        output_scalar_constant_values = (
            str(node.value)
            for output in parsed_template.find_all(nodes.Output)
            for node in output.find_all(nodes.Const)
            if node.value is None or isinstance(node.value, (int, float))
        )
        if any(
            is_exact_secret_or_encoding(value)
            or redact_secrets_from_text(value, floored_secrets, match_inside_words=True) != value
            for value in output_scalar_constant_values
        ):
            return True

        referenced_names = jinja2_meta.find_undeclared_variables(parsed_template)
        secret_references = set(workflow_run_context.secrets) | secrets
        references: list[tuple[str, tuple[str | int, ...]]] = []
        template_values = self._build_block_parameter_template_data(
            workflow_run_context,
            copy_block_metadata=True,
            include_workflow_run_summary="workflow_run_summary" in referenced_names,
        )

        def access_path(node: nodes.Node) -> tuple[str, tuple[str | int, ...]] | None:
            if isinstance(node, nodes.Name):
                return node.name, ()
            if isinstance(node, nodes.Getattr):
                parent = access_path(node.node)
                return (parent[0], (*parent[1], node.attr)) if parent else None
            if isinstance(node, nodes.Getitem):
                if isinstance(node.arg, nodes.Const):
                    key = node.arg.value
                elif isinstance(node.arg, nodes.Name):
                    key = template_values.get(node.arg.name)
                else:
                    return None
                parent = access_path(node.node)
                return (parent[0], (*parent[1], key)) if parent and isinstance(key, (str, int)) else None
            if (
                isinstance(node, nodes.Call)
                and isinstance(node.node, nodes.Getattr)
                and node.node.attr == "get"
                and len(node.args) in (1, 2)
                and not node.kwargs
                and isinstance(node.args[0], nodes.Const)
                and isinstance(node.args[0].value, (str, int))
            ):
                parent = access_path(node.node.node)
                return (parent[0], (*parent[1], node.args[0].value)) if parent else None
            return None

        def collect_references(node: nodes.Node, *, direct: bool = False) -> None:
            if not direct and isinstance(node, (nodes.Getattr, nodes.Getitem, nodes.Call)):
                path = access_path(node)
                if path:
                    if path[0] in referenced_names:
                        references.append(path)
                    if isinstance(node, nodes.Getitem) and isinstance(node.arg, nodes.Name):
                        references.append((node.arg.name, ()))
                    if isinstance(node, nodes.Call):
                        for argument in node.args[1:]:
                            collect_references(argument, direct=direct)
                    return
            if isinstance(node, nodes.Name):
                if node.name in referenced_names:
                    references.append((node.name, ()))
                return
            for child in node.iter_child_nodes():
                # Unknown calls may inspect any part of their inputs.
                collect_references(child, direct=direct or isinstance(node, nodes.Call))

        collect_references(parsed_template)

        def contains_secret_reference(value: Any) -> bool:
            if isinstance(value, (str, bool, int, float, Decimal)):
                text = value if isinstance(value, str) else str(value)
                return (
                    text in secret_references
                    or is_exact_secret_or_encoding(text)
                    or redact_secrets_from_text(text, floored_secrets, match_inside_words=True) != text
                )
            if isinstance(value, Mapping):
                return any(
                    contains_secret_reference(key) or contains_secret_reference(item) for key, item in value.items()
                )
            if isinstance(value, (list, tuple)):
                return any(contains_secret_reference(item) for item in value)
            return False

        for name, path in references:
            value = template_values.get(name)
            if name in secret_references:
                return True
            if path:
                for key in path:
                    if isinstance(value, Mapping):
                        value = value.get(key)
                    elif isinstance(value, Sequence) and not isinstance(value, str) and isinstance(key, int):
                        value = value[key] if -len(value) <= key < len(value) else None
                    else:
                        value = getattr(value, key, None) if isinstance(key, str) else None
                if contains_secret_reference(value):
                    return True
            elif contains_secret_reference(value):
                return True
        return False

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: Any,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        secrets = self._registered_secret_values(workflow_run_context)
        reason_secrets = {
            secret
            for secret in secrets
            if len(secret) >= MIN_SECRET_LENGTH and not (secret.isdigit() and len(secret) < MIN_NUMERIC_SECRET_LENGTH)
        }
        error_code = ""
        reason_dropped = False
        try:
            reason_dropped = self._template_contains_registered_secret(self.reason, workflow_run_context)
            reason = ""
            if not reason_dropped:
                rendered_reason = self.render_templatable_field("reason", self.reason, workflow_run_context)
                rendered_reason_casefolded = rendered_reason.casefold()
                if any(secret.casefold() in rendered_reason_casefolded for secret in reason_secrets):
                    reason_dropped = True
                else:
                    reason = rendered_reason
        except Exception as exc:
            return await self._template_format_failure_result(
                exc, str(exc), workflow_run_context, workflow_run_id, workflow_run_block_id, organization_id
            )

        error_code_dropped = False
        if self.error_code:
            try:
                if self._template_contains_registered_secret(self.error_code, workflow_run_context):
                    error_code_dropped = True
                else:
                    rendered_error_code = self.render_templatable_field(
                        "error_code", self.error_code, workflow_run_context
                    )
                    error_code = rendered_error_code.strip()
                    rendered_error_code_casefolded = rendered_error_code.casefold()
                    if error_code and (
                        error_code_key_error(error_code)
                        or re.fullmatch(r"[A-Za-z0-9_.:-]+", error_code) is None
                        or redact_secrets_from_text(rendered_error_code, secrets, match_inside_words=True)
                        != rendered_error_code
                        or any(secret.casefold() in rendered_error_code_casefolded for secret in secrets)
                    ):
                        error_code_dropped = True
                        error_code = ""
            except Exception:
                error_code_dropped = True
                error_code = ""

        if not reason.strip():
            # A template such as "{{ code }}" can render blank; the run still stops, with a reason that says where.
            if any(secret.casefold() in self.label.casefold() for secret in reason_secrets):
                reason = "Terminated by a Terminate block"
            else:
                reason = f"Terminated by the {self.label} block"
        if reason_dropped:
            reason += ". Reason was dropped because it was unusable after rendering."
        if error_code_dropped:
            reason += " Error code was dropped because it was unusable after rendering."
        reason = redact_secrets_from_text(reason, secrets)
        output: dict[str, Any] = {"reason": reason}
        if error_code_dropped:
            output["error_code_dropped"] = True
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output)
        return await self.build_block_result(
            success=False,
            failure_reason=reason,
            output_parameter_value=output,
            status=BlockStatus.terminated,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            error_codes=[error_code] if error_code else None,
        )
