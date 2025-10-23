import type * as Skyvern from "../index.mjs";
export interface WorkflowRequest {
    /** Workflow definition in JSON format */
    json_definition?: Skyvern.WorkflowCreateYamlRequest;
    /** Workflow definition in YAML format */
    yaml_definition?: string;
}
