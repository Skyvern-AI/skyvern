import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const TaskBlockDataSchema: core.serialization.Schema<serializers.TaskBlockDataSchema.Raw, Skyvern.TaskBlockDataSchema>;
export declare namespace TaskBlockDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
