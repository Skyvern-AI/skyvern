import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const UrlBlockDataSchema: core.serialization.Schema<serializers.UrlBlockDataSchema.Raw, Skyvern.UrlBlockDataSchema>;
export declare namespace UrlBlockDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
