import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const UrlBlockDataSchema: core.serialization.Schema<serializers.UrlBlockDataSchema.Raw, Skyvern.UrlBlockDataSchema>;
export declare namespace UrlBlockDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
