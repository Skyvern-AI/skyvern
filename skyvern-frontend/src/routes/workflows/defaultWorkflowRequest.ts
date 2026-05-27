import type { WorkflowCreateYAMLRequest } from "./types/workflowYamlTypes";

const defaultWorkflowRequest: WorkflowCreateYAMLRequest = {
  title: "New Agent",
  description: "",
  ai_fallback: true,
  code_version: 2,
  run_with: "agent",
  workflow_definition: {
    version: 2,
    blocks: [],
    parameters: [],
  },
};

export { defaultWorkflowRequest };
