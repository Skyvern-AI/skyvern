import type { Edge } from "@xyflow/react";
import type { ReviewProjection } from "../../copilot/narrativeState";
import type { WorkflowBlock } from "../../types/workflowTypes";
import type { AppNode } from "../nodes";
import {
  matchedBranchId,
  type InputReview,
  type ReviewFieldChange,
  type WorkflowReviewDiff,
} from "../panels/workflowReviewDiff";
import type { ParametersState } from "../types";

export type DocProposalStepMark = "added" | "changed";

export type DocNoteChange = { before: string | null; after: string | null };

export type DocRemovedStep = {
  label: string;
  blockType: string;
  notes: string | null;
  trailingNotes: string | null;
  // The surviving neighbour, enclosing loop/branch, or null when neither survives.
  anchor:
    | { label: string; side: "before" | "after" }
    | { label: string; side: "loop" }
    | { label: string; branchId: string; side: "branch" }
    | null;
};

export type DocProposal = {
  comparisonPending?: boolean;
  comparisonError?: string;
  nodes: AppNode[];
  edges: Edge[];
  // The same version diff used by the workflow comparison panel.
  marks: Map<string, DocProposalStepMark>;
  removed: DocRemovedStep[];
  // What Accept would save, so the Doc's description and Inputs show the proposal, not the live editor.
  description: string | null;
  descriptionChange: DocNoteChange | null;
  settingsChanges: ReviewFieldChange[];
  inputs: InputReview[];
  parameters: ParametersState;
  notes: Map<string, { above?: DocNoteChange; trailing?: DocNoteChange }>;
  keptNotes: string[];
  keptDescriptions: string[];
  review: ReviewProjection | null;
  tested: boolean;
};

type BlockLike = {
  label?: unknown;
  block_type?: unknown;
  notes?: unknown;
  trailing_notes?: unknown;
  loop_blocks?: unknown;
  next_block_label?: unknown;
  branch_conditions?: unknown;
  id?: unknown;
};

type TopologyLink = { label: string; branchId?: string };

function links(block: BlockLike): TopologyLink[] {
  const targets: TopologyLink[] = [];
  if (typeof block.next_block_label === "string" && block.next_block_label)
    targets.push({ label: block.next_block_label });
  if (Array.isArray(block.branch_conditions))
    for (const branch of block.branch_conditions) {
      const branchData = branch as BlockLike | null;
      if (
        typeof branchData?.next_block_label === "string" &&
        branchData.next_block_label
      )
        targets.push({
          label: branchData.next_block_label,
          ...(typeof branchData.id === "string" && {
            branchId: branchData.id,
          }),
        });
    }
  return targets;
}

function noteText(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function children(block: BlockLike): BlockLike[] {
  return Array.isArray(block.loop_blocks)
    ? (block.loop_blocks as BlockLike[])
    : [];
}

function collect(blocks: BlockLike[], into: Map<string, BlockLike>) {
  for (const block of blocks) {
    if (typeof block.label === "string") into.set(block.label, block);
    collect(children(block), into);
  }
}

function removedSteps(
  level: BlockLike[],
  survives: (label: string) => boolean,
  into: DocRemovedStep[],
  topology: {
    previous: Map<string, TopologyLink[]>;
    next: Map<string, TopologyLink[]>;
  } | null,
  enclosingLoops: string[] = [],
) {
  const labels = level.map((b) => (typeof b.label === "string" ? b.label : ""));
  const walk = (from: string, step: Map<string, TopologyLink[]>) => {
    const seen = new Set<string>();
    for (let cursor = from; !seen.has(cursor); ) {
      const parents = step.get(cursor);
      const first = parents?.[0];
      if (!first) return undefined;
      const at =
        parents.length > 1 &&
        parents.some((parent) => parent.branchId) &&
        parents.every((parent) => parent.label === first.label)
          ? { label: first.label }
          : first;
      if (survives(at.label)) return at;
      seen.add(at.label);
      cursor = at.label;
    }
    return undefined;
  };
  level.forEach((block, index) => {
    const label = labels[index]!;
    if (label && !survives(label)) {
      // The run order follows next_block_label and branch targets: the flat array also holds every
      // branch's steps, so adjacency alone can anchor a step beside another branch.
      const beforeLabel = topology
        ? undefined
        : labels.slice(0, index).reverse().find(survives);
      const afterLabel = topology
        ? undefined
        : labels.slice(index + 1).find(survives);
      const before = topology
        ? walk(label, topology.previous)
        : beforeLabel
          ? { label: beforeLabel }
          : undefined;
      const after = topology
        ? walk(label, topology.next)
        : afterLabel
          ? { label: afterLabel }
          : undefined;
      const enclosingLoop = [...enclosingLoops].reverse().find(survives);
      const anchor = before?.branchId
        ? {
            label: before.label,
            branchId: before.branchId,
            side: "branch" as const,
          }
        : before
          ? { label: before.label, side: "after" as const }
          : after
            ? { label: after.label, side: "before" as const }
            : enclosingLoop
              ? { label: enclosingLoop, side: "loop" as const }
              : null;
      into.push({
        label,
        blockType: typeof block.block_type === "string" ? block.block_type : "",
        notes: noteText(block.notes),
        trailingNotes: noteText(block.trailing_notes),
        anchor,
      });
    }
    const nestedLoops =
      Array.isArray(block.loop_blocks) && typeof block.label === "string"
        ? [...enclosingLoops, block.label]
        : enclosingLoops;
    removedSteps(children(block), survives, into, topology, nestedLoops);
  });
}

function topologyOf(blocks: BlockLike[]) {
  const previous = new Map<string, TopologyLink[]>();
  const next = new Map<string, TopologyLink[]>();
  let linked = false;
  const visit = (level: BlockLike[]) => {
    for (const block of level) {
      if (typeof block.label === "string") {
        for (const target of links(block)) {
          linked = true;
          previous.set(target.label, [
            ...(previous.get(target.label) ?? []),
            {
              label: block.label,
              ...(target.branchId && { branchId: target.branchId }),
            },
          ]);
        }
        if (
          typeof block.next_block_label === "string" &&
          block.next_block_label
        )
          next.set(block.label, [{ label: block.next_block_label }]);
      }
      visit(children(block));
    }
  };
  visit(blocks);
  return linked ? { previous, next } : null;
}

export function buildDocProposal({
  nodes,
  edges,
  proposedBlocks,
  baselineBlocks,
  diff,
  review,
  tested = false,
  description,
  parameters,
}: {
  nodes: AppNode[];
  edges: Edge[];
  proposedBlocks: unknown[];
  baselineBlocks: unknown[];
  diff: WorkflowReviewDiff;
  review: ReviewProjection | null;
  tested?: boolean;
  description: string | null;
  parameters: ParametersState;
}): DocProposal {
  const proposed = new Map<string, BlockLike>();
  const baseline = new Map<string, BlockLike>();
  collect(proposedBlocks as BlockLike[], proposed);
  collect(baselineBlocks as BlockLike[], baseline);

  const marks = new Map<string, DocProposalStepMark>();
  for (const [label, block] of diff.after) {
    if (block.status === "new" || block.status === "changed") {
      marks.set(label, block.status === "new" ? "added" : "changed");
    }
  }

  const removed: DocRemovedStep[] = [];
  removedSteps(
    baselineBlocks as BlockLike[],
    (label) => {
      const before = baseline.get(label);
      const after = proposed.get(label);
      const moved = diff.before
        .get(label)
        ?.changes.some((change) => change.key === "__scope");
      return (
        before !== undefined &&
        after !== undefined &&
        after.block_type === before.block_type &&
        !moved
      );
    },
    removed,
    topologyOf(baselineBlocks as BlockLike[]),
  );
  // Copilot conversion can regenerate branch IDs, so render removals against the proposed IDs.
  const matchedBranchRemovals = removed.map((step) => {
    const anchor = step.anchor;
    if (anchor?.side !== "branch") return step;
    const beforeConditional = baseline.get(anchor.label);
    const afterConditional = proposed.get(anchor.label);
    if (!beforeConditional || !afterConditional) return step;
    const branchId = matchedBranchId(
      beforeConditional as WorkflowBlock,
      afterConditional as WorkflowBlock,
      anchor.branchId,
    );
    if (branchId) return { ...step, anchor: { ...anchor, branchId } };
    const stillThere = (
      afterConditional.branch_conditions as BlockLike[] | undefined
    )?.some((branch) => branch?.id === anchor.branchId);
    // A branch Copilot dropped has nowhere to render, so its steps sit after the conditional.
    return stillThere
      ? step
      : { ...step, anchor: { label: anchor.label, side: "after" as const } };
  });

  const notes: DocProposal["notes"] = new Map();
  proposed.forEach((block, label) => {
    const prior = baseline.get(label);
    const change = (key: "notes" | "trailing_notes") => {
      const before = noteText(prior?.[key]);
      const after = noteText(block[key]);
      return before === after ? undefined : { before, after };
    };
    const above = change("notes");
    const trailing = change("trailing_notes");
    if (above || trailing) notes.set(label, { above, trailing });
  });
  const descriptionDiff = diff.settings.find(
    (change) => change.key === "description",
  );

  return {
    nodes,
    edges,
    marks,
    removed: matchedBranchRemovals,
    description,
    descriptionChange: descriptionDiff
      ? { before: descriptionDiff.before, after: descriptionDiff.after }
      : null,
    settingsChanges: diff.settings,
    inputs: diff.inputs,
    parameters,
    notes,
    keptNotes: review?.keptNotes ?? [],
    keptDescriptions: review?.keptDescriptions ?? [],
    review,
    tested,
  };
}
