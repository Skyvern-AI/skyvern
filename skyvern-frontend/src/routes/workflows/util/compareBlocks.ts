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

// Not block content: the label is the block's identity, `next_block_label`
// follows from where the block sits, and loop children are compared as blocks
// of their own.
const STRUCTURAL_BLOCK_KEYS: ReadonlySet<string> = new Set([
  "label",
  "next_block_label",
  "loop_blocks",
]);

function shouldOmitForComparison(key: string): boolean {
  return COMPARISON_OMIT_KEYS.has(key) || key.endsWith("_parameter_id");
}

type RawFieldChange = {
  key: string;
  before: unknown;
  after: unknown;
};

function isEmptyValue(value: unknown): boolean {
  if (value === null || value === undefined || value === "") return true;
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === "object") return Object.keys(value).length === 0;
  return false;
}

function comparableValue(value: unknown): string | undefined {
  // Absent, null, and empty all read as "not set" to a reviewer, and the two
  // sides reach the panel through different serializers that disagree on them.
  if (isEmptyValue(value)) return undefined;
  return stableStringify(value, { omit: shouldOmitForComparison });
}

function areValuesEquivalent(before: unknown, after: unknown): boolean {
  return comparableValue(before) === comparableValue(after);
}

function diffRecordFields(
  before: Record<string, unknown>,
  after: Record<string, unknown>,
  ignoredKeys: ReadonlySet<string> = new Set(),
): Array<RawFieldChange> {
  const keys = [...new Set([...Object.keys(after), ...Object.keys(before)])];
  return keys
    .filter((key) => !ignoredKeys.has(key) && !shouldOmitForComparison(key))
    .filter((key) => !areValuesEquivalent(before[key], after[key]))
    .map((key) => ({ key, before: before[key], after: after[key] }));
}

// A block's parameters embed whole inputs, which are reviewed on their own, so
// a block only differs in which inputs it reads. Branch targets are structure,
// like `next_block_label`.
function comparableBlock(block: WorkflowBlock): Record<string, unknown> {
  const record = { ...block } as Record<string, unknown>;
  if (Array.isArray(record.parameters)) {
    record.parameters = record.parameters.map((parameter: unknown) =>
      parameter !== null &&
      typeof parameter === "object" &&
      typeof (parameter as { key?: unknown }).key === "string"
        ? (parameter as { key: string }).key
        : parameter,
    );
  }
  if (Array.isArray(record.branch_conditions)) {
    record.branch_conditions = record.branch_conditions.map(
      (branch: Record<string, unknown>) => ({
        ...branch,
        next_block_label: undefined,
      }),
    );
  }
  return record;
}

function diffBlockFields(
  before: WorkflowBlock,
  after: WorkflowBlock,
): Array<RawFieldChange> {
  return diffRecordFields(
    comparableBlock(before),
    comparableBlock(after),
    STRUCTURAL_BLOCK_KEYS,
  );
}

export {
  areValuesEquivalent,
  diffBlockFields,
  diffRecordFields,
  isEmptyValue,
  shouldOmitForComparison,
  type RawFieldChange,
};
