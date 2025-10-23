import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const WorkflowParameterDefaultValue: core.serialization.Schema<serializers.WorkflowParameterDefaultValue.Raw, Skyvern.WorkflowParameterDefaultValue>;
export declare namespace WorkflowParameterDefaultValue {
    type Raw = string | number | number | boolean | Record<string, unknown> | unknown[];
}
