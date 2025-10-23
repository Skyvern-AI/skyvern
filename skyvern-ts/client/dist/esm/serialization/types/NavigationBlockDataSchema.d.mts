import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const NavigationBlockDataSchema: core.serialization.Schema<serializers.NavigationBlockDataSchema.Raw, Skyvern.NavigationBlockDataSchema>;
export declare namespace NavigationBlockDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
