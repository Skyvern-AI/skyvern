import type { Node } from "@xyflow/react";

import { ReadableActionTypes } from "@/api/types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

import { NodeBaseData, workflowBlockTitle } from "../types";

export const AGENT_STEP_MAX_TURNS_LIMIT = 200;

// Titles of the tools that run no workflow block; a block-backed tool takes its block's title.
export const agentStepToolTitles: Record<string, string> = {
  page_goto: workflowBlockTitle.goto_url,
  page_click: ReadableActionTypes.click,
  page_fill: ReadableActionTypes.input_text,
  page_extract: ReadableActionTypes.extract,
  read_run_data: "Read run data",
  finish: "Finish",
};

export type AgentStepNodeData = NodeBaseData & {
  prompt: string;
  dataSchema: string;
  maxTurns: number;
  tools: Array<string>;
  parameterKeys: Array<string>;
};

export type AgentStepNode = Node<AgentStepNodeData, "agentStep">;

export const agentStepDefaultDataSchema = {
  type: "object",
  properties: { answer: { type: "string" } },
  required: ["answer"],
};

export const agentStepNodeDefaultData: AgentStepNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("agent_step"),
  editable: true,
  label: "",
  prompt: "",
  dataSchema: JSON.stringify(agentStepDefaultDataSchema, null, 2),
  maxTurns: 25,
  tools: [
    "browser_task",
    "code",
    "download_to_s3",
    "file_download",
    "page_click",
    "page_fill",
    "page_goto",
    "web_search",
    "page_extract",
  ],
  parameterKeys: [],
  continueOnFailure: false,
  model: null,
} as const;

export function isAgentStepNode(node: Node): node is AgentStepNode {
  return node.type === "agentStep";
}
