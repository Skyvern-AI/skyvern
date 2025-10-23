import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const WorkflowParameterDefaultValue: core.serialization.Schema<serializers.WorkflowParameterDefaultValue.Raw, Skyvern.WorkflowParameterDefaultValue>;
export declare namespace WorkflowParameterDefaultValue {
    type Raw = string | number | number | boolean | Record<string, unknown> | unknown[];
}
