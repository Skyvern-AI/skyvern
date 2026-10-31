import type { Node } from "@xyflow/react";

import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

import { NodeBaseData } from "../types";

export const AGENT_STEP_MAX_TURNS_LIMIT = 200;

export type AgentStepNodeData = NodeBaseData & {
  prompt: string;
  dataSchema: string;
  maxTurns: number;
  // null offers the step every tool, including ones added later.
  tools: Array<string> | null;
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
  tools: [],
  parameterKeys: [],
  continueOnFailure: false,
  model: null,
} as const;

export function agentStepToolsFromSelection(
  selected: Array<string>,
  choosable: Array<string>,
): Array<string> | null {
  return choosable.length > 0 &&
    choosable.every((tool) => selected.includes(tool))
    ? null
    : selected;
}

export function isAgentStepNode(node: Node): node is AgentStepNode {
  return node.type === "agentStep";
}
