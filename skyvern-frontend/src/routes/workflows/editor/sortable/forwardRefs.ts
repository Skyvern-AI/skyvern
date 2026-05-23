import type { Node } from "@xyflow/react";

import {
  getAffectedBlocks,
  objectContainsJinjaReference,
  SKIP_KEYS_FOR_JINJA_CHECK,
} from "../jinjaReferences";

/**
 * A block that contains a Jinja reference to the moved block's label and
 * would — after the reorder — execute before the moved block. Skyvern
 * evaluates blocks in chain order, so a forward reference is unresolved at
 * execution time and must be rejected before the drop commits.
 */
export type ForwardReferenceViolation = {
  referrerNodeId: string;
  referrerLabel: string;
};

type MovableNode = Node & { data?: { label?: unknown } };

type FindForwardReferenceViolationsInput<T extends MovableNode> = {
  /** Full node list (rich data, not the scope-minimal shape). */
  nodes: Array<T>;
  /** Sibling ids in the scope *after* the drop has been applied. */
  newOrder: Array<string>;
  /** Id of the block being moved. */
  movedNodeId: string;
};

/**
 * Returns every block whose Jinja references would be broken by the drop —
 * in *either* direction:
 *
 *   (a) Moved-referent direction — another block references the moved
 *       block's label and now precedes it in the chain. Covers e.g.
 *       "drag A down past B, and B references A".
 *   (b) Moved-referrer direction — the moved block itself references
 *       another block's label and now precedes it in the chain. Covers
 *       e.g. "drag C up past A, and C references A".
 *
 * Both directions produce the same silent-null-at-runtime failure mode
 * because Skyvern evaluates blocks in chain order, so both must be caught
 * here. The caller is expected to block the drop and surface a toast.
 *
 * Delegates to `getAffectedBlocks` + `objectContainsJinjaReference` (both
 * in `../jinjaReferences`) so refs nested inside arrays/objects (loop
 * prompts, HTTP headers, etc.) are caught, not just top-level string
 * fields.
 *
 * Returns an empty list when the reorder is safe.
 */
export function findForwardReferenceViolations<T extends MovableNode>(
  input: FindForwardReferenceViolationsInput<T>,
): ForwardReferenceViolation[] {
  const { nodes, newOrder, movedNodeId } = input;

  const movedNode = nodes.find((n) => n.id === movedNodeId);
  const movedLabelRaw = movedNode?.data?.label;
  if (typeof movedLabelRaw !== "string" || movedLabelRaw.length === 0) {
    return [];
  }

  const movedNewIndex = newOrder.indexOf(movedNodeId);
  if (movedNewIndex < 0) return [];

  const violations: ForwardReferenceViolation[] = [];

  // (a) Moved-referent direction — scan all other blocks for references to
  // the moved block's label OR its output key, then flag any that now
  // precede the moved block in the chain.
  const movedOutputKey = movedLabelRaw + "_output";
  const affectedByLabel = getAffectedBlocks(nodes, movedLabelRaw);
  const affectedByOutput = getAffectedBlocks(nodes, movedOutputKey);

  const seenInA = new Set<string>();

  // Label refs: only Jinja refs carry ordering semantics for the raw label.
  // A `parameterKeys` entry matching the raw label is a workflow-parameter
  // name collision, not a block-output dependency, so those are skipped.
  for (const ref of affectedByLabel) {
    if (ref.nodeId === movedNodeId) continue;
    if (!ref.hasJinjaReference) continue;
    seenInA.add(ref.nodeId);
    const referrerIndex = newOrder.indexOf(ref.nodeId);
    // TODO(SKY-9520): extend scoping to nested reorders.
    if (referrerIndex < 0) continue;
    if (referrerIndex < movedNewIndex) {
      violations.push({ referrerNodeId: ref.nodeId, referrerLabel: ref.label });
    }
  }

  // Output-key refs: `parameterKeys = ["A_output"]` is a real block-output
  // dependency stored by ParametersMultiSelect, so both Jinja and
  // parameterKey matches count here.
  for (const ref of affectedByOutput) {
    if (ref.nodeId === movedNodeId) continue;
    if (!ref.hasJinjaReference && !ref.hasParameterKeyReference) continue;
    if (seenInA.has(ref.nodeId)) continue;
    seenInA.add(ref.nodeId);
    const referrerIndex = newOrder.indexOf(ref.nodeId);
    // TODO(SKY-9520): extend scoping to nested reorders.
    if (referrerIndex < 0) continue;
    if (referrerIndex < movedNewIndex) {
      violations.push({ referrerNodeId: ref.nodeId, referrerLabel: ref.label });
    }
  }

  // (b) Moved-referrer direction — scan the moved block's own data for
  // references to every other block's label/output-key, then flag the
  // moved block once if any of those referents now land after it in the
  // chain. Checks both Jinja text fields and `parameterKeys` (the latter
  // stores output-key deps from ParametersMultiSelect).
  const movedData = movedNode?.data;
  const movedParamKeys = Array.isArray(movedData?.parameterKeys)
    ? (movedData!.parameterKeys as Array<string>)
    : [];
  if (movedData) {
    let movedReferencesLaterBlock = false;
    for (const otherNode of nodes) {
      if (otherNode.id === movedNodeId) continue;
      const otherLabel = otherNode.data?.label;
      if (typeof otherLabel !== "string" || otherLabel.length === 0) continue;
      const otherNewIndex = newOrder.indexOf(otherNode.id);
      if (otherNewIndex <= movedNewIndex) continue;
      const otherOutputKey = otherLabel + "_output";
      if (
        objectContainsJinjaReference(
          movedData,
          otherLabel,
          SKIP_KEYS_FOR_JINJA_CHECK,
        ) ||
        objectContainsJinjaReference(
          movedData,
          otherOutputKey,
          SKIP_KEYS_FOR_JINJA_CHECK,
        ) ||
        movedParamKeys.includes(otherOutputKey)
      ) {
        movedReferencesLaterBlock = true;
        break;
      }
    }
    if (movedReferencesLaterBlock) {
      violations.push({
        referrerNodeId: movedNodeId,
        referrerLabel: movedLabelRaw,
      });
    }
  }

  return violations;
}
