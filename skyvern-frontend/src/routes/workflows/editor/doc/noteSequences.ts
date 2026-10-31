import type { Edge } from "@xyflow/react";

import type { AppNode } from "../nodes";
import { getWorkflowBlocks } from "../workflowEditorUtils";

// Mirrors skyvern/forge/sdk/workflow/block_notes.py (_sequences and step_notes_by_label): the steps that read
// each note at run time. Keep the two in step; a marker or Doc line that disagrees with the run misleads.

type NoteBlock = {
  label: string;
  block_type: string;
  next_block_label?: string | null;
  branch_conditions?: Array<{ next_block_label?: string | null }>;
  loop_blocks?: Array<unknown>;
  notes?: string | null;
  trailing_notes?: string | null;
};

function isBlock(value: unknown): value is NoteBlock {
  const block = value as Partial<NoteBlock> | null;
  return typeof block?.label === "string" && block.block_type != null;
}

function noteText(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function branchHeads(block: NoteBlock): Array<string | null> {
  return Array.isArray(block.branch_conditions)
    ? block.branch_conditions.map((branch) =>
        typeof branch?.next_block_label === "string" && branch.next_block_label
          ? branch.next_block_label
          : null,
      )
    : [];
}

function sequences(blocks: Array<unknown>, finallyLabel: string | null) {
  const candidates = blocks.filter(isBlock);
  const byLabel = new Map(candidates.map((block) => [block.label, block]));
  const inOrder = candidates
    .filter((block) => block.label !== finallyLabel)
    .map((block) => ({
      ...block,
      next_block_label:
        block.next_block_label === finallyLabel ? null : block.next_block_label,
      branch_conditions:
        block.block_type === "conditional" &&
        Array.isArray(block.branch_conditions)
          ? block.branch_conditions.map((branch) => ({
              ...branch,
              next_block_label:
                branch?.next_block_label === finallyLabel
                  ? null
                  : branch?.next_block_label,
            }))
          : block.branch_conditions,
    }));
  const runByLabel = new Map(inOrder.map((block) => [block.label, block]));
  const nextByLabel = new Map<string, string | null>(
    inOrder.map((block) => [
      block.label,
      typeof block.next_block_label === "string"
        ? block.next_block_label
        : null,
    ]),
  );
  if (!inOrder.some((block) => block.block_type === "conditional"))
    inOrder.forEach((block, index) => {
      if (nextByLabel.get(block.label) === null)
        nextByLabel.set(block.label, inOrder[index + 1]?.label ?? null);
    });

  let changed = true;
  while (changed) {
    changed = false;
    for (const block of inOrder) {
      if (block.block_type !== "conditional") continue;
      const successor = nextByLabel.get(block.label);
      if (!successor) continue;
      for (const head of branchHeads(block)) {
        const visited = new Set<string>();
        let current = head;
        while (
          current &&
          current !== successor &&
          runByLabel.has(current) &&
          !visited.has(current)
        ) {
          visited.add(current);
          const next = nextByLabel.get(current) ?? null;
          if (next === null) {
            nextByLabel.set(current, successor);
            changed = true;
            break;
          }
          current = next;
        }
      }
    }
  }

  const placed = new Set<string>();
  const result: NoteBlock[][] = [];
  const pendingConditionals: NoteBlock[] = [];
  const walk = (start: string | null, stop: string | null) => {
    const sequence: NoteBlock[] = [];
    let current = start;
    while (
      current &&
      runByLabel.has(current) &&
      !placed.has(current) &&
      current !== stop
    ) {
      placed.add(current);
      const block = runByLabel.get(current)!;
      sequence.push(block);
      if (block.block_type === "conditional") pendingConditionals.push(block);
      current = nextByLabel.get(current) ?? null;
    }
    if (sequence.length) result.push(sequence);
  };

  const targets = new Set([
    ...nextByLabel.values(),
    ...inOrder.flatMap(branchHeads),
  ]);
  const head = inOrder.find((block) => !targets.has(block.label));
  if (head) walk(head.label, null);
  while (pendingConditionals.length) {
    const conditional = pendingConditionals.shift()!;
    const merge =
      typeof conditional.next_block_label === "string"
        ? conditional.next_block_label
        : null;
    for (const branchHead of branchHeads(conditional)) walk(branchHead, merge);
  }
  for (const block of candidates) {
    if (!placed.has(block.label)) {
      placed.add(block.label);
      result.push([block]);
    }
  }
  return { sequences: result, byLabel, nextByLabel };
}

export type NoteReadings = {
  // Note texts each step reads, in the order the run assembles them: own, next step's, trailing.
  textsByLabel: Map<string, string[]>;
  // The other step that reads a step's above note: the one before it in its run sequence.
  previousByLabel: Map<string, string>;
};

export function noteReadings(
  blocks: Array<unknown>,
  finallyLabel: string | null = null,
): NoteReadings {
  const textsByLabel = new Map<string, string[]>();
  const previousByLabel = new Map<string, string>();
  const visit = (items: Array<unknown>, finally_: string | null) => {
    const {
      sequences: runs,
      byLabel,
      nextByLabel,
    } = sequences(items, finally_);
    for (const sequence of runs) {
      sequence.forEach((block, index) => {
        const following =
          sequence[index + 1] ??
          byLabel.get(nextByLabel.get(block.label) ?? "");
        if (following) previousByLabel.set(following.label, block.label);
        const parts = [
          noteText(block.notes),
          following ? noteText(following.notes) : null,
          noteText(block.trailing_notes),
        ].filter((part): part is string => part !== null);
        if (parts.length) textsByLabel.set(block.label, parts);
        if (Array.isArray(block.loop_blocks)) visit(block.loop_blocks, null);
      });
    }
  };
  visit(blocks, finallyLabel);
  return { textsByLabel, previousByLabel };
}

export function graphNoteReadings(
  nodes: AppNode[],
  edges: Edge[],
  finallyLabel: string | null,
): NoteReadings {
  return noteReadings(getWorkflowBlocks(nodes, edges), finallyLabel);
}
