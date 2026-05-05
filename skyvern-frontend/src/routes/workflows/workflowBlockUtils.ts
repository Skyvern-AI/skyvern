import {
  isNestedLoopWorkflowBlock,
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
