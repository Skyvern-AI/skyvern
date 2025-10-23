import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { WorkflowParameterDefaultValue } from "./WorkflowParameterDefaultValue.js";
import { WorkflowParameterType } from "./WorkflowParameterType.js";
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
