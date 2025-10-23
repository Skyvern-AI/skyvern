import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const TaskBlockDataSchema: core.serialization.Schema<serializers.TaskBlockDataSchema.Raw, Skyvern.TaskBlockDataSchema>;
export declare namespace TaskBlockDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
