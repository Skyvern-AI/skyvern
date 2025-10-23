import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const WorkflowParameterYamlDefaultValue: core.serialization.Schema<serializers.WorkflowParameterYamlDefaultValue.Raw, Skyvern.WorkflowParameterYamlDefaultValue>;
export declare namespace WorkflowParameterYamlDefaultValue {
    type Raw = string | number | number | boolean | Record<string, unknown> | unknown[];
}
