import { getReadableActionType } from "@/api/types";

import {
  isNestedLoopWorkflowBlock,
  type CodeBlockStep,
  type WorkflowBlock,
  type WorkflowBlockType,
} from "./types/workflowTypes";

export function findWorkflowBlockByLabel(
  blocks: Array<WorkflowBlock>,
  label: string,
): WorkflowBlock | null {
  let found: WorkflowBlock | null = null;

  visitWorkflowBlocks(blocks, (block) => {
    if (!found && block.label === label) {
      found = block;
      return false;
    }
  });

  return found;
}

export function visitWorkflowBlocks(
  blocks: Array<WorkflowBlock>,
  visit: (block: WorkflowBlock) => void | false,
) {
  for (const block of blocks) {
    if (visit(block) === false) {
      return false;
    }

    if (isNestedLoopWorkflowBlock(block) && block.loop_blocks.length > 0) {
      if (visitWorkflowBlocks(block.loop_blocks, visit) === false) {
        return false;
      }
    }
  }

  return true;
}

export function isBlockOfType<T extends WorkflowBlockType>(
  block: WorkflowBlock | null,
  type: T,
): block is Extract<WorkflowBlock, { block_type: T }> {
  return block?.block_type === type;
}

/**
 * Map each code block's label to its definition step outline, descending into
 * loop bodies. The run timeline carries no step outline on the runtime block, so
 * it looks steps up here by label to render them beneath the code block.
 */
export function buildCodeStepsByLabel(
  blocks: Array<WorkflowBlock>,
): Map<string, Array<CodeBlockStep>> {
  const stepsByLabel = new Map<string, Array<CodeBlockStep>>();

  visitWorkflowBlocks(blocks, (block) => {
    if (block.block_type === "code" && block.steps && block.steps.length > 0) {
      stepsByLabel.set(block.label, block.steps);
    }
  });

  return stepsByLabel;
}

/**
 * Plain-English text for a code-block step: prefer the generated title, then
 * the description, and only humanize the raw action type when neither is
 * present.
 */
export function getCodeStepPlainText(step: CodeBlockStep): string {
  const title = step.title?.trim();
  if (title) {
    return title;
  }
  const description = step.description?.trim();
  if (description) {
    return description;
  }
  return getReadableActionType(step.action_type);
}

/**
 * Resolve the definition step a recorded action belongs to by its source line.
 * A fired action carries its `code_line`; match it to the step whose
 * `line_start` equals that line, falling back to the step whose
 * `[line_start, line_end]` range contains it.
 */
export function findCodeStepForLine(
  steps: Array<CodeBlockStep>,
  codeLine: number | null,
): CodeBlockStep | null {
  if (codeLine == null) {
    return null;
  }
  const exact = steps.find((step) => step.line_start === codeLine);
  if (exact) {
    return exact;
  }
  return (
    steps.find(
      (step) =>
        step.line_start != null &&
        codeLine >= step.line_start &&
        codeLine <= (step.line_end ?? step.line_start),
    ) ?? null
  );
}
