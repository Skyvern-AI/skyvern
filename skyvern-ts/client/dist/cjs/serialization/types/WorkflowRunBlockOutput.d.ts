import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const WorkflowRunBlockOutput: core.serialization.Schema<serializers.WorkflowRunBlockOutput.Raw, Skyvern.WorkflowRunBlockOutput>;
export declare namespace WorkflowRunBlockOutput {
    type Raw = Record<string, unknown> | unknown[] | string;
}
