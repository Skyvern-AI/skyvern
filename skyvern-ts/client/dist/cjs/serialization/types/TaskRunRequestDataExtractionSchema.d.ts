import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const TaskRunRequestDataExtractionSchema: core.serialization.Schema<serializers.TaskRunRequestDataExtractionSchema.Raw, Skyvern.TaskRunRequestDataExtractionSchema>;
export declare namespace TaskRunRequestDataExtractionSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
