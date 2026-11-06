import { create } from "zustand";

import { Status } from "@/api/types";
import { statusIsFinalized } from "@/routes/tasks/types";
import type { WorkflowCopilotDemonstrationInsertion } from "@/routes/workflows/copilot/workflowCopilotTypes";

export type DemonstrateFromBlockTarget = {
  workflowPermanentId: string;
  blockLabel: string;
  prefixLabels: string[];
  insertion: WorkflowCopilotDemonstrationInsertion;
  // Set when the earlier blocks can't be run for this block; the user starts from the current page instead.
  runUnavailableReason?: string | null;
};

export type PendingDemonstration = DemonstrateFromBlockTarget & {
  runId: string;
  browserSessionId: string | null;
  retry: () => void;
};

type DemonstrateFromBlockState = {
  pending: PendingDemonstration | null;
  setPending: (pending: PendingDemonstration | null) => void;
};

// Set once the run of the earlier blocks is accepted; the status card takes it from there.
export const useDemonstrateFromBlockStore = create<DemonstrateFromBlockState>()(
  (set) => ({
    pending: null,
    setPending: (pending) => set({ pending }),
  }),
);

export type DemonstrateFromBlockStep = "running" | "start" | "failed";

// What the status card does next, given the earlier blocks' run.
export function demonstrateFromBlockStep(
  runStatus: Status | undefined,
): DemonstrateFromBlockStep {
  if (!runStatus || !statusIsFinalized({ status: runStatus })) return "running";
  return runStatus === Status.Completed ? "start" : "failed";
}

type GraphNode = {
  id: string;
  type?: string;
  parentId?: string;
  data?: { label?: unknown };
};
type GraphEdge = { source: string; target: string };

// The top-level blocks that run before the target, in run order. Null when the path from the start is not
// a single chain to the target (the target is nested, or the path branches).
export function demonstrationPrefixFromGraph(
  nodes: Array<GraphNode>,
  edges: Array<GraphEdge>,
  targetNodeId: string,
): Array<{ id: string; label: string }> | null {
  const topLevel = new Map(
    nodes.filter((node) => !node.parentId).map((node) => [node.id, node]),
  );
  const prefix: Array<{ id: string; label: string }> = [];
  const seen = new Set<string>();
  let current = nodes.find(
    (node) => node.type === "start" && !node.parentId,
  )?.id;
  while (current && !seen.has(current)) {
    if (current === targetNodeId) return prefix;
    seen.add(current);
    const node = topLevel.get(current);
    // A prefix run executes only the listed labels, and a conditional's branch blocks are not top-level.
    if (node?.type === "conditional") return null;
    const label = node?.data?.label;
    if (node && node.type !== "start" && typeof label === "string")
      prefix.push({ id: node.id, label });
    const next = edges.filter(
      (edge) => edge.source === current && topLevel.has(edge.target),
    );
    if (next.length !== 1) return null;
    current = next[0]!.target;
  }
  return null;
}
