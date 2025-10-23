import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const WorkflowParameterYamlDefaultValue: core.serialization.Schema<serializers.WorkflowParameterYamlDefaultValue.Raw, Skyvern.WorkflowParameterYamlDefaultValue>;
export declare namespace WorkflowParameterYamlDefaultValue {
    type Raw = string | number | number | boolean | Record<string, unknown> | unknown[];
}
