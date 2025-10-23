import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const WorkflowRunBlockDataSchema: core.serialization.Schema<serializers.WorkflowRunBlockDataSchema.Raw, Skyvern.WorkflowRunBlockDataSchema>;
export declare namespace WorkflowRunBlockDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
