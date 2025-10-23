import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const ContextParameterValue: core.serialization.Schema<serializers.ContextParameterValue.Raw, Skyvern.ContextParameterValue>;
export declare namespace ContextParameterValue {
    type Raw = string | number | number | boolean | Record<string, unknown> | unknown[];
}
