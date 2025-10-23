import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const WorkflowRunBlockDataSchema: core.serialization.Schema<serializers.WorkflowRunBlockDataSchema.Raw, Skyvern.WorkflowRunBlockDataSchema>;
export declare namespace WorkflowRunBlockDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
