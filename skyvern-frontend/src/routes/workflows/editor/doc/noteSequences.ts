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

function reachableLabels(
  starts: Array<string | null>,
  runByLabel: Map<string, NoteBlock>,
  nextByLabel: Map<string, string | null>,
): Set<string> {
  const seen = new Set<string>();
  const pending = [...starts];
  while (pending.length) {
    const label = pending.pop();
    const block = label ? runByLabel.get(label) : undefined;
    if (!label || !block || seen.has(label)) continue;
    seen.add(label);
    if (block.block_type === "conditional") {
      pending.push(...branchHeads(block));
      continue;
    }
    pending.push(nextByLabel.get(label) ?? null);
  }
  return seen;
}

function linkBranchTails(
  conditionals: Array<NoteBlock>,
  runByLabel: Map<string, NoteBlock>,
  nextByLabel: Map<string, string | null>,
  skipReached: boolean,
): Set<string> {
  const skipped = new Set<string>();
  let changed = true;
  while (changed) {
    changed = false;
    for (const block of conditionals) {
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
            if (
              skipReached &&
              reachableLabels([successor], runByLabel, nextByLabel).has(current)
            ) {
              skipped.add(block.label);
            } else {
              nextByLabel.set(current, successor);
              changed = true;
            }
            break;
          }
          current = next;
        }
      }
    }
  }
  return skipped;
}

// Mirrors _SKIP_PATH_WORK_LIMIT in block.py: a bigger looping graph keeps every link.
const SKIP_PATH_WORK_LIMIT = 1_000_000;

function hasLoop(
  runByLabel: Map<string, NoteBlock>,
  nextByLabel: Map<string, string | null>,
): boolean {
  const successors = (label: string) =>
    (runByLabel.get(label)!.block_type === "conditional"
      ? branchHeads(runByLabel.get(label)!)
      : [nextByLabel.get(label) ?? null]
    ).filter((target): target is string => !!target && runByLabel.has(target));
  const state = new Map<string, "open" | "done">();
  for (const root of runByLabel.keys()) {
    if (state.has(root)) continue;
    state.set(root, "open");
    const stack: Array<[string, Iterator<string>]> = [
      [root, successors(root)[Symbol.iterator]()],
    ];
    while (stack.length) {
      const [label, pending] = stack[stack.length - 1]!;
      const step = pending.next();
      if (step.done) {
        state.set(label, "done");
        stack.pop();
      } else if (state.get(step.value) === "open") {
        return true;
      } else if (!state.has(step.value)) {
        state.set(step.value, "open");
        stack.push([step.value, successors(step.value)[Symbol.iterator]()]);
      }
    }
  }
  return false;
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

  const conditionals = inOrder.filter(
    (block) => block.block_type === "conditional",
  );
  let resolved = new Map(nextByLabel);
  linkBranchTails(conditionals, runByLabel, resolved, false);
  // Links are skipped only when resolving every link leaves a loop.
  let skipped = new Set<string>();
  const edges =
    runByLabel.size +
    conditionals.reduce((sum, block) => sum + branchHeads(block).length, 0);
  if (
    hasLoop(runByLabel, resolved) &&
    conditionals.length * edges <= SKIP_PATH_WORK_LIMIT
  ) {
    resolved = new Map(nextByLabel);
    skipped = linkBranchTails(conditionals, runByLabel, resolved, true);
  }
  if (skipped.size) {
    const reach = new Map(
      conditionals.map((block) => [
        block.label,
        reachableLabels(branchHeads(block), runByLabel, nextByLabel),
      ]),
    );
    const involved = new Set(skipped);
    skipped.forEach((label) =>
      reach.get(label)?.forEach((reached) => involved.add(reached)),
    );
    reach.forEach((reached, label) => {
      if ([...skipped].some((s) => reached.has(s))) involved.add(label);
    });
    const slots = conditionals.flatMap((block, index) =>
      involved.has(block.label) ? [index] : [],
    );
    const byReach = slots
      .map((index) => conditionals[index]!)
      .sort(
        (a, b) =>
          (reach.get(b.label)?.size ?? 0) - (reach.get(a.label)?.size ?? 0) ||
          (a.label < b.label ? -1 : a.label > b.label ? 1 : 0),
      );
    const reordered = [...conditionals];
    slots.forEach((slot, index) => {
      reordered[slot] = byReach[index]!;
    });
    resolved = new Map(nextByLabel);
    linkBranchTails(reordered, runByLabel, resolved, true);
  }
  resolved.forEach((next, label) => nextByLabel.set(label, next));

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
