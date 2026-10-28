import type { Node } from "@xyflow/react";

import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

import { NodeBaseData } from "../types";

export const AGENT_STEP_MAX_TURNS_LIMIT = 200;

export type AgentStepNodeData = NodeBaseData & {
  prompt: string;
  dataSchema: string;
  maxTurns: number;
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
  parameterKeys: [],
  continueOnFailure: false,
  model: null,
} as const;

export function isAgentStepNode(node: Node): node is AgentStepNode {
  return node.type === "agentStep";
}
