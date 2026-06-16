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
  for (const block of blocks) {
    if (block.label === label) {
      return block;
    }
    if (isNestedLoopWorkflowBlock(block) && block.loop_blocks.length > 0) {
      const nested = findWorkflowBlockByLabel(block.loop_blocks, label);
      if (nested) {
        return nested;
      }
    }
  }
  return null;
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
  const visit = (items: Array<WorkflowBlock>) => {
    for (const block of items) {
      if (
        block.block_type === "code" &&
        block.steps &&
        block.steps.length > 0
      ) {
        stepsByLabel.set(block.label, block.steps);
      }
      if (isNestedLoopWorkflowBlock(block) && block.loop_blocks.length > 0) {
        visit(block.loop_blocks);
      }
    }
  };
  visit(blocks);
  return stepsByLabel;
}
