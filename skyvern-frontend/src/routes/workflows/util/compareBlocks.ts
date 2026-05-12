import { stableStringify } from "@/util/stableStringify";
import { WorkflowBlock } from "../types/workflowTypes";

// Backend-generated fields that diverge across workflow versions even when the
// user-visible block content is unchanged. Copilot's rehydrated proposal
// regenerates IDs and timestamps for every parameter, and convert_workflow_definition
// rebuilds BranchCondition with a fresh UUID `id`, so leaving any of these in
// the equality input would flip every block to "modified". The bare `id` here
// targets BranchCondition.id - the only non-suffixed ID field in the block
// model today; revisit if a block type ever gains a user-meaningful `id`.
const COMPARISON_OMIT_KEYS = new Set([
  "output_parameter",
  "workflow_id",
  "created_at",
  "modified_at",
  "deleted_at",
  "id",
]);

function shouldOmitForComparison(key: string): boolean {
  return COMPARISON_OMIT_KEYS.has(key) || key.endsWith("_parameter_id");
}

function areBlocksIdentical(
  block1: WorkflowBlock,
  block2: WorkflowBlock,
): boolean {
  return (
    stableStringify(block1, { omit: shouldOmitForComparison }) ===
    stableStringify(block2, { omit: shouldOmitForComparison })
  );
}

export { areBlocksIdentical, shouldOmitForComparison };
