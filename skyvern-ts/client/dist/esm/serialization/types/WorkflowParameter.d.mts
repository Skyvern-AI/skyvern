import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { WorkflowParameterDefaultValue } from "./WorkflowParameterDefaultValue.mjs";
import { WorkflowParameterType } from "./WorkflowParameterType.mjs";
export declare const WorkflowParameter: core.serialization.ObjectSchema<serializers.WorkflowParameter.Raw, Skyvern.WorkflowParameter>;
export declare namespace WorkflowParameter {
    interface Raw {
        key: string;
        description?: string | null;
        workflow_parameter_id: string;
        workflow_parameter_type: WorkflowParameterType.Raw;
        workflow_id: string;
        default_value?: WorkflowParameterDefaultValue.Raw | null;
        created_at: string;
        modified_at: string;
        deleted_at?: string | null;
    }
}
