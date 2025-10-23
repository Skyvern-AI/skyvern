import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const ValidationBlockDataSchema: core.serialization.Schema<serializers.ValidationBlockDataSchema.Raw, Skyvern.ValidationBlockDataSchema>;
export declare namespace ValidationBlockDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
