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
export type CopilotResponseType = "REPLY" | "ASK_QUESTION" | "REPLACE_WORKFLOW";

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
  audio_artifact_id?: string | null;
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
  audio_artifact_id?: string | null;
  workflow_yaml: string;
  mode?: "ask" | "build" | null;
  code_block?: boolean | null;
  cancel_token?: string;
  target_block_label?: string | null;
  fix_origin?: boolean;
  keep_pending_proposal?: boolean;
  // Opt-in: only clients that can render the credential_required frame set
  // this, so the backend never pauses a turn a client would silently drop.
  supports_credential_pause?: boolean;
}

export interface WorkflowCopilotCancelRequest {
  cancel_token: string;
}

export interface WorkflowCopilotChatHistoryMessage {
  sender: WorkflowCopilotChatSender;
  content: string;
  audio_artifact_id?: string | null;
  created_at: string;
  // Typed turn outcome persisted on assistant rows; optional so the FE
  // tolerates an older backend that does not serve it.
  turn_outcome?: { response_kind?: string | null } | null;
  narrative_payload?: Record<string, unknown> | null;
}

export interface WorkflowCopilotChatHistoryResponse {
  workflow_copilot_chat_id: string | null;
  chat_history: WorkflowCopilotChatHistoryMessage[];
  proposed_workflow?: WorkflowApiResponse | null;
  auto_accept?: boolean | null;
}

export interface WorkflowCopilotChatSummary {
  workflow_copilot_chat_id: string;
  workflow_permanent_id: string;
  // Returned by the API; not rendered since the list is already scoped to one workflow.
  workflow_title?: string | null;
  title: string;
  created_at: string;
  modified_at: string;
}

export interface WorkflowCopilotClearProposedWorkflowRequest {
  workflow_copilot_chat_id: string;
  auto_accept: boolean;
}

export interface WorkflowCopilotApplyProposedWorkflowRequest {
  workflow_copilot_chat_id: string;
  auto_accept: boolean;
}

export interface WorkflowCopilotAudioUploadResponse {
  workflow_copilot_chat_id: string;
  audio_artifact_id: string;
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
  | "run_outcome"
  | "turn_start"
  | "design_start"
  | "design_end"
  | "workflow_draft"
  | "credential_required";

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
  response_type?: CopilotResponseType;
  proposal_disposition: ProposalDisposition;
  workflow_applied?: boolean;
  // Cancel forces explicit review.
  cancelled?: boolean;
  // Optional so the FE tolerates an older backend that does not emit the
  // turn-narrative envelope.
  turn_id?: string | null;
  narrative_summary?: string | null;
  narrative_payload?: Record<string, unknown> | null;
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
  // Block count of the canonical workflow at turn entry. Drives the FE's
  // edit-vs-build chip; the snap-back source is captured client-side at
  // submit time so unsaved local canvas edits survive.
  prior_block_count?: number | null;
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
  workflow?: WorkflowApiResponse | null;
}

// Mid-build pause frame: the turn stays open (SSE alive) while the client
// surfaces a credential card. reason stays a raw string here — CredentialCard
// tolerates unknown reason tokens, so a newer backend can't break the wiring.
export interface WorkflowCopilotCredentialRequiredUpdate {
  type: "credential_required";
  turn_id: string;
  workflow_copilot_chat_id: string;
  resume_token: string;
  reason: string;
  message: string;
  login_page_urls: string[];
  credential_refs: string[];
  timeout_seconds: number;
  expires_at: string;
  timestamp: string;
}

export interface WorkflowCopilotToolCallUpdate {
  type: "tool_call";
  tool_name: string;
  display_label?: string | null;
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
  // Present once the backend stamps the dispatched run id onto progress frames
  // (surfaced mid-execution). Absent against older backends, which only carry
  // the run id on run_outcome; the chat falls back to that.
  workflow_run_id?: string | null;
  block_label: string;
  block_type: string;
  status: string;
  iteration: number;
  timestamp: string;
}

export type WorkflowCopilotRunOutcomeVerdict =
  | "evaluating"
  | "demonstrated"
  | "not_demonstrated"
  | "not_evaluated";

export interface WorkflowCopilotRunOutcomeUpdate {
  type: "run_outcome";
  workflow_run_id: string;
  workflow_run_block_ids: string[];
  block_labels: string[];
  verdict: WorkflowCopilotRunOutcomeVerdict;
  reason_code?: string | null;
  display_reason?: string | null;
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
