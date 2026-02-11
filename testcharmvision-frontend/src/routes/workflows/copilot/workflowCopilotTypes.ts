import {
  WorkflowApiResponse,
  WorkflowDefinition,
} from "@/routes/workflows/types/workflowTypes";

export type WorkflowCopilotChatSender = "user" | "ai";

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
  message: string;
  workflow_yaml: string;
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

export type WorkflowCopilotStreamMessageType =
  | "processing_update"
  | "response"
  | "error";

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
}

export interface WorkflowCopilotStreamErrorUpdate {
  type: "error";
  error: string;
}

export interface WorkflowYAMLConversionRequest {
  workflow_definition_yaml: string;
  workflow_id: string;
}

export interface WorkflowYAMLConversionResponse {
  workflow_definition: WorkflowDefinition;
}
