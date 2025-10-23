import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const TaskBlockYamlDataSchema: core.serialization.Schema<serializers.TaskBlockYamlDataSchema.Raw, Skyvern.TaskBlockYamlDataSchema>;
export declare namespace TaskBlockYamlDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
