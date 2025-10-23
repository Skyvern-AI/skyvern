import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const ActionBlockDataSchema: core.serialization.Schema<serializers.ActionBlockDataSchema.Raw, Skyvern.ActionBlockDataSchema>;
export declare namespace ActionBlockDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
