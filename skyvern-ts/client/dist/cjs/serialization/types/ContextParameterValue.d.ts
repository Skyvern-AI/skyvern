import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const ContextParameterValue: core.serialization.Schema<serializers.ContextParameterValue.Raw, Skyvern.ContextParameterValue>;
export declare namespace ContextParameterValue {
    type Raw = string | number | number | boolean | Record<string, unknown> | unknown[];
}
