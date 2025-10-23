import type * as Skyvern from "../index.mjs";
export interface WorkflowParameter {
    key: string;
    description?: string;
    workflow_parameter_id: string;
    workflow_parameter_type: Skyvern.WorkflowParameterType;
    workflow_id: string;
    default_value?: WorkflowParameter.DefaultValue;
    created_at: string;
    modified_at: string;
    deleted_at?: string;
}
export declare namespace WorkflowParameter {
    type DefaultValue = string | number | number | boolean | Record<string, unknown> | unknown[];
}
