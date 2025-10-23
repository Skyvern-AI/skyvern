import type * as Skyvern from "../index.js";
export interface WorkflowParameterYaml {
    key: string;
    description?: string;
    workflow_parameter_type: Skyvern.WorkflowParameterType;
    default_value?: WorkflowParameterYaml.DefaultValue;
}
export declare namespace WorkflowParameterYaml {
    type DefaultValue = string | number | number | boolean | Record<string, unknown> | unknown[];
}
