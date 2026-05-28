import {
  WorkflowApiResponse,
  WorkflowDefinition,
} from "@/routes/workflows/types/workflowTypes";

export type WorkflowCopilotChatSender = "user" | "ai";
export type ProposalDisposition =
  | "no_proposal"
  | "auto_applicable"
  | "review_untested"
  | "review_tested";

export interface WorkflowCopilotChat {
  workflow_copilot_chat_id: string;
  organization_id: string;
  workflow_permanent_id: string;
  created_at: string;
  modified_at: string;
}

export interface WorkflowCopilotChatMessage {
  workflow_copilot_chat_message_id: string;
  workflow_copilot_chat_id: string;
  sender: WorkflowCopilotChatSender;
  content: string;
  global_llm_context: string | null;
  created_at: string;
  modified_at: string;
}

export interface WorkflowCopilotChatRequest {
  workflow_permanent_id: string;
  workflow_id: string;
  workflow_copilot_chat_id?: string | null;
  workflow_run_id?: string | null;
  browser_session_id?: string | null;
  message: string;
  workflow_yaml: string;
  cancel_token?: string;
}

export interface WorkflowCopilotCancelRequest {
  cancel_token: string;
}

export interface WorkflowCopilotChatHistoryMessage {
  sender: WorkflowCopilotChatSender;
  content: string;
  created_at: string;
}

export interface WorkflowCopilotChatHistoryResponse {
  workflow_copilot_chat_id: string | null;
  chat_history: WorkflowCopilotChatHistoryMessage[];
  proposed_workflow?: WorkflowApiResponse | null;
  auto_accept?: boolean | null;
}

export interface WorkflowCopilotClearProposedWorkflowRequest {
  workflow_copilot_chat_id: string;
  auto_accept: boolean;
}

export interface WorkflowCopilotApplyProposedWorkflowRequest {
  workflow_copilot_chat_id: string;
  auto_accept: boolean;
}

export type WorkflowCopilotStreamMessageType =
  | "processing_update"
  | "response"
  | "error"
  | "tool_call"
  | "tool_result"
  | "condensing"
  | "narration"
  | "block_progress"
  | "turn_start"
  | "design_start"
  | "design_end"
  | "workflow_draft";

export interface WorkflowCopilotProcessingUpdate {
  type: "processing_update";
  status: string;
  timestamp: string;
}

export interface WorkflowCopilotStreamResponseUpdate {
  type: "response";
  workflow_copilot_chat_id: string;
  message: string;
  updated_workflow?: WorkflowApiResponse | null;
  response_time: string;
  proposal_disposition: ProposalDisposition;
  // Cancel forces explicit review.
  cancelled?: boolean;
  // Optional so the FE tolerates an older backend that does not emit the
  // turn-narrative envelope.
  turn_id?: string | null;
  narrative_summary?: string | null;
}

export interface WorkflowCopilotStreamErrorUpdate {
  type: "error";
  error: string;
  turn_id?: string | null;
  narrative_summary?: string | null;
}

export interface WorkflowCopilotTurnStartUpdate {
  type: "turn_start";
  turn_id: string;
  turn_index: number;
  mode: string;
  timestamp: string;
}

export interface WorkflowCopilotDesignStartUpdate {
  type: "design_start";
  timestamp: string;
}

export interface WorkflowCopilotDesignEndUpdate {
  type: "design_end";
  timestamp: string;
}

// Summary-only payload — the full workflow definition is delivered via the
// terminal response's updated_workflow or via the chat's proposed_workflow
// field, not here.
export interface WorkflowCopilotWorkflowDraftUpdate {
  type: "workflow_draft";
  block_count: number;
  block_labels: string[];
  summary: string | null;
  timestamp: string;
}

export interface WorkflowCopilotToolCallUpdate {
  type: "tool_call";
  tool_name: string;
  tool_input: Record<string, unknown>;
  iteration: number;
  tool_call_id: string;
}

export interface WorkflowCopilotToolResultUpdate {
  type: "tool_result";
  tool_name: string;
  success: boolean;
  summary: string;
  iteration: number;
  tool_call_id: string;
  detail?: string | null;
}

export interface WorkflowCopilotCondensingUpdate {
  type: "condensing";
  status: "started" | "completed";
}

export interface WorkflowCopilotNarrationUpdate {
  type: "narration";
  narration: string;
  iteration: number;
  timestamp: string;
}

export interface WorkflowCopilotBlockProgressUpdate {
  type: "block_progress";
  workflow_run_block_id: string;
  block_label: string;
  block_type: string;
  status: string;
  iteration: number;
  timestamp: string;
}

export interface WorkflowYAMLConversionRequest {
  workflow_definition_yaml: string;
  workflow_id: string;
}

export interface WorkflowYAMLConversionResponse {
  workflow_definition: WorkflowDefinition;
}
