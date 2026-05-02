import {
  WorkflowCopilotBlockProgressUpdate,
  WorkflowCopilotToolCallUpdate,
  WorkflowCopilotToolResultUpdate,
} from "./workflowCopilotTypes";

export interface ToolActivity {
  tool_name: string;
  tool_call_id: string;
  status: "running" | "success" | "error";
  summary?: string;
  detail?: string;
  recovered?: boolean;
  // True for the adjacent failure/success pair only — gates the visual
  // connector (left border + ↻). Non-adjacent retries get amber dot only.
  linkedRecovery?: boolean;
}

export function getActivityDotClass(activity: ToolActivity): string {
  if (activity.status === "running") {
    return "animate-pulse bg-blue-400";
  }
  if (activity.status === "success") {
    return "bg-green-400";
  }
  return activity.recovered ? "bg-amber-400" : "animate-pulse bg-amber-400";
}

// Append a new running entry and stop the pulse on any prior unrecovered
// failure — once the agent has started anything new, the previous failure
// is no longer the live event.
export function applyToolCall(
  prev: ToolActivity[],
  payload: WorkflowCopilotToolCallUpdate,
): ToolActivity[] {
  const cleared = prev.map((item) =>
    item.status === "error" && !item.recovered
      ? { ...item, recovered: true }
      : item,
  );
  return [
    ...cleared,
    {
      tool_name: payload.tool_name,
      tool_call_id: payload.tool_call_id,
      status: "running" as const,
    },
  ];
}

// On success, marks every prior unrecovered same-tool error as `recovered`;
// only the immediately adjacent failure also gets `linkedRecovery` so the
// visual connector renders for that pair alone.
export function applyToolResult(
  prev: ToolActivity[],
  payload: WorkflowCopilotToolResultUpdate,
): ToolActivity[] {
  const idx = prev.findIndex(
    (item) => item.tool_call_id === payload.tool_call_id,
  );
  if (idx === -1) {
    return prev;
  }

  // Wire shape allows null on optional fields; UI shape uses undefined.
  const detail = payload.detail ?? undefined;

  if (!payload.success) {
    return prev.map((item, i) =>
      i === idx
        ? {
            ...item,
            status: "error" as const,
            summary: payload.summary,
            detail,
          }
        : item,
    );
  }

  // Adjacency for the visual link is independent of `recovered` — by the
  // time this fires, applyToolCall has already marked the prior failure
  // recovered, but the connector still belongs on the immediately adjacent
  // pair regardless.
  const adjacent = prev[idx - 1];
  const linkedFailureIdx =
    adjacent &&
    adjacent.status === "error" &&
    adjacent.tool_name === payload.tool_name
      ? idx - 1
      : -1;

  // Backwards scan covers the isolated case (applyToolResult invoked
  // without applyToolCall having run first, e.g. unit tests, replay).
  // In the live flow these entries are already recovered=true.
  const recoveredIndices = new Set<number>();
  for (let i = idx - 1; i >= 0; i -= 1) {
    const candidate = prev[i];
    if (
      candidate &&
      candidate.status === "error" &&
      candidate.tool_name === payload.tool_name &&
      !candidate.recovered
    ) {
      recoveredIndices.add(i);
    }
  }

  return prev.map((item, i) => {
    if (i === idx) {
      return {
        ...item,
        status: "success" as const,
        summary: payload.summary,
        detail,
        linkedRecovery: linkedFailureIdx !== -1,
      };
    }
    if (i === linkedFailureIdx) {
      return { ...item, recovered: true, linkedRecovery: true };
    }
    if (recoveredIndices.has(i)) {
      return { ...item, recovered: true };
    }
    return item;
  });
}

// Same status taxonomy as the backend BlockStatus enum.
const BLOCK_FAILED_STATUSES = new Set([
  "failed",
  "terminated",
  "timed_out",
  "canceled",
]);
const BLOCK_COMPLETED_STATUSES = new Set(["completed", "skipped"]);

function blockStatusToActivityStatus(
  status: string,
): "running" | "success" | "error" | null {
  if (status === "running") return "running";
  if (BLOCK_COMPLETED_STATUSES.has(status)) return "success";
  if (BLOCK_FAILED_STATUSES.has(status)) return "error";
  return null;
}

// Per-block lifecycle update from inside long-running tool calls. Keyed by
// `workflow_run_block_id` so duplicate labels in the same run never collide.
export function applyBlockProgress(
  prev: ToolActivity[],
  payload: WorkflowCopilotBlockProgressUpdate,
): ToolActivity[] {
  const nextStatus = blockStatusToActivityStatus(payload.status);
  if (nextStatus === null) return prev;

  const tool_call_id = `block:${payload.workflow_run_block_id}`;
  const tool_name = `Block ${payload.block_label}`;
  const summary = payload.status;

  const idx = prev.findIndex((item) => item.tool_call_id === tool_call_id);
  if (idx === -1) {
    return [
      ...prev,
      {
        tool_name,
        tool_call_id,
        status: nextStatus,
        summary,
      },
    ];
  }

  return prev.map((item, i) =>
    i === idx
      ? {
          ...item,
          tool_name,
          status: nextStatus,
          summary,
        }
      : item,
  );
}
