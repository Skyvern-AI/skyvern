import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const TaskRunRequestDataExtractionSchema: core.serialization.Schema<serializers.TaskRunRequestDataExtractionSchema.Raw, Skyvern.TaskRunRequestDataExtractionSchema>;
export declare namespace TaskRunRequestDataExtractionSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
