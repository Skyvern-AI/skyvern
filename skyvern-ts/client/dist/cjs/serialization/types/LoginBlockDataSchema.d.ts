import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const LoginBlockDataSchema: core.serialization.Schema<serializers.LoginBlockDataSchema.Raw, Skyvern.LoginBlockDataSchema>;
export declare namespace LoginBlockDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
