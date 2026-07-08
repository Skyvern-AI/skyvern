"""Control-flow blocks: ForLoopBlock, WhileLoopBlock, and ConditionalBlock.

Extracted from block.py (8/8). Imports Block + helpers from block_base and the branching
subsystem from branching.py; shared helpers still come from block.py (block-first import).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, Field, model_validator

from skyvern.constants import (
    GET_DOWNLOADED_FILES_TIMEOUT,
)
from skyvern.exceptions import (
    ContextParameterValueNotFound,
)
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import (
    resolve_run_download_id,
)
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.workflow.context_manager import BlockMetadata, WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import (
    FailedToFormatJinjaStyleParameter,
    InvalidWorkflowDefinition,
    MissingJinjaVariables,
    NoIterableValueFound,
)
from skyvern.forge.sdk.workflow.loop_download_filter import (
    DOWNLOADED_FILE_SIGS_KEY,
    to_downloaded_file_signature,
)
from skyvern.forge.sdk.workflow.models.block import (
    DEFAULT_MAX_LOOP_ITERATIONS,
    DEFAULT_MAX_STEPS_PER_ITERATION,
    MAX_LOOP_OVER_VALUE_LOG_CHARS,
    PERSIST_LOOP_OUTPUT_INTERVAL,
    _maybe_truncate_loop_outputs,
)
from skyvern.forge.sdk.workflow.models.block_base import (  # noqa: F401  (re-exported for tests/back-compat)
    CURRENT_DATE_FORMAT,
    MAX_STEPS_DOWNLOAD_WARNING_THRESHOLD,
    Block,
    capture_block_download_baseline,
    jinja_sandbox_env,
    warn_if_file_download_max_steps_low,
)
from skyvern.forge.sdk.workflow.models.branching import (
    BranchCondition,
    BranchCriteriaTypeVar,
    BranchEvaluationContext,
    PromptBranchCriteria,
    _cap_debug_field,
    _evaluate_prompt_branch_conditions_batch,
    _render_jinja_expression_for_display,
    _trim_branch_evaluations,
)
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
    ContextParameter,
    OutputParameter,
    ParameterType,
    WorkflowParameter,
)
from skyvern.forge.sdk.workflow.models.task_blocks import ExtractionBlock
from skyvern.schemas.workflows import (  # noqa: F401  # re-exported for callers importing FileType from this module
    AIFallbackMode,
    BlockResult,
    BlockStatus,
    BlockType,
    FileType,
)
from skyvern.utils.strings import generate_random_string

LOG = structlog.get_logger()

if TYPE_CHECKING:
    from skyvern.forge.sdk.workflow.models.block import BlockTypeVar


class LoopBlockExecutedResult(BaseModel):
    outputs_with_loop_values: list[list[dict[str, Any]]]
    block_outputs: list[BlockResult]
    last_block: BlockTypeVar | None
    # True only when the loop exhausted all iterations naturally (for-loop) or the
    # condition turned false (while-loop). False on every early-return path
    # (cancel, structural error, max iterations, body failure with no swallow flag).
    natural_completion: bool = False

    def is_canceled(self) -> bool:
        return len(self.block_outputs) > 0 and self.block_outputs[-1].status == BlockStatus.canceled

    def is_synthetic_loop_failure(self) -> bool:
        """Last appended result is a loop-structural / safety-limit failure, not a child."""
        return bool(self.block_outputs) and self.block_outputs[-1].is_synthetic_loop_failure

    def is_completed(self) -> bool:
        if len(self.block_outputs) == 0:
            return False

        if self.last_block is None:
            return False

        if self.is_canceled():
            return False

        last_ouput = self.block_outputs[-1]
        if last_ouput.success:
            return True

        # Swallow flags apply only on natural-completion paths whose last result
        # is a real child failure; structural/safety synthetics must propagate.
        if not self.natural_completion or self.is_synthetic_loop_failure():
            return False

        if self.last_block.continue_on_failure:
            return True

        if self.last_block.next_loop_on_failure:
            return True

        return False

    def is_terminated(self) -> bool:
        return len(self.block_outputs) > 0 and self.block_outputs[-1].status == BlockStatus.terminated

    def get_failure_reason(self) -> str | None:
        if self.is_completed():
            return None

        if self.is_canceled():
            return f"Block({self.last_block.label if self.last_block else ''}) with type {self.last_block.block_type if self.last_block else ''} was canceled, canceling for loop"

        return self.block_outputs[-1].failure_reason if len(self.block_outputs) > 0 else "No block has been executed"

    def resolve_status(self, parent_next_loop_on_failure: bool) -> tuple[BlockStatus, bool, str | None]:
        """Decide the loop block's overall status, success flag, and failure_reason.

        ``parent_next_loop_on_failure`` is the parent loop's swallow flag; when
        set, body failures swallowed mid-loop must not re-surface as the loop's
        overall status. Synthetic safety/structural failures still propagate.
        """
        parent_swallow = (
            parent_next_loop_on_failure
            and self.natural_completion
            and not self.is_canceled()
            and not self.is_synthetic_loop_failure()
        )

        if self.is_canceled():
            block_status = BlockStatus.canceled
            success = False
        elif self.is_completed() or parent_swallow:
            block_status = BlockStatus.completed
            success = True
        elif self.is_terminated():
            block_status = BlockStatus.terminated
            success = False
        else:
            block_status = BlockStatus.failed
            success = False

        failure_reason = None if success else self.get_failure_reason()
        return block_status, success, failure_reason


def compute_conditional_scopes(
    label_to_block: dict[str, Any],
    default_next_map: dict[str, str | None],
) -> dict[str, str]:
    """Map each block label to the conditional block label whose scope it belongs to.

    For each conditional block, trace each branch's chain of blocks via
    ``default_next_map``.  Labels that appear in **all** branch chains are
    considered merge-point blocks (i.e. they come *after* the conditional
    reconverges) and are **not** scoped.  Labels that appear in fewer chains
    than the total number of branches **are** inside the conditional.

    Inner conditionals are themselves scoped to an outer conditional, but
    their *own* branch targets are handled by a recursive application of
    the same logic (inner wins via the ``if lbl not in scopes`` guard).
    """
    scopes: dict[str, str] = {}

    conditional_labels = [lbl for lbl, blk in label_to_block.items() if blk.block_type == BlockType.CONDITIONAL]

    for cond_label in conditional_labels:
        cond_block = label_to_block[cond_label]
        branch_targets: list[str | None] = [branch.next_block_label for branch in cond_block.ordered_branches]
        # Deduplicate while preserving order – two branches may point to the same target
        seen_targets: set[str | None] = set()
        unique_targets: list[str | None] = []
        for t in branch_targets:
            if t not in seen_targets:
                seen_targets.add(t)
                unique_targets.append(t)

        num_branches = len(unique_targets)
        if num_branches == 0:
            continue

        # For each unique branch target, trace the chain via default_next_map.
        # Stop at other conditional blocks (they handle their own branches).
        chain_sets: list[list[str]] = []
        for target in unique_targets:
            chain: list[str] = []
            cur = target
            while cur and cur in label_to_block:
                chain.append(cur)
                # Stop tracing when we hit another conditional – it owns its own sub-tree
                if label_to_block[cur].block_type == BlockType.CONDITIONAL:
                    break
                cur = default_next_map.get(cur)
            chain_sets.append(chain)

        # Count how many branch chains each label appears in
        label_count: dict[str, int] = {}
        for chain in chain_sets:
            for lbl in chain:
                label_count[lbl] = label_count.get(lbl, 0) + 1

        # Labels appearing in ALL branches are merge points (after the conditional).
        # Labels appearing in fewer branches are inside the conditional.
        for chain in chain_sets:
            for lbl in chain:
                if label_count[lbl] >= num_branches:
                    # This is a merge point – stop scoping further along this chain
                    break
                if lbl not in scopes:
                    scopes[lbl] = cond_label

    return scopes


class ForLoopBlock(Block):
    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.FOR_LOOP] = BlockType.FOR_LOOP  # type: ignore

    loop_blocks: list[BlockTypeVar]
    loop_over: PARAMETER_TYPE | None = None
    loop_variable_reference: str | None = None
    complete_if_empty: bool = False
    # Note: intentionally excludes `list` (unlike BaseTaskBlock.data_schema) because a list schema
    # does not describe the shape of individual loop items -- only dict schemas are meaningful here.
    data_schema: dict[str, Any] | str | None = None

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        parameters = set()
        if self.loop_over is not None:
            parameters.add(self.loop_over)

        for loop_block in self.loop_blocks:
            for parameter in loop_block.get_all_parameters(workflow_run_id):
                parameters.add(parameter)
        return list(parameters)

    def get_loop_block_context_parameters(self, workflow_run_id: str, loop_data: Any) -> list[ContextParameter]:
        context_parameters = []

        for loop_block in self.loop_blocks:
            # todo: handle the case where the loop_block is a ForLoopBlock

            all_parameters = loop_block.get_all_parameters(workflow_run_id)
            for parameter in all_parameters:
                if isinstance(parameter, ContextParameter):
                    context_parameters.append(parameter)

        if self.loop_over is None:
            return context_parameters

        for context_parameter in context_parameters:
            if context_parameter.source.key != self.loop_over.key:
                continue
            # If the loop_data is a dict, we need to check if the key exists in the loop_data
            if isinstance(loop_data, dict):
                if context_parameter.key in loop_data:
                    context_parameter.value = loop_data[context_parameter.key]
                else:
                    raise ContextParameterValueNotFound(
                        parameter_key=context_parameter.key,
                        existing_keys=list(loop_data.keys()),
                        workflow_run_id=workflow_run_id,
                    )
            else:
                # If the loop_data is a list, we can directly assign the loop_data to the context_parameter value
                context_parameter.value = loop_data

        return context_parameters

    async def get_values_from_loop_variable_reference(
        self,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
    ) -> list[Any]:
        parameter_value = None
        if self.loop_variable_reference:
            LOG.debug("Processing loop variable reference", loop_variable_reference=self.loop_variable_reference)

            # Check if this looks like a parameter path (contains dots and/or _output)
            is_likely_parameter_path = "extracted_information." in self.loop_variable_reference

            # Try parsing as Jinja template
            parameter_value = self.try_parse_jinja_template(workflow_run_context)

            if parameter_value is None and not is_likely_parameter_path:
                try:
                    # Create and execute extraction block using the current block's workflow_id
                    extraction_block = self._create_initial_extraction_block(
                        self.loop_variable_reference, workflow_run_context=workflow_run_context
                    )

                    LOG.info(
                        "Processing natural language loop input",
                        prompt=self.loop_variable_reference,
                        extraction_goal=extraction_block.data_extraction_goal,
                    )

                    extraction_result = await extraction_block.execute(
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                    )

                    if not extraction_result.success:
                        LOG.error("Extraction block failed", failure_reason=extraction_result.failure_reason)
                        raise ValueError(
                            f"Extraction block failed: "
                            f"{extraction_result.failure_reason or 'Unknown error (no failure reason provided)'}"
                        )

                    LOG.debug("Extraction block succeeded", output=extraction_result.output_parameter_value)

                    # Store the extraction result in the workflow context
                    await extraction_block.record_output_parameter_value(
                        workflow_run_context=workflow_run_context,
                        workflow_run_id=workflow_run_id,
                        value=extraction_result.output_parameter_value,
                    )

                    # Get the extracted information
                    if not isinstance(extraction_result.output_parameter_value, dict):
                        LOG.error(
                            "Extraction result output_parameter_value is not a dict",
                            output_parameter_value=extraction_result.output_parameter_value,
                        )
                        raise ValueError("Extraction result output_parameter_value is not a dictionary")

                    if "extracted_information" not in extraction_result.output_parameter_value:
                        LOG.error(
                            "Extraction result missing extracted_information key",
                            output_parameter_value=extraction_result.output_parameter_value,
                        )
                        raise ValueError("Extraction result missing extracted_information key")

                    extracted_info = extraction_result.output_parameter_value["extracted_information"]

                    # Handle different possible structures of extracted_info
                    if isinstance(extracted_info, list):
                        # If it's a list, take the first element
                        if len(extracted_info) > 0:
                            extracted_info = extracted_info[0]
                        else:
                            LOG.error("Extracted information list is empty")
                            raise ValueError("Extracted information list is empty")

                    # At this point, extracted_info should be a dict
                    if not isinstance(extracted_info, dict):
                        LOG.error("Invalid extraction result structure - not a dict", extracted_info=extracted_info)
                        raise ValueError("Extraction result is not a dictionary")

                    # Extract the loop values
                    loop_values = extracted_info.get("loop_values", [])

                    if not loop_values:
                        LOG.error("No loop values found in extraction result")
                        raise ValueError("No loop values found in extraction result")

                    LOG.info("Extracted loop values", count=len(loop_values), values=loop_values)

                    # Update the loop variable reference to point to the extracted loop values
                    # We'll use a temporary key that we can reference
                    temp_key = f"extracted_loop_values_{generate_random_string()}"
                    workflow_run_context.set_value(temp_key, loop_values)
                    self.loop_variable_reference = temp_key

                    # Now try parsing again with the updated reference
                    parameter_value = self.try_parse_jinja_template(workflow_run_context)

                except Exception as e:
                    LOG.error("Failed to process natural language loop input", error=str(e))
                    raise FailedToFormatJinjaStyleParameter(self.loop_variable_reference, str(e))

            if parameter_value is None:
                # Fall back to the original Jinja template approach
                value_template = f"{{{{ {self.loop_variable_reference.strip(' {}')} | tojson }}}}"
                try:
                    value_json = self.format_block_parameter_template_from_workflow_run_context(
                        value_template, workflow_run_context
                    )
                except Exception as e:
                    raise FailedToFormatJinjaStyleParameter(value_template, str(e))
                parameter_value = json.loads(value_json)

        if isinstance(parameter_value, list):
            return parameter_value
        else:
            return [parameter_value]

    async def get_loop_over_parameter_values(
        self,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
    ) -> list[Any]:
        # parse the value from self.loop_variable_reference and then from self.loop_over
        if self.loop_variable_reference:
            return await self.get_values_from_loop_variable_reference(
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )
        elif self.loop_over is not None:
            if isinstance(self.loop_over, WorkflowParameter):
                parameter_value = workflow_run_context.get_value(self.loop_over.key)
            elif isinstance(self.loop_over, OutputParameter):
                # If the output parameter is for a TaskBlock, it will be a TaskOutput object. We need to extract the
                # value from the TaskOutput object's extracted_information field.
                output_parameter_value = workflow_run_context.get_value(self.loop_over.key)
                if isinstance(output_parameter_value, dict) and "extracted_information" in output_parameter_value:
                    parameter_value = output_parameter_value["extracted_information"]
                else:
                    parameter_value = output_parameter_value
            elif isinstance(self.loop_over, ContextParameter):
                parameter_value = self.loop_over.value
                if not parameter_value:
                    source_parameter_value = workflow_run_context.get_value(self.loop_over.source.key)
                    if isinstance(source_parameter_value, dict):
                        if "extracted_information" in source_parameter_value:
                            parameter_value = source_parameter_value["extracted_information"].get(self.loop_over.key)
                        else:
                            parameter_value = source_parameter_value.get(self.loop_over.key)
                    else:
                        raise ValueError("ContextParameter source value should be a dict")
            else:
                raise NotImplementedError()

        else:
            if self.complete_if_empty:
                return []
            else:
                raise NoIterableValueFound()

        if isinstance(parameter_value, list):
            return parameter_value
        else:
            # TODO (kerem): Should we raise an error here?
            return [parameter_value]

    def try_parse_jinja_template(self, workflow_run_context: WorkflowRunContext) -> Any | None:
        """Try to parse the loop variable reference as a Jinja template."""
        try:
            # Try the exact reference first
            try:
                if self.loop_variable_reference is None:
                    return None
                value_template = f"{{{{ {self.loop_variable_reference.strip(' {}')} | tojson }}}}"
                value_json = self.format_block_parameter_template_from_workflow_run_context(
                    value_template, workflow_run_context
                )
                parameter_value = json.loads(value_json)
                if parameter_value is not None:
                    return parameter_value
            except Exception:
                pass

            # If that fails, try common access patterns for extraction results
            if self.loop_variable_reference is None:
                return None
            access_patterns = [
                f"{self.loop_variable_reference}.extracted_information",
                f"{self.loop_variable_reference}.extracted_information.results",
                f"{self.loop_variable_reference}.results",
            ]

            for pattern in access_patterns:
                try:
                    value_template = f"{{{{ {pattern.strip(' {}')} | tojson }}}}"
                    value_json = self.format_block_parameter_template_from_workflow_run_context(
                        value_template, workflow_run_context
                    )
                    parameter_value = json.loads(value_json)
                    if parameter_value is not None:
                        return parameter_value
                except Exception:
                    continue

            return None
        except Exception:
            return None

    def _create_initial_extraction_block(
        self,
        natural_language_prompt: str,
        workflow_run_context: WorkflowRunContext | None = None,
    ) -> ExtractionBlock:
        """Create an extraction block to process natural language input."""

        # Determine the items schema for loop_values
        items_schema: dict[str, Any] | None = None
        if self.data_schema is not None:
            if isinstance(self.data_schema, dict):
                items_schema = self.data_schema
            elif isinstance(self.data_schema, str):
                # Interpolate Jinja templates before parsing, matching how BaseTaskBlock.setup_block_v2
                # handles data_schema strings (see line 652-654)
                schema_str = self.data_schema
                if workflow_run_context is not None:
                    schema_str = self.format_block_parameter_template_from_workflow_run_context(
                        schema_str, workflow_run_context
                    )
                try:
                    parsed = json.loads(schema_str)
                    if isinstance(parsed, dict):
                        items_schema = parsed
                    else:
                        LOG.warning(
                            "Parsed data_schema is not a dict, falling back to default string schema",
                            block_label=self.label,
                            data_schema=self.data_schema,
                        )
                except (json.JSONDecodeError, TypeError):
                    LOG.warning(
                        "Failed to parse data_schema string, falling back to default string schema",
                        block_label=self.label,
                        data_schema=self.data_schema,
                    )

        if items_schema is not None:
            # User provided a custom schema — each loop iteration will produce a structured object
            data_schema: dict[str, Any] = {
                "type": "object",
                "properties": {
                    "loop_values": {
                        "type": "array",
                        "description": "Array of structured values to iterate over, matching the provided schema.",
                        "items": items_schema,
                    }
                },
            }
        else:
            # Default: extract simple string array
            data_schema = {
                "type": "object",
                "properties": {
                    "loop_values": {
                        "type": "array",
                        "description": "Array of values to iterate over. Each value should be the primary data needed for the loop blocks.",
                        "items": {
                            "type": "string",
                            "description": "The primary value to be used in the loop iteration (e.g., URL, text, identifier, etc.)",
                        },
                    }
                },
            }

        # Create extraction goal that includes the natural language prompt
        extraction_goal = prompt_engine.load_prompt(
            "extraction_prompt_for_nat_language_loops", natural_language_prompt=natural_language_prompt
        )

        # Create a temporary output parameter using the current block's workflow_id

        output_param = OutputParameter(
            output_parameter_id=str(uuid.uuid4()),
            key=f"natural_lang_extraction_{generate_random_string()}",
            workflow_id=self.output_parameter.workflow_id,
            created_at=datetime.now(),
            modified_at=datetime.now(),
            parameter_type=ParameterType.OUTPUT,
            description="Natural language extraction result",
        )

        return ExtractionBlock(
            label=f"natural_lang_extraction_{generate_random_string()}",
            data_extraction_goal=extraction_goal,
            data_schema=data_schema,
            output_parameter=output_param,
        )

    def _build_loop_graph(
        self,
        blocks: list[BlockTypeVar],
        skip_sequential_defaulting: bool = False,
    ) -> tuple[str, dict[str, BlockTypeVar], dict[str, str | None]]:
        label_to_block: dict[str, BlockTypeVar] = {}
        default_next_map: dict[str, str | None] = {}

        for block in blocks:
            if block.label in label_to_block:
                raise InvalidWorkflowDefinition(f"Duplicate block label detected in loop: {block.label}")
            label_to_block[block.label] = block
            default_next_map[block.label] = block.next_block_label

        if not skip_sequential_defaulting:
            has_conditional_blocks = any(block.block_type == BlockType.CONDITIONAL for block in blocks)
            if not has_conditional_blocks:
                for idx, block in enumerate(blocks[:-1]):
                    if default_next_map.get(block.label) is None:
                        default_next_map[block.label] = blocks[idx + 1].label

        # SKY-8571: connect conditional branch terminals to the conditional's merge-point successor.
        from skyvern.forge.sdk.workflow.models.block import resolve_conditional_merge_edges

        resolve_conditional_merge_edges(blocks, label_to_block, default_next_map)

        adjacency: dict[str, set[str]] = {label: set() for label in label_to_block}
        incoming: dict[str, int] = {label: 0 for label in label_to_block}

        def _add_edge(source: str, target: str | None) -> None:
            if not target:
                return
            if target not in label_to_block:
                raise InvalidWorkflowDefinition(
                    f"Block {source} references unknown next_block_label {target} inside loop {self.label}"
                )
            # Allow multiple branches of a conditional to point to the same target
            # without double-counting the incoming edge.
            if target not in adjacency[source]:
                adjacency[source].add(target)
                incoming[target] += 1

        for label, block in label_to_block.items():
            if block.block_type == BlockType.CONDITIONAL:
                for branch in block.ordered_branches:
                    _add_edge(label, branch.next_block_label)
            else:
                _add_edge(label, default_next_map.get(label))

        roots = [label for label, count in incoming.items() if count == 0]
        if not roots:
            raise InvalidWorkflowDefinition(
                f"Circular reference detected inside loop {self.label}: every block is the target of another"
                " block's next_block_label, so there is no starting block."
                " At least one block must not be the target of any next_block_label or branch condition."
            )
        if len(roots) > 1:
            raise InvalidWorkflowDefinition(
                f"Disconnected blocks detected inside loop {self.label}: blocks"
                f" ({', '.join(sorted(roots))}) are not reachable from any other block."
                " Every block must be reachable from the first block through next_block_label or"
                " conditional branch references."
                " Either connect them by setting another block's next_block_label to point to them, or remove them."
            )

        queue: deque[str] = deque([roots[0]])
        visited_count = 0
        in_degree = dict(incoming)
        while queue:
            node = queue.popleft()
            visited_count += 1
            for neighbor in adjacency[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited_count != len(label_to_block):
            raise InvalidWorkflowDefinition(
                f"Circular reference detected inside loop {self.label}: some blocks form a loop through their"
                " next_block_label references, causing an infinite cycle."
                " Ensure that following next_block_label from any block eventually reaches a block"
                " with next_block_label set to null."
            )

        return roots[0], label_to_block, default_next_map

    def validate_loop_blocks(self) -> None:
        """Validate the loop_blocks graph for cycles, orphans, and dangling references.

        Skips sequential defaulting so that disconnected subgraphs are detected.
        Also recursively validates any nested loop block children.
        Raises InvalidWorkflowDefinition (422) on validation failure.
        """
        if not self.loop_blocks:
            return
        self._build_loop_graph(self.loop_blocks, skip_sequential_defaulting=True)
        for block in self.loop_blocks:
            if isinstance(block, (ForLoopBlock, WhileLoopBlock)):
                block.validate_loop_blocks()

    async def _persist_partial_loop_output(
        self,
        workflow_run_id: str,
        outputs_with_loop_values: list[list[dict[str, Any]]],
        loop_idx: int,
    ) -> None:
        """Persist partial for-loop output to DB so data survives Temporal
        activity timeouts. The timeout handler runs on a different node and
        reads from DB — without this, accumulated iteration data is lost when
        the loop is killed mid-execution.

        Uses the DB UPSERT directly instead of record_output_parameter_value
        to avoid re-registering context parameters and emitting spurious
        'already has a registered value' warnings on every call.

        On the normal iteration path, this is called every
        PERSIST_LOOP_OUTPUT_INTERVAL iterations and on the final iteration
        to balance durability vs DB load. Early-return paths (failure,
        cancellation) always persist since they are terminal."""
        if not self.output_parameter:
            return
        _maybe_truncate_loop_outputs(
            outputs_with_loop_values,
            workflow_run_id=workflow_run_id,
            output_parameter_id=self.output_parameter.output_parameter_id,
        )
        try:
            await app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter(
                workflow_run_id=workflow_run_id,
                output_parameter_id=self.output_parameter.output_parameter_id,
                value=outputs_with_loop_values,
            )
        except Exception:
            LOG.warning(
                "Failed to incrementally persist for-loop output",
                workflow_run_id=workflow_run_id,
                output_parameter_id=self.output_parameter.output_parameter_id,
                loop_idx=loop_idx,
                exc_info=True,
            )

    async def execute_loop_helper(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        workflow_run_context: WorkflowRunContext,
        loop_over_values: list[Any],
        organization_id: str | None = None,
        browser_session_id: str | None = None,
    ) -> LoopBlockExecutedResult:
        outputs_with_loop_values: list[list[dict[str, Any]]] = []
        block_outputs: list[BlockResult] = []
        current_block: BlockTypeVar | None = None

        start_label, label_to_block, default_next_map = self._build_loop_graph(self.loop_blocks)
        conditional_scopes = compute_conditional_scopes(label_to_block, default_next_map)

        for loop_idx, loop_over_value in enumerate(loop_over_values):
            # Check max_iterations limit
            if loop_idx >= DEFAULT_MAX_LOOP_ITERATIONS:
                LOG.info(
                    f"ForLoopBlock Reached max_iterations limit ({DEFAULT_MAX_LOOP_ITERATIONS}), stopping loop",
                    workflow_run_id=workflow_run_id,
                    loop_idx=loop_idx,
                    max_iterations=DEFAULT_MAX_LOOP_ITERATIONS,
                )
                failure_block_result = await self.build_block_result(
                    success=False,
                    status=BlockStatus.failed,
                    failure_reason=f"Reached max_loop_iterations limit of {DEFAULT_MAX_LOOP_ITERATIONS}",
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    is_synthetic_loop_failure=True,
                )
                block_outputs.append(failure_block_result)
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                return LoopBlockExecutedResult(
                    outputs_with_loop_values=outputs_with_loop_values,
                    block_outputs=block_outputs,
                    last_block=current_block,
                )
            loop_over_value_repr = repr(loop_over_value)
            if len(loop_over_value_repr) > MAX_LOOP_OVER_VALUE_LOG_CHARS:
                loop_over_value_repr = (
                    loop_over_value_repr[:MAX_LOOP_OVER_VALUE_LOG_CHARS]
                    + f"...[truncated, original size: {len(loop_over_value_repr)}]"
                )
            LOG.info("Starting loop iteration", loop_idx=loop_idx, loop_over_value=loop_over_value_repr)

            # Capture baseline downloaded files for per-iteration scoping (SKY-7005).
            # Download-producing child blocks re-capture their own per-block baseline
            # at start; this seed only covers filtering before the first such capture.
            loop_context = skyvern_context.current()
            if loop_context:
                downloaded_file_sigs_before: list[tuple[str | None, str | None, str | None]] = []
                baseline_timed_out = False
                try:
                    async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                        downloaded_file_sigs_before = [
                            to_downloaded_file_signature(fi)
                            for fi in await app.STORAGE.get_downloaded_files(
                                organization_id=organization_id or "",
                                run_id=resolve_run_download_id(loop_context, fallback_run_id=workflow_run_id),
                            )
                        ]
                except asyncio.TimeoutError:
                    baseline_timed_out = True
                    LOG.warning(
                        "Timeout getting baseline downloaded files for loop iteration",
                        workflow_run_id=workflow_run_id,
                        loop_idx=loop_idx,
                    )
                if baseline_timed_out:
                    loop_context.loop_internal_state = None
                else:
                    loop_context.loop_internal_state = {
                        DOWNLOADED_FILE_SIGS_KEY: downloaded_file_sigs_before,
                    }

            # context parameter has been deprecated. However, it's still used by task v2 - we should migrate away from it.
            context_parameters_with_value = self.get_loop_block_context_parameters(workflow_run_id, loop_over_value)
            for context_parameter in context_parameters_with_value:
                workflow_run_context.set_value(context_parameter.key, context_parameter.value)

            each_loop_output_values: list[dict[str, Any]] = []

            iteration_step_count = 0
            LOG.debug(
                "ForLoopBlock starting iteration",
                workflow_run_id=workflow_run_id,
                loop_idx=loop_idx,
                max_steps_per_iteration=DEFAULT_MAX_STEPS_PER_ITERATION,
            )

            block_idx = 0
            current_label: str | None = start_label
            conditional_wrb_ids: dict[str, str] = {}
            while current_label:
                loop_block = label_to_block.get(current_label)
                if not loop_block:
                    LOG.error(
                        "Unable to find loop block with label in loop graph",
                        workflow_run_id=workflow_run_id,
                        loop_label=self.label,
                        current_label=current_label,
                    )
                    failure_block_result = await self.build_block_result(
                        success=False,
                        status=BlockStatus.failed,
                        failure_reason=f"Unable to find block with label {current_label} inside loop {self.label}",
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                        is_synthetic_loop_failure=True,
                    )
                    block_outputs.append(failure_block_result)
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                metadata: BlockMetadata = {
                    "current_index": loop_idx,
                    "current_value": loop_over_value,
                    "current_item": loop_over_value,
                }
                workflow_run_context.update_block_metadata(self.label, metadata)
                workflow_run_context.update_block_metadata(loop_block.label, metadata)

                original_loop_block = loop_block
                loop_block = loop_block.model_copy(deep=True)
                current_block = loop_block

                # Determine the parent for timeline nesting: if this block is
                # inside a conditional's scope, parent it to that conditional's
                # workflow_run_block rather than the loop's.
                parent_wrb_id = workflow_run_block_id
                if current_label in conditional_scopes:
                    cond_label = conditional_scopes[current_label]
                    if cond_label in conditional_wrb_ids:
                        parent_wrb_id = conditional_wrb_ids[cond_label]

                block_output = await loop_block.execute_safe(
                    workflow_run_id=workflow_run_id,
                    parent_workflow_run_block_id=parent_wrb_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    current_value=str(loop_over_value),
                    current_index=loop_idx,
                )

                # Track conditional workflow_run_block_ids so branch targets
                # can be parented to them.
                if loop_block.block_type == BlockType.CONDITIONAL and block_output.workflow_run_block_id:
                    conditional_wrb_ids[current_label] = block_output.workflow_run_block_id

                output_value = (
                    workflow_run_context.get_value(block_output.output_parameter.key)
                    if workflow_run_context.has_value(block_output.output_parameter.key)
                    else None
                )

                # Log the output value for debugging
                if block_output.output_parameter.key.endswith("_output"):
                    LOG.debug("Block output", block_type=loop_block.block_type, output_value=output_value)

                # Log URL information for goto_url blocks
                if loop_block.block_type == BlockType.GOTO_URL:
                    LOG.info("Goto URL block executed", url=loop_block.url, loop_idx=loop_idx)
                each_loop_output_values.append(
                    {
                        "loop_value": loop_over_value,
                        "output_parameter": block_output.output_parameter,
                        "output_value": output_value,
                    }
                )
                try:
                    if block_output.workflow_run_block_id:
                        await app.DATABASE.observer.update_workflow_run_block(
                            workflow_run_block_id=block_output.workflow_run_block_id,
                            organization_id=organization_id,
                            current_value=str(loop_over_value),
                            current_index=loop_idx,
                        )
                except Exception:
                    LOG.warning(
                        "Failed to update workflow run block",
                        workflow_run_block_id=block_output.workflow_run_block_id,
                        loop_over_value=loop_over_value,
                        loop_idx=loop_idx,
                    )
                loop_block = original_loop_block
                block_outputs.append(block_output)

                # Check max_steps_per_iteration limit after each block execution
                iteration_step_count += 1  # Count each block execution as a step
                if iteration_step_count >= DEFAULT_MAX_STEPS_PER_ITERATION:
                    LOG.info(
                        f"ForLoopBlock Reached max_steps_per_iteration limit ({DEFAULT_MAX_STEPS_PER_ITERATION}) in iteration {loop_idx}, stopping iteration",
                        workflow_run_id=workflow_run_id,
                        loop_idx=loop_idx,
                        max_steps_per_iteration=DEFAULT_MAX_STEPS_PER_ITERATION,
                        iteration_step_count=iteration_step_count,
                    )
                    # Create a failure block result for this iteration
                    failure_block_result = await self.build_block_result(
                        success=False,
                        status=BlockStatus.failed,
                        failure_reason=f"Reached max_steps_per_iteration limit of {DEFAULT_MAX_STEPS_PER_ITERATION}",
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                        is_synthetic_loop_failure=True,
                    )
                    block_outputs.append(failure_block_result)
                    # If next_loop_on_failure is False, stop the entire loop
                    if not self.next_loop_on_failure:
                        outputs_with_loop_values.append(each_loop_output_values)
                        await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                        return LoopBlockExecutedResult(
                            outputs_with_loop_values=outputs_with_loop_values,
                            block_outputs=block_outputs,
                            last_block=current_block,
                        )
                    # If next_loop_on_failure is True, break out of the block loop for this iteration
                    break

                if block_output.status == BlockStatus.canceled:
                    LOG.info(
                        f"ForLoopBlock Block with type {loop_block.block_type} at index {block_idx} during loop {loop_idx} was canceled for workflow run {workflow_run_id}, canceling for loop",
                        block_type=loop_block.block_type,
                        workflow_run_id=workflow_run_id,
                        block_idx=block_idx,
                        block_result=block_outputs,
                    )
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                if (
                    not block_output.success
                    and not loop_block.continue_on_failure
                    and not loop_block.next_loop_on_failure
                    and not self.next_loop_on_failure
                ):
                    LOG.info(
                        f"ForLoopBlock Encountered a failure processing block {block_idx} during loop {loop_idx}, terminating early",
                        block_outputs=block_outputs,
                        loop_idx=loop_idx,
                        block_idx=block_idx,
                        loop_over_value=loop_over_value,
                        loop_block_continue_on_failure=loop_block.continue_on_failure,
                        failure_reason=block_output.failure_reason,
                        next_loop_on_failure=loop_block.next_loop_on_failure or self.next_loop_on_failure,
                    )
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                if block_output.success or loop_block.continue_on_failure:
                    next_label: str | None = None
                    if loop_block.block_type == BlockType.CONDITIONAL:
                        branch_metadata = (
                            block_output.output_parameter_value
                            if isinstance(block_output.output_parameter_value, dict)
                            else None
                        )
                        next_label = (branch_metadata or {}).get("next_block_label")
                    else:
                        next_label = default_next_map.get(loop_block.label)

                    if not next_label:
                        break

                    if next_label not in label_to_block:
                        failure_block_result = await self.build_block_result(
                            success=False,
                            status=BlockStatus.failed,
                            failure_reason=f"Next block label {next_label} not found inside loop {self.label}",
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                            is_synthetic_loop_failure=True,
                        )
                        block_outputs.append(failure_block_result)
                        outputs_with_loop_values.append(each_loop_output_values)
                        await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                        return LoopBlockExecutedResult(
                            outputs_with_loop_values=outputs_with_loop_values,
                            block_outputs=block_outputs,
                            last_block=current_block,
                        )

                    current_label = next_label
                    block_idx += 1
                    continue

                if loop_block.next_loop_on_failure or self.next_loop_on_failure:
                    LOG.info(
                        f"ForLoopBlock Block {block_idx} during loop {loop_idx} failed but will continue to next iteration",
                        block_outputs=block_outputs,
                        loop_idx=loop_idx,
                        block_idx=block_idx,
                        loop_over_value=loop_over_value,
                        loop_block_next_loop_on_failure=loop_block.next_loop_on_failure or self.next_loop_on_failure,
                    )
                    break

                break

            outputs_with_loop_values.append(each_loop_output_values)
            is_last_iteration = loop_idx == len(loop_over_values) - 1
            if loop_idx % PERSIST_LOOP_OUTPUT_INTERVAL == 0 or is_last_iteration:
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)

        return LoopBlockExecutedResult(
            outputs_with_loop_values=outputs_with_loop_values,
            block_outputs=block_outputs,
            last_block=current_block,
            natural_completion=True,
        )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # Save the caller's loop_internal_state so we can restore it after this
        # loop finishes. Supports nested loops (parent's state is preserved) and
        # ensures stale per-iteration baselines don't leak into subsequent blocks.
        outer_context = skyvern_context.current()
        outer_loop_state = outer_context.loop_internal_state if outer_context else None
        try:
            return await self._run_loop(
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                **kwargs,
            )
        finally:
            if outer_context:
                outer_context.loop_internal_state = outer_loop_state

    async def _run_loop(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)
        try:
            loop_over_values = await self.get_loop_over_parameter_values(
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except Exception as e:
            return await self.build_block_result(
                success=False,
                failure_reason=f"failed to get loop values: {str(e)}",
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        await app.DATABASE.observer.update_workflow_run_block(
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            loop_values=loop_over_values,
        )

        LOG.info(
            f"Number of loop_over values: {len(loop_over_values)}",
            block_type=self.block_type,
            workflow_run_id=workflow_run_id,
            num_loop_over_values=len(loop_over_values),
        )
        if not loop_over_values or len(loop_over_values) == 0:
            LOG.info(
                "No loop_over values found, terminating block",
                block_type=self.block_type,
                workflow_run_id=workflow_run_id,
                num_loop_over_values=len(loop_over_values),
                complete_if_empty=self.complete_if_empty,
            )
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, [])
            if self.complete_if_empty:
                return await self.build_block_result(
                    success=True,
                    failure_reason=None,
                    output_parameter_value=[],
                    status=BlockStatus.completed,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            else:
                return await self.build_block_result(
                    success=False,
                    failure_reason="No iterable value found for the loop block",
                    status=BlockStatus.terminated,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )

        if not self.loop_blocks or len(self.loop_blocks) == 0:
            LOG.info(
                "No defined blocks to loop, terminating block",
                block_type=self.block_type,
                workflow_run_id=workflow_run_id,
                num_loop_blocks=len(self.loop_blocks),
            )
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, [])
            return await self.build_block_result(
                success=False,
                failure_reason="No defined blocks to loop",
                status=BlockStatus.terminated,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        try:
            loop_executed_result = await self.execute_loop_helper(
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                workflow_run_context=workflow_run_context,
                loop_over_values=loop_over_values,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
            )
        except InvalidWorkflowDefinition as exc:
            LOG.error(
                "Loop graph validation failed",
                error=str(exc),
                workflow_run_id=workflow_run_id,
                loop_label=self.label,
            )
            return await self.build_block_result(
                success=False,
                failure_reason=str(exc),
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        await self.record_output_parameter_value(
            workflow_run_context, workflow_run_id, loop_executed_result.outputs_with_loop_values
        )

        block_status, success, failure_reason = loop_executed_result.resolve_status(self.next_loop_on_failure)

        return await self.build_block_result(
            success=success,
            failure_reason=failure_reason,
            output_parameter_value=loop_executed_result.outputs_with_loop_values,
            status=block_status,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class WhileLoopBlock(Block):
    """Loop block driven by a runtime condition. Iterates while ``condition`` evaluates truthy.

    Top-of-loop semantics: the condition is evaluated *before* each iteration (including the
    first). If the condition is false on the first check, the body never runs and the block
    returns success with an empty output list.

    Safety: the loop is capped at ``DEFAULT_MAX_LOOP_ITERATIONS`` (500). Reaching the cap is
    treated as a failure so that a misbehaving condition can never spin forever.
    """

    block_type: Literal[BlockType.WHILE_LOOP] = BlockType.WHILE_LOOP  # type: ignore

    loop_blocks: list[BlockTypeVar]
    # The discriminated union on ``criteria_type`` handles dict→typed coercion. Pydantic
    # rejects a dict missing ``criteria_type`` with ``union_tag_not_found`` before any
    # model_validator runs, so no extra coercion validator is needed here.
    condition: BranchCriteriaTypeVar

    def get_all_parameters(
        self,
        workflow_run_id: str,
    ) -> list[PARAMETER_TYPE]:
        parameters: set[PARAMETER_TYPE] = set()
        for loop_block in self.loop_blocks:
            for parameter in loop_block.get_all_parameters(workflow_run_id):
                parameters.add(parameter)
        return list(parameters)

    def _build_loop_graph(
        self,
        blocks: list[BlockTypeVar],
        skip_sequential_defaulting: bool = False,
    ) -> tuple[str, dict[str, BlockTypeVar], dict[str, str | None]]:
        # Duplicated from ForLoopBlock._build_loop_graph for PR 1; promotion to a shared
        # helper is tracked in PR 7 (refactor).
        label_to_block: dict[str, BlockTypeVar] = {}
        default_next_map: dict[str, str | None] = {}

        for block in blocks:
            if block.label in label_to_block:
                raise InvalidWorkflowDefinition(f"Duplicate block label detected in loop: {block.label}")
            label_to_block[block.label] = block
            default_next_map[block.label] = block.next_block_label

        if not skip_sequential_defaulting:
            has_conditional_blocks = any(block.block_type == BlockType.CONDITIONAL for block in blocks)
            if not has_conditional_blocks:
                for idx, block in enumerate(blocks[:-1]):
                    if default_next_map.get(block.label) is None:
                        default_next_map[block.label] = blocks[idx + 1].label

        # SKY-8571: connect conditional branch terminals to the conditional's merge-point successor.
        from skyvern.forge.sdk.workflow.models.block import resolve_conditional_merge_edges

        resolve_conditional_merge_edges(blocks, label_to_block, default_next_map)

        adjacency: dict[str, set[str]] = {label: set() for label in label_to_block}
        incoming: dict[str, int] = {label: 0 for label in label_to_block}

        def _add_edge(source: str, target: str | None) -> None:
            if not target:
                return
            if target not in label_to_block:
                raise InvalidWorkflowDefinition(
                    f"Block {source} references unknown next_block_label {target} inside loop {self.label}"
                )
            if target not in adjacency[source]:
                adjacency[source].add(target)
                incoming[target] += 1

        for label, block in label_to_block.items():
            if block.block_type == BlockType.CONDITIONAL:
                for branch in block.ordered_branches:
                    _add_edge(label, branch.next_block_label)
            else:
                _add_edge(label, default_next_map.get(label))

        roots = [label for label, count in incoming.items() if count == 0]
        if not roots:
            raise InvalidWorkflowDefinition(
                f"Circular reference detected inside loop {self.label}: every block is the target of another"
                " block's next_block_label, so there is no starting block."
                " At least one block must not be the target of any next_block_label or branch condition."
            )
        if len(roots) > 1:
            raise InvalidWorkflowDefinition(
                f"Disconnected blocks detected inside loop {self.label}: blocks"
                f" ({', '.join(sorted(roots))}) are not reachable from any other block."
                " Every block must be reachable from the first block through next_block_label or"
                " conditional branch references."
                " Either connect them by setting another block's next_block_label to point to them, or remove them."
            )

        queue: deque[str] = deque([roots[0]])
        visited_count = 0
        in_degree = dict(incoming)
        while queue:
            node = queue.popleft()
            visited_count += 1
            for neighbor in adjacency[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited_count != len(label_to_block):
            raise InvalidWorkflowDefinition(
                f"Circular reference detected inside loop {self.label}: some blocks form a loop through their"
                " next_block_label references, causing an infinite cycle."
                " Ensure that following next_block_label from any block eventually reaches a block"
                " with next_block_label set to null."
            )

        return roots[0], label_to_block, default_next_map

    def validate_loop_blocks(self) -> None:
        """Validate the loop_blocks graph and recurse into nested loop blocks."""
        if not self.loop_blocks:
            return
        self._build_loop_graph(self.loop_blocks, skip_sequential_defaulting=True)
        for block in self.loop_blocks:
            if isinstance(block, (ForLoopBlock, WhileLoopBlock)):
                block.validate_loop_blocks()

    async def _persist_partial_loop_output(
        self,
        workflow_run_id: str,
        outputs_with_loop_values: list[list[dict[str, Any]]],
        loop_idx: int,
    ) -> None:
        """Persist partial while-loop output to DB so accumulated iteration data survives
        Temporal activity timeouts. Mirrors ``ForLoopBlock._persist_partial_loop_output``.
        """
        if not self.output_parameter:
            return
        _maybe_truncate_loop_outputs(
            outputs_with_loop_values,
            workflow_run_id=workflow_run_id,
            output_parameter_id=self.output_parameter.output_parameter_id,
        )
        try:
            await app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter(
                workflow_run_id=workflow_run_id,
                output_parameter_id=self.output_parameter.output_parameter_id,
                value=outputs_with_loop_values,
            )
        except Exception:
            LOG.warning(
                "Failed to incrementally persist while-loop output",
                workflow_run_id=workflow_run_id,
                output_parameter_id=self.output_parameter.output_parameter_id,
                loop_idx=loop_idx,
                exc_info=True,
            )

    async def _evaluate_condition(
        self,
        workflow_run_context: WorkflowRunContext,
        *,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        browser_session_id: str | None,
    ) -> bool:
        """Evaluate the loop condition. Raises on rendering errors so the caller can convert
        the failure into a block result with a clear message.

        ``current_index`` (the 0-indexed iteration counter) is read from this block's own
        metadata via the existing for_loop injection in
        :meth:`format_block_parameter_template_from_workflow_run_context`. ``current_value``
        holds the same integer so ``{{ current_value }}`` caps work like For Each loops.
        The caller writes both onto ``self.label`` before invoking this method, so
        condition authors can bootstrap iteration 1 with
        ``{{ current_index == 0 or <body_output_ref> }}``.
        """
        evaluation_context = BranchEvaluationContext(
            workflow_run_context=workflow_run_context,
            block_label=self.label,
            template_renderer=lambda potential_template: self.format_block_parameter_template_from_workflow_run_context(
                potential_template,
                workflow_run_context,
            ),
        )
        if isinstance(self.condition, PromptBranchCriteria):
            synthetic_branch = BranchCondition(
                id=str(uuid.uuid4()),
                criteria=self.condition,
                next_block_label=None,
                is_default=False,
            )
            results, _, _, _ = await _evaluate_prompt_branch_conditions_batch(
                log_label=self.label,
                branches=[synthetic_branch],
                evaluation_context=evaluation_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                workflow_id=self.output_parameter.workflow_id,
                extraction_description_suffix="while_loop condition",
            )
            return results[0]

        return await self.condition.evaluate(evaluation_context)

    async def _execute_while_loop_helper(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        workflow_run_context: WorkflowRunContext,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
    ) -> LoopBlockExecutedResult:
        outputs_with_loop_values: list[list[dict[str, Any]]] = []
        block_outputs: list[BlockResult] = []
        current_block: BlockTypeVar | None = None

        start_label, label_to_block, default_next_map = self._build_loop_graph(self.loop_blocks)
        conditional_scopes = compute_conditional_scopes(label_to_block, default_next_map)

        loop_idx = 0
        while True:
            # Evaluate the condition at the top of every iteration (including the first).
            # The cap check fires *after* the condition check so that a loop which would
            # naturally exit on the (cap+1)-th check returns success rather than tripping
            # the cap one iteration early.
            #
            # Condition rendering errors always terminate the loop, regardless of
            # ``next_loop_on_failure``. The flag governs *body* failures (which can vary
            # iteration to iteration), but a Jinja render error means the condition itself
            # is malformed and will fail identically on the next iteration — there is no
            # forward progress to be made by retrying.
            # Expose ``current_index`` to the condition's template scope before evaluation
            # so authors can bootstrap iteration 0 or cap iterations. ``current_value`` and
            # ``current_item`` stay None so Jinja matches persisted timeline rows
            # (``execute_safe(..., current_value=None)``) and outer for-loop rows cannot leak.
            condition_metadata: BlockMetadata = {
                "current_index": loop_idx,
                "current_value": None,
                "current_item": None,
            }
            workflow_run_context.update_block_metadata(self.label, condition_metadata)

            try:
                should_continue = await self._evaluate_condition(
                    workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                )
            except (FailedToFormatJinjaStyleParameter, MissingJinjaVariables, ValueError) as exc:
                LOG.error(
                    "WhileLoopBlock condition evaluation failed",
                    workflow_run_id=workflow_run_id,
                    block_label=self.label,
                    error=str(exc),
                )
                failure_block_result = await self.build_block_result(
                    success=False,
                    status=BlockStatus.failed,
                    failure_reason=f"Failed to evaluate while-loop condition: {str(exc)}",
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
                block_outputs.append(failure_block_result)
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                return LoopBlockExecutedResult(
                    outputs_with_loop_values=outputs_with_loop_values,
                    block_outputs=block_outputs,
                    last_block=current_block,
                )

            if not should_continue:
                LOG.info(
                    "WhileLoopBlock condition is false, exiting loop",
                    workflow_run_id=workflow_run_id,
                    loop_idx=loop_idx,
                )
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                break

            # Check max_iterations limit: only fires when the condition is still true at
            # iteration index ``cap``, i.e. the loop would have run a (cap+1)-th body.
            if loop_idx >= DEFAULT_MAX_LOOP_ITERATIONS:
                LOG.info(
                    "WhileLoopBlock reached max_iterations limit, stopping loop",
                    workflow_run_id=workflow_run_id,
                    loop_idx=loop_idx,
                    max_iterations=DEFAULT_MAX_LOOP_ITERATIONS,
                )
                failure_block_result = await self.build_block_result(
                    success=False,
                    status=BlockStatus.failed,
                    failure_reason=f"Reached max_loop_iterations limit of {DEFAULT_MAX_LOOP_ITERATIONS}",
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    is_synthetic_loop_failure=True,
                )
                block_outputs.append(failure_block_result)
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                return LoopBlockExecutedResult(
                    outputs_with_loop_values=outputs_with_loop_values,
                    block_outputs=block_outputs,
                    last_block=current_block,
                )

            # Capture baseline downloaded files for per-iteration scoping (SKY-7005).
            # Download-producing child blocks re-capture their own per-block baseline
            # at start; this seed only covers filtering before the first such capture.
            loop_context = skyvern_context.current()
            if loop_context:
                downloaded_file_sigs_before: list[tuple[str | None, str | None, str | None]] = []
                baseline_timed_out = False
                try:
                    async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                        downloaded_file_sigs_before = [
                            to_downloaded_file_signature(fi)
                            for fi in await app.STORAGE.get_downloaded_files(
                                organization_id=organization_id or "",
                                run_id=resolve_run_download_id(loop_context, fallback_run_id=workflow_run_id),
                            )
                        ]
                except asyncio.TimeoutError:
                    baseline_timed_out = True
                    LOG.warning(
                        "Timeout getting baseline downloaded files for loop iteration",
                        workflow_run_id=workflow_run_id,
                        loop_idx=loop_idx,
                    )
                if baseline_timed_out:
                    loop_context.loop_internal_state = None
                else:
                    loop_context.loop_internal_state = {
                        DOWNLOADED_FILE_SIGS_KEY: downloaded_file_sigs_before,
                    }

            each_loop_output_values: list[dict[str, Any]] = []

            iteration_step_count = 0
            LOG.debug(
                "WhileLoopBlock starting iteration",
                workflow_run_id=workflow_run_id,
                loop_idx=loop_idx,
                max_steps_per_iteration=DEFAULT_MAX_STEPS_PER_ITERATION,
            )

            block_idx = 0
            current_label: str | None = start_label
            conditional_wrb_ids: dict[str, str] = {}
            while current_label:
                loop_block = label_to_block.get(current_label)
                if not loop_block:
                    LOG.error(
                        "Unable to find loop block with label in loop graph",
                        workflow_run_id=workflow_run_id,
                        loop_label=self.label,
                        current_label=current_label,
                    )
                    failure_block_result = await self.build_block_result(
                        success=False,
                        status=BlockStatus.failed,
                        failure_reason=f"Unable to find block with label {current_label} inside loop {self.label}",
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                        is_synthetic_loop_failure=True,
                    )
                    block_outputs.append(failure_block_result)
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                # ``current_index`` is the iteration counter. ``current_value`` stays None so
                # runtime matches ``execute_safe`` / timeline rows; use ``{{ current_index }}``
                # in Jinja. ``current_item`` stays None.
                metadata: BlockMetadata = {
                    "current_index": loop_idx,
                    "current_value": None,
                    "current_item": None,
                }
                workflow_run_context.update_block_metadata(self.label, metadata)
                workflow_run_context.update_block_metadata(loop_block.label, metadata)

                original_loop_block = loop_block
                loop_block = loop_block.model_copy(deep=True)
                current_block = loop_block

                parent_wrb_id = workflow_run_block_id
                if current_label in conditional_scopes:
                    cond_label = conditional_scopes[current_label]
                    if cond_label in conditional_wrb_ids:
                        parent_wrb_id = conditional_wrb_ids[cond_label]

                # ``current_value`` is None on persisted timeline rows and in block metadata;
                # iteration is available only as ``current_index``.
                block_output = await loop_block.execute_safe(
                    workflow_run_id=workflow_run_id,
                    parent_workflow_run_block_id=parent_wrb_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                    current_value=None,
                    current_index=loop_idx,
                )

                if loop_block.block_type == BlockType.CONDITIONAL and block_output.workflow_run_block_id:
                    conditional_wrb_ids[current_label] = block_output.workflow_run_block_id

                output_value = (
                    workflow_run_context.get_value(block_output.output_parameter.key)
                    if workflow_run_context.has_value(block_output.output_parameter.key)
                    else None
                )

                if block_output.output_parameter.key.endswith("_output"):
                    LOG.debug("Block output", block_type=loop_block.block_type, output_value=output_value)

                if loop_block.block_type == BlockType.GOTO_URL:
                    LOG.info("Goto URL block executed", url=loop_block.url, loop_idx=loop_idx)

                each_loop_output_values.append(
                    {
                        "output_parameter": block_output.output_parameter,
                        "output_value": output_value,
                    }
                )

                try:
                    if block_output.workflow_run_block_id:
                        await app.DATABASE.observer.update_workflow_run_block(
                            workflow_run_block_id=block_output.workflow_run_block_id,
                            organization_id=organization_id,
                            current_value=None,
                            current_index=loop_idx,
                        )
                except Exception:
                    LOG.warning(
                        "Failed to update workflow run block",
                        workflow_run_block_id=block_output.workflow_run_block_id,
                        loop_idx=loop_idx,
                    )
                loop_block = original_loop_block
                block_outputs.append(block_output)

                iteration_step_count += 1
                if iteration_step_count >= DEFAULT_MAX_STEPS_PER_ITERATION:
                    LOG.info(
                        "WhileLoopBlock reached max_steps_per_iteration limit, stopping iteration",
                        workflow_run_id=workflow_run_id,
                        loop_idx=loop_idx,
                        max_steps_per_iteration=DEFAULT_MAX_STEPS_PER_ITERATION,
                        iteration_step_count=iteration_step_count,
                    )
                    failure_block_result = await self.build_block_result(
                        success=False,
                        status=BlockStatus.failed,
                        failure_reason=f"Reached max_steps_per_iteration limit of {DEFAULT_MAX_STEPS_PER_ITERATION}",
                        workflow_run_block_id=workflow_run_block_id,
                        organization_id=organization_id,
                        is_synthetic_loop_failure=True,
                    )
                    block_outputs.append(failure_block_result)
                    if not self.next_loop_on_failure:
                        outputs_with_loop_values.append(each_loop_output_values)
                        await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                        return LoopBlockExecutedResult(
                            outputs_with_loop_values=outputs_with_loop_values,
                            block_outputs=block_outputs,
                            last_block=current_block,
                        )
                    break

                if block_output.status == BlockStatus.canceled:
                    LOG.info(
                        "WhileLoopBlock child block canceled, canceling while loop",
                        block_type=loop_block.block_type,
                        workflow_run_id=workflow_run_id,
                        block_idx=block_idx,
                        loop_idx=loop_idx,
                        block_result=block_outputs,
                    )
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                if (
                    not block_output.success
                    and not loop_block.continue_on_failure
                    and not loop_block.next_loop_on_failure
                    and not self.next_loop_on_failure
                ):
                    LOG.info(
                        "WhileLoopBlock encountered a failure processing block, terminating early",
                        block_outputs=block_outputs,
                        loop_idx=loop_idx,
                        block_idx=block_idx,
                        loop_block_continue_on_failure=loop_block.continue_on_failure,
                        failure_reason=block_output.failure_reason,
                        next_loop_on_failure=loop_block.next_loop_on_failure or self.next_loop_on_failure,
                    )
                    outputs_with_loop_values.append(each_loop_output_values)
                    await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                    return LoopBlockExecutedResult(
                        outputs_with_loop_values=outputs_with_loop_values,
                        block_outputs=block_outputs,
                        last_block=current_block,
                    )

                if block_output.success or loop_block.continue_on_failure:
                    next_label: str | None = None
                    if loop_block.block_type == BlockType.CONDITIONAL:
                        branch_metadata = (
                            block_output.output_parameter_value
                            if isinstance(block_output.output_parameter_value, dict)
                            else None
                        )
                        next_label = (branch_metadata or {}).get("next_block_label")
                    else:
                        next_label = default_next_map.get(loop_block.label)

                    if not next_label:
                        break

                    if next_label not in label_to_block:
                        failure_block_result = await self.build_block_result(
                            success=False,
                            status=BlockStatus.failed,
                            failure_reason=f"Next block label {next_label} not found inside loop {self.label}",
                            workflow_run_block_id=workflow_run_block_id,
                            organization_id=organization_id,
                            is_synthetic_loop_failure=True,
                        )
                        block_outputs.append(failure_block_result)
                        outputs_with_loop_values.append(each_loop_output_values)
                        await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)
                        return LoopBlockExecutedResult(
                            outputs_with_loop_values=outputs_with_loop_values,
                            block_outputs=block_outputs,
                            last_block=current_block,
                        )

                    current_label = next_label
                    block_idx += 1
                    continue

                if loop_block.next_loop_on_failure or self.next_loop_on_failure:
                    LOG.info(
                        "WhileLoopBlock child block failed but will continue to next iteration",
                        block_outputs=block_outputs,
                        loop_idx=loop_idx,
                        block_idx=block_idx,
                        loop_block_next_loop_on_failure=loop_block.next_loop_on_failure or self.next_loop_on_failure,
                    )
                    break

                break

            outputs_with_loop_values.append(each_loop_output_values)
            # We don't know "is_last_iteration" for a while-loop ahead of time, so persist
            # every PERSIST_LOOP_OUTPUT_INTERVAL iterations and once again at the top of the
            # next iteration when the condition is false (handled at the break above).
            if loop_idx % PERSIST_LOOP_OUTPUT_INTERVAL == 0:
                await self._persist_partial_loop_output(workflow_run_id, outputs_with_loop_values, loop_idx)

            loop_idx += 1

        return LoopBlockExecutedResult(
            outputs_with_loop_values=outputs_with_loop_values,
            block_outputs=block_outputs,
            last_block=current_block,
            natural_completion=True,
        )

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        # Save the caller's loop_internal_state so we can restore it after this loop
        # finishes. Mirrors ForLoopBlock.execute.
        outer_context = skyvern_context.current()
        outer_loop_state = outer_context.loop_internal_state if outer_context else None
        try:
            return await self._run_loop(
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                **kwargs,
            )
        finally:
            if outer_context:
                outer_context.loop_internal_state = outer_loop_state

    async def _run_loop(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        if not self.loop_blocks:
            LOG.info(
                "No defined blocks to loop, terminating block",
                block_type=self.block_type,
                workflow_run_id=workflow_run_id,
                num_loop_blocks=len(self.loop_blocks),
            )
            await self.record_output_parameter_value(workflow_run_context, workflow_run_id, [])
            return await self.build_block_result(
                success=False,
                failure_reason="No defined blocks to loop",
                status=BlockStatus.terminated,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        try:
            loop_executed_result = await self._execute_while_loop_helper(
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                workflow_run_context=workflow_run_context,
                organization_id=organization_id,
                browser_session_id=browser_session_id,
            )
        except InvalidWorkflowDefinition as exc:
            LOG.error(
                "While-loop graph validation failed",
                error=str(exc),
                workflow_run_id=workflow_run_id,
                loop_label=self.label,
            )
            return await self.build_block_result(
                success=False,
                failure_reason=str(exc),
                status=BlockStatus.failed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        await self.record_output_parameter_value(
            workflow_run_context, workflow_run_id, loop_executed_result.outputs_with_loop_values
        )

        # Special case: condition false on the very first check. The body never ran, so
        # there are no block_outputs. Return success with an empty output list — this is
        # the normal/expected "nothing to do" path for a while-loop.
        if not loop_executed_result.block_outputs:
            return await self.build_block_result(
                success=True,
                failure_reason=None,
                output_parameter_value=loop_executed_result.outputs_with_loop_values,
                status=BlockStatus.completed,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        block_status, success, failure_reason = loop_executed_result.resolve_status(self.next_loop_on_failure)

        return await self.build_block_result(
            success=success,
            failure_reason=failure_reason,
            output_parameter_value=loop_executed_result.outputs_with_loop_values,
            status=block_status,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )


class ConditionalBlock(Block):
    """Branching block that selects the next block label based on list-ordered conditions."""

    # There is a mypy bug with Literal. Without the type: ignore, mypy will raise an error:
    # Parameter 1 of Literal[...] cannot be of type "Any"
    block_type: Literal[BlockType.CONDITIONAL] = BlockType.CONDITIONAL  # type: ignore

    branch_conditions: list[BranchCondition] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_branches(self) -> ConditionalBlock:
        if not self.branch_conditions:
            raise ValueError("Conditional blocks require at least one branch.")

        default_branches = [branch for branch in self.branch_conditions if branch.is_default]
        if len(default_branches) > 1:
            raise ValueError("Only one default branch is permitted per conditional block.")

        return self

    def get_all_parameters(
        self,
        workflow_run_id: str,  # noqa: ARG002 - preserved for interface compatibility
    ) -> list[PARAMETER_TYPE]:
        # BranchCriteria subclasses will surface their parameter dependencies once implemented.
        return []

    async def _evaluate_prompt_branches(
        self,
        *,
        branches: list[BranchCondition],
        evaluation_context: BranchEvaluationContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
    ) -> tuple[list[bool], list[str], str | None, dict | None]:
        """
        Evaluate natural language branch conditions in batch.

        All prompt-based conditions are batched into ONE LLM call for performance.
        Jinja parts ({{ }}) are pre-rendered before sending to LLM.

        Evaluation strategy:
        - If any condition is pure natural language, use ExtractionBlock for browser/page context.
        - If all conditions contain Jinja and are pre-rendered, use direct LLM call (no browser context).

        Returns:
            A tuple of (results, rendered_expressions, extraction_goal, llm_response):
            - results: List of boolean results for each branch
            - rendered_expressions: List of expressions after Jinja pre-rendering
            - extraction_goal: The prompt sent to the LLM (for UI display)
            - llm_response: The raw LLM response for debugging
        """
        return await _evaluate_prompt_branch_conditions_batch(
            log_label=self.label,
            branches=branches,
            evaluation_context=evaluation_context,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            workflow_id=self.output_parameter.workflow_id,
            extraction_description_suffix=f"{len(branches)} conditions",
        )

    async def execute(  # noqa: D401
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: dict,
    ) -> BlockResult:
        """
        Evaluate conditional branches and determine next block to execute.

        Returns a BlockResult with branch metadata in the output_parameter_value.
        """
        workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
        evaluation_context = BranchEvaluationContext(
            workflow_run_context=workflow_run_context,
            block_label=self.label,
            template_renderer=(
                lambda potential_template: self.format_block_parameter_template_from_workflow_run_context(
                    potential_template,
                    workflow_run_context,
                )
            )
            if workflow_run_context
            else None,
        )

        matched_branch = None
        failure_reason: str | None = None

        # Track all branch evaluations for UI display
        branch_evaluations_list: list[dict] = []
        prompt_rendered_by_id: dict[str, str] = {}

        natural_language_branches = [
            branch for branch in self.ordered_branches if isinstance(branch.criteria, PromptBranchCriteria)
        ]
        prompt_results_by_id: dict[str, bool] = {}
        prompt_llm_response: dict | None = None
        prompt_extraction_goal: str | None = None
        if natural_language_branches:
            try:
                (
                    prompt_results,
                    prompt_rendered_expressions,
                    prompt_extraction_goal,
                    prompt_llm_response,
                ) = await self._evaluate_prompt_branches(
                    branches=natural_language_branches,
                    evaluation_context=evaluation_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                )
                prompt_results_by_id = {
                    branch.id: result for branch, result in zip(natural_language_branches, prompt_results, strict=False)
                }
                prompt_rendered_by_id = {
                    branch.id: rendered
                    for branch, rendered in zip(natural_language_branches, prompt_rendered_expressions, strict=False)
                }
            except Exception as exc:
                failure_reason = f"Failed to evaluate natural language branches: {str(exc)}"
                LOG.error(
                    "Failed to evaluate natural language branches",
                    block_label=self.label,
                    error=str(exc),
                    exc_info=True,
                )

        for idx, branch in enumerate(self.ordered_branches):
            branch_eval: dict = {
                "branch_id": branch.id,
                "branch_index": idx,
                "criteria_type": branch.criteria.criteria_type if branch.criteria else None,
                "original_expression": branch.criteria.expression if branch.criteria else None,
                "rendered_expression": None,
                "result": None,
                "is_matched": False,
                "is_default": branch.is_default,
                "next_block_label": branch.next_block_label,
                "error": None,
            }

            # Handle default branch (no criteria to evaluate)
            if branch.criteria is None:
                # Default branch - only matched if no other branch matches
                branch_evaluations_list.append(branch_eval)
                continue

            if branch.criteria.criteria_type == "prompt":
                if failure_reason:
                    branch_eval["error"] = failure_reason
                    branch_evaluations_list.append(branch_eval)
                    break
                prompt_result = prompt_results_by_id.get(branch.id)
                rendered_expr = prompt_rendered_by_id.get(branch.id)
                branch_eval["rendered_expression"] = rendered_expr
                if prompt_result is None:
                    failure_reason = "Missing result for natural language branch evaluation"
                    branch_eval["error"] = failure_reason
                    LOG.error(
                        "Missing prompt evaluation result",
                        block_label=self.label,
                        branch_index=idx,
                        branch_id=branch.id,
                    )
                    branch_evaluations_list.append(branch_eval)
                    break
                branch_eval["result"] = prompt_result
                branch_evaluations_list.append(branch_eval)
                if prompt_result:
                    matched_branch = branch
                    branch_eval["is_matched"] = True
                    LOG.info(
                        "Conditional natural language branch matched",
                        block_label=self.label,
                        branch_index=idx,
                        next_block_label=branch.next_block_label,
                    )
                    break
                continue

            # Jinja template branch
            try:
                # Render the expression for UI display - substitute variables without evaluating
                rendered_expression = _render_jinja_expression_for_display(
                    expression=branch.criteria.expression,
                    context_values=evaluation_context.workflow_run_context.values
                    if evaluation_context.workflow_run_context
                    else {},
                    block_label=self.label,
                )
                branch_eval["rendered_expression"] = rendered_expression

                result = await branch.criteria.evaluate(evaluation_context)
                branch_eval["result"] = result
                branch_evaluations_list.append(branch_eval)

                if result:
                    matched_branch = branch
                    branch_eval["is_matched"] = True
                    LOG.info(
                        "Conditional branch matched",
                        block_label=self.label,
                        branch_index=idx,
                        next_block_label=branch.next_block_label,
                    )
                    break
            except Exception as exc:
                failure_reason = f"Failed to evaluate branch {idx} for {self.label}: {str(exc)}"
                branch_eval["error"] = str(exc)
                branch_eval["result"] = None
                branch_evaluations_list.append(branch_eval)
                LOG.error(
                    "Failed to evaluate conditional branch",
                    block_label=self.label,
                    branch_index=idx,
                    error=str(exc),
                    exc_info=True,
                )
                break

        if matched_branch is None and failure_reason is None:
            matched_branch = self.get_default_branch()
            # Update is_matched for default branch in evaluations
            if matched_branch:
                for eval_entry in branch_evaluations_list:
                    if eval_entry["branch_id"] == matched_branch.id:
                        eval_entry["is_matched"] = True
                        break

        matched_index = self.ordered_branches.index(matched_branch) if matched_branch in self.ordered_branches else None
        next_block_label = matched_branch.next_block_label if matched_branch else None
        executed_branch_id = matched_branch.id if matched_branch else None

        # Extract execution details for frontend display
        executed_branch_expression: str | None = None
        executed_branch_result: bool | None = None
        executed_branch_next_block: str | None = None

        if matched_branch:
            executed_branch_next_block = matched_branch.next_block_label
            if matched_branch.is_default:
                # Default/else branch - no expression to evaluate
                executed_branch_expression = None
                executed_branch_result = None
            elif matched_branch.criteria:
                # Regular condition branch - it matched
                executed_branch_expression = matched_branch.criteria.expression
                executed_branch_result = True

        branch_metadata: BlockMetadata = {
            "branch_taken": next_block_label,
            "branch_index": matched_index,
            "branch_id": executed_branch_id,
            "branch_description": matched_branch.description if matched_branch else None,
            "criteria_type": matched_branch.criteria.criteria_type
            if matched_branch and matched_branch.criteria
            else None,
            "criteria_expression": matched_branch.criteria.expression
            if matched_branch and matched_branch.criteria
            else None,
            "next_block_label": next_block_label,
            # Detailed evaluation info for all branches (rendered_expression trimmed/capped — SKY-9779)
            "evaluations": _trim_branch_evaluations(branch_evaluations_list) if branch_evaluations_list else None,
            # Raw LLM response for debugging prompt-based evaluations (masked for secrets, capped)
            "llm_response": _cap_debug_field(
                workflow_run_context.mask_secrets_in_data(prompt_llm_response)
                if workflow_run_context and prompt_llm_response
                else prompt_llm_response
            ),
            # The exact prompt sent to LLM for debugging (masked for secrets, capped)
            "llm_prompt": _cap_debug_field(
                workflow_run_context.mask_secrets_in_data(prompt_extraction_goal)
                if workflow_run_context and prompt_extraction_goal
                else prompt_extraction_goal
            ),
        }

        status = BlockStatus.completed
        success = True

        if failure_reason:
            status = BlockStatus.failed
            success = False
        elif matched_branch is None:
            failure_reason = "No conditional branch matched and no default branch configured"
            status = BlockStatus.failed
            success = False

        if workflow_run_context:
            workflow_run_context.update_block_metadata(self.label, branch_metadata)
            try:
                await self.record_output_parameter_value(
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    value=branch_metadata,
                )
            except Exception as exc:
                LOG.warning(
                    "Failed to record branch metadata as output parameter",
                    workflow_run_id=workflow_run_id,
                    block_label=self.label,
                    error=str(exc),
                )

        block_result = await self.build_block_result(
            success=success,
            failure_reason=failure_reason,
            output_parameter_value=branch_metadata,
            status=status,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            executed_branch_id=executed_branch_id,
            executed_branch_expression=executed_branch_expression,
            executed_branch_result=executed_branch_result,
            executed_branch_next_block=executed_branch_next_block,
        )
        return block_result

    @property
    def ordered_branches(self) -> list[BranchCondition]:
        """Convenience accessor that returns branches in author-specified list order."""
        return list(self.branch_conditions)

    def get_default_branch(self) -> BranchCondition | None:
        """Return the default/else branch when configured."""
        return next((branch for branch in self.branch_conditions if branch.is_default), None)
