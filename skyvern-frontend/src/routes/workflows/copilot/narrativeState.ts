// Pure reducer + types for the workflow copilot turn-narrative bubble. Kept
// separate from NarrativeView.tsx so Vite Fast Refresh can hot-reload the
// component without re-evaluating reducer state, and so the reducer can be
// exercised under vitest without a JSX runtime.

import {
  WorkflowCopilotBlockProgressUpdate,
  WorkflowCopilotDesignEndUpdate,
  WorkflowCopilotDesignStartUpdate,
  WorkflowCopilotStreamErrorUpdate,
  WorkflowCopilotStreamResponseUpdate,
  WorkflowCopilotTurnStartUpdate,
  WorkflowCopilotWorkflowDraftUpdate,
} from "./workflowCopilotTypes";

// Discriminated union of every event the reducer below consumes. The bubble
// only renders state derived from these payloads — it does not subscribe to
// tool_call / tool_result / processing_update / narration / condensing.
export type NarrativeEvent =
  | WorkflowCopilotTurnStartUpdate
  | WorkflowCopilotDesignStartUpdate
  | WorkflowCopilotDesignEndUpdate
  | WorkflowCopilotWorkflowDraftUpdate
  | WorkflowCopilotBlockProgressUpdate
  | WorkflowCopilotStreamResponseUpdate
  | WorkflowCopilotStreamErrorUpdate;

// Block lifecycle states as observed via block_progress. The bubble groups
// failed-style states (failed, terminated, timed_out, canceled) under one
// chip and treats `skipped` as a separate neutral state.
export type BlockUIState =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "skipped";

export interface BlockState {
  workflowRunBlockId: string;
  label: string;
  blockType: string;
  state: BlockUIState;
  lastSeenIteration: number;
}

export interface TurnNarrativeState {
  turnId: string | null;
  turnIndex: number | null;
  mode: string;
  designStarted: boolean;
  designEnded: boolean;
  draft: {
    blockCount: number;
    blockLabels: string[];
    summary: string | null;
  } | null;
  blocks: BlockState[];
  terminal: "response" | "error" | null;
  terminalMessage: string | null;
  narrativeSummary: string | null;
}

export const EMPTY_NARRATIVE: TurnNarrativeState = Object.freeze({
  turnId: null,
  turnIndex: null,
  mode: "unknown",
  designStarted: false,
  designEnded: false,
  draft: null,
  blocks: [],
  terminal: null,
  terminalMessage: null,
  narrativeSummary: null,
}) as TurnNarrativeState;

export function mapBlockStatus(raw: string): BlockUIState {
  switch (raw) {
    case "running":
      return "running";
    case "completed":
      return "completed";
    case "failed":
    case "terminated":
    case "timed_out":
    case "canceled":
      return "failed";
    case "skipped":
      return "skipped";
    case "queued":
    default:
      return "queued";
  }
}

export function applyNarrativeEvent(
  prev: TurnNarrativeState,
  event: NarrativeEvent,
): TurnNarrativeState {
  switch (event.type) {
    case "turn_start":
      return {
        ...EMPTY_NARRATIVE,
        turnId: event.turn_id,
        turnIndex: event.turn_index,
        mode: event.mode || "unknown",
      };

    case "design_start":
      return { ...prev, designStarted: true };

    case "design_end":
      return { ...prev, designEnded: true };

    case "workflow_draft":
      return {
        ...prev,
        draft: {
          blockCount: event.block_count,
          blockLabels: event.block_labels,
          summary: event.summary,
        },
      };

    case "block_progress": {
      const incomingState = mapBlockStatus(event.status);
      // Key on workflow_run_block_id, not block_label, so loop iterations
      // (e.g. a for_loop body) render as distinct rows.
      const existing = prev.blocks.findIndex(
        (b) => b.workflowRunBlockId === event.workflow_run_block_id,
      );
      const nextEntry: BlockState = {
        workflowRunBlockId: event.workflow_run_block_id,
        label: event.block_label,
        blockType: event.block_type,
        state: incomingState,
        lastSeenIteration: event.iteration,
      };
      if (existing >= 0) {
        const nextBlocks = prev.blocks.slice();
        nextBlocks[existing] = nextEntry;
        return { ...prev, blocks: nextBlocks };
      }
      return { ...prev, blocks: [...prev.blocks, nextEntry] };
    }

    case "response":
      // Close the design phase on terminal regardless of whether design_end
      // arrived — a refusal turn can emit design_start via the first tool
      // call and then exit without persisting a workflow.
      return {
        ...prev,
        designEnded: true,
        terminal: "response",
        terminalMessage: event.message,
        narrativeSummary: event.narrative_summary ?? event.message,
      };

    case "error":
      return {
        ...prev,
        designEnded: true,
        terminal: "error",
        terminalMessage: event.error,
        narrativeSummary: event.narrative_summary ?? null,
      };

    default:
      // Exhaustiveness guard — any new NarrativeEvent variant added to the
      // union must extend this switch.
      return prev;
  }
}
