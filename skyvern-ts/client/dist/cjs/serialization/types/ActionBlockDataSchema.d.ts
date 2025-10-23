import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const ActionBlockDataSchema: core.serialization.Schema<serializers.ActionBlockDataSchema.Raw, Skyvern.ActionBlockDataSchema>;
export declare namespace ActionBlockDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
