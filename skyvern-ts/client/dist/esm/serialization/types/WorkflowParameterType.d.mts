import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const WorkflowParameterType: core.serialization.Schema<serializers.WorkflowParameterType.Raw, Skyvern.WorkflowParameterType>;
export declare namespace WorkflowParameterType {
    type Raw = "string" | "integer" | "float" | "boolean" | "json" | "file_url" | "credential_id";
}
