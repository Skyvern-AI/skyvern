import {
  WorkflowApiResponse,
  WorkflowDefinition,
} from "@/routes/workflows/types/workflowTypes";

export type WorkflowCopilotChatSender = "user" | "ai" | "product";
export type CopilotProductAction = {
  kind: "diagnose_run";
  workflowRunId: string;
  nonce: string;
};
export type ProposalDisposition =
  | "no_proposal"
  | "auto_applicable"
  | "review_untested"
  | "review_tested";
export type CopilotResponseType = "REPLY" | "ASK_QUESTION" | "REPLACE_WORKFLOW";

export interface QuestionChoice {
  choice_id: string;
  text: string;
}

export interface QuestionPart {
  part_id: string;
  prompt: string;
  choices: QuestionChoice[];
}

export interface QuestionAnswer {
  part_id: string;
  choice_id?: string | null;
  text?: string | null;
}

export interface QuestionResponse {
  answers?: QuestionAnswer[];
  text?: string | null;
  skipped?: boolean;
}

export interface QuestionInteraction {
  interaction_id: string;
  turn_id: string;
  tool_call_id: string;
  parts: QuestionPart[];
  status: "pending" | "resolved" | "cancelled" | "interrupted";
  response: QuestionResponse | null;
  created_at: string;
  resolved_at: string | null;
}

export interface WorkflowCopilotQuestionRequired {
  type: "question_required";
  turn_id: string;
  workflow_copilot_chat_id: string;
  interactions: QuestionInteraction[];
  cancel_token: string | null;
}

export interface WorkflowCopilotQuestionResolved {
  type: "question_resolved";
  interaction: QuestionInteraction;
  continued: boolean;
}

export interface ConnectedAccountChoice {
  connection_id: string;
  name: string;
  state: string;
  email_address?: string | null;
}

export interface BudgetExpiryOutcome {
  budget_expired?: boolean;
  budget_expiry_source?: "deadline" | "max_turns" | null;
  budget_expiry_report_produced?: boolean | null;
  budget_expiry_staged_draft_id?: string | null;
  drain_fingerprint?: string | null;
}

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
  selected_connected_account_id?: string | null;
  workflow_permanent_id: string;
  workflow_id: string;
  workflow_copilot_chat_id?: string | null;
  workflow_run_id?: string | null;
  browser_session_id?: string | null;
  message: string;
  audio_artifact_id?: string | null;
  workflow_yaml: string;
  mode?: "build" | null;
  code_block?: boolean | null;
  cancel_token?: string;
  idempotency_key?: string | null;
  target_block_label?: string | null;
  // Ambient fact: the block selected on the studio canvas when the message was
  // sent — context for "this block" references, never a directive.
  selected_block_label?: string | null;
  keep_pending_proposal?: boolean;
  product_action?: "test_end_to_end" | "diagnose_run" | null;
  // Opt-in: only clients that can render the credential_required frame set
  // this, so the backend never pauses a turn a client would silently drop.
  supports_credential_pause?: boolean;
  supports_question_tool?: boolean;
}

export type WorkflowCopilotCancelSource = "escape_key" | "stop_button" | "api";

export interface WorkflowCopilotCancelRequest {
  cancel_token: string;
  source: WorkflowCopilotCancelSource;
}

export interface WorkflowCopilotChatHistoryMessage {
  sender: WorkflowCopilotChatSender;
  content: string;
  audio_artifact_id?: string | null;
  created_at: string;
  // Typed turn outcome persisted on assistant rows; optional so the FE
  // tolerates an older backend that does not serve it.
  turn_outcome?:
    | (BudgetExpiryOutcome & {
        response_kind?: string | null;
        connected_account_choices?: ConnectedAccountChoice[] | null;
        // Server-minted id of the turn that wrote this row; the same id the
        // turn_start frame carries, so a client can correlate a row to its own send.
        copilot_turn_id?: string | null;
        terminal_reason?: string | null;
      })
    | null;
  narrative_payload?: Record<string, unknown> | null;
}

export interface WorkflowCopilotChatHistoryResponse {
  question_interactions?: QuestionInteraction[];
  pending_question_cancel_token?: string | null;
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
  // Absent on responses from a backend that predates the marker.
  awaiting_user_input?: boolean;
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
  | "run_started"
  | "run_outcome"
  | "turn_start"
  | "design_start"
  | "design_end"
  | "workflow_draft"
  | "title_update"
  | "credential_required"
  | "question_required";

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
  // A write and its test share one tool call, so the tool_result arrives only after the run.
  // These carry the patch at write time and name the call whose row it belongs to.
  code_diffs?: unknown;
  tool_call_id?: string | null;
}

// Emitted once the backend has persisted a derived agent name, before any block
// exists. Clients must not treat it as authoritative over a user-chosen title.
export interface WorkflowCopilotTitleUpdate {
  type: "title_update";
  turn_id: string;
  workflow_permanent_id: string;
  title: string;
  timestamp: string;
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
  timestamp?: string | null;
}

// One changed code block's line delta. `patch` is absent when the server dropped
// it for size; the counts are always the full change and never the patch's.
export interface CodeWriteDiff {
  label: string;
  added: number;
  removed: number;
  patch?: string;
  patchDropped?: boolean;
}

export interface WorkflowCopilotToolResultUpdate {
  type: "tool_result";
  tool_name: string;
  display_label?: string | null;
  success: boolean;
  summary: string;
  iteration: number;
  tool_call_id: string;
  code_diffs?: CodeWriteDiff[] | null;
  detail?: string | null;
  timestamp?: string | null;
}

export interface WorkflowCopilotCondensingUpdate {
  type: "condensing";
  status: "started" | "completed";
}

export interface WorkflowCopilotNarrationUpdate {
  type: "narration";
  narration: string;
  // Narrator-authored row titles. Absent against a backend that predates them,
  // so the row falls back to its tool-derived label.
  active_label?: string | null;
  outcome_label?: string | null;
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

export interface WorkflowCopilotRunStartedUpdate {
  type: "run_started";
  workflow_run_id: string;
  timestamp: string;
}

export type WorkflowCopilotRunOutcomeVerdict =
  | "evaluating"
  | "demonstrated"
  | "not_demonstrated"
  | "not_evaluated";

export type RunOutcomeRole = "recorded" | "adjudicated" | "interim_build_test";

export interface WorkflowCopilotRunOutcomeUpdate {
  type: "run_outcome";
  workflow_run_id: string;
  workflow_run_block_ids: string[];
  block_labels: string[];
  verdict: WorkflowCopilotRunOutcomeVerdict;
  role?: RunOutcomeRole;
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
