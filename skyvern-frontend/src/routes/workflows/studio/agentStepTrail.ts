import { type ActionsApiResponse, Status } from "@/api/types";
import { statusIsFinalized } from "@/routes/tasks/types";
import { parseUtcIsoMs } from "@/routes/workflows/copilot/narrativeState";

import { agentStepToolTitles } from "../editor/nodes/AgentStepNode/types";
import { workflowBlockTitle } from "../editor/nodes/types";
import type { BlockYAML } from "../types/workflowYamlTypes";
import {
  type AgentStepToolCall,
  isBlockItem,
  type WorkflowRunBlock,
  type WorkflowRunTimelineBlockItem,
  type WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";

export type AgentStepTrailEntry = {
  call: AgentStepToolCall;
  child: WorkflowRunTimelineBlockItem | null;
  running: boolean;
  // A call left unfinished when its step ended shows the step's final status.
  status: Status;
};

export function agentStepTrail(
  block: WorkflowRunBlock,
  children: Array<WorkflowRunTimelineItem>,
): Array<AgentStepTrailEntry> {
  const calls = Array.isArray(block.tool_calls)
    ? [...block.tool_calls].sort((a, b) => a.index - b.index)
    : [];
  const childByLabel = new Map(
    children.filter(isBlockItem).map((item) => [item.block.label, item]),
  );
  const final =
    block.status !== null && statusIsFinalized({ status: block.status });
  return calls.map((call) => ({
    call,
    child: (call.label && childByLabel.get(call.label)) || null,
    running: call.status === Status.Running && !final,
    status: toolCallStatus(call, final ? block.status : null),
  }));
}

function toolCallStatus(
  call: AgentStepToolCall,
  finalStepStatus: Status | null,
): Status {
  const status = call.status;
  if (status === Status.Running) {
    return finalStepStatus ?? Status.Running;
  }
  if (status === Status.Skipped) {
    return status;
  }
  return status &&
    status !== "invalid_arguments" &&
    statusIsFinalized({ status })
    ? status
    : Status.Failed;
}

// By label, the newest row (the latest loop iteration); by id, exactly that row.
export type AgentStepRef = { label: string } | { workflowRunBlockId: string };

export function findAgentStepItem(
  timeline: Array<WorkflowRunTimelineItem>,
  ref: AgentStepRef,
): WorkflowRunTimelineBlockItem | null {
  let found: WorkflowRunTimelineBlockItem | null = null;
  const visit = (items: Array<WorkflowRunTimelineItem>) => {
    for (const item of items) {
      if (
        isBlockItem(item) &&
        item.block.block_type === "agent_step" &&
        ("workflowRunBlockId" in ref
          ? item.block.workflow_run_block_id === ref.workflowRunBlockId
          : item.block.label === ref.label) &&
        (found === null || item.block.created_at >= found.block.created_at)
      ) {
        found = item;
      }
      visit(item.children);
    }
  };
  visit(timeline);
  return found;
}

export function toolCallTitle(call: AgentStepToolCall): string {
  if (call.block_type) {
    return workflowBlockTitle[call.block_type];
  }
  return agentStepToolTitles[call.tool] ?? call.tool;
}

function parsedArguments(call: AgentStepToolCall): unknown {
  try {
    return JSON.parse(call.arguments);
  } catch {
    return call.arguments;
  }
}

function displayValue(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

export function toolCallArgumentFields(
  call: AgentStepToolCall,
): Array<[string, string]> {
  const parsed = parsedArguments(call);
  if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
    return Object.entries(parsed).map(([key, value]) => [
      key,
      displayValue(value),
    ]);
  }
  return call.arguments ? [["arguments", displayValue(parsed)]] : [];
}

export function toolCallSummary(call: AgentStepToolCall): string {
  const [first] = toolCallArgumentFields(call);
  return first ? first[1] : "";
}

// The column holds the child block's workflow YAML as JSON, written by the agent step loop.
export function toolCallBlock(
  blockJson: string | null | undefined,
): BlockYAML | null {
  if (!blockJson) {
    return null;
  }
  try {
    const block: unknown = JSON.parse(blockJson);
    return block !== null && typeof block === "object" && !Array.isArray(block)
      ? (block as BlockYAML)
      : null;
  } catch {
    return null;
  }
}

function inCallWindow(call: AgentStepToolCall, created: number): boolean {
  const start = parseUtcIsoMs(call.started_at);
  const end = parseUtcIsoMs(call.finished_at) ?? Number.MAX_SAFE_INTEGER;
  return start !== null && created >= start && created <= end;
}

// The agent step's own page actions inside the call's window, oldest first. Windows can share an edge, so an action
// belongs to the newest call whose window holds it; block calls record theirs on the child row.
export function agentStepCallActions(
  agentStep: WorkflowRunBlock,
  call: AgentStepToolCall,
): Array<ActionsApiResponse> {
  if (call.block_type) {
    return [];
  }
  const later = (agentStep.tool_calls ?? []).filter(
    (other) => !other.block_type && other.index > call.index,
  );
  return (agentStep.actions ?? [])
    .filter((action) => {
      const created = parseUtcIsoMs(action.created_at);
      return (
        created !== null &&
        inCallWindow(call, created) &&
        !later.some((other) => inCallWindow(other, created))
      );
    })
    .sort(
      (a, b) =>
        (parseUtcIsoMs(a.created_at) ?? 0) - (parseUtcIsoMs(b.created_at) ?? 0),
    );
}

// The child row for a block call; otherwise the call's newest page action, else the step.
export function agentStepCallFrameId(
  agentStep: WorkflowRunBlock,
  entry: AgentStepTrailEntry,
): string {
  const childId =
    entry.child?.block.workflow_run_block_id ??
    entry.call.child_workflow_run_block_id;
  if (childId) {
    return childId;
  }
  const actions = agentStepCallActions(agentStep, entry.call);
  return (
    actions[actions.length - 1]?.action_id ?? agentStep.workflow_run_block_id
  );
}
