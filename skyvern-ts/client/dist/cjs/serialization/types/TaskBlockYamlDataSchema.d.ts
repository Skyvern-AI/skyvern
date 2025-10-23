import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const TaskBlockYamlDataSchema: core.serialization.Schema<serializers.TaskBlockYamlDataSchema.Raw, Skyvern.TaskBlockYamlDataSchema>;
export declare namespace TaskBlockYamlDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
