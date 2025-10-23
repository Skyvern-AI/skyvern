import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const NavigationBlockDataSchema: core.serialization.Schema<serializers.NavigationBlockDataSchema.Raw, Skyvern.NavigationBlockDataSchema>;
export declare namespace NavigationBlockDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
